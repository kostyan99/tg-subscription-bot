import datetime
import os
import secrets
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqladmin import Admin, ModelView, BaseView, expose
from sqladmin.authentication import AuthenticationBackend
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, func
from sqlalchemy.orm import aliased

from bot.config import BOT_TOKEN, CHANNEL_ID
from bot.database import engine, async_session
from bot.models import (
    User, Order, Subscription, ReferralEarning, Withdrawal, ManualInviteLink,
    SubscriptionStatus, OrderStatus,
)
from admin_panel.theme import render_page

ADMIN_PANEL_USERNAME = os.environ["ADMIN_PANEL_USERNAME"]
ADMIN_PANEL_PASSWORD = os.environ["ADMIN_PANEL_PASSWORD"]
SECRET_KEY = os.environ["ADMIN_PANEL_SECRET_KEY"]  # любая длинная случайная строка

# Отдельный Bot-клиент для действий из веб-панели (создание ссылок) —
# не связан с процессом бота, просто ещё один HTTP-клиент к Bot API
bot_client = Bot(token=BOT_TOKEN)


def check_credentials(username: str, password: str) -> bool:
    # compare_digest — защита от timing-атак (иначе неправильный пароль
    # можно подобрать по времени ответа сервера символ за символом)
    correct_user = secrets.compare_digest(username, ADMIN_PANEL_USERNAME)
    correct_pass = secrets.compare_digest(password, ADMIN_PANEL_PASSWORD)
    return correct_user and correct_pass


# ---------- Авторизация для раздела SQLAdmin (/admin) ----------

class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        if check_credentials(form.get("username", ""), form.get("password", "")):
            request.session.update({"authenticated": True})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        return request.session.get("authenticated", False)


app = FastAPI(title="Admin Panel")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)


@app.on_event("shutdown")
async def shutdown():
    await bot_client.session.close()

# admin_panel/main.py -> repo root -> templates/
# Абсолютный путь, а не относительный — иначе SQLAdmin ищет "templates/" от
# текущей рабочей директории процесса, а она может отличаться от репозитория
# в зависимости от того, как именно Railway запускает Custom Start Command.
TEMPLATES_DIR = str(Path(__file__).resolve().parent.parent / "templates")

admin = Admin(
    app,
    engine,
    authentication_backend=AdminAuth(secret_key=SECRET_KEY),
    title="Подписки — админка",
    templates_dir=TEMPLATES_DIR,
)


class UserAdmin(ModelView, model=User):
    name = "Пользователь"
    name_plural = "Пользователи"
    icon = "fa-solid fa-user"
    column_list = [
        User.id, User.telegram_id, User.username, User.created_at,
        User.referral_code, User.referred_by_id,
    ]
    column_searchable_list = [User.telegram_id, User.username, User.referral_code]
    column_sortable_list = [User.created_at]
    column_default_sort = [(User.created_at, True)]
    can_create = can_edit = can_delete = False  # только просмотр — правки через бота


class SubscriptionAdmin(ModelView, model=Subscription):
    name = "Подписка"
    name_plural = "Подписки"
    icon = "fa-solid fa-id-card"
    column_list = [
        Subscription.id, Subscription.user, Subscription.status,
        Subscription.start_date, Subscription.end_date, Subscription.reminded,
    ]
    column_sortable_list = [Subscription.end_date, Subscription.status]
    column_default_sort = [(Subscription.end_date, True)]
    can_create = can_edit = can_delete = False


class OrderAdmin(ModelView, model=Order):
    name = "Заказ"
    name_plural = "Заказы (история платежей)"
    icon = "fa-solid fa-receipt"
    column_list = [
        Order.id, Order.user, Order.method, Order.amount,
        Order.status, Order.telegram_charge_id, Order.created_at,
    ]
    column_searchable_list = [Order.id, Order.telegram_charge_id]
    column_sortable_list = [Order.created_at]
    column_default_sort = [(Order.created_at, True)]
    can_create = can_edit = can_delete = False


class ReferralEarningAdmin(ModelView, model=ReferralEarning):
    name = "Реферальное начисление"
    name_plural = "Реферальные начисления"
    icon = "fa-solid fa-people-arrows"
    column_list = [
        ReferralEarning.id, ReferralEarning.referrer, ReferralEarning.referred_user,
        ReferralEarning.method, ReferralEarning.amount, ReferralEarning.paid_out,
        ReferralEarning.created_at,
    ]
    column_sortable_list = [ReferralEarning.created_at]
    column_default_sort = [(ReferralEarning.created_at, True)]
    can_create = can_edit = can_delete = False


class WithdrawalAdmin(ModelView, model=Withdrawal):
    name = "Вывод средств"
    name_plural = "Выводы средств"
    icon = "fa-solid fa-money-bill-transfer"
    column_list = [
        Withdrawal.id, Withdrawal.user_id, Withdrawal.method, Withdrawal.amount,
        Withdrawal.status, Withdrawal.cryptobot_transfer_id, Withdrawal.error_message,
        Withdrawal.created_at,
    ]
    column_sortable_list = [Withdrawal.created_at]
    column_default_sort = [(Withdrawal.created_at, True)]
    can_create = can_edit = can_delete = False


admin.add_view(UserAdmin)
admin.add_view(SubscriptionAdmin)
admin.add_view(OrderAdmin)
admin.add_view(ReferralEarningAdmin)
admin.add_view(WithdrawalAdmin)


# ---------- Генератор пригласительных ссылок (кастомная страница внутри /admin) ----------

class ReferralOverviewView(BaseView):
    name = "Рефералы"
    icon = "fa-solid fa-people-group"

    @expose("/referral-overview", methods=["GET"])
    async def page(self, request: Request):
        ReferrerUser = aliased(User)
        ReferredUser = aliased(User)

        async with async_session() as session:
            # Сколько всего юзеров привёл каждый реферер (считаем по User.referred_by_id,
            # а не по ReferralEarning — так учитываются и те, кто ещё не оплатил)
            invited_counts = dict(
                (row[0], row[1])
                for row in (
                    await session.execute(
                        select(User.referred_by_id, func.count(User.id))
                        .where(User.referred_by_id.is_not(None))
                        .group_by(User.referred_by_id)
                    )
                ).all()
            )

            # Сумма начислений по каждому рефереру и методу оплаты
            earnings_by_referrer: dict[int, dict[str, int]] = {}
            for referrer_id, method, total in (
                await session.execute(
                    select(ReferralEarning.referrer_id, ReferralEarning.method, func.sum(ReferralEarning.amount))
                    .group_by(ReferralEarning.referrer_id, ReferralEarning.method)
                )
            ).all():
                earnings_by_referrer.setdefault(referrer_id, {})[method] = total

            if invited_counts:
                referrers = (
                    await session.execute(select(User).where(User.id.in_(invited_counts.keys())))
                ).scalars().all()
            else:
                referrers = []

            # Детальная разбивка: кто конкретно кого привёл и сколько за это начислено
            detail_rows = (
                await session.execute(
                    select(
                        ReferrerUser.telegram_id, ReferrerUser.username,
                        ReferredUser.telegram_id, ReferredUser.username,
                        ReferralEarning.method, ReferralEarning.amount,
                        ReferralEarning.paid_out, ReferralEarning.created_at,
                    )
                    .join(ReferrerUser, ReferralEarning.referrer_id == ReferrerUser.id)
                    .join(ReferredUser, ReferralEarning.referred_user_id == ReferredUser.id)
                    .order_by(ReferralEarning.created_at.desc())
                    .limit(200)
                )
            ).all()

        def fmt_amounts(amounts: dict[str, int]) -> str:
            parts = []
            if amounts.get("stars"):
                parts.append(f"⭐ {amounts['stars']}")
            if amounts.get("crypto"):
                parts.append(f"₿ {amounts['crypto'] / 100:.2f}")
            return " · ".join(parts) if parts else "—"

        def label(telegram_id: int, username: str | None) -> str:
            return f"@{username}" if username else f"id{telegram_id}"

        summary_rows = "".join(
            f"""<tr>
                <td>{label(r.telegram_id, r.username)}</td>
                <td>{invited_counts.get(r.id, 0)}</td>
                <td>{fmt_amounts(earnings_by_referrer.get(r.id, {}))}</td>
            </tr>"""
            for r in sorted(referrers, key=lambda u: invited_counts.get(u.id, 0), reverse=True)
        ) or '<tr><td colspan="3" style="color:var(--text-dim)">Рефералов пока нет.</td></tr>'

        def fmt_one(method: str, amount: int) -> str:
            return f"⭐ {amount}" if method == "stars" else f"₿ {amount / 100:.2f}"

        detail_html = "".join(
            f"""<tr>
                <td>{label(rt, ru)}</td>
                <td>{label(dt, du)}</td>
                <td>{fmt_one(method, amount)}</td>
                <td>{"✅ выплачено" if paid else "⏳ ожидает"}</td>
                <td>{created.strftime("%d.%m.%Y %H:%M")}</td>
            </tr>"""
            for rt, ru, dt, du, method, amount, paid, created in detail_rows
        ) or '<tr><td colspan="5" style="color:var(--text-dim)">Начислений пока нет.</td></tr>'

        body = f"""
        <h2>👥 Рефералы</h2>

        <h3 style="font-size:16px; color:var(--text-dim); margin: 0 0 12px 0;">Сводка по каждому рефереру</h3>
        <div class="panel" style="padding:0; overflow:hidden; margin-bottom:32px;">
            <table>
                <thead><tr><th>Реферер</th><th>Приглашено</th><th>Заработано всего</th></tr></thead>
                <tbody>{summary_rows}</tbody>
            </table>
        </div>

        <h3 style="font-size:16px; color:var(--text-dim); margin: 0 0 12px 0;">Кто кого привёл — по каждому начислению</h3>
        <div class="panel" style="padding:0; overflow:hidden;">
            <table>
                <thead><tr><th>Реферер</th><th>Приглашённый</th><th>Сумма</th><th>Статус</th><th>Дата</th></tr></thead>
                <tbody>{detail_html}</tbody>
            </table>
        </div>
        """
        return HTMLResponse(render_page("referral-overview", "Рефералы", body))


admin.add_view(ReferralOverviewView)


class InviteLinksView(BaseView):
    name = "Пригласительные ссылки"
    icon = "fa-solid fa-link"

    @expose("/invite-links", methods=["GET", "POST"])
    async def page(self, request: Request):
        banner = ""

        if request.method == "POST":
            form = await request.form()
            note = (form.get("note") or "").strip()[:255]
            try:
                link = await bot_client.create_chat_invite_link(
                    chat_id=CHANNEL_ID,
                    member_limit=1,
                    name=(note[:32] if note else None),  # Telegram ограничивает name 32 символами
                    expire_date=int(
                        (datetime.datetime.utcnow() + datetime.timedelta(days=7)).timestamp()
                    ),
                )
                async with async_session() as session:
                    session.add(
                        ManualInviteLink(
                            invite_link=link.invite_link,
                            note=note or None,
                            expire_date=datetime.datetime.utcnow() + datetime.timedelta(days=7),
                        )
                    )
                    await session.commit()
                banner = '<div class="banner banner-ok">✅ Ссылка создана — она одноразовая (member_limit=1) и живёт 7 дней.</div>'
            except TelegramAPIError as e:
                banner = f'<div class="banner banner-err">❌ Не удалось создать ссылку: {e}. Проверь, что бот всё ещё админ канала с правом приглашать по ссылке.</div>'

        async with async_session() as session:
            result = await session.execute(
                select(ManualInviteLink).order_by(ManualInviteLink.created_at.desc()).limit(30)
            )
            links = result.scalars().all()

        rows = "".join(
            f"""<tr>
                <td><code>{l.invite_link}</code></td>
                <td>{l.note or "—"}</td>
                <td>{l.created_at.strftime("%d.%m.%Y %H:%M")}</td>
                <td>{l.expire_date.strftime("%d.%m.%Y") if l.expire_date else "—"}</td>
                <td><button class="btn-ghost" onclick="navigator.clipboard.writeText('{l.invite_link}')">Скопировать</button></td>
            </tr>"""
            for l in links
        ) or '<tr><td colspan="5" style="color:var(--text-dim)">Пока ничего не создано.</td></tr>'

        body = f"""
        <h2>🔗 Пригласительные ссылки</h2>
        {banner}
        <div class="panel">
            <form method="post">
                <input type="text" name="note" placeholder="Заметка (необязательно) — например, кому эта ссылка">
                <button type="submit">Сгенерировать одноразовую ссылку</button>
            </form>
        </div>
        <div class="panel" style="padding:0; overflow:hidden;">
            <table>
                <thead><tr><th>Ссылка</th><th>Заметка</th><th>Создана</th><th>Истекает</th><th></th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
        """
        return HTMLResponse(render_page("invite-links", "Пригласительные ссылки", body))


admin.add_view(InviteLinksView)


@app.get("/health")
async def health():
    # Без авторизации — специально для healthcheck-пинга Railway.
    # Никаких данных не отдаёт, просто подтверждает, что процесс жив.
    return {"status": "ok"}


# ---------- Дашборд со сводкой (отдельная простая HTTP Basic-авторизация) ----------

security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if not check_credentials(credentials.username, credentials.password):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


@app.get("/", response_class=HTMLResponse)
async def dashboard(_: str = Depends(require_auth)):
    now = datetime.datetime.utcnow()

    async with async_session() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar_one()

        active_subs = (
            await session.execute(
                select(func.count(Subscription.id)).where(
                    Subscription.status == SubscriptionStatus.active,
                    Subscription.end_date > now,
                )
            )
        ).scalar_one()

        revenue_by_method = (
            await session.execute(
                select(Order.method, func.sum(Order.amount))
                .where(Order.status == OrderStatus.paid)
                .group_by(Order.method)
            )
        ).all()

        pending_referral_payouts = (
            await session.execute(
                select(ReferralEarning.method, func.sum(ReferralEarning.amount)).where(
                    ReferralEarning.paid_out.is_(False)
                ).group_by(ReferralEarning.method)
            )
        ).all()

    def fmt(rows):
        if not rows:
            return "—"
        parts = []
        for method, total in rows:
            if method == "stars":
                parts.append(f"⭐ {total}")
            elif method == "crypto":
                parts.append(f"₿ {total / 100:.2f}")
        return " · ".join(parts) if parts else "—"

    body = f"""
    <h2>📊 Дашборд</h2>
    <div class="grid">
        <div class="card">
            <div class="label">Всего пользователей</div>
            <div class="value">{total_users}</div>
        </div>
        <div class="card">
            <div class="label">Активных подписок сейчас</div>
            <div class="value">{active_subs}</div>
        </div>
        <div class="card">
            <div class="label">Выручка всего</div>
            <div class="value">{fmt(revenue_by_method)}</div>
        </div>
        <div class="card">
            <div class="label">Невыплаченные рефки</div>
            <div class="value">{fmt(pending_referral_payouts)}</div>
        </div>
    </div>
    <p style="color:var(--text-dim)">Подробные таблицы — в разделе <a class="plain" href="/admin">Таблицы</a>, генерация ссылок на канал — в разделе <a class="plain" href="/admin/invite-links">Пригласительные ссылки</a>.</p>
    """
    return HTMLResponse(render_page("dashboard", "Дашборд", body))

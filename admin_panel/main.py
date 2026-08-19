import datetime
import os
import secrets

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, func

from bot.database import engine, async_session
from bot.models import User, Order, Subscription, ReferralEarning, Withdrawal, SubscriptionStatus, OrderStatus

ADMIN_PANEL_USERNAME = os.environ["ADMIN_PANEL_USERNAME"]
ADMIN_PANEL_PASSWORD = os.environ["ADMIN_PANEL_PASSWORD"]
SECRET_KEY = os.environ["ADMIN_PANEL_SECRET_KEY"]  # любая длинная случайная строка


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

admin = Admin(
    app,
    engine,
    authentication_backend=AdminAuth(secret_key=SECRET_KEY),
    title="Подписки — админка",
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
        Subscription.id, Subscription.user_id, Subscription.status,
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
        Order.id, Order.user_id, Order.method, Order.amount,
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
        ReferralEarning.id, ReferralEarning.referrer_id, ReferralEarning.referred_user_id,
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

    def fmt(rows, stars_label="Stars", usdt_label="USDT"):
        if not rows:
            return "нет данных"
        parts = []
        for method, total in rows:
            if method == "stars":
                parts.append(f"{total} {stars_label}")
            elif method == "crypto":
                parts.append(f"{total / 100:.2f} {usdt_label}")
        return ", ".join(parts) if parts else "нет данных"

    html = f"""
    <html>
    <head>
        <title>Сводка</title>
        <style>
            body {{ font-family: -apple-system, sans-serif; background: #0e0e12; color: #eee; padding: 32px; }}
            .card {{ background: #1a1a20; border-radius: 12px; padding: 20px; margin-bottom: 16px; max-width: 480px; }}
            .num {{ font-size: 28px; font-weight: 700; }}
            a {{ color: #7aa2ff; }}
        </style>
    </head>
    <body>
        <h2>📊 Сводка</h2>
        <div class="card"><div>Всего пользователей</div><div class="num">{total_users}</div></div>
        <div class="card"><div>Активных подписок сейчас</div><div class="num">{active_subs}</div></div>
        <div class="card"><div>Выручка всего (оплаченные заказы)</div><div class="num">{fmt(revenue_by_method)}</div></div>
        <div class="card"><div>Невыплаченные реферальные начисления</div><div class="num">{fmt(pending_referral_payouts)}</div></div>
        <p><a href="/admin">→ Открыть подробные таблицы (пользователи, подписки, заказы...)</a></p>
    </body>
    </html>
    """
    return HTMLResponse(html)

import datetime

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import CHANNEL_ID, SUBSCRIPTION_DAYS
from bot.models import Subscription, SubscriptionStatus, User


async def activate_subscription(session: AsyncSession, bot: Bot, user: User) -> str:
    """Продлевает/создаёт подписку и возвращает одноразовую invite-ссылку.
    Вызывается ПОСЛЕ того, как оплата уже подтверждена (неважно, каким способом)."""

    now = datetime.datetime.utcnow()
    result = await session.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    sub = result.scalar_one_or_none()

    # Если подписка ещё активна — продлеваем от даты окончания, а не от "сейчас"
    base_date = sub.end_date if (sub and sub.end_date > now) else now
    new_end = base_date + datetime.timedelta(days=SUBSCRIPTION_DAYS)

    if sub is None:
        sub = Subscription(
            user_id=user.id,
            start_date=now,
            end_date=new_end,
            status=SubscriptionStatus.active,
            reminded=False,
        )
        session.add(sub)
    else:
        sub.end_date = new_end
        sub.status = SubscriptionStatus.active
        sub.reminded = False

    await session.commit()

    invite_link = await bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        member_limit=1,
        expire_date=int((now + datetime.timedelta(days=1)).timestamp()),
    )
    return invite_link.invite_link

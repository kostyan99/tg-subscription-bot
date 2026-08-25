import datetime
import enum
import uuid

from sqlalchemy import BigInteger, String, DateTime, Enum, ForeignKey, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship, DeclarativeBase


class Base(DeclarativeBase):
    pass


class OrderStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"
    failed = "failed"


class SubscriptionStatus(str, enum.Enum):
    active = "active"
    expired = "expired"
    kicked = "kicked"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

    # Реферальная программа
    referral_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    referred_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )

    orders: Mapped[list["Order"]] = relationship(back_populates="user")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user")

    def __str__(self) -> str:
        return f"@{self.username}" if self.username else f"id{self.telegram_id}"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    method: Mapped[str] = mapped_column(String(32), index=True)  # "stars" / "crypto"
    amount: Mapped[int] = mapped_column()  # в минимальных единицах (для Stars — целое число)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus), default=OrderStatus.pending, index=True
    )
    # уникальный ID платежа от Telegram/CryptoBot — защита от повторной обработки
    telegram_charge_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, index=True
    )

    user: Mapped["User"] = relationship(back_populates="orders")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    start_date: Mapped[datetime.datetime] = mapped_column(DateTime)
    end_date: Mapped[datetime.datetime] = mapped_column(DateTime, index=True)
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus), default=SubscriptionStatus.active, index=True
    )
    reminded: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship(back_populates="subscriptions")


class ReferralEarning(Base):
    __tablename__ = "referral_earnings"

    id: Mapped[int] = mapped_column(primary_key=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    referred_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    # метод оплаты копируем из заказа — суммы в разных валютах нельзя просто
    # складывать вместе (Stars и центы USDT это разные единицы), поэтому
    # отчёты всегда считаем сгруппированными по method
    method: Mapped[str] = mapped_column(String(32))
    amount: Mapped[int] = mapped_column()  # % от order.amount, в тех же единицах
    paid_out: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

    # Два разных FK на одну и ту же таблицу users — нужно явно указать
    # foreign_keys для каждой связи, иначе SQLAlchemy не поймёт, какая есть какая
    referrer: Mapped["User"] = relationship(foreign_keys=[referrer_id])
    referred_user: Mapped["User"] = relationship(foreign_keys=[referred_user_id])


class WithdrawalStatus(str, enum.Enum):
    pending = "pending"
    completed = "completed"
    failed = "failed"


class Withdrawal(Base):
    __tablename__ = "withdrawals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    method: Mapped[str] = mapped_column(String(32))  # пока только "crypto" — Stars нельзя перевести программно
    amount: Mapped[int] = mapped_column()  # в центах USDT
    status: Mapped[WithdrawalStatus] = mapped_column(
        Enum(WithdrawalStatus), default=WithdrawalStatus.pending, index=True
    )
    cryptobot_transfer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    raw_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )


class ManualInviteLink(Base):
    """Ссылки, сгенерированные вручную из веб-админки (не привязаны к оплате) —
    например, чтобы пригласить кого-то напрямую, без прохождения бота."""
    __tablename__ = "manual_invite_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    invite_link: Mapped[str] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expire_date: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from bot.config import DATABASE_URL
from bot.models import Base

if DATABASE_URL.startswith("sqlite"):
    # SQLite (только локальная разработка) — свои правила пулинга, доп. параметры не нужны/не поддерживаются
    engine = create_async_engine(DATABASE_URL, echo=False)
else:
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,   # проверяет соединение перед использованием — спасает от "битых"
                               # коннектов после простоя (managed Postgres любит их рвать)
        pool_recycle=1800,    # пересоздаёт соединения раз в 30 минут на всякий случай
        pool_size=20,         # держим 20 живых соединений
        max_overflow=10,      # плюс до 10 временных сверх пула при пиковой нагрузке
    )

async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Создаёт таблицы, если их ещё нет. На проде вместо этого лучше
    использовать миграции (alembic), но для старта достаточно."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session

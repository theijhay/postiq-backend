from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from api.utils.settings import settings
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse


def _build_async_url(url: str):
    """Convert postgres URL to asyncpg-compatible URL, handling SSL params."""
    if url.startswith("postgresql://") or url.startswith("postgres://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    ssl_required = params.pop("sslmode", [None])[0] in ("require", "verify-ca", "verify-full")
    params.pop("channel_binding", None)
    clean_query = urlencode({k: v[0] for k, v in params.items()})
    clean_url = urlunparse(parsed._replace(query=clean_query))
    return clean_url, ssl_required


_DB_URL, _SSL = _build_async_url(settings.DB_URL)


def get_db_engine():
    kwargs = {"connect_args": {"ssl": "require"}} if _SSL else {}
    return create_async_engine(
        _DB_URL,
        # Keep connection use well under the DB's max_connections.
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,   # drop idle conns before the server does
        pool_pre_ping=True,                       # avoid handing out dead connections
        **kwargs,
    )


engine = get_db_engine()

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

Base = declarative_base()


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

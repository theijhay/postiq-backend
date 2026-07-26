import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware
from api.core import mq
from api.core.redis_client import redis_client
from api.db.database import engine
from api.v1.routes import api_version_one
from api.utils.settings import settings
from api.utils.success_response import success_response
from starlette.status import HTTP_200_OK
from api.utils.logger import logger
import logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("PostgreSQL connection OK")

    await redis_client.ping()
    logger.info("Redis connection OK")

    # Webhooks degrade to inline processing without the broker, so warn instead of failing
    try:
        await mq.connect()
        logger.info("RabbitMQ connection OK")
    except Exception as exc:
        logger.warning(
            "RabbitMQ unreachable at startup (%s) — webhooks will process inline until it returns",
            exc,
        )

    yield

    await mq.close()
    await redis_client.aclose()
    await engine.dispose()


# Setup FastAPI app
app = FastAPI(
    lifespan=lifespan,
)

# CORS
origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


# Sessions (for auth, email, etc.)
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)


# Mount routes
app.include_router(api_version_one)

# Health & Root Check
@app.get("/", tags=["Health"])
def read_root():
    return success_response(
        status_code=HTTP_200_OK,
        message="PostIQ API is running",
    )

@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "healthy"}



if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.APP_PORT,
        reload=settings.DEBUG
    )

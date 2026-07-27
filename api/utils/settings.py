from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # App
    PYTHON_ENV: str = "development"
    DEBUG: bool = False
    APP_PORT: int = 8000
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    # Auth
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Token vault — AES-256-GCM key (base64-encoded 32 bytes) for Meta tokens at rest
    ENCRYPTION_KEY: str

    # Database
    DB_URL: str
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800

    # Infra
    REDIS_URL: str = "redis://localhost:6379/0"
    RABBITMQ_URL: str = "amqp://guest:guest@localhost:5672/"
    WEBHOOK_QUEUE_NAME: str = "postiq.webhooks"
    INGESTION_QUEUE_NAME: str = "postiq.ingestion"

    # Meta Graph API
    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    META_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/meta/callback"
    META_GRAPH_VERSION: str = "v22.0"
    META_GRAPH_BASE: str = "https://graph.facebook.com"
    META_POST_CONNECT_REDIRECT: str = "http://localhost:3000/accounts"
    # OAuth CSRF state lifetime in Redis
    META_OAUTH_STATE_TTL_SECONDS: int = 600

    @property
    def is_production(self) -> bool:
        return self.PYTHON_ENV == "production"


settings = Settings()

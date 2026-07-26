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

    @property
    def is_production(self) -> bool:
        return self.PYTHON_ENV == "production"


settings = Settings()

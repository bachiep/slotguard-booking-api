"""Application configuration powered by Pydantic Settings."""

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and optional .env files."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "SlotGuard Booking API"
    app_env: str = "local"
    api_host: str = "0.0.0.0"
    api_port: int = 8102
    log_level: str = "INFO"

    database_url: str = "sqlite+aiosqlite:///./slotguard_dev.db"

    rate_limit_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6382/0"
    rate_limit_auth_requests: int = 20
    rate_limit_booking_requests: int = 60
    rate_limit_webhook_requests: int = 100
    rate_limit_window_seconds: int = 60

    jwt_secret: str = "dev-only-change-me-before-production-use-at-least-32-chars"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 604800

    booking_hold_ttl_seconds: int = 900
    webhook_secret: str = "dev-only-webhook-secret-change-me-in-production"
    webhook_replay_window_seconds: int = 300

    worker_enabled: bool = True
    worker_poll_interval_seconds: int = 30

    @property
    def environment(self) -> str:
        """Expose normalized environment alias for runtime guards."""
        return self.app_env

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        """Fail fast when production runs with placeholder or undersized secrets."""
        if self.app_env.strip().lower() != "production":
            return self
        for field_name, secret_val in (
            ("JWT_SECRET", self.jwt_secret),
            ("WEBHOOK_SECRET", self.webhook_secret),
        ):
            normalized = secret_val.strip().lower()
            if (
                len(secret_val.encode("utf-8")) < 32
                or normalized.startswith("dev-only-")
                or normalized.startswith("change-me")
            ):
                raise ValueError(
                    f"Production environment requires a non-default {field_name} (>= 32 bytes)."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached runtime settings instance."""
    return Settings()

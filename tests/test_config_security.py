"""Unit tests for Settings production secret validation."""

import pytest
from pydantic import ValidationError

from slotguard.core.config import Settings


def test_settings_allows_dev_defaults_outside_production() -> None:
    """Local and docker profiles allow default secrets for convenience."""
    local_cfg = Settings(app_env="local")
    docker_cfg = Settings(app_env="docker")
    assert local_cfg.jwt_secret.startswith("dev-only-")
    assert docker_cfg.webhook_secret.startswith("dev-only-")


def test_settings_rejects_placeholder_secrets_in_production() -> None:
    """Production profile fails fast if default or short secrets are supplied."""
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(
            app_env="production",
            webhook_secret="S9xM4vK2pR8nW1qZ6tY3bN7cF0hJ5dL2aE9uV4",
        )

    with pytest.raises(ValidationError, match="WEBHOOK_SECRET"):
        Settings(
            app_env="production",
            jwt_secret="S9xM4vK2pR8nW1qZ6tY3bN7cF0hJ5dL2aE9uV4",
            webhook_secret="short-secret",
        )


def test_settings_accepts_strong_secrets_in_production() -> None:
    """Production profile boots cleanly with 32+ byte non-default secrets."""
    cfg = Settings(
        app_env="production",
        jwt_secret="K8mP9vX2qR7zW4nB6yT1cF5hJ0dL3sA9eG2uM8pQ4wZ6",
        webhook_secret="R3tN7yB2wQ9xL5pM1zK8vC4hJ6dS0fA3gT9uW2nE5rY8",
    )
    assert cfg.environment == "production"

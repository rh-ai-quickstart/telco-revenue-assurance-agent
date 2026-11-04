"""Environment-backed settings with one validation policy."""

from pydantic_settings import BaseSettings as PydanticBaseSettings
from pydantic_settings import SettingsConfigDict


class BaseSettings(PydanticBaseSettings):
    """Base class for typed settings loaded from the process environment."""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_file=None,
        extra="ignore",
        validate_default=True,
    )


__all__ = ["BaseSettings"]

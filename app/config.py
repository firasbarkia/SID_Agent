from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    sid_service_api_key: SecretStr = SecretStr("")
    groq_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    groq_model: str = Field(default="llama-3.1-8b-instant", min_length=1)
    gemini_model: str = Field(default="gemini-3.1-flash-lite", pattern=r"^[A-Za-z0-9._-]+$")
    ai_provider_timeout_seconds: float = Field(default=15, gt=0, le=120)
    ai_request_timeout_seconds: float = Field(default=35, gt=0, le=300)
    ai_max_concurrent_requests: int = Field(default=4, ge=1, le=100)
    ai_max_output_tokens: int = Field(default=1200, ge=64, le=4096)
    ai_circuit_failure_threshold: int = Field(default=3, ge=1, le=20)
    ai_circuit_recovery_seconds: float = Field(default=30, gt=0, le=86400)
    ai_configuration_cooldown_seconds: float = Field(default=300, gt=0, le=86400)

    @field_validator("sid_service_api_key", "groq_api_key", "gemini_api_key", mode="before")
    @classmethod
    def trim_keys(cls, value: str | SecretStr) -> str:
        return (value.get_secret_value() if isinstance(value, SecretStr) else value).strip()

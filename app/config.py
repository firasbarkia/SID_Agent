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
    mongodb_uri: SecretStr = SecretStr("")
    mongodb_database: str = Field(default="sid_agent", pattern=r"^[A-Za-z0-9_-]+$")
    mongodb_timeout_seconds: float = Field(default=5, gt=0, le=30)
    platform_users_collection: str = "users"
    platform_candidates_collection: str = "candidate_profiles"
    platform_companies_collection: str = "company_profiles"
    platform_offers_collection: str = "job_offers"
    ai_account_scope: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]+$")
    ai_shared_max_concurrent_requests: int = Field(default=4, ge=1, le=100)
    groq_requests_per_minute: int = Field(default=0, ge=0)
    groq_tokens_per_minute: int = Field(default=0, ge=0)
    groq_requests_per_day: int = Field(default=0, ge=0)
    gemini_requests_per_minute: int = Field(default=0, ge=0)
    gemini_tokens_per_minute: int = Field(default=0, ge=0)
    gemini_requests_per_day: int = Field(default=0, ge=0)
    worker_lease_seconds: int = Field(default=90, ge=10, le=600)
    worker_max_attempts: int = Field(default=3, ge=1, le=10)
    worker_poll_seconds: float = Field(default=1, gt=0, le=30)

    @field_validator(
        "sid_service_api_key", "groq_api_key", "gemini_api_key", "mongodb_uri", mode="before"
    )
    @classmethod
    def trim_keys(cls, value: str | SecretStr) -> str:
        return (value.get_secret_value() if isinstance(value, SecretStr) else value).strip()

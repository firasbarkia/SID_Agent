import pytest

from app.config import Settings


@pytest.fixture
def settings():
    # Explicit values prevent local .env secrets from entering tests.
    return Settings(
        _env_file=None,
        sid_service_api_key="test-service-key",
        groq_api_key="test-groq-key",
        gemini_api_key="test-gemini-key",
        groq_model="llama-3.1-8b-instant",
        gemini_model="gemini-3.1-flash-lite",
        ai_circuit_failure_threshold=2,
        ai_circuit_recovery_seconds=30,
        ai_configuration_cooldown_seconds=300,
        ai_provider_timeout_seconds=1,
        ai_request_timeout_seconds=3,
        ai_max_concurrent_requests=2,
        ai_max_output_tokens=1000,
    )


@pytest.fixture
def draft_payload():
    return {
        "candidate_profile": "Computer science student with a Python inventory project.",
        "job_description": "PFE internship in web development using Python and SQL in Sfax.",
        "company_context": "A Tunisian software startup.",
        "language": "en",
    }

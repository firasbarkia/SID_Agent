import os
from uuid import uuid4

import pytest
from pymongo import AsyncMongoClient
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from app.config import Settings
from app.db.connection import ensure_indexes
from app.identity import Principal


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
        mongodb_uri="",
        mongodb_database="sid_agent",
        ai_account_scope="test",
        groq_requests_per_minute=100,
        groq_tokens_per_minute=1000000,
        groq_requests_per_day=10000,
        gemini_requests_per_minute=100,
        gemini_tokens_per_minute=1000000,
        gemini_requests_per_day=10000,
        ai_shared_max_concurrent_requests=10,
        worker_lease_seconds=90,
        worker_max_attempts=3,
        worker_poll_seconds=1,
        qdrant_url="",
        qdrant_api_key="",
        qdrant_timeout_seconds=5,
        embedding_model_directory=".models/test-only",
        embedding_threads=1,
        indexing_batch_size=16,
        knowledge_max_concurrent_queries=2,
        platform_users_collection="users",
        platform_candidates_collection="candidate_profiles",
        platform_companies_collection="company_profiles",
        platform_offers_collection="job_offers",
    )


@pytest.fixture
def draft_payload():
    return {
        "candidate_profile": "Computer science student with a Python inventory project.",
        "job_description": "PFE internship in web development using Python and SQL in Sfax.",
        "company_context": "A Tunisian software startup.",
        "language": "en",
    }


@pytest.fixture
def candidate_user():
    return Principal(user_id="candidate-one", role="CANDIDATE")


@pytest.fixture
async def mongo_db():
    uri = os.environ.get("SID_TEST_MONGODB_URI")
    if not uri:
        pytest.skip("Set SID_TEST_MONGODB_URI to opt into replica-set integration tests")
    # Never read the app's MONGODB_URI or drop an existing database.
    name = "sid_test_" + uuid4().hex
    async with AsyncMongoClient(
        uri, tz_aware=True, serverSelectionTimeoutMS=5000, timeoutMS=5000
    ) as client:
        hello = await client.admin.command("hello")
        assert hello.get("setName"), "Integration tests require a disposable replica set"
        db = client[name].with_options(
            write_concern=WriteConcern("majority"), read_concern=ReadConcern("majority")
        )
        try:
            await ensure_indexes(db)
            yield db
        finally:
            assert name.startswith("sid_test_") and len(name) == len("sid_test_") + 32
            await client.drop_database(name)

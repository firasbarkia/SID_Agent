"""Explicit localhost test harness. Production app.main authentication is unchanged."""

import asyncio
import json
import logging
import os
import re
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from pydantic import SecretStr
from pymongo.uri_parser import parse_uri
from starlette.responses import JSONResponse

from app.config import Settings
from app.cv.worker import CVWorker
from app.db.store import ownership, public
from app.identity import get_principal
from app.knowledge.normalize import normalize
from app.knowledge.store import KnowledgeStore
from app.knowledge.worker import IndexWorker
from app.main import create_app
from app.matching.aliases import normalize_term
from app.matching.catalog import CITIES
from app.matching.models import OfferFilters
from app.matching.seed import seed_platform
from app.matching.worker import MatchSyncWorker
from app.worker import DraftWorker

DEMO_SERVICE_KEY = "sid-local-demo-only"
DEMO_USERS = {
    "demo-candidate": "CANDIDATE",
    "demo-candidate-two": "CANDIDATE",
    "demo-recruiter": "RECRUITER",
}
# Skills the simulated router recognizes. Live routing uses the provider gateway.
DEMO_SKILLS = [
    "Python", "SQL", "ReactJS", "React", "Frontend", "Java", "Node.js", "MongoDB",
    "Flutter", "Docker", "Angular", "TypeScript", "Django", "Power BI", "Figma", "Kotlin",
    "PHP", "Laravel", "Machine learning", "Excel", "Accounting", "Digital marketing",
    "Embedded systems", "C++", "Kubernetes", "Git",
]  # fmt: skip
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)


class LocalOnly:
    """Reject remote peers/hosts and browser requests from nonlocal origins."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope)
            origin = request.headers.get("origin")
            if (
                request.client is None
                or request.client.host not in LOOPBACK
                or request.url.hostname not in LOOPBACK
                or (origin and urlsplit(origin).hostname not in LOOPBACK)
            ):
                return await JSONResponse(
                    {"detail": {"code": "demo_localhost_only"}}, status_code=403
                )(scope, receive, send)
        await self.app(scope, receive, send)


async def verify_demo_user(request):
    user_id = request.headers.get("X-Demo-User", "demo-candidate")
    if user_id not in DEMO_USERS:
        raise HTTPException(401, detail={"code": "unknown_demo_user"})
    return user_id


def demo_settings(source=None, *, live=False):
    source = source or Settings()
    uri = source.mongodb_uri.get_secret_value() or (
        "mongodb://127.0.0.1:27018/?replicaSet=rs0&directConnection=true"
    )
    if any(host not in LOOPBACK for host, _ in parse_uri(uri)["nodelist"]):
        raise ValueError("Demo requires a localhost MongoDB URI")
    qdrant = source.qdrant_url or "http://127.0.0.1:6335"
    if urlsplit(qdrant).hostname not in LOOPBACK:
        raise ValueError("Demo requires a localhost Qdrant URL")
    changes = {
        "mongodb_uri": SecretStr(uri),
        "mongodb_database": "sid_demo",
        "sid_service_api_key": SecretStr(DEMO_SERVICE_KEY),
        "platform_users_collection": "demo_users",
        "platform_candidates_collection": "demo_candidates",
        "platform_companies_collection": "demo_companies",
        "platform_offers_collection": "demo_offers",
        "qdrant_url": qdrant,
        "embedding_model_directory": str(ROOT / source.embedding_model_directory),
    }
    if not live:
        changes.update(
            groq_api_key=SecretStr("simulated-no-network"),
            gemini_api_key=SecretStr(""),
            groq_model="simulated-demo",
            ai_account_scope="sid_demo_simulated",
            groq_requests_per_minute=120,
            groq_tokens_per_minute=1000000,
            groq_requests_per_day=10000,
        )
    return Settings(_env_file=None, **{**source.model_dump(), **changes})


def fixture_filters(message, previous=(), last_filters=None):
    """Deterministic router aid for the simulated demo; live routing uses its schema."""
    folded = f" {normalize_term(message)} "

    def has(term):
        return f" {normalize_term(term)} " in folded

    last = OfferFilters.model_validate(last_filters or {})
    cities = [city for city in CITIES if has(city)]
    remembered = [p for p in previous if normalize_term(p) in CITIES]
    if cities:
        location = max(cities, key=lambda city: folded.rfind(f" {city} ")).title()
    else:
        location = remembered[0].title() if remembered else last.location
    if has("pfe"):
        contract = "PFE"
    elif has("internship") or has("stage"):
        contract = "INTERNSHIP"
    elif has("employment") or has("emploi") or has("job"):
        contract = "EMPLOYMENT"
    else:
        contract = last.contract
    skills = [skill for skill in DEMO_SKILLS if has(skill)]
    return OfferFilters(location=location, contract=contract, skills=skills or last.skills)


def simulated_output(system, data):
    """Small deterministic fixtures for exercising workflows, not quality evaluation."""
    title = data.get("schema", {}).get("title")
    if title == "ContextDecision":
        message = data["message"]
        matching = re.search(r"find|match|rank|cherche|trouve", message, re.I)
        last = data.get("history", [])[-1:] or [{}]
        followup = last[0].get("intent") == "matching" and not re.search(
            r"advice|conseil|improve|amélior", message, re.I
        )
        filters = fixture_filters(message, data.get("keywords", []), last[0].get("filters"))
        # Ask for clarification rather than inventing interpretation of alternatives/negation.
        locations = [
            name
            for name in ("sfax", "tunis", "sousse")
            if re.search(r"\b" + name + r"\b", message, re.I)
        ]
        ambiguous = len(locations) > 1 or bool(
            re.search(r"\b(not|pas|sans|without|except|sauf|aucun|no)\b", message, re.I)
        )
        words = re.findall(r"[\w+-]{2,}", message)
        memory = [*words, *data.get("keywords", [])]
        if filters.location:
            memory = [
                word
                for word in memory
                if word.casefold() not in {"sfax", "tunis", "sousse"}
                or word.casefold() == filters.location.casefold()
            ]
        return json.dumps(
            {
                "intent": "clarify"
                if ambiguous
                else "matching"
                if matching or followup
                else "advice",
                "query": message[:350],
                "keywords": list(dict.fromkeys(memory))[:8],
                "filters": filters.model_dump(),
            }
        )
    if title == "MatchAnswer":
        french = data["language"] == "fr"
        evidence = []
        for match in data["matches"]:
            percent = match["compatibility_percent"]
            if percent is None:
                reason = "Correspond à la recherche." if french else "Matches the search."
            elif french:
                reason = f"Matching {percent} % : couvre les exigences principales."
            else:
                reason = f"Matching {percent}%: covers the main requirements."
            quote = match["text"].splitlines()[0][:150]
            evidence.append({"id": match["id"], "quote": quote, "reason": reason})
        return json.dumps(
            {
                "text": (
                    "Résultats simulés pour tester le parcours de matching."
                    if french
                    else "Simulated explanations for testing the matching workflow."
                ),
                "evidence": evidence,
            }
        )
    if title == "AssessmentAnswer":
        french = data["language"] == "fr"
        values = [
            line.partition(":")[2].strip()
            for line in data["profile"].splitlines()
            if line.partition(":")[2].strip()
        ]
        strengths = [
            {"text": f"Point fort : {value}" if french else f"Strength: {value}", "quote": value}
            for value in values[:2]
        ] or [
            {
                "text": "Profil fourni" if french else "Profile provided",
                "quote": data["profile"][:80],
            }
        ]
        advice = [{"text": item, "basis": "missing_field"} for item in data["missing_fields"][:2]]
        for gap in data["market_gaps"][:2]:
            text = (
                f"Montrez un projet avec {gap['requirement']}."
                if french
                else f"Show a project using {gap['requirement']}."
            )
            advice.append({"text": text, "basis": "market_gap"})
        if not advice:
            advice = [{"text": "Describe one project with results.", "basis": "wording"}]
        return json.dumps({"strengths": strengths, "advice": advice[:6]})
    if title == "Answer":
        ref = data["references"][0]
        return json.dumps(
            {
                "text": f"Demo advice: explore {ref['label']} and describe a concrete project "
                "that demonstrates your skills. This is a simulated response.",
                "cited_ids": [ref["id"]],
            }
        )
    if title == "Extraction":
        facts = []
        for page in data["pages"]:
            for line in page["text"].splitlines():
                match = re.match(
                    r"(name|email|phone|location|headline|skill|education|experience|project|language)"
                    r"\s*:\s*(.+)",
                    line.strip(),
                    re.I,
                )
                if match:
                    value = match[2][:240]
                    facts.append(
                        {
                            "field": match[1].lower(),
                            "value": value,
                            "evidence": {"page": page["page"], "quote": value},
                        }
                    )
        return json.dumps({"facts": facts[:40]})
    letter = (
        "Dear recruitment team,\n\nI am interested in your opportunity. "
        "My profile includes the following information: "
        + data.get("candidate_profile", "")[:500]
        + "\n\nI would welcome the opportunity to discuss my application.\n\nKind regards"
    )
    if title == "Dossier":
        job = data["job_description"].splitlines()[0][:200]
        return json.dumps(
            {
                "cover_letter": letter,
                "evidence": [
                    {
                        "requirement": job,
                        "job_quote": job,
                        "candidate_quote": None,
                        "assessment": "not_evidenced",
                    }
                ],
                "cv_suggestions": [],
            }
        )
    return letter


def simulated_response(request):
    body = json.loads(request.content)
    system = body["messages"][0]["content"]
    data = json.loads(body["messages"][1]["content"])
    return httpx.Response(
        200,
        json={
            "choices": [
                {"finish_reason": "stop", "message": {"content": simulated_output(system, data)}}
            ]
        },
    )


async def run_worker(worker):
    while True:
        try:
            if not await worker.run_once():
                await asyncio.sleep(worker.settings.worker_poll_seconds)
        except Exception:
            logger.exception("Demo worker error; retrying")
            await asyncio.sleep(2)


async def run_match_sync(worker, seconds):
    while True:
        await asyncio.sleep(seconds)
        try:
            await worker.run_once()
        except Exception:
            logger.exception("Demo match sync error; retrying")


def create_demo_app(settings=None, *, live=None, knowledge_embedder=None):
    live = os.getenv("SID_DEMO_LIVE_AI") == "1" if live is None else live
    config = demo_settings(settings, live=live)
    # Search can be enabled after downloading the model and restarting the demo.
    if knowledge_embedder is None and not Path(config.embedding_model_directory).is_dir():
        config.qdrant_url = ""
    application = create_app(
        config,
        verify_user_id=verify_demo_user,
        transport=None if live else httpx.MockTransport(simulated_response),
        knowledge_embedder=knowledge_embedder,
    )
    application.add_middleware(LocalOnly)
    original_lifespan = application.router.lifespan_context
    application.state.demo_setup_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            db = app.state.store.db
            await seed_platform(db, config)
            workers = [DraftWorker, CVWorker]
            tasks = [
                asyncio.create_task(run_worker(worker(db, app.state.ai_gateway, config)))
                for worker in workers
            ]
            engine = app.state.match_engine
            if engine is not None:
                sync = MatchSyncWorker(db, engine.index.qdrant, engine.embedder, config)
                # Index once before serving so the first search finds the seeded data.
                try:
                    await sync.run_once()
                except Exception:
                    logger.exception("Initial demo match sync failed; retrying in background")
                tasks.append(asyncio.create_task(run_match_sync(sync, config.match_sync_seconds)))
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if engine is not None:
                    await sync.release()

    application.router.lifespan_context = lifespan

    @application.get("/demo/status", tags=["Local demo"])
    async def demo_status():
        engine = application.state.match_engine
        return {
            "demo": True,
            "auth": "localhost synthetic identities",
            "ai_mode": "live" if live else "simulated",
            "database": config.mongodb_database,
            "users": DEMO_USERS,
            "workers": ["draft", "cv", "match-sync"] if engine else ["draft", "cv"],
            "knowledge_configured": application.state.knowledge_search is not None,
            "matching": "platform" if engine else "disabled",
            "matching_index": await engine.index.status() if engine else None,
            "platform_data": "demo seed (synthetic people and companies)",
        }

    @application.get("/demo/offers", tags=["Local demo"])
    async def demo_offers(request: Request):
        """Open, verified offers for console forms; recruiters see their own company's."""
        principal = await get_principal(request)
        offers, _ = await application.state.catalog.eligible_offers()
        if principal.role == "RECRUITER":
            offers = [offer for offer in offers if offer.company_id == principal.company_id]
        return [
            {
                "id": offer.id,
                "version": offer.version,
                "title": offer.title,
                "company": offer.company.name,
                "company_id": offer.company_id,
                "city": offer.company.city,
                "contract": offer.contract,
                "requirements": offer.requirements,
            }
            for offer in sorted(offers, key=lambda offer: offer.id)
        ]

    @application.get("/demo/workspace", tags=["Local demo"])
    async def workspace(request: Request):
        principal = await get_principal(request)
        db = application.state.store.db
        result = {}
        for name, collection in {
            "profiles": "sid_profiles",
            "drafts": "sid_drafts",
            "cv_imports": "sid_cv_imports",
            "conversations": "sid_conversations",
        }.items():
            rows = (
                await db[collection]
                .find(ownership(principal))
                .sort("created_at", -1)
                .limit(100)
                .to_list()
            )
            result[name] = [public(row) for row in rows if row.get("status") != "deleted"]
        return result

    @application.post("/demo/knowledge/setup", tags=["Local demo"])
    async def setup_knowledge():
        search = application.state.knowledge_search
        if search is None:
            raise HTTPException(503, detail="Download the embedding model and restart the demo.")
        if application.state.demo_setup_lock.locked():
            raise HTTPException(409, detail="Reference setup is already running.")
        async with application.state.demo_setup_lock:
            store = KnowledgeStore(search.store.db)
            try:
                generation = await store.active(search.embedder.fingerprint)
                await search.index.verify(generation)
                return {"generation": generation, "status": "already_ready"}
            except ValueError:
                pass
            snapshot = await asyncio.to_thread(normalize, ROOT / "data/knowledge_base/2026-09-29")
            if snapshot.rejected:
                raise HTTPException(422, detail="Reference snapshot has rejected rows.")
            counts, records = Counter(), []
            for record in snapshot.records:
                group = record.source, record.kind
                if counts[group] < 15:
                    records.append(record)
                    counts[group] += 1
            await store.import_batch(records)
            generation = await store.begin_generation(
                search.embedder.fingerprint, search.embedder.dimensions, prefix="sid_test_"
            )
            await search.index.create(generation)
            await store.seed_generation(generation)
            worker = IndexWorker(search.store.db, search.index, search.embedder, config)
            async with asyncio.timeout(180):
                while await worker.run_once():
                    pass
            await store.promote(generation, search.index)
            return {"generation": generation, "records": len(records), "status": "ready"}

    return application


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.demo:create_demo_app", factory=True, host="127.0.0.1", port=8001, proxy_headers=False
    )

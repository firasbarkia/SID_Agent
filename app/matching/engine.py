"""Matching engine for both branches of the AI pipeline diagram.

Candidate → offers and company → candidates share one path: compose a query vector
from the prompt, the profile or company context and the conversation keywords;
retrieve from the versioned index; reload every hit from the platform; score with
the deterministic rubric; keep the top five. Explanations are added by callers.
"""

import asyncio

import httpx
import numpy as np

from app.db.connection import server_time
from app.domain import DomainError
from app.knowledge.models import digest
from app.matching.aliases import normalize_term
from app.matching.anonymize import Anonymizer
from app.matching.catalog import city_of
from app.matching.models import OfferFilters
from app.matching.results import (
    CandidateBinding,
    CandidateMatch,
    CompanySummary,
    OfferBinding,
    OfferMatch,
    ProfileView,
    SearchResult,
)
from app.matching.rubric import HIGH, template_justification

# Pipeline diagram: "Profil user + Mots-clés conversation + Prompt".
QUERY_WEIGHTS = (0.60, 0.25, 0.15)
# Candidate branch: relevance to the request, then fit to the candidate's profile.
RELEVANCE_WEIGHT, COMPATIBILITY_WEIGHT = 0.6, 0.4
TOP = 5
IDENTITY_FIELDS = {"name", "email", "phone"}
STATEMENT_FIELDS = {"experience", "project", "education", "headline"}


def profile_view(content: str) -> ProfileView:
    """Read an accepted SID profile (`field: value` lines) without identity fields."""
    skills, statements, kept = [], [], []
    for line in content.splitlines():
        field, separator, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if separator and field in IDENTITY_FIELDS:
            continue
        if separator and field == "skill" and value:
            skills.append(value)
        elif separator and field in STATEMENT_FIELDS and value:
            statements.append(value)
        if line.strip():
            kept.append(line.strip())
    return ProfileView(skills=skills, statements=statements, text="\n".join(kept))


async def resolve_platform_offer(catalog, principal, offer_id, version):
    """Packs need only the catalog: an open, verified offer at the exact version."""
    if principal.role != "CANDIDATE":
        raise DomainError(403, "candidate_access_required")
    offer = await catalog.offer(offer_id)
    if offer is None:
        raise DomainError(404, "offer_not_accessible")
    if offer.version != version:
        raise DomainError(409, "stale_offer_version")
    return offer


def _normalized(vector):
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-12 else vector


class MatchEngine:
    def __init__(self, db, catalog, index, embedder, rubric, settings):
        self.db, self.catalog, self.index = db, catalog, index
        self.embedder, self.rubric, self.settings = embedder, rubric, settings
        self.running = 0
        secret = settings.sid_service_api_key.get_secret_value()
        self.anonymizer = Anonymizer(secret) if secret else None

    async def _encode(self, texts):
        if not texts:
            return []
        if self.running >= self.settings.match_max_concurrent_queries:
            raise DomainError(503, "matching_busy")
        self.running += 1
        try:
            vectors = await asyncio.to_thread(self.embedder.encode, list(texts))
        except ValueError as exc:
            raise DomainError(503, "matching_unavailable") from exc
        finally:
            self.running -= 1
        return [np.asarray(vector, dtype=np.float32) for vector in vectors]

    async def _phrases(self, texts):
        texts = [text for text in dict.fromkeys(texts) if text.strip()]
        return list(zip(texts, await self._encode(texts), strict=True))

    async def compose(self, prompt, context, keywords):
        parts = [
            (weight, self.embedder.chunks(text) if text.strip() else [])
            for weight, text in zip(
                QUERY_WEIGHTS, (prompt or "", context or "", " ".join(keywords)), strict=True
            )
        ]
        vectors = iter(await self._encode([chunk for _, chunks in parts for chunk in chunks]))
        total = None
        for weight, chunks in parts:
            if chunks:
                part = _normalized(sum(next(vectors) for _ in chunks))
                total = weight * part if total is None else total + weight * part
        if total is None:
            raise DomainError(422, "empty_search_query")
        return _normalized(total)

    @staticmethod
    def _city(location):
        if not location:
            return None
        # An unknown place filters to nothing rather than silently widening the search.
        return city_of(location) or normalize_term(location)

    async def _retrieve(self, kind, vector, filters, limit=None):
        try:
            hits = await self.index.query(
                kind,
                [float(value) for value in vector],
                limit=limit or self.settings.match_pool_size,
                filters=filters,
            )
            records = await self.index.records(kind, [hit["record_id"] for hit in hits])
        except ValueError as exc:
            raise DomainError(503, "matching_index_not_ready") from exc
        except httpx.HTTPError as exc:
            raise DomainError(503, "matching_unavailable") from exc
        return hits, records

    @staticmethod
    def binding(offer) -> OfferBinding:
        return OfferBinding(
            offer_id=offer.id,
            offer_version=offer.version,
            company_id=offer.company_id,
            company_version=offer.company_version,
            snapshot_sha256=digest(offer.model_dump()),
        )

    def _satisfies(self, wanted, requirements, offer):
        text = [(offer.title, None), (offer.description, None)]
        for skill, vector in wanted:
            in_requirements = self.rubric.best(skill, vector, requirements, sentences=False)[0]
            in_text = self.rubric.best(skill, vector, text, sentences=True)[0]
            if max(in_requirements, in_text) < HIGH:
                return False
        return True

    async def search_offers(self, principal, query, *, profile, keywords, filters):
        if principal.role != "CANDIDATE":
            raise DomainError(403, "candidate_access_required")
        filters = OfferFilters.model_validate(filters)
        vector = await self.compose(query, profile.text if profile else "", keywords)
        hits, records = await self._retrieve(
            "offers", vector, {"contract": filters.contract, "city": self._city(filters.location)}
        )
        wanted = await self._phrases(filters.skills)
        scored = profile is not None and bool(profile.skills or profile.statements)
        skills = await self._phrases(profile.skills) if scored else []
        statements = await self._phrases(profile.statements) if scored else []
        results, stale = [], 0
        for hit in hits:
            record = records.get(hit["record_id"])
            offer = await self.catalog.offer(hit["record_id"])
            if (
                offer is None
                or record is None
                or offer.version != hit["version"]
                or record["version"] != offer.version
            ):
                stale += 1
                continue
            requirements = record["phrases"].get("requirements", [])
            if not self._satisfies(wanted, requirements, offer):
                continue
            compatibility = self.rubric.score(requirements, skills, statements) if scored else None
            relevance = float(np.clip(hit["score"], 0.0, 1.0))
            score = relevance
            if compatibility is not None:
                score = (
                    RELEVANCE_WEIGHT * relevance
                    + COMPATIBILITY_WEIGHT * compatibility.percent / 100
                )
            results.append(
                OfferMatch(
                    id=offer.id,
                    version=offer.version,
                    title=offer.title,
                    contract=offer.contract,
                    company=CompanySummary(
                        id=offer.company.id,
                        name=offer.company.name,
                        sector=offer.company.sector,
                        city=offer.company.city,
                    ),
                    description=offer.description,
                    requirements=offer.requirements,
                    relevance=relevance,
                    compatibility=compatibility,
                    score=score,
                    binding=self.binding(offer),
                )
            )
        results.sort(key=lambda result: (-result.score, result.id))
        return SearchResult(
            target="offers",
            filters=filters,
            results=results[:TOP],
            stale_dropped=stale,
            pool=len(hits),
        )

    def _recruiter(self, principal):
        if principal.role != "RECRUITER" or not principal.company_id:
            raise DomainError(403, "recruiter_access_required")

    async def recruiter_offer(self, principal, offer_id):
        self._recruiter(principal)
        offer = await self.catalog.offer(offer_id)
        if offer is None or offer.company_id != principal.company_id:
            raise DomainError(404, "offer_not_accessible")
        return offer

    async def _candidate_match(self, principal, candidate, *, relevance, compatibility, anonymized):
        if anonymized:
            if self.anonymizer is None:
                raise DomainError(503, "anonymization_not_configured")
            reference = self.anonymizer.pseudonym(principal.company_id, candidate.id)
            await self.db.sid_candidate_refs.update_one(
                {"_id": reference},
                {
                    "$setOnInsert": {
                        "company_id": principal.company_id,
                        "candidate_id": candidate.id,
                        "created_at": await server_time(self.db),
                    }
                },
                upsert=True,
            )
            identity = {"candidate_ref": reference, "display_name": reference}
        else:
            name = (await self.catalog.identity(candidate.id))["name"]
            identity = {
                "candidate_id": candidate.id,
                "display_name": name or candidate.id,
                "city": candidate.city,
            }
        if compatibility is not None:
            score = compatibility.percent / 100
        else:
            score = relevance or 0.0
        return CandidateMatch(
            anonymized=anonymized,
            **identity,
            skills=candidate.skills,
            highlights=candidate.statements[:3],
            is_new=candidate.is_new,
            relevance=relevance,
            compatibility=compatibility,
            score=score,
            binding=CandidateBinding(candidate_version=candidate.version),
        )

    async def search_candidates(
        self, principal, query, *, criteria, context, keywords, filters, anonymized
    ):
        self._recruiter(principal)
        if anonymized and self.anonymizer is None:
            raise DomainError(503, "anonymization_not_configured")
        filters = OfferFilters.model_validate(filters)
        vector = await self.compose(query, context, keywords)
        # Anonymized selection is based on skills and experience only (§5.2).
        city = None if anonymized else self._city(filters.location)
        hits, records = await self._retrieve("candidates", vector, {"city": city})
        criteria = list(dict.fromkeys(c for c in criteria if c.strip()))[:12]
        wanted = await self._phrases(criteria)
        rows, stale = [], 0
        for hit in hits:
            record = records.get(hit["record_id"])
            candidate = await self.catalog.candidate(hit["record_id"])
            if (
                candidate is None
                or record is None
                or candidate.version != hit["version"]
                or record["version"] != candidate.version
            ):
                stale += 1
                continue
            phrases = record["phrases"]
            compatibility = self.rubric.score(
                wanted, phrases.get("skills", []), phrases.get("statements", [])
            )
            rows.append((candidate, float(np.clip(hit["score"], 0.0, 1.0)), compatibility))
        rows.sort(key=lambda row: (-(row[2].percent if row[2] else -1), -row[1], row[0].id))
        results = [
            await self._candidate_match(
                principal,
                candidate,
                relevance=relevance,
                compatibility=compatibility,
                anonymized=anonymized,
            )
            for candidate, relevance, compatibility in rows[:TOP]
        ]
        return SearchResult(
            target="candidates",
            filters=filters,
            criteria=criteria,
            results=results,
            stale_dropped=stale,
            pool=len(hits),
        )

    async def resolve_candidate_ref(self, principal, reference):
        self._recruiter(principal)
        row = await self.db.sid_candidate_refs.find_one(
            {"_id": reference, "company_id": principal.company_id}
        )
        if row is None:
            raise DomainError(404, "candidate_ref_not_found")
        return row["candidate_id"]

    async def resolve_offer(self, principal, offer_id, version):
        return await resolve_platform_offer(self.catalog, principal, offer_id, version)

    async def score(self, principal, offer_id, candidate_id, *, language="fr", anonymized=False):
        offer = await self.recruiter_offer(principal, offer_id)
        candidate = await self.catalog.candidate(candidate_id)
        if candidate is None:
            raise DomainError(404, "candidate_not_accessible")
        compatibility = self.rubric.score(
            await self._phrases(offer.requirements),
            await self._phrases(candidate.skills),
            await self._phrases(candidate.statements),
        )
        match = await self._candidate_match(
            principal,
            candidate,
            relevance=None,
            compatibility=compatibility,
            anonymized=anonymized,
        )
        if compatibility is None:
            explanation = (
                "L'offre ne liste aucune compétence requise."
                if language == "fr"
                else "The offer lists no required skills."
            )
        else:
            explanation = template_justification(compatibility, language)
        # The binding pins the exact offer content this score was computed against.
        return self.binding(offer), match.model_copy(
            update={"explanation": explanation, "explanation_source": "template"}
        )

    async def market_gaps(self, profile: ProfileView, limit=12):
        vector = await self.compose(profile.text or " ".join(profile.skills), "", [])
        hits, records = await self._retrieve("offers", vector, {}, limit=limit)
        skills = await self._phrases(profile.skills)
        statements = await self._phrases(profile.statements)
        counts, labels, compared = {}, {}, 0
        for hit in hits:
            record = records.get(hit["record_id"])
            offer = await self.catalog.offer(hit["record_id"])
            if offer is None or record is None or record["version"] != offer.version:
                continue
            compatibility = self.rubric.score(
                record["phrases"].get("requirements", []), skills, statements
            )
            if compatibility is None:
                continue
            compared += 1
            for row in compatibility.criteria:
                if row.level in {"missing", "related"}:
                    key = self.rubric.aliases.canonical(row.requirement)
                    counts[key] = counts.get(key, 0) + 1
                    labels.setdefault(key, row.requirement)
        ranked = sorted(counts.items(), key=lambda item: (-item[1], labels[item[0]]))
        return [
            {"requirement": labels[key], "offers": count, "of": compared}
            for key, count in ranked[:5]
        ]

    async def revalidate(self, principal, response):
        """A replayed turn may only resurface results that are still current."""
        for item in response.get("results", []):
            if item.get("kind") == "offer":
                offer = await self.catalog.offer(item["id"])
                if offer is None or offer.version != item["version"]:
                    raise DomainError(409, "search_results_changed")
            elif item.get("kind") == "candidate":
                candidate_id = item.get("candidate_id")
                if candidate_id is None:
                    try:
                        candidate_id = await self.resolve_candidate_ref(
                            principal, item["candidate_ref"]
                        )
                    except DomainError as exc:
                        raise DomainError(409, "search_results_changed") from exc
                candidate = await self.catalog.candidate(candidate_id)
                if candidate is None or candidate.version != item["binding"]["candidate_version"]:
                    raise DomainError(409, "search_results_changed")

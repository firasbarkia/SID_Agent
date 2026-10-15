"""In-memory synthetic index, never connected to platform candidate/job collections."""

import hashlib
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path

from app.domain import DomainError
from app.knowledge.models import digest
from app.matching.models import (
    OfferBinding,
    OfferFilters,
    OfferMatch,
    OfferSearchResult,
    SyntheticCorpus,
)

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "data/contracts/offers-v1.json"
# Reviewed fixture aliases only. This is not a real semantic model.
ALIASES = {
    "reactjs": "react",
    "react.js": "react",
    "developpement": "development",
    "stage": "internship",
    "emploi": "employment",
    "web": "frontend",
}
STOP = {"i", "a", "in", "for", "the", "with", "je", "un", "une", "en", "de", "cherche"}
FINGERPRINT = digest({"encoder": "synthetic-token-hash-v1", "aliases": ALIASES, "dims": 128})


def tokens(text):
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c)
    )
    return [ALIASES.get(t, t) for t in re.findall(r"[\w.#+-]+", folded) if t not in STOP]


def vector(text):
    result = [0.0] * 128
    for word, count in Counter(tokens(text)).items():
        index = int.from_bytes(hashlib.sha256(word.encode()).digest()[:2], "big") % len(result)
        result[index] += count
    norm = math.sqrt(sum(value * value for value in result))
    return [value / norm for value in result] if norm else result


def same_term(a, b):
    return tokens(a) == tokens(b)


def fixture_filters(message, previous=(), last_filters=None):
    """Deterministic router aid for the simulated demo; live routing uses its schema."""
    words = set(tokens(message))
    locations = [name for name in ("Sfax", "Tunis", "Sousse") if name.casefold() in words]
    previous_locations = [p for p in previous if p.casefold() in {"sfax", "tunis", "sousse"}]
    contract = next(
        (
            value
            for term, value in (
                ("pfe", "PFE"),
                ("internship", "INTERNSHIP"),
                ("employment", "EMPLOYMENT"),
            )
            if term in words
        ),
        None,
    )
    last = OfferFilters.model_validate(last_filters or {})
    current_skills = [
        skill
        for skill in ("Python", "SQL", "React", "Frontend", "Java")
        if skill.casefold() in words
    ]
    return OfferFilters(
        location=locations[-1] if locations else next(iter(previous_locations), last.location),
        contract=contract or last.contract,
        skills=current_skills or last.skills,
    )


class SyntheticOfferSearch:
    """Explicit fixture injection only. Replacement/deletion rebuilds the whole index."""

    def __init__(self, corpus: SyntheticCorpus):
        self.replace(corpus)

    @classmethod
    def from_fixture(cls):
        return cls(SyntheticCorpus.model_validate_json(CORPUS.read_text("utf-8")))

    def replace(self, corpus):
        corpus = SyntheticCorpus.model_validate(corpus).model_copy(deep=True)
        self._offers = {offer.id: offer for offer in corpus.offers}
        self._vectors = {
            offer.id: vector(" ".join([offer.title, offer.description, *offer.skills]))
            for offer in corpus.offers
        }
        self.generation = digest({"corpus": corpus.model_dump(), "encoder": FINGERPRINT})

    def resolve(self, principal, offer_id, version):
        if principal.role != "CANDIDATE":
            raise DomainError(403, "candidate_access_required")
        offer = self._offers.get(offer_id)
        if offer is None or offer.status != "OPEN" or offer.visibility != "PUBLIC":
            raise DomainError(404, "offer_not_accessible")
        if offer.version != version:
            raise DomainError(409, "stale_offer_version")
        return offer.model_copy(deep=True)

    @staticmethod
    def binding(offer):
        return OfferBinding(
            offer_id=offer.id,
            offer_version=offer.version,
            company_id=offer.company_id,
            company_version=offer.company_version,
            snapshot_sha256=digest(offer.model_dump()),
        )

    async def search(self, principal, query, *, profile, keywords, filters):
        if principal.role != "CANDIDATE":
            raise DomainError(403, "candidate_access_required")
        filters = OfferFilters.model_validate(filters)
        # Current query has greatest weight. Profile and memory enrich ranking only;
        # they cannot relax explicit filters or create candidate qualifications.
        q = vector(query)
        p = vector(profile["content"] if profile else "")
        m = vector(" ".join(keywords))
        combined = [a * 0.6 + b * 0.25 + c * 0.15 for a, b, c in zip(q, p, m, strict=True)]
        norm = math.sqrt(sum(value * value for value in combined))
        combined = [value / norm for value in combined] if norm else combined
        query_terms = set(
            tokens(
                " ".join([query, *keywords, profile["content"] if profile else "", *filters.skills])
            )
        )
        found = []
        for record in self._offers.values():
            if record.status != "OPEN" or record.visibility != "PUBLIC":
                continue
            if filters.location and not same_term(filters.location, record.location):
                continue
            if filters.contract and filters.contract != record.contract:
                continue
            if any(
                not any(same_term(skill, term) for term in record.skills)
                for skill in filters.skills
            ):
                continue
            matched = sorted(query_terms & set(tokens(" ".join(record.skills))))
            # Hash collisions alone never establish relevance.
            if not matched:
                continue
            offer = self.resolve(principal, record.id, record.version)
            score = sum(a * b for a, b in zip(combined, self._vectors[offer.id], strict=True))
            found.append(
                OfferMatch(
                    id=offer.id,
                    version=offer.version,
                    title=offer.title,
                    company_id=offer.company_id,
                    location=offer.location,
                    contract=offer.contract,
                    ranking_score=min(1.0, max(-1.0, score)),
                    matched_terms=matched,
                    evidence=offer.description,
                    binding=self.binding(offer),
                )
            )
        return OfferSearchResult(
            generation=self.generation,
            filters=filters,
            results=sorted(found, key=lambda result: (-result.ranking_score, result.id))[:5],
        )

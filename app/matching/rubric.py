"""Deterministic compatibility rubric compat-v1. The LLM never computes scores.

Calibration (15 October 2026, pinned multilingual MiniLM, 384 dimensions): unrelated
short terms scored up to 0.42 and cross-language equivalents 0.86–0.97, while related
technical pairs ranged from 0.09 to 0.70 and relevant sentences from 0.35 to 0.58.
Hence HIGH=0.80 for an equivalent and LOW=0.50 for a related term; technical
synonyms come from the reviewed alias table and word sequences inside sentences.
"""

from statistics import fmean
from typing import Literal

import numpy as np
from pydantic import Field

from app.domain import Input
from app.matching.aliases import AliasTable, normalize_term

RUBRIC_VERSION = "compat-v1"
HIGH, LOW = 0.80, 0.50
POINTS = {"demonstrated": 1.0, "listed": 0.8, "related": 0.4, "missing": 0.0}
LABEL = "Coverage of the offer's stated requirements, not a hiring probability."
Level = Literal["demonstrated", "listed", "related", "missing"]


class Criterion(Input):
    requirement: str
    level: Level
    points: float
    similarity: float = Field(ge=0, le=1)
    evidence: str | None = None


class Compatibility(Input):
    percent: int = Field(ge=0, le=100)
    rubric_version: Literal["compat-v1"] = RUBRIC_VERSION
    model_fingerprint: str
    label: str = LABEL
    criteria: list[Criterion]
    gaps: list[str]


def ngrams(text, longest=4):
    words = normalize_term(text).split()
    for size in range(1, longest + 1):
        for start in range(len(words) - size + 1):
            gram = " ".join(words[start : start + size])
            # Single letters ("c'est" → "c") would otherwise match the C language.
            if len(gram) >= 2:
                yield gram


class Rubric:
    def __init__(self, aliases: AliasTable, fingerprint: str, *, high=HIGH, low=LOW):
        self.aliases, self.fingerprint = aliases, fingerprint
        self.high, self.low = high, low

    @staticmethod
    def _cosine(a, b) -> float:
        if a is None or b is None:
            return 0.0
        return float(np.clip(np.dot(np.asarray(a), np.asarray(b)), 0.0, 1.0))

    def best(self, requirement, vector, phrases, *, sentences):
        best = (0.0, None)
        for text, other in phrases:
            if sentences:
                covered = any(self.aliases.covers(requirement, gram) for gram in ngrams(text))
            else:
                covered = self.aliases.covers(requirement, text)
            similarity = 1.0 if covered else self._cosine(vector, other)
            if similarity > best[0]:
                best = (similarity, text)
        return best

    def score(self, criteria, skills, statements) -> Compatibility | None:
        """Each argument is a list of (text, vector) pairs; vectors may be None."""
        if not criteria:
            return None
        rows = []
        for requirement, vector in criteria:
            shown = self.best(requirement, vector, statements, sentences=True)
            listed = self.best(requirement, vector, skills, sentences=False)
            if shown[0] >= self.high:
                level, (similarity, evidence) = "demonstrated", shown
            elif listed[0] >= self.high:
                level, (similarity, evidence) = "listed", listed
            elif max(shown[0], listed[0]) >= self.low:
                level, (similarity, evidence) = "related", max(shown, listed, key=lambda b: b[0])
            else:
                level, similarity, evidence = "missing", max(shown[0], listed[0]), None
            rows.append(
                Criterion(
                    requirement=requirement,
                    level=level,
                    points=POINTS[level],
                    similarity=round(similarity, 3),
                    evidence=evidence,
                )
            )
        return Compatibility(
            percent=round(100 * fmean(row.points for row in rows)),
            model_fingerprint=self.fingerprint,
            criteria=rows,
            gaps=[row.requirement for row in rows if row.level == "missing"],
        )


def template_justification(compatibility: Compatibility, language: str) -> str:
    strong = [
        r.requirement for r in compatibility.criteria if r.level in {"demonstrated", "listed"}
    ]
    related = [r.requirement for r in compatibility.criteria if r.level == "related"]
    missing = compatibility.gaps
    if language == "fr":
        parts = [
            f"couvre {', '.join(strong)}" if strong else "",
            f"proche de {', '.join(related)}" if related else "",
            f"à renforcer : {', '.join(missing)}" if missing else "",
        ]
        return f"Matching {compatibility.percent} % : " + " ; ".join(p for p in parts if p) + "."
    parts = [
        f"covers {', '.join(strong)}" if strong else "",
        f"related: {', '.join(related)}" if related else "",
        f"missing: {', '.join(missing)}" if missing else "",
    ]
    return f"Matching {compatibility.percent}%: " + "; ".join(p for p in parts if p) + "."

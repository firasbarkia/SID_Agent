"""Reviewed skill equivalences. Embeddings cover the rest; this table is never inferred."""

import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALIASES = ROOT / "data/matching/skill-aliases-v1.json"


def normalize_term(text) -> str:
    folded = "".join(
        c
        for c in unicodedata.normalize("NFKD", str(text).casefold())
        if not unicodedata.combining(c)
    )
    return " ".join(re.findall(r"[a-z0-9#+]+", folded))


class AliasTable:
    def __init__(self, data):
        self.version = data["version"]
        self._closure = {}
        self._canonical = {}
        self._display = {}
        for canonical, variants in data["groups"].items():
            self._display[canonical] = variants[0] if variants else canonical
            for name in (canonical, *variants):
                self._canonical[normalize_term(name)] = canonical
        self._longest = max(len(name.split()) for name in self._canonical)
        self._implies = {
            canonical: [self.canonical(name) for name in names]
            for canonical, names in data["implies"].items()
        }

    @classmethod
    def load(cls, path=ALIASES):
        return cls(json.loads(Path(path).read_text("utf-8")))

    def canonical(self, term) -> str:
        normalized = normalize_term(term)
        return self._canonical.get(normalized, normalized)

    def implied(self, term) -> frozenset[str]:
        """Canonical names a skill demonstrates, including itself (transitive)."""
        start = self.canonical(term)
        if start not in self._closure:
            seen, pending = set(), [start]
            while pending:
                name = pending.pop()
                if name not in seen:
                    seen.add(name)
                    pending.extend(self._implies.get(name, ()))
            self._closure[start] = frozenset(seen)
        return self._closure[start]

    def extract(self, text) -> list[str]:
        """Reviewed skills named in free text, longest match first, as display names."""
        words, found, start = normalize_term(text).split(), [], 0
        while start < len(words):
            for size in range(min(self._longest, len(words) - start), 0, -1):
                canonical = self._canonical.get(" ".join(words[start : start + size]))
                # Single letters ("c'est" -> "c") never count as a skill mention.
                if canonical and len(" ".join(words[start : start + size])) >= 2:
                    if self._display[canonical] not in found:
                        found.append(self._display[canonical])
                    start += size
                    break
            else:
                start += 1
        return found

    def covers(self, requirement, skill) -> bool:
        return self.canonical(requirement) in self.implied(skill)

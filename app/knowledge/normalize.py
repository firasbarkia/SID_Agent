"""Deterministic, offline normalization of the collected, versioned snapshot."""

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZipFile

from pydantic import ValidationError

from app.knowledge.models import KnowledgeRecord, Provenance, digest, exact_skill_name


def decode_json(data: bytes):
    try:
        text = data.decode("utf-8-sig")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = data.decode("cp1252")  # Strict: never introduce replacement characters.
        encoding = "cp1252"
    return json.loads(text), encoding


def repair_mixed_text(value):
    # Some CP1252 ROME files contain individual fields already encoded as UTF-8.
    if isinstance(value, str) and any(marker in value for marker in ("Ã", "Â", "â€")):
        try:
            return value.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return value
    if isinstance(value, list):
        return [repair_mixed_text(v) for v in value]
    if isinstance(value, dict):
        return {k: repair_mixed_text(v) for k, v in value.items()}
    return value


@dataclass
class Snapshot:
    records: list[KnowledgeRecord] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    files: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)

    def add(self, row: int, **values):
        try:
            self.records.append(KnowledgeRecord(**values))
        except (ValidationError, TypeError, ValueError) as exc:
            self.rejected.append(
                {"source": values.get("source"), "row": row, "reason": type(exc).__name__}
            )

    def manifest(self):
        return {
            "pipeline": "sid-reference-v1",
            "counts": dict(sorted(Counter(f"{r.source}:{r.kind}" for r in self.records).items())),
            "coverage": self.coverage,
            "files": self.files,
            "rejected": self.rejected,
            "records_sha256": digest(sorted((r.id, r.content_hash) for r in self.records)),
        }


def normalize(path: Path) -> Snapshot:
    snapshot = Snapshot()
    checks = json.loads((path / "availability_checks.json").read_text(encoding="utf-8"))
    evidence = {Path(c["local_file"]).name: c for c in checks if c.get("local_file")}

    def load(name):
        data = (path / name).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        check = evidence.get(Path(name).name)
        if check and check["sha256"] != sha:
            raise ValueError(f"Snapshot integrity mismatch: {name}")
        parsed, encoding = decode_json(data)
        snapshot.files[name] = {"sha256": sha, "encoding": encoding}
        return parsed

    def provenance(name, *, url, version, license, attribution, verified=True):
        return Provenance(
            source_url=url,
            source_version=version,
            snapshot=path.name,
            file=name,
            sha256=snapshot.files[name]["sha256"],
            license=license,
            license_url={
                "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
                "Licence Ouverte / Open Licence 2.0": "https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
                "European Commission reuse": "https://eur-lex.europa.eu/eli/dec/2011/833/oj/eng",
            }[license],
            attribution=attribution,
            edition_verified=verified,
        )

    esco = load("esco_it_seed.json")
    # Verify the original responses too; retain their checksums in the import manifest.
    for name in sorted((path / "raw").glob("esco_*.json")):
        load("raw/" + name.name)
    for i, row in enumerate(esco):
        raw = "raw/" + Path(row["raw_file"]).name
        descriptions = {
            lang: value["literal"]
            for lang, value in row.get("description", {}).items()
            if lang in {"en", "fr", "ar"} and value.get("literal")
        }
        aliases = []
        for lang, values in (row.get("alternative_labels") or {}).items():
            if lang in {"en", "fr", "ar"}:
                aliases.extend(values if isinstance(values, list) else [values])
        snapshot.add(
            i,
            source="esco",
            kind="occupation" if row["type"] == "Occupation" else "skill",
            source_id=row["source_uri"],
            labels=row["labels"],
            descriptions=descriptions,
            aliases=aliases,
            attributes={"review_status": row["review_status"]},
            provenance=provenance(
                raw,
                url=evidence[Path(raw).name]["requested_url"],
                version="API default; upstream edition unknown",
                license="European Commission reuse",
                attribution="European Commission, ESCO; translations require domain review",
                verified=False,
            ),
        )
    snapshot.coverage["esco"] = {"selected_it_sample": len(esco), "full_taxonomy": False}

    occupations = load("raw/onet_occupations.json")["row"]
    software = load("raw/onet_software_skills.json")["row"]
    onet_license = "CC-BY-4.0"
    for i, row in enumerate(occupations):
        snapshot.add(
            i,
            source="onet",
            kind="occupation",
            source_id=row["onetsoc_code"],
            labels={"en": row["title"]},
            descriptions={"en": row["description"]},
            provenance=provenance(
                "raw/onet_occupations.json",
                url=evidence["onet_occupations.json"]["requested_url"],
                version="31.0",
                license=onet_license,
                attribution="O*NET 31.0, U.S. Department of Labor / "
                "Employment and Training Administration",
            ),
        )
    groups = defaultdict(list)
    for row in software:
        groups[row["workplace_example"]].append(row)
    for i, (label, rows) in enumerate(sorted(groups.items())):
        # No upstream software concept ID exists: make the derived identity explicit,
        # preserving each SOC/element relationship in evidence rather than inventing one.
        snapshot.add(
            i,
            source="onet",
            kind="skill",
            source_id="software-label:" + digest(label),
            labels={"en": label},
            aliases=(["ReactJS", "React.js"] if exact_skill_name(label) == "react" else []),
            attributes={"identity": "derived_exact_label", "occupation_associations": rows},
            provenance=provenance(
                "raw/onet_software_skills.json",
                url=evidence["onet_software_skills.json"]["requested_url"],
                version="31.0",
                license=onet_license,
                attribution="O*NET 31.0, U.S. Department of Labor / "
                "Employment and Training Administration",
            ),
        )
    snapshot.coverage["onet"] = {
        "occupations": len(occupations),
        "software_associations": len(software),
        "distinct_software_labels": len(groups),
        "market": "US reference; not Tunisian demand",
    }

    locations = load("tunisia_locations.json")
    archive_name = "raw/geonames_tunisia.zip"
    archive_data = (path / archive_name).read_bytes()
    archive_hash = hashlib.sha256(archive_data).hexdigest()
    if evidence["geonames_tunisia.zip"]["sha256"] != archive_hash:
        raise ValueError("GeoNames archive integrity mismatch")
    snapshot.files[archive_name] = {"sha256": archive_hash, "encoding": "zip / utf-8"}
    for i, row in enumerate(locations):
        snapshot.add(
            i,
            source="geonames",
            kind="location",
            source_id=row["source_id"],
            labels={"und": row["name"]},
            aliases=row["alternate_names"],
            attributes={
                k: row[k]
                for k in (
                    "latitude",
                    "longitude",
                    "feature_class",
                    "feature_code",
                    "country_code",
                    "admin1_code",
                    "population",
                    "modified",
                )
            },
            provenance=provenance(
                archive_name,
                url="https://download.geonames.org/export/dump/TN.zip",
                version="snapshot " + path.name,
                license="CC-BY-4.0",
                attribution="GeoNames",
            ),
        )
    snapshot.coverage["geonames"] = {
        "selected_populated_or_administrative": len(locations),
        "feature_codes": dict(Counter(r["feature_code"] for r in locations)),
        "note": "Typed features; historical places and administrative areas are not cities",
    }

    archive_name = "raw/rome_json_export.zip"
    archive_hash = hashlib.sha256((path / archive_name).read_bytes()).hexdigest()
    if evidence["rome_json_export.zip"]["sha256"] != archive_hash:
        raise ValueError("ROME archive integrity mismatch")
    snapshot.files[archive_name] = {"sha256": archive_hash, "encoding": "zip / mixed"}
    with ZipFile(path / archive_name) as archive:
        files = {}
        for name in archive.namelist():
            data = archive.read(name)
            if name.endswith(".json"):
                value, encoding = decode_json(data)
                files[name] = repair_mixed_text(value)
            else:
                encoding = "utf-8"
            snapshot.files[f"{archive_name}!{name}"] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "encoding": encoding,
            }
        version_text = archive.read("version.txt").decode("utf-8")
        if "ROME 4.0 version 61 - 26M06" not in version_text:
            raise ValueError("Unexpected ROME edition; review the parser before importing")
    version = "ROME 4.0 version 61 - 26M06 (2026-06-15)"

    def rome_provenance(name):
        return provenance(
            f"{archive_name}!{name}",
            url=evidence["rome_json_export.zip"]["requested_url"],
            version=version,
            license="Licence Ouverte / Open Licence 2.0",
            attribution="France Travail, ROME 4.0 version 61",
        )

    name = "unix_fiche_emploi_metier_v461.json"
    profiles = files[name]
    for i, row in enumerate(profiles):
        snapshot.add(
            i,
            source="rome",
            kind="occupation",
            source_id=row["rome"]["code_rome"],
            labels={"fr": row["rome"]["intitule"]},
            descriptions={"fr": row.get("definition", "")},
            aliases=[a["libelle"] for a in row.get("appellations", [])],
            attributes={"code_ogr": row["rome"]["code_ogr"], "profile": row},
            provenance=rome_provenance(name),
        )
    for name in ("unix_referentiel_competence_v461.json", "unix_referentiel_savoir_v461.json"):
        rows = files[name]
        if isinstance(rows, dict):
            rows = rows["item_referentiel_competence"]
        category = "competence" if "competence" in name else "savoir"
        for i, row in enumerate(rows):
            snapshot.add(
                i,
                source="rome",
                kind="skill",
                source_id=f"{category}:{row['code_ogr']}",
                labels={"fr": row["libelle"]},
                descriptions={"fr": row.get("definition_comp", "")},
                attributes=row,
                provenance=rome_provenance(name),
            )
    snapshot.coverage["rome"] = {
        "profiles": len(profiles),
        "appellation_rows": len(files["unix_referentiel_appellation_v461.json"]),
        "all_archive_json_files_verified": len(files),
        "normalized": "occupation profiles with appellations/relations, competencies and knowledge",
    }
    ids = [record.id for record in snapshot.records]
    if len(set(ids)) != len(ids):
        raise ValueError("Conflicting source identities; import aborted")
    return snapshot

"""Collect a small public reference-data seed; never publish or import into a DB.

Run with Python 3.11+ and curl available on PATH. Downloads have size/time limits.
Only the explicitly listed open reference datasets are retained. Website probes
retain status metadata, not job listings, company profiles or candidate data.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlencode
import zipfile


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)
OUT = ROOT / "data" / "knowledge_base" / NOW.strftime("%Y-%m-%d")
RAW = OUT / "raw"
CURL = shutil.which("curl.exe") or shutil.which("curl")

SOURCES = [
    ("onet_occupations", "https://www.onetcenter.org/dl_files/database/db_31_0_json/occupation_data.json", "json"),
    ("onet_software_skills", "https://www.onetcenter.org/dl_files/database/db_31_0_json/software_skills.json", "json"),
    ("geonames_tunisia", "https://download.geonames.org/export/dump/TN.zip", "zip"),
    ("geonames_readme", "https://download.geonames.org/export/dump/readme.txt", "txt"),
    ("rome_catalog", "https://www.data.gouv.fr/api/1/datasets/58da857388ee384902e505f5/", "json"),
    ("rome_json_export", "https://api.francetravail.fr/api-nomenclatureemploi/v1/open-data/json", "zip"),
    ("rome_xlsx_probe", "https://www.francetravail.org/files/live/sites/peorg/files/documents/Statistiques-et-analyses/Open-data/ROME/rome-arborescence-principale-juin-2026.xlsx", None),
    ("startup_tunisia_directory", "https://startup.gov.tn/fr/database", None),
    ("startup_tunisia_jobs", "https://startup.gov.tn/en/startup_jobs", None),
    ("keejob", "https://www.keejob.com/", None),
    ("tanitjobs", "https://www.tanitjobs.com/", None),
    ("aneti_employment", "https://www.emploi.nat.tn/", None),
    ("aneti_actual_page", "https://www.emploi.nat.tn/fo/Fr/global.php", None),
    ("remotive_api", "https://remotive.com/api/remote-jobs?limit=1", None),
]
for term in ("Python", "JavaScript", "React", "SQL", "web developer"):
    query = urlencode({"text": term, "language": "en", "type": "occupation" if term == "web developer" else "skill", "limit": 5, "full": "true"})
    SOURCES.append(("esco_" + term.lower().replace(" ", "_"), "https://ec.europa.eu/esco/api/search?" + query, "json"))


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch(item):
    name, url, extension = item
    temporary = RAW / (name + ".download")
    check = {"id": name, "method": "GET", "requested_url": url, "checked_at_utc": datetime.now(timezone.utc).isoformat(), "retained": False}
    try:
        seconds = 120 if name in {"onet_software_skills", "rome_json_export"} else 40
        result = subprocess.run(
            [CURL, "--location", "--compressed", "--max-time", str(seconds), "--connect-timeout", "10", "--max-filesize", "25000000", "--silent", "--show-error", "--output", str(temporary), "--write-out", "%{json}", url],
            capture_output=True, text=True, timeout=seconds + 5, check=False,
        )
        info = json.loads(result.stdout) if result.stdout.strip() else {}
        check.update(http_status=info.get("http_code"), final_url=info.get("url_effective"), content_type=info.get("content_type"), curl_exit_code=result.returncode)
        if result.stderr:
            check["error"] = result.stderr.strip()[:800]
        if result.returncode != 0 or check["http_status"] != 200:
            return check
        payload = temporary.read_bytes()
        check.update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        if extension == "json":
            value = json.loads(payload)
            if name.startswith("esco_"):
                check["total_matches"] = value.get("total")
                check["sample_count"] = len(value.get("_embedded", {}).get("results", []))
            elif name == "rome_catalog":
                check["dataset_license"] = value.get("license")
                check["dataset_modified"] = value.get("last_modified")
            else:
                check["json_structure"] = list(value)[:8] if isinstance(value, dict) else "list"
        elif extension == "zip":
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                if name == "geonames_tunisia":
                    assert "TN.txt" in archive.namelist(), "Missing Tunisia data"
                elif name == "rome_json_export":
                    files = [f for f in archive.infolist() if f.filename.endswith(".json")]
                    assert files, "Missing JSON datasets in ROME archive"
                    assert sum(f.file_size for f in files) < 150_000_000, "Archive exceeds inspection limit"
                    check["json_files"] = []
                    for file in files:
                        content = archive.read(file)
                        encoding = "utf-8-sig"
                        try:
                            decoded = content.decode(encoding)
                        except UnicodeDecodeError:
                            encoding = "cp1252"
                            decoded = content.decode(encoding)
                        document = json.loads(decoded)
                        check["json_files"].append({"name": file.filename, "bytes": file.file_size, "decoding_used": encoding, "encoding_detection": "fallback heuristic; raw bytes preserved", "top_level_type": type(document).__name__, "top_level_count": len(document) if isinstance(document, (list, dict)) else None})
        elif name == "remotive_api":
            value = json.loads(payload)
            check["sample_count"] = len(value.get("jobs", []))
        if extension:
            destination = RAW / (name + "." + extension)
            temporary.replace(destination)
            check.update(retained=True, local_file=str(destination.relative_to(ROOT)).replace("\\", "/"))
    except Exception as exc:
        check["error"] = type(exc).__name__ + ": " + str(exc)[:800]
    finally:
        temporary.unlink(missing_ok=True)
    return check


def normalize(checks):
    good = {c["id"]: c for c in checks if c["retained"]}
    summary = {}
    esco = {}
    for name, check in good.items():
        if name.startswith("esco_"):
            for concept in json.loads((ROOT / check["local_file"]).read_text(encoding="utf-8"))["_embedded"]["results"]:
                uri = concept["uri"]
                labels = concept.get("preferredLabel", {})
                esco[uri] = {
                    "source": "ESCO / European Commission", "source_uri": uri,
                    "source_version": "API default; version not pinned", "retrieved_at_utc": check["checked_at_utc"],
                    "type": concept.get("className"),
                    "labels": {language: labels[language] for language in ("en", "fr", "ar") if language in labels},
                    "description": concept.get("description"),
                    "alternative_labels": concept.get("alternativeLabel", concept.get("altLabel")),
                    "raw_file": check["local_file"], "review_status": "unreviewed_search_sample",
                }
    if esco:
        save_json(OUT / "esco_seed.json", list(esco.values()))
        summary["esco_sample_concepts"] = len(esco)
        topic_labels = {"JavaScript", "JavaScript Framework", "AJAX", "web programming", "Python (computer programming)", "use scripting programming", "SQL", "SQL Server", "database management systems", "SQL Server Integration Services", "NoSQL", "web developer", "user interface developer", "ICT system developer", "software developer"}
        screened = [{**record, "review_status": "topic_screened; bilingual_and_domain_review_pending"} for record in esco.values() if record["labels"].get("en") in topic_labels]
        save_json(OUT / "esco_it_seed.json", screened)
        summary["esco_it_topic_screened_concepts"] = len(screened)
    if "geonames_tunisia" in good:
        with zipfile.ZipFile(ROOT / good["geonames_tunisia"]["local_file"]) as archive:
            lines = archive.read("TN.txt").decode("utf-8").splitlines()
        places = []
        for line in lines:
            fields = line.split("\t")
            if len(fields) != 19 or fields[8] != "TN" or fields[6] not in ("P", "A"):
                continue
            places.append({
                "source": "GeoNames", "source_id": fields[0], "name": fields[1], "ascii_name": fields[2],
                "alternate_names": fields[3].split(",") if fields[3] else [],
                "latitude": float(fields[4]), "longitude": float(fields[5]),
                "feature_class": fields[6], "feature_code": fields[7], "country_code": fields[8],
                "admin1_code": fields[10], "population": int(fields[14]), "modified": fields[18],
                "license": "CC-BY-4.0", "retrieved_at_utc": good["geonames_tunisia"]["checked_at_utc"],
            })
        save_json(OUT / "tunisia_locations.json", places)
        summary.update(geonames_total_features=len(lines), tunisia_populated_places_and_administrative_areas=len(places))
    for name in ("onet_occupations", "onet_software_skills"):
        if name in good:
            value = json.loads((ROOT / good[name]["local_file"]).read_text(encoding="utf-8"))
            if isinstance(value, list):
                summary[name + "_rows"] = len(value)
            elif isinstance(value, dict):
                summary[name + "_rows"] = len(value.get("row", []))
    if "rome_json_export" in good:
        members = good["rome_json_export"].get("json_files", [])
        summary["rome_json_files"] = len(members)
        for member in members:
            if "fiche_emploi_metier" in member["name"]:
                summary["rome_occupation_profiles"] = member["top_level_count"]
            elif "referentiel_appellation" in member["name"]:
                summary["rome_occupation_titles"] = member["top_level_count"]
    return summary


def main():
    if not CURL:
        raise SystemExit("curl is required")
    RAW.mkdir(parents=True, exist_ok=True)
    checks = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch, source) for source in SOURCES]
        for future in as_completed(futures):
            check = future.result()
            checks.append(check)
            print(json.dumps({k: check[k] for k in ("id", "http_status", "retained", "error", "bytes") if k in check}), flush=True)
    checks.sort(key=lambda c: c["id"])
    save_json(OUT / "availability_checks.json", checks)
    summary = normalize(checks)
    save_json(OUT / "collection_summary.json", {"collected_at_utc": NOW.isoformat(), "counts": summary, "note": "Reference seed only. Not imported into MongoDB or Qdrant; no company, job or candidate corpus collected."})
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()

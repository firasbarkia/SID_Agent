# Initial knowledge-base sources

Checked on **29 September 2026** for the SID Agent recruitment MVP, with Tunisia and French/English as the initial focus.

Availability here means an actual HTTP/API/file check from this workspace. A successful webpage response does not establish that its full dataset is downloadable or reusable. The checks are a snapshot, not an uptime guarantee. Detailed timestamps, response codes, URLs and hashes are in `data/knowledge_base/2026-09-29/availability_checks.json`.

## Source catalog

| Priority | Source | Useful data and MVP use | Access verified | Reuse and decision |
|---|---|---|---|---|
| P0 | [ESCO — European Commission](https://esco.ec.europa.eu/en/use-esco/use-esco-services-api) | Multilingual skills, occupations, synonyms and relationships. Primary vocabulary for profile normalization and matching. | Public JSON API returned 200 without credentials. Five searches were saved with English, French and Arabic labels. Full classification export is also offered; its download workflow requests an email address and acceptance of a privacy statement. | Classification reuse is permitted with acknowledgment and identification of modifications. Start with the API seed; pin a release and retrieve occupation-skill relations for a full import. API software licensing is separate from the data terms. |
| P0 | [GeoNames](https://www.geonames.org/export/) | Tunisian place names, alternate spellings, coordinates and administrative codes. Useful for location filtering and disambiguation. | `TN.zip` downloaded and parsed successfully; 25,850 geographic features. Extracted 4,631 populated-place and administrative-area records. | CC BY 4.0 according to the downloaded README. Include attribution. These are geographic records, not 4,631 cities; historical and administrative feature codes need filtering for product use. |
| P1 | [O*NET® 31.0 Database](https://www.onetcenter.org/database.html) | Occupation definitions and software-skill links. Supplemental English vocabulary and technical examples. | Both JSON files downloaded and parsed: 1,016 occupation records and 31,821 occupation-software associations. The larger download initially timed out, then completed with a longer bound. | Downloadable database is CC BY 4.0. Attribute the version and USDOL/ETA. US occupational context must not be treated as evidence of Tunisian demand or candidate suitability. |
| P1 | [ROME — France Travail](https://www.data.gouv.fr/datasets/repertoire-operationnel-des-metiers-et-des-emplois-rome) | French occupation titles and associated competencies. Useful for French profile parsing and career guidance. | Downloaded the 5,492,023-byte ZIP and parsed all 12 JSON members, including 1,911 occupation profiles and 14,301 occupation titles. The endpoint returns 404 to HEAD but 200 to GET. Alternative June 2026 hierarchy XLSX also returned 200; not retained or parsed as a workbook. | Catalog declares Licence Ouverte; ROME-specific license is linked below. Use GET-based validation rather than declaring this source unavailable from its HEAD response. |
| P1 | [Startup Tunisia directory](https://startup.gov.tn/fr/database) and [Startup Jobs](https://startup.gov.tn/en/startup_jobs) | Tunisian startup context and jobs; relevant to startup/PFE queries and company-specific letters. | Both public pages returned 200. A reusable structured export, API and dataset freshness were not verified. | Public viewing does not establish bulk reuse permission. Candidate integration source; obtain a permitted feed or company-supplied information. No company/job corpus ingested. |
| P1 | [Keejob](https://www.keejob.com/) | Tunisian vacancies and employer pages. Potential local opportunity coverage. | Public page returned 200 and contains listings. No public bulk feed/API verified in this pass. | Reuse rights unresolved. Treat as a partnership/feed candidate, not an approved scrape source. |
| P1 | [ANETI employment portal](https://www.emploi.nat.tn/fo/Fr/global.php) | Tunisian employment information and vacancy discovery. | Root page is a redirect shell; actual French page returned 200 using the Windows curl client. Python's initial TLS verification failed, so client compatibility requires attention. No structured feed verified. | Bulk reuse terms and feed access unresolved. Page reachability does not prove vacancy completeness or freshness. |
| P2 | [Tanitjobs](https://www.tanitjobs.com/) | Tunisian vacancies and internships. Potential local coverage. | Visible through the web research service, but direct automated requests returned 403. | Blocked for this direct ingestion method. No bypass attempted; use an authorized feed or partner integration. |
| P2 | [Remotive public API](https://remotive.com/remote-jobs/api) | Remote jobs for additional international coverage and retrieval experiments. | JSON endpoint returned 200 and 16 jobs despite the requested `limit=1`; no job payload retained. | API-specific conditions require source credit and links, impose distribution restrictions, and describe a 24-hour delay. Check compatibility with the product before importing. Remote does not automatically mean eligible from Tunisia. |

## Initial files

The dated data folder contains raw open reference responses, normalized ESCO/location JSON, an availability log and a collection summary. Phase 3 now provides a repeatable normalization/import/indexing pipeline and a 51,921-record normalization manifest; see [Phase 3 setup](phase-3.md). The full snapshot is imported into local development MongoDB, without a claim of integration into the main platform.

- `raw/onet_occupations.json`: original occupation dataset, release 31.0.
- `raw/onet_software_skills.json`: 31,821 source occupation-software associations, release 31.0; these are not 31,821 distinct technologies.
- `raw/geonames_tunisia.zip` and `raw/geonames_readme.txt`: country dump and its format/license documentation.
- `tunisia_locations.json`: populated places and administrative areas selected from that dump; names and source identifiers retained.
- `raw/esco_*.json`: original API responses, including descriptions and relationship links returned by the source.
- `esco_seed.json`: all 22 unique search results, explicitly unreviewed.
- `esco_it_seed.json`: 15 concepts screened for IT relevance, with English/French/Arabic labels; translation and domain review still pending.
- `raw/rome_catalog.json`: dataset metadata and published resource URLs, not the ROME taxonomy itself.
- `raw/rome_json_export.zip`: completed ROME archive; all 12 JSON members parsed successfully using strict UTF-8/CP1252 decoding. The original archive is unchanged. Phase 3 normalizes occupation profiles, competencies and knowledge, with source IDs and mixed-field repair; `version.txt` confirms ROME 4.0 version 61 / 26M06.
- `availability_checks_first_pass.json`, `availability_checks.json` and `rome_alternative_check.json`: evidence, including unsuccessful requests.
- `collection_summary.json`: actual retained record counts. Only completed, parsed downloads count as collected data.

The collector is `scripts/collect_initial_kb.py`. Run it from the project with:

```powershell
.\.venv\Scripts\python.exe scripts/collect_initial_kb.py
```

It creates a folder for the current UTC date, limits request time and size, and retains only the explicitly listed open reference datasets. Re-running on the same date replaces that day's collector outputs; use a separate archival copy if preserving every run is required.

## Quality findings

1. ESCO keyword search is not an alias dictionary. The five returned `React` hits describe reacting to situations; none establishes the React JavaScript library. The `web developer` search also returned a property-development occupation. These unrelated results remain in the raw evidence but are excluded from the IT subset. Do not infer that React is absent from the entire classification based on this limited search.
2. The ESCO API's default version was not pinned. The portal advertises release 1.2.1, but that alone does not prove which release each API response used. Preserve hashes and resolve a fixed release before production import.
3. ESCO labels in multiple languages are source data, not translations we have validated. Taxonomy relationships, software aliases and directional relationships such as React → frontend need explicit curation.
4. Geographic feature codes distinguish cities, villages, administrative units and historical features. Keep these distinctions and add a reviewed mapping to Tunisian governorate names before using them as location filters.
5. O*NET and ROME describe occupations in their respective national contexts. They are reference vocabulary, not evidence that a particular person has a skill, nor a substitute for Tunisian job offers.
6. ROME's bulk JSON URL returns a ZIP rather than a single JSON response, and some members fail UTF-8 decoding. The collector records any CP1252 fallback and preserves the original archive; encoding and accented labels need checking before production normalization. A failed HEAD check alone is not evidence of a missing GET resource.
7. None of these sources provides a validated candidate–offer relevance benchmark. That requires consented/anonymized or synthetic profiles, real permitted offers, and human relevance labels.

## How this feeds the architecture

- **MongoDB reference collections:** skills, occupations, aliases, locations, source/version metadata and licensing notes. Preserve each source's IDs instead of merging concepts solely by their names.
- **Qdrant:** embed selected occupation/skill descriptions and, later, permitted job/company content. Keep source IDs, language and version in every search record.
- **Candidate profiles:** originate from the platform and user-submitted CVs under the platform's visibility rules. No public resume scraping was performed.
- **Live jobs and company facts:** start with platform-owned offers and employer-submitted company profiles. Add external feeds only after access and reuse terms are established; retain expiry dates and verification times.
- **Refresh:** use release-based updates for taxonomies, periodic GeoNames snapshots, and source-specific freshness rules for live listings. A date in this folder is a retrieval date, not necessarily a data publication date.

Recommended next ingestion order: ESCO skills/occupations/relations with a pinned release → reviewed Tunisian location mapping → O*NET software aliases → normalization of the collected ROME archive → platform-owned offers and consented CVs. The current seed establishes access and schemas; it is not a complete production knowledge base.

## Data credits and terms

This service uses the [ESCO classification](https://esco.ec.europa.eu/) of the European Commission. The sample selection and normalized JSON are adaptations for SID Agent; original responses are preserved. See [ESCO reuse conditions](https://esco.ec.europa.eu/fr/about-esco/faq).

O*NET® data source: **O*NET® 31.0 Database**, U.S. Department of Labor, Employment and Training Administration. Downloaded files are preserved unchanged; any future derived records must identify their modifications. Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); see the [database license](https://www.onetcenter.org/license_db.html).

Geographic source: [GeoNames](https://www.geonames.org/), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). SID Agent filtered the Tunisia dump and converted selected fields into JSON; the source ZIP is unchanged.

Other source terms: [ROME open license](https://www.francetravail.org/files/live/sites/peorg/files/documents/Statistiques-et-analyses/Open-data/ROME/rome_licence_ouverte.pdf), [Remotive API conditions](https://remotive.com/remote-jobs/api). No open-data license was verified for the Tunisian job boards or Startup Tunisia pages in this pass.

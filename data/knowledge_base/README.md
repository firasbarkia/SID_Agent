# Knowledge-base collection

Source descriptions, usage decisions and data credits: [source report](../../docs/knowledge-base-sources.md).

The `2026-09-29` directory contains the first reference-data snapshot. See its
`collection_summary.json` for completed downloads and `availability_checks.json`
for HTTP evidence, source URLs, UTC timestamps and checksums. The `raw` directory
preserves source bytes. Normalized files are derived selections, not complete
copies of their respective taxonomies.

Phase 3 normalizes 51,921 public reference records, with a committed
`2026-09-29/normalized_manifest.json` and a repeatable MongoDB/Qdrant ingestion
pipeline. The full snapshot has been imported into the local `sid_phase3_dev`
MongoDB database and indexed into 55,035 vectors in local Qdrant. See
[setup and coverage](../../docs/phase-3.md) to reproduce the active index.
No main-platform database import is claimed. No live-job,
company-profile or candidate-CV corpus was collected. License/source metadata
travels with canonical records and search results.

The collector is `scripts/collect_initial_kb.py` in the project root. It requires
Python 3.11+ and curl, and writes a snapshot under the current UTC date. The source
URLs include fixed O*NET and ROME resources and should be reviewed for new releases.

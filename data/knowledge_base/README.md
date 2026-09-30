# Knowledge-base collection

Source descriptions, usage decisions and data credits: [source report](../../docs/knowledge-base-sources.md).

The `2026-09-29` directory contains the first reference-data snapshot. See its
`collection_summary.json` for completed downloads and `availability_checks.json`
for HTTP evidence, source URLs, UTC timestamps and checksums. The `raw` directory
preserves source bytes. Normalized files are derived selections, not complete
copies of their respective taxonomies.

No records have been imported into MongoDB or Qdrant. No live-job, company-profile
or candidate-CV corpus was collected. Source terms and attribution must travel
with subsequent imports; public website access alone does not grant bulk reuse.

The collector is `scripts/collect_initial_kb.py` in the project root. It requires
Python 3.11+ and curl, and writes a snapshot under the current UTC date. The source
URLs include fixed O*NET and ROME resources and should be reviewed for new releases.

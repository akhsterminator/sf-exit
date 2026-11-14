# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-14

First public release.

### Added
- `sf-exit export`: every queryable object to CSV, via Bulk API 2.0 (large objects) or REST (small objects), with automatic fallback between them.
- Downloads of ContentVersion, Attachment and Document bodies, verified against Salesforce's MD5 checksums, plus `files/index.csv`.
- `ContentDocumentLink` export (file ↔ record links), which Salesforce won't return without a filter.
- Resumable exports (`.sf-exit/state.json`), `--include-deleted`, include/exclude globs.
- Schema output: per-object describe JSON, DDL for Postgres/DuckDB/SQLite, a Mermaid ER diagram, `manifest.json` and `REPORT.md`.
- `sf-exit load` into SQLite, DuckDB, Postgres and Parquet, with typed columns.
- `sf-exit verify` (offline row counts and SHA-256, and `--live` comparison against the org).
- `sf-exit plan` (dry run with API-limit estimate) and `sf-exit doctor` (permission check).
- Login via Salesforce CLI, access token, OAuth client credentials, JWT bearer or username-password.
- GitHub Action and Docker image.
- Composite batch requests: describes and small-object queries are fetched 25 per API call.
- Knowledge articles (`KnowledgeArticleVersion`, `*__kav`) are exported per `PublishStatus`.
- Objects that Salesforce refuses to export unfiltered are reported as skipped (with the reason) instead of failing the run. Permission and limit errors still count as failures.
- Resuming with different options (for example `--include-deleted`) starts a clean export instead of mixing results.
- Bulk jobs that don't finish within 6 hours are aborted and reported, instead of being polled forever.

[0.1.0]: https://github.com/akhsterminator/sf-exit/releases/tag/v0.1.0

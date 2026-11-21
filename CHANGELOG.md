# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-21

The commands below were documented in the README but missing from the 0.1.0 package on PyPI. This release ships them.

### Added
- `sf-exit audit`: offline data dictionary and org health report (HTML, CSV, Markdown, JSON), covering unused fields, never-ticked checkboxes, unused picklist values, duplicate contacts and accounts, overdue opportunities, records owned by inactive users, stale and empty objects.
- `--metadata` and `sf-exit metadata`: Apex, Flows, objects, layouts, reports, dashboards, permission sets and more via the Metadata API, as a deployable folder with `package.xml`.
- `--incremental`: fetch only records changed or deleted since the previous export (`SystemModstamp` + `queryAll`), merge them in, and keep each run's changes in `changes/<timestamp>/`.
- `sf-exit diff`: records added, removed and changed between two exports, with per-field detail.
- `sf-exit sql`: query an export with DuckDB immediately, either as a one-off or interactively.
- `--redact` and `--redact-pii`: consistent salted hashing of chosen or PII fields.
- `--audit` and `--load` run the audit or a load straight after an export.
- `sf-exit browse`: an offline, read-only web app over an export, with list views, record pages with lookups resolved, related lists, attached files and full-text search. Standard library only.
- `sf-exit mcp`: a Model Context Protocol server with read-only tools, so AI assistants can query an export.
- `sf-exit restore` (experimental): re-create records in a sandbox or another org in dependency order, with lookups remapped, a second pass for self-references, user and record-type matching, resume, and dry-run and production safeguards.
- Audit: a 0–100 health score with a grade, findings for open leads that duplicate contacts and for custom fields that always hold one value, and an inventory of what's been built (Apex, Flows, reports…) when metadata is present.
- Audit and diff use every CPU core on large exports.
- File paths in `manifest.json` and `files/index.csv` always use forward slashes, so exports are portable between Windows, macOS and Linux.
- Recipes: migrate to another CRM, free nightly backups, archive before shutdown, Postgres/DuckDB warehouse.
- Thanks to [hephz-pekun](https://github.com/hephz-pekun), whose pull request #17 proposed the lead/contact overlap finding.

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

[0.2.0]: https://github.com/akhsterminator/sf-exit/releases/tag/v0.2.0
[0.1.0]: https://github.com/akhsterminator/sf-exit/releases/tag/v0.1.0

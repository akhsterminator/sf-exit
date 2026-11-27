# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-27

This release ships the commands documented in the README but missing from the 0.1.0 package on PyPI. It also adds a time machine for your org, a dashboard, file restores and standalone binaries, and fixes a long list of edge cases found by checking every API call against Salesforce's documented behaviour.

### Highlights
- **Time machine.** Every run into an existing export folder records what changed in `changes/<run>/`, together with the version each record had before. `sf-exit history` shows one record's timeline with old → new values for every field. `sf-exit snapshot --as-of` rebuilds the whole export as it was at any moment. `sf-exit browse` shows a History section on every record and a Changes page, and AI assistants get a `get_record_history` MCP tool.
- **Dashboard** in `sf-exit browse`: records created per month, largest objects, open pipeline by stage, files, and backup history. It's drawn locally, with no JavaScript libraries.
- **Restore, version 2:**
  - `--files` uploads Salesforce Files and Attachments and links them to the restored records.
  - `--upsert OBJECT.FIELD` matches on an External ID, so re-runs update instead of duplicating.
  - Restoring into the source org re-creates only the records that no longer exist there.
  - A write whose outcome is unknown after a network failure is recorded in `unknown.csv` and never blindly repeated.
- **Standalone binaries** for Linux (x86_64, arm64), macOS (Apple silicon) and Windows, attached to each release with `SHA256SUMS`, plus an `install.sh` that verifies them.

### Added
- `sf-exit audit`: an offline data dictionary and org health report (HTML, CSV, Markdown, JSON). It covers:
  - unused fields and never-ticked checkboxes;
  - unused picklist values;
  - duplicate contacts and accounts;
  - overdue opportunities;
  - records owned by inactive users;
  - stale and empty objects.
- Audit extras:
  - a 0–100 health score with a grade;
  - findings for open leads that duplicate contacts, and for custom fields that always hold one value;
  - an inventory of what's been built (Apex, Flows, reports…) when metadata is present.
- `--metadata` and `sf-exit metadata` retrieve Apex, Flows, objects, layouts, reports, dashboards, permission sets and more via the Metadata API, as a deployable folder with `package.xml`:
  - Profiles and permission sets come with their complete field, object and class permissions.
  - Big types are split across retrieves.
  - Classic email-template folders and standard value sets are included.
  - A failed retrieve keeps the previous copy.
- `--incremental` fetches only records changed or deleted since each object was last exported (`SystemModstamp` + `queryAll`) and merges them in, streaming. Records fetched again by the one-hour overlap aren't counted as changes.
- `sf-exit diff`: records added, removed and changed between two exports, with per-field detail. Numbers are compared by value.
- `sf-exit sql`: query an export with DuckDB immediately, either as a one-off or interactively.
- `--redact` and `--redact-pii`: consistent salted hashing of chosen fields or of personal data. This covers emails, phones, person names, usernames, addresses, birthdates, and fields named like SSN, passport or IBAN.
- `--audit` and `--load` run the audit or a load straight after an export.
- `sf-exit browse`: an offline, read-only web app over an export. It has list views, record pages with lookups resolved, related lists, attached files and full-text search, using the standard library only. `--host` lets it run inside a container.
- `sf-exit mcp`: a Model Context Protocol server with read-only tools, so AI assistants can query an export.
- `sf-exit restore` (experimental) re-creates records in a sandbox or another org:
  - in dependency order, with lookups remapped and a second pass for self-references;
  - matching users and record types;
  - resumable, with dry-run and production safeguards.
- `doctor` checks **View Setup and Configuration**. Without it, `plan` and the export count records from each query's first page instead.
- `SF_AUDIENCE_URL` (or `SFDX_AUDIENCE_URL`) for JWT logins with a custom audience.
- Audit and diff use every CPU core on large exports.
- Recipes: migrate to another CRM, free nightly backups, archive before shutdown, Postgres/DuckDB warehouse.
- Thanks to [hephz-pekun](https://github.com/hephz-pekun), whose pull request #17 proposed the lead/contact overlap finding.

### Changed
- Small objects are queried 5 per call through the Composite API, which Salesforce bills as a single API call. 0.1.0 used `/composite/batch`, which counts every subrequest against the daily limit. Schemas are still described 25 per call.
- A completed export is never "resumed". Every run exports afresh, or incrementally with `--incremental`. `--fresh` now only discards an interrupted run.
- `export` exits with status 1 when any object, file listing or file failed, so scripts and CI notice. `verify` fails on them too.
- Numbers are written exactly as Salesforce stores them, with no floating-point rounding.
- File paths in `manifest.json` and `files/index.csv` always use forward slashes, so exports are portable between Windows, macOS and Linux.

### Fixed
- Running an export again into a folder whose last run had completed skipped every object, so scheduled backups silently stopped refreshing.
- A connection dropped in the middle of a Bulk API results page or a file download failed the whole object. It's now retried.
- An unexpected error in one object aborted the whole export. It's now reported in `REPORT.md` and the export carries on.
- Token refreshes could race during long parallel exports.
- JWT logins to sandboxes on enhanced domains used the production audience.
- `plan` failed for users without **View Setup and Configuration**.
- Knowledge exports that only partly succeeded were reported as complete.
- Re-running an export for some objects (`-i`) dropped the others from `manifest.json` and `files/index.csv`.

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

# Contributing to sf-exit

Thanks for helping people get their data back! Every kind of contribution helps: bug reports, docs, recipes and code.

## Development setup

```bash
git clone https://github.com/akhsterminator/sf-exit && cd sf-exit
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest            # runs against an in-process fake Salesforce org, no account needed
ruff check .      # lint
```

## How the code is laid out

| File | What it does |
|---|---|
| `src/sf_exit/client.py` | Thin REST + Bulk API 2.0 client: retries, token refresh, streaming |
| `src/sf_exit/auth.py` | Login methods (Salesforce CLI, token, JWT, password, client credentials) |
| `src/sf_exit/discovery.py` | Which objects and fields get exported, and why others are skipped |
| `src/sf_exit/exporter.py` | The pipeline: records, ContentDocumentLink, files, manifest, report |
| `src/sf_exit/schema.py` | DDL for each SQL dialect and the Mermaid ERD |
| `src/sf_exit/loader.py` | `sf-exit load` targets |
| `src/sf_exit/verify.py` | Offline and live verification |
| `src/sf_exit/metadata.py` | Metadata API (SOAP) describe, list and retrieve |
| `src/sf_exit/audit.py` | The offline audit: findings, data dictionary and the HTML report template |
| `src/sf_exit/diff.py` | Comparing two exports |
| `src/sf_exit/sql.py` | DuckDB views over an export |
| `src/sf_exit/index.py` | The local SQLite index (catalog, lookup indexes, full-text search) |
| `src/sf_exit/browse.py` | The offline web app: JSON API, safe file serving and the single-page UI |
| `src/sf_exit/mcp.py` | The MCP server and its read-only tools |
| `src/sf_exit/restore.py` | Restore planning, Id remapping, the two-pass insert/upsert and file uploads |
| `src/sf_exit/history.py` | The time machine: record timelines and point-in-time snapshots from `changes/` |
| `src/sf_exit/cli.py` | Click commands and Rich output |
| `tests/fake_org.py` | A fake org that speaks enough of the Salesforce API (REST, Bulk 2.0, composite, collections, multipart uploads, Metadata SOAP) to test everything, with switches to inject failures (dropped connections, broken streams, missing permissions) |
| `scripts/build_binary.py` | Builds the standalone binary with PyInstaller (`pip install pyinstaller` first) |
| `scripts/record_demo.py` | Regenerates the README's demo SVGs and screenshots |

## Ideas that make great first contributions

- A new audit finding in `audit.py`, for example "Leads that match an existing Contact by email" or "fields with a single distinct value".
- A recipe in `docs/recipes/` for a CRM or warehouse you know well.
- An object that fails in your org, added to `NOT_EXPORTABLE` or handled properly.

## Found an object that fails to export?

That's the most valuable bug report you can file. Please include:

- the object name and the error from `REPORT.md`
- your org edition, and whether it's a sandbox

If the object can never be exported without a filter, add it to `NOT_EXPORTABLE` in `discovery.py` with a short reason, and add a test.

## Pull requests

- Keep PRs focused, and add a test for every bug fix (extend `tests/fake_org.py` if Salesforce behaves in a way it doesn't model yet).
- Run `pytest` and `ruff check .` before pushing.
- Add a line to `CHANGELOG.md` under "Unreleased".
- Never commit real exports or credentials. `.gitignore` covers the defaults.

## Releasing (maintainers)

1. Bump `__version__` in `src/sf_exit/__init__.py`, update `CHANGELOG.md` and merge to `main`.
2. Push a tag `vX.Y.Z` (`git tag vX.Y.Z && git push origin vX.Y.Z`) or publish a GitHub release with that tag.
3. The release workflow then publishes to PyPI and GHCR, creates the release if needed, attaches the wheel,
   the sdist, binaries for Linux (x86_64, arm64), macOS (arm64) and Windows, and `SHA256SUMS`, and moves the
   `v0` tag so GitHub Action users get the update.

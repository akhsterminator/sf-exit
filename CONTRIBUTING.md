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
| `src/sf_exit/cli.py` | Click commands and Rich output |
| `tests/fake_org.py` | A fake org that speaks enough of the Salesforce API to test everything |

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

1. Bump `__version__` in `src/sf_exit/__init__.py` and update `CHANGELOG.md`.
2. Tag `vX.Y.Z` and push the tag. CI publishes to PyPI and GHCR and creates the GitHub release.
3. Move the `v0` tag so GitHub Action users get the update: `git tag -f v0 && git push -f origin v0`.

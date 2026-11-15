# Command reference

Every command has `--help` with all options. Commands that talk to Salesforce accept the [login options](authentication.md). The others work offline on an export folder.

| Command | Talks to Salesforce | What it does |
|---|:---:|---|
| [`export`](#export) | ✅ | Records, files and (optionally) metadata into a folder |
| [`metadata`](#metadata) | ✅ | Configuration only: Apex, Flows, objects, layouts, reports… |
| [`plan`](#plan) | ✅ | Dry run: sizes, row counts, API calls needed |
| [`doctor`](#doctor) | ✅ | Checks your permissions and limits |
| [`restore`](#restore) | ✅ writes | Re-create records in a sandbox or another org, relationships intact |
| [`browse`](#browse) | – | Click through the exported org in your browser |
| [`mcp`](#mcp) | – | MCP server so AI assistants can query the export |
| [`audit`](#audit) | – | Health score, findings and a searchable data dictionary |
| [`sql`](#sql) | – | Query an export with SQL immediately |
| [`diff`](#diff) | – | What changed between two exports |
| [`load`](#load) | – | Load into SQLite, DuckDB, Postgres or Parquet |
| [`verify`](#verify) | optional | Re-count rows and re-hash files; `--live` compares with the org |

## export

```bash
sf-exit export --org prod -d ./prod-export
```

| Option | What it does |
|---|---|
| `-d, --out DIR` | Export folder (default `./sf-export`) |
| `-i, --include PATTERN` | Only these objects. Glob, case-insensitive, repeatable: `-i Account -i 'Invoice*__c'` |
| `-x, --exclude PATTERN` | Skip these objects: `-x '*History' -x '*Share'` |
| `--include-deleted` | Add Recycle Bin and archived records (`queryAll`) |
| `--incremental` | Only fetch records changed or deleted since the last export into this folder, then merge them in. Changed rows are also kept in `changes/<timestamp>/` |
| `--metadata` | Also retrieve configuration into `metadata/` (see [metadata](#metadata)) |
| `--metadata-type TYPE` | Limit metadata to these types (implies `--metadata`) |
| `--redact OBJECT.FIELD` | Hash these fields (glob): `--redact 'Contact.Email' --redact '*.Phone'` |
| `--redact-pii` | Hash every email and phone field plus names like Birthdate, SSN, Street, PostalCode, IBAN |
| `--no-files` / `--files-only` | Skip binary files, or download only files |
| `--all-file-versions` | Every ContentVersion, not just the latest |
| `--engine auto\|bulk\|rest` | Force an API. `auto` uses Bulk API 2.0 for objects with 10k+ rows |
| `--workers N` / `--file-workers N` | Parallelism (default 4 objects, 8 file downloads) |
| `--fresh` | Ignore resume state and start over |
| `--audit` | Run `sf-exit audit` when the export finishes |
| `--load TARGET` | Run `sf-exit load` when the export finishes |

**Resuming.** Interrupt with Ctrl-C at any time. Running the same command again skips finished objects and files that were already downloaded.

**Redaction.** Hashes are `redacted:<16 hex chars>` of a salted SHA-256 of the lower-cased value. The same value gets the same hash throughout an export, so joins and duplicate checks still work. Set `SF_EXIT_REDACT_SALT` to keep hashes stable across runs. Ids and lookups are never redacted, and files are not redacted.

**Incremental mode.** This uses `SystemModstamp`, with one hour of overlap that's removed by Id when merging, and `queryAll`, so records sent to the Recycle Bin are removed from the CSVs. Objects whose fields changed, or that lack `SystemModstamp`, are re-exported in full. Records hard-deleted (purged from the Recycle Bin) between runs can't be seen by any API, so run a full export now and then, for example weekly.

## metadata

```bash
sf-exit metadata --org prod -d ./prod-export
sf-exit metadata --org prod --type ApexClass --type ApexTrigger --type 'Flow*'
```

This retrieves configuration with the Metadata API into `<out>/metadata/`: Apex classes and triggers, Flows, custom objects and fields, layouts, Lightning pages, validation rules, reports and dashboards (including those in folders), email templates, permission sets, profiles and more. The result is a standard Metadata API folder with a `package.xml`, so you can:

```bash
sf project deploy start --metadata-dir ./prod-export/metadata --target-org sandbox   # redeploy
sf project convert mdapi --root-dir ./prod-export/metadata --output-dir ./force-app   # to source format
```

Types that fail to retrieve are isolated and listed in `metadata/retrieve-warnings.txt`, and the rest still succeed.

## plan

Read-only. It lists the largest objects, file counts and sizes, and estimates the API calls an export will need against your remaining daily limit.

## doctor

This checks API access, **View All Data**, **Query All Files** and your remaining daily API and Bulk limits. Without the two permissions, Salesforce silently returns only what your user can see.

## restore

```bash
sf-exit restore ./prod-export --org my-sandbox -i Account -i Contact -i Opportunity             # plan
sf-exit restore ./prod-export --org my-sandbox -i Account -i Contact -i Opportunity --execute   # write
```

| Option | What it does |
|---|---|
| `-i, --include PATTERN` | Objects to restore (required, glob, repeatable) |
| `-x, --exclude PATTERN` | Objects to leave out |
| `--execute` | Create the records. Without it, only the plan is shown |
| `--allow-production` | Permit a production org as the target |
| `--allow-same-org` | Permit the org the export came from as the target |

Parents are inserted before children, lookups are rewritten to the new Ids, and the Id map and errors are written to `restore/<org id>/`. Experimental: see [restore.md](restore.md) for exactly what it does and doesn't do.

## browse

```bash
sf-exit browse ./prod-export
```

A local, read-only web app over the export: list views, record pages, related lists, files and full-text search. Options: `--port` (default 8484, `0` for any free port), `--no-open`, `--reindex`. See [browse.md](browse.md).

## mcp

```bash
sf-exit mcp ./prod-export
```

A Model Context Protocol server over stdio with read-only tools (`list_objects`, `describe_object`, `query`, `search`, `get_record`, `audit_findings`). See [ai-assistants.md](ai-assistants.md) for client set-up.

## audit

```bash
sf-exit audit ./prod-export          # writes ./prod-export/audit/
sf-exit audit ./prod-export --open   # and opens the report
```

Offline, with no API calls, using every CPU core on large exports. It prints a health score and writes `index.html` (an interactive report), `data-dictionary.csv`, `findings.md` and `audit.json`. See [audit.md](audit.md) for what each finding means.

## sql

```bash
sf-exit sql ./prod-export "SELECT StageName, count(*), sum(Amount) FROM Opportunity GROUP BY 1"
sf-exit sql ./prod-export "SELECT * FROM Contact WHERE Email LIKE '%@acme.com'" --format csv > acme.csv
sf-exit sql ./prod-export      # interactive: end statements with ; · .tables · .quit
```

Each object is a typed view (dates are dates, numbers are numbers) over its CSV, and `_files` maps files to records. Needs `pip install 'sf-exit[duckdb]'`.

## diff

```bash
sf-exit diff ./backup-2026-11-13 ./backup-2026-11-14
sf-exit diff ./old ./new --details ./changes   # one CSV per object: Id, change, field, old, new
sf-exit diff ./old ./new --json
```

Shows records added, removed and changed per object, which fields changed most, and objects or fields that appeared or disappeared. `LastViewedDate`, `LastReferencedDate` and `SystemModstamp` are ignored because they change without anyone editing the record.

## load

```bash
sf-exit load ./prod-export --to sqlite:///prod.db
sf-exit load ./prod-export --to duckdb:///prod.duckdb
sf-exit load ./prod-export --to parquet:///./parquet
sf-exit load ./prod-export --to postgresql://me@localhost/crm --lowercase
```

| Option | What it does |
|---|---|
| `--lowercase` | Lowercase table and column names (nicer in Postgres) |
| `--replace` | Drop and recreate existing tables |
| `--only OBJECT` | Load just these objects |

Re-loading is safe: each table is emptied and reloaded.

## verify

```bash
sf-exit verify ./prod-export                # offline: row counts + SHA-256 of every CSV and file
sf-exit verify ./prod-export --live --org prod   # also compare counts with the org now
```

Exits with status 1 if anything is missing or altered, which makes it useful in CI.

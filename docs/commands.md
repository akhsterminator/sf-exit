# Command reference

Every command has `--help` with all options. Commands that talk to Salesforce accept the [login options](authentication.md). The others work offline on an export folder.

| Command | Talks to Salesforce | What it does |
|---|:---:|---|
| [`export`](#export) | ✅ | Records, files and (optionally) metadata into a folder |
| [`metadata`](#metadata) | ✅ | Configuration only: Apex, Flows, objects, layouts, reports… |
| [`plan`](#plan) | ✅ | Dry run: sizes, row counts, API calls needed |
| [`doctor`](#doctor) | ✅ | Checks your permissions and limits |
| [`restore`](#restore) | ✅ writes | Re-create records (and files) in a sandbox or another org, relationships intact |
| [`history`](#history) | – | One record's timeline across backup runs |
| [`snapshot`](#snapshot) | – | Rebuild the export as the org was at any moment |
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
| `--incremental` | Only fetch records changed or deleted since each object was last exported into this folder, then merge them in. Changed rows (and the versions they replaced) are kept in `changes/<run>/` |
| `--metadata` | Also retrieve configuration into `metadata/` (see [metadata](#metadata)) |
| `--metadata-type TYPE` | Limit metadata to these types (implies `--metadata`) |
| `--redact OBJECT.FIELD` | Hash these fields (glob): `--redact 'Contact.Email' --redact '*.Phone'` |
| `--redact-pii` | Hash personal data: emails, phones, person names, usernames, addresses, birthdates, and fields named like SSN, passport or IBAN (list below) |
| `--no-files` / `--files-only` | Skip binary files, or download only files |
| `--all-file-versions` | Every ContentVersion, not just the latest |
| `--engine auto\|bulk\|rest` | Force an API. `auto` uses Bulk API 2.0 for objects with 10k+ rows |
| `--workers N` / `--file-workers N` | Parallelism (default 4 objects, 8 file downloads) |
| `--fresh` | Discard an interrupted run's progress instead of resuming it |
| `--audit` | Run `sf-exit audit` when the export finishes |
| `--load TARGET` | Run `sf-exit load` when the export finishes |

**Resuming.** Interrupt with Ctrl-C at any time, or lose the connection, or hit a CI timeout. Running the same command again resumes the interrupted run: finished objects are skipped and downloaded files are kept. A run that *completed* is never reused, so the next run exports everything afresh (or only the changes, with `--incremental`).

**Network failures.** Reads are retried with backoff, including connections that drop in the middle of a Bulk results page or a file download. One object or file that still fails is reported in `REPORT.md` and the exit code is 1, but it never stops the rest of the export.

**History.** Every run into an existing export folder records what changed in `changes/<run>/`. See [time-machine.md](time-machine.md).

**Redaction.** Hashes are `redacted:<16 hex chars>` of a salted SHA-256 of the lower-cased value. The same value gets the same hash throughout an export, so joins and duplicate checks still work. `--redact-pii` hashes:
- email and phone fields;
- person names (`FirstName`, `LastName`, `MiddleName`, `Suffix`, and `Name` on Contact, Lead, User and Individual);
- `User.Username`, `Alias`, `CommunityNickname` and `FederationIdentifier`;
- address parts (street, city, postal code, latitude, longitude);
- birthdates;
- text fields whose names mention email, phone, mobile, fax, SSN, passport, tax ID, national ID, IBAN or bank account.

Free text such as `Description` is *not* hashed: add `--redact 'Case.Description'` for those. Ids and lookups are never redacted, and files are not redacted. Incremental runs with redaction need `SF_EXIT_REDACT_SALT`, so hashes stay consistent from one night to the next. Changing redaction settings forces a full re-export, so a file never mixes hashed and clear values.

**Incremental mode.** For each object, sf-exit asks for rows whose `SystemModstamp` is later than that object's own last export, minus an hour of overlap (duplicates are removed by Id when merging). It uses `queryAll`, so records sent to the Recycle Bin are removed from the CSVs, and archived activities are dropped as a normal export would. Objects whose fields changed, or that lack `SystemModstamp`, are re-exported in full. `ContentDocumentLink` is always refreshed in full, because sharing a file doesn't change the file. Two things only a full export refreshes:
- records hard-deleted (purged from the Recycle Bin) between runs, which no API can see;
- formula fields on rows that didn't change.

Run a plain `sf-exit export` into the same folder now and then, for example weekly.

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

This checks API access, **View All Data**, **Query All Files**, **View Setup and Configuration** and your remaining daily API and Bulk limits. Without the first two, Salesforce silently returns only what your user can see. Without the third, `plan` can't read record counts up front (the export still works and counts as it goes).

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
| `--allow-same-org` | Permit the org the export came from as the target. Only records that no longer exist there are re-created |
| `--upsert OBJECT.FIELD` | Match records on an External ID field (glob on the object, e.g. `'*.Legacy_Id__c'`), filled with the old Salesforce Id. Re-runs then update instead of duplicating |
| `--files` | Also upload Salesforce Files and Attachments and link them to the restored records |

Parents are inserted before children, lookups are rewritten to the new Ids, and the Id map and errors are written to `restore/<org id>/`. Experimental: see [restore.md](restore.md) for exactly what it does and doesn't do.

## history

```bash
sf-exit history ./backup 0015g00000ABCDEAA4          # timeline: created / updated (old → new) / deleted
sf-exit history ./backup 0015g00000ABCDEAA4 --json
```

## snapshot

```bash
sf-exit snapshot ./backup --as-of 2026-11-20 -o ./backup-nov20                 # end of that day, UTC
sf-exit snapshot ./backup --as-of 2026-11-20T09:30:00Z -o ./before --with-files
```

Writes a complete export folder as the org was at that moment, built from the history recorded in `changes/`. See [time-machine.md](time-machine.md).

## browse

```bash
sf-exit browse ./prod-export
```

A local, read-only web app over the export: a dashboard, list views, record pages with each record's history, related lists, files, a "what changed" page and full-text search. Options: `--port` (default 8484, `0` for any free port), `--no-open`, `--reindex`, `--host` (use `0.0.0.0` only inside a container). See [browse.md](browse.md).

## mcp

```bash
sf-exit mcp ./prod-export
```

A Model Context Protocol server over stdio with read-only tools (`list_objects`, `describe_object`, `query`, `search`, `get_record`, `get_record_history`, `audit_findings`). See [ai-assistants.md](ai-assistants.md) for client set-up.

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

Shows records added, removed and changed per object, which fields changed most, and objects or fields that appeared or disappeared. `LastViewedDate`, `LastReferencedDate` and `SystemModstamp` are ignored because they change without anyone editing the record. Numbers are compared by value, so `1000` and `1000.0` are the same. `LastModifiedDate` and `LastModifiedById` count as changes but are left out of the "most-changed fields" ranking, since every edit changes them.

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

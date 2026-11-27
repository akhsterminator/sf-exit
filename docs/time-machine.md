# Time machine: record history and point-in-time snapshots

Every time you export into the same folder, for example nightly with `--incremental`, sf-exit records what changed since the previous run. Over weeks that builds a complete history of your org, which you can query three ways:

```bash
sf-exit history  ./backup 0015g00000ABCDEAA4                 # one record's timeline
sf-exit snapshot ./backup --as-of 2026-11-20 -o ./backup-nov20   # the whole org as it was
sf-exit browse   ./backup                                    # History tab on every record + "Changes" page
```

Salesforce's own field history tracking is limited to 20 fields per object and 18–24 months of retention. sf-exit's history covers every field of every object for as long as you keep the folder, and it costs no extra API calls.

## What gets recorded

Each run that finds changes writes `changes/<run>/`:

| File | Contents |
|---|---|
| `<Object>.csv` | The new version of every record created, changed or deleted in this run (deleted rows have `IsDeleted = "true"`) |
| `<Object>.before.csv` | The version each of those records had before this run |
| `run.json` | When the run started and finished, and how many records changed per object |

Both incremental and full runs record history. Incremental runs ask Salesforce for rows whose `SystemModstamp` changed. Full runs compare every row with the previous CSV, which also catches edits that don't bump `SystemModstamp`. A few fields are ignored when deciding whether a record changed: formula fields (they're recalculated, not edited), `SystemModstamp`, `LastViewedDate` and `LastReferencedDate`.

## Record history

```console
$ sf-exit history ./backup 0015g00000ABCDEAA4
Account Northwind Traders (0015g00000ABCDEAA4)

● 2026-11-03 22:00  updated
  Name           Northwind Ltd  →  Northwind Traders
  AnnualRevenue  332000.0       →  410000.0

● 2026-11-12 22:00  updated
  OwnerId        0055g000001xyzAAA  →  0055g000004abcAAA
```

Add `--json` for machine-readable output. In `sf-exit browse`, the same timeline appears on every record page, and the **Changes** page lists each run with the records it touched. AI assistants get it through the `get_record_history` MCP tool.

## Snapshots

```bash
sf-exit snapshot ./backup --as-of 2026-11-20 -o ./backup-nov20
sf-exit snapshot ./backup --as-of 2026-11-20T09:30:00Z -o ./before-the-import --with-files
```

A date means the end of that day (UTC). The snapshot is a normal export folder, so everything works on it:

- `sf-exit browse ./backup-nov20`: click through the org as it was.
- `sf-exit diff ./backup-nov20 ./backup`: everything that changed since then, field by field.
- `sf-exit restore ./backup-nov20 --org sandbox -i Account -i Contact`: rebuild the org as it was in a sandbox, or bring back records deleted since then (see [restore](restore.md#bringing-back-deleted-records)).
- `sf-exit load ./backup-nov20 --to duckdb:///nov20.duckdb`: query it.

`--with-files` includes the binary files, hard-linked so they take no extra disk space where the filesystem allows it.

## Limits

- **History starts with your first run.** A snapshot can't go back before it.
- **Purged deletions** (records emptied from the Recycle Bin between runs) are invisible to incremental runs. The next full run catches them, so keep a weekly full export in your schedule (see the [backup recipe](recipes/nightly-backups.md)).
- **Granularity is one run.** If a record changed three times between two nightly runs, only the last version is kept.
- **Objects exported for the first time** have no history before that run, so a snapshot shows them as they were when they were first exported.
- **Very large objects re-exported in full** (more than 2 GB of CSV) skip the row-by-row comparison. This is noted in `REPORT.md`, and incremental runs don't have this limit.

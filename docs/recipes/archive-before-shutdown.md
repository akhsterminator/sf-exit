# Recipe: archive an org before you switch it off

You're leaving Salesforce, or closing an acquired company's org, and legal wants everything kept. This checklist gets **everything** out in a form you can still read in ten years without Salesforce.

## 1. Check permissions and limits

```bash
sf-exit doctor --org old
sf-exit plan   --org old
```

The export user needs **View All Data** and **Query All Files**. If `plan` shows the export would use most of your daily API allowance, run it over a weekend or with `--workers 2`. It can be resumed at any time.

## 2. Export everything

```bash
sf-exit export --org old -d ./archive \
  --include-deleted \
  --all-file-versions \
  --metadata \
  --audit
```

- `--include-deleted`: Recycle Bin and archived activities too
- `--all-file-versions`: every version of every file, not just the latest
- `--metadata`: Apex, Flows, objects, layouts, reports, permission sets, email templates
- `--audit`: a human-readable data dictionary for whoever opens this archive next

## 3. Prove it's complete

```bash
sf-exit verify ./archive --live --org old
```

Every object's row count should match the org exactly. Keep this output with the archive as evidence.

## 4. Make it readable without any tools

```bash
sf-exit load ./archive --to sqlite:///./archive/archive.db
sf-exit load ./archive --to parquet:///./archive/parquet
```

SQLite and Parquet are open formats with readers in every language, which makes them a good fit for long-term retention. The CSVs and `schema/*.json` stay alongside them.

## 5. Store it

- Compress: `tar -czf salesforce-archive-$(date +%F).tar.gz archive/`
- Store two copies in different places, for example an encrypted bucket with object lock or retention, plus offline storage.
- Record the SHA-256 of the tarball alongside it.
- Write down where it is and who can access it. `archive/REPORT.md` and `archive/audit/index.html` are the best starting points for whoever needs it later.

## 6. Then switch off

Before you cancel, also export anything sf-exit doesn't cover:

- **Field history beyond 18–24 months** (Field Audit Trail's `FieldHistoryArchive` big object), if you used it
- **Event Monitoring logs** (`EventLogFile`), if you need them for compliance (exported as a normal object, but files expire after 30 days)
- **Chatter files in private groups** (needs Query All Files)
- **Reports' run results** (sf-exit exports report definitions, not snapshots of their output)

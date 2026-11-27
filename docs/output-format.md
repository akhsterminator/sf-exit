# Output format

Everything sf-exit writes is plain files, so other tools can read an export without sf-exit.

## `data/<Object>.csv`

- UTF-8, RFC 4180, every value quoted, `\n` line endings. Multi-line text is preserved inside quotes.
- The header row uses field API names. `Id` is always the first column.
- `null` is an empty value. Salesforce does not distinguish null from empty for text fields.
- Booleans are `true` / `false`. Dates are `YYYY-MM-DD`. Date-times are ISO 8601 UTC (`2024-01-31T09:00:00.000Z`).
- Compound fields (`BillingAddress`, geolocation) are represented by their component fields.
- Base64 fields (file bodies) aren't in the CSV. The binaries are in `files/`.

- Numbers are written exactly as Salesforce stores them (no floating-point rounding), so `0.1` stays `0.1` and large currency amounts keep every digit.

Whichever API exported an object (Bulk or REST), its CSV uses the same format.

## `files/index.csv`

| Column | Meaning |
|---|---|
| `kind` | `ContentVersion`, `Attachment` or `Document` |
| `record_id` | Id of the ContentVersion, Attachment or Document |
| `parent_id` | ContentDocumentId, the Attachment's ParentId, or the Document's FolderId |
| `name` | Original title or file name |
| `path` | Relative path inside the export |
| `bytes`, `sha256`, `md5` | Computed from the downloaded bytes |
| `sf_checksum` | Salesforce's MD5 (`ContentVersion.Checksum`), compared with `md5` |
| `status`, `error` | `ok`, `ok (resumed)`, `checksum-mismatch` or `error` |

To find the files attached to a record, join `ContentDocumentLink.LinkedEntityId` → `ContentDocumentId` → `files/index.csv.parent_id`. For Attachments, use `parent_id` directly.

## `manifest.json`

Machine-readable: tool version, org, API version, totals, and for each object its status, row count, engine, SHA-256, warnings and when it was last exported (the incremental watermark). It also lists skipped objects and why, per-kind file totals, `file_listing_errors` (file queries that failed, so `verify` fails too), and `runs`: when each export run into this folder started and finished, with how many records changed.

## `metadata/` (with `--metadata`)

A standard Metadata API folder: `package.xml` plus one directory per type (`classes/`, `triggers/`, `flows/`, `objects/`, `layouts/`, `reports/`…). Deploy it with `sf project deploy start --metadata-dir`, or convert it to source format with `sf project convert mdapi`. Problems are listed in `metadata/retrieve-warnings.txt`.

## `changes/<run>/`

Written by every run into an existing export folder that finds changes ([time machine](time-machine.md)):

| File | Contents |
|---|---|
| `<Object>.csv` | The new version of each record created, updated or deleted in this run, in the same CSV format as `data/`. Deleted rows have `IsDeleted = "true"` |
| `<Object>.before.csv` | The version of each of those records before this run (absent for new records) |
| `run.json` | `stamp`, `started_at`, `finished_at`, `incremental`, and changed records per object |

`sf-exit history` and `sf-exit snapshot` read these folders. Keep them and you can see what any record looked like after any run.

## `restore/<org id>/` (written by `sf-exit restore`)

| File | Contents |
|---|---|
| `id-map.csv` | Old Id → new Id for every restored record (re-runs skip these) |
| `errors.csv` | Records Salesforce rejected, with its error message |
| `files-map.csv` | Files uploaded with `--files` (old Id → new Id) |
| `unknown.csv` | Records whose insert outcome is unknown after a network failure. Check the org, then delete the file to retry them |

## `audit/` (with `--audit` or `sf-exit audit`)

`index.html` (interactive report), `data-dictionary.csv`, `findings.md` and `audit.json`. See [audit.md](audit.md).

## Redacted values

With `--redact` or `--redact-pii`, chosen fields hold `redacted:<16 hex>` instead of the value. `manifest.json` → `redacted` lists which fields were hashed in each object.

## `.sf-exit/`

Internal bookkeeping: `state.json` lets an interrupted run resume (pass `--fresh` to discard it) and `index.db` is the search index used by `browse` and `mcp`. Both can be deleted at any time.

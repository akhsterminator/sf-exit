# Output format

Everything sf-exit writes is plain files, so other tools can read an export without sf-exit.

## `data/<Object>.csv`

- UTF-8, RFC 4180, every value quoted, `\n` line endings. Multi-line text is preserved inside quotes.
- The header row uses field API names. `Id` is always the first column.
- `null` is an empty value. Salesforce does not distinguish null from empty for text fields.
- Booleans are `true` / `false`. Dates are `YYYY-MM-DD`. Date-times are ISO 8601 UTC (`2024-01-31T09:00:00.000Z`).
- Compound fields (`BillingAddress`, geolocation) are represented by their component fields.
- Base64 fields (file bodies) aren't in the CSV. The binaries are in `files/`.

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

Machine-readable: tool version, org, API version, totals, and for each object its status, row count, engine and SHA-256. It also lists skipped objects and why, plus per-kind file totals.

## `metadata/` (with `--metadata`)

A standard Metadata API folder: `package.xml` plus one directory per type (`classes/`, `triggers/`, `flows/`, `objects/`, `layouts/`, `reports/`…). Deploy it with `sf project deploy start --metadata-dir`, or convert it to source format with `sf project convert mdapi`. Problems are listed in `metadata/retrieve-warnings.txt`.

## `changes/<timestamp>/` (with `--incremental`)

For each object, the rows that were added, updated or deleted in that incremental run, in the same CSV format as `data/`. Deleted rows have `IsDeleted = "true"`. These snapshots are the history of your org: keep them and you can see what any record looked like on any night.

## `audit/` (with `--audit` or `sf-exit audit`)

`index.html` (interactive report), `data-dictionary.csv`, `findings.md` and `audit.json`. See [audit.md](audit.md).

## Redacted values

With `--redact` or `--redact-pii`, chosen fields hold `redacted:<16 hex>` instead of the value. `manifest.json` → `redacted` lists which fields were hashed in each object.

## `.sf-exit/state.json`

Resume bookkeeping. You can delete it, or pass `--fresh`, to start over.

# sf-exit restore (experimental)

`sf-exit restore` re-creates records from an export in a Salesforce org, with their relationships intact. Use it to seed a sandbox with real data, rebuild an org, or bring back records that were deleted.

It is the only sf-exit command that writes to Salesforce, so it is careful by default:

- **Dry run unless you pass `--execute`.** Without it you only see the plan.
- **Sandboxes and Developer Edition orgs only**, unless you pass `--allow-production`.
- **Refuses the org the export came from** unless you pass `--allow-same-org`. Even then it only re-creates records that no longer exist there (see [below](#bringing-back-deleted-records)).

It's marked experimental because Salesforce orgs vary enormously (validation rules, triggers, required fields). Always try it on a sandbox first.

![sf-exit restore: dry run, then execute](restore.svg)

## Usage

```bash
# 1. See the plan: order, record counts, fields, second-pass lookups
sf-exit restore ./export --org my-sandbox -i Account -i Contact -i Opportunity -i 'Invoice__c'

# 2. Do it
sf-exit restore ./export --org my-sandbox -i Account -i Contact -i Opportunity -i 'Invoice__c' --execute
```

`-i` takes globs, so `-i '*__c'` restores every custom object. Use `-x` to leave objects out.

## What it does

1. **Plans the order.** Objects are inserted parents-first, based on the lookups between the objects you chose.
2. **Inserts in batches of 200**, sending only fields that exist and are createable in the target. Formula, auto-number and system fields are left out, and fields missing from the target are listed.
3. **Remaps every lookup.** Each new record's Id is recorded against its old Id in `restore/<org id>/id-map.csv`, and child records are written with the new parent Ids.
4. **Second pass for backward links.** Self-references such as `Account.ParentId`, and lookups inside cycles, are filled in with an update once both records exist.
5. **Matches users and record types.** Owners are matched to active users in the target by username, then email. Unmatched owners default to you. Record types are matched by developer name.
6. **Logs failures** to `restore/<org id>/errors.csv` with Salesforce's error message, and carries on.

## Resuming

Re-run the same command. Records already in `id-map.csv` are skipped, so an interrupted restore continues without duplicates.

## Files

`--files` uploads the newest version of every Salesforce File linked to a restored record, links it to all of the restored records it was linked to, and re-creates Attachments on their restored parents. Uploads stream from disk (up to 2 GB each). `files-map.csv` records what was uploaded, so re-running doesn't upload anything twice.

## Upsert: restores that are safe to re-run

```bash
sf-exit restore ./export --org my-sandbox -i Account -i Contact --upsert '*.Legacy_Id__c' --execute
```

Create a text field marked **External ID** and **Unique** (here `Legacy_Id__c`) on the objects you restore. sf-exit fills it with each record's original Salesforce Id, or with the exported value if the export has one, and upserts on it. Re-running the restore, or later restoring a newer export into the same sandbox, then **updates** the records it created before instead of duplicating them, and a batch that hit a network error can simply be retried. For an explicitly named object, a field that isn't an External ID is reported before anything is written.

## Bringing back deleted records

Restoring into the org the export came from (`--allow-same-org`) re-creates only the records that are missing there. Before writing anything, sf-exit checks which exported records still exist (a query per object, or one Bulk job for big objects) and leaves them alone, so `-i Contact` brings back the deleted contacts without duplicating the others. The plan shows how many still exist. Recovered records are new records with new Ids. Their lookups to parents that still exist keep the original Ids, so recovered contacts are re-attached to their accounts.

Combine this with a [snapshot](time-machine.md) to recover what was deleted after a given moment, as it was then:

```bash
sf-exit snapshot ./backup --as-of 2026-11-20T09:00:00Z -o ./before-the-cleanup
sf-exit restore ./before-the-cleanup --org prod -i Contact --allow-same-org --allow-production   # read the plan first!
```

To put old *values* back on records that still exist, for example after a bad import, `sf-exit diff ./before-the-cleanup ./backup` shows exactly which fields changed. Then load the columns you want back from the snapshot's `data/Contact.csv` with an update in Data Loader, matching on `Id`.

## When the network fails mid-write

A batch whose outcome is unknown (the connection dropped *after* it was sent) is never retried blindly. sf-exit writes those records' old Ids to `restore/<org id>/unknown.csv` and stops. Re-running skips them, so nothing is created twice. After checking the org, delete the file to retry them. With `--upsert` this can't happen: upserts are safe to retry, so sf-exit simply retries them.

## What it doesn't do (yet)

| Not restored | Use instead |
|---|---|
| Metadata (objects, fields, Apex, Flows) | `sf project deploy start --metadata-dir ./export/metadata`, **before** restoring records |
| Users, profiles, sharing rows, field history | Recreate users first so owners can be matched |
| Original Ids and created dates | New records get new Ids. Created dates need Salesforce's "Set Audit Fields" permission, which isn't used |
| Documents (the old folder-based kind) and old file versions | Only the newest version of each Salesforce File is uploaded |

## Tips

- **Automation fires.** Triggers, flows and validation rules run on inserted records. In a sandbox, consider switching off email deliverability and noisy automation first.
- **Order of operations for a full rebuild:** deploy metadata → create users → `sf-exit restore`.
- **Lookups left empty** are reported at the end, by field. They point at records that weren't in the export (for example, deleted parents) or objects you didn't include.

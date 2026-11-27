# Recipe: free nightly Salesforce backups

Salesforce's own backup product and third-party backup vendors charge per user, per month. You can do it with a scheduled GitHub Action (or cron on any server) and storage you already own.

Each night this setup:

1. downloads last night's export from your bucket
2. runs an **incremental** export, fetching only records changed or deleted since then, plus new files
3. verifies every row count and checksum
4. writes a **diff** of what changed today
5. uploads the export, plus a dated copy of today's changes

A weekly full export catches records that were hard-deleted (purged from the Recycle Bin), which no API can detect incrementally, and refreshes formula fields on unchanged rows. Every run, full or incremental, also records what changed in `changes/`, which gives you [a time machine](../time-machine.md): `sf-exit history` for any record and `sf-exit snapshot --as-of` for the whole org.

## GitHub Actions + S3

Put this in a **private** repository as `.github/workflows/salesforce-backup.yml`. Add the secrets `SF_INSTANCE_URL`, `SF_CLIENT_ID` and `SF_CLIENT_SECRET` ([client-credentials setup](../authentication.md#client-credentials)), plus `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `BACKUP_BUCKET`.

```yaml
name: Salesforce backup
on:
  schedule:
    - cron: "17 2 * * *"     # nightly, incremental
    - cron: "47 3 * * 0"     # Sundays, full
  workflow_dispatch:
permissions: { contents: read }
jobs:
  backup:
    runs-on: ubuntu-latest
    timeout-minutes: 360
    env:
      AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
      AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
      BUCKET: ${{ secrets.BACKUP_BUCKET }}
      SF_INSTANCE_URL: ${{ secrets.SF_INSTANCE_URL }}
      SF_CLIENT_ID: ${{ secrets.SF_CLIENT_ID }}
      SF_CLIENT_SECRET: ${{ secrets.SF_CLIENT_SECRET }}
    steps:
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install 'sf-exit[duckdb]'
      - name: Restore last export
        run: aws s3 sync "s3://$BUCKET/salesforce/current/" sf/ --only-show-errors || true
      - name: Keep a copy for the diff
        run: cp -r sf previous || mkdir previous
      - name: Export
        run: |
          if [ "${{ github.event.schedule }}" = "47 3 * * 0" ]; then MODE=; else MODE=--incremental; fi
          sf-exit export -d sf $MODE --include-deleted --metadata
      - name: Verify
        run: sf-exit verify sf
      - name: What changed
        run: sf-exit diff previous sf --details sf/diff | tee diff.txt
      - name: Upload
        run: |
          aws s3 sync sf/ "s3://$BUCKET/salesforce/current/" --delete --sse AES256 --only-show-errors
          aws s3 cp diff.txt "s3://$BUCKET/salesforce/history/$(date -u +%F)/diff.txt"
          if [ -d sf/changes ]; then aws s3 sync sf/changes "s3://$BUCKET/salesforce/history/$(date -u +%F)/changes/" --only-show-errors; fi
```

Turn on **versioning** on the bucket and add a lifecycle rule (for example, keep old versions for 90 days). That gives you point-in-time restore of every file for free.

## Any Linux server (cron)

```bash
# crontab -e
17 2 * * 1-6  cd /srv/sf && sf-exit export --org prod -d current --incremental --include-deleted >> backup.log 2>&1
47 3 * * 0    cd /srv/sf && sf-exit export --org prod -d current --include-deleted --metadata >> backup.log 2>&1
```

Snapshot the folder with your existing backup tool (restic, borg, ZFS snapshots…). Because unchanged CSVs and files don't change, deduplicating backup tools store each night's changes very cheaply.

## Restoring

sf-exit gives you the data, the files and the metadata in open formats. To put records back:

- **A few records:** look them up with `sf-exit sql` and re-enter them, or restore with Data Loader from the CSV.
- **Many records:** use Data Loader or `sf data import bulk` with the CSV. Original Ids are in the `Id` column, so relationships can be re-linked with an upsert on an external-Id field.
- **Configuration:** `sf project deploy start --metadata-dir sf/metadata`.

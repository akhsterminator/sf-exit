# Security policy

sf-exit handles credentials and complete copies of CRM data, so we take security seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately via
[GitHub Security Advisories](https://github.com/akhsterminator/sf-exit/security/advisories/new).
You'll get a response within a few days.

## What sf-exit does and doesn't do

- It is **read-only**. Its only write-style call creates Bulk API *query* jobs. It never creates, updates or deletes records.
- It sends data **only** to your Salesforce instance and your local disk. There is no telemetry.
- It never writes access tokens to disk. With `--org`, tokens stay in the Salesforce CLI's own store.

## Keeping exports safe

An export is a full copy of your CRM, so treat it like a production database:

- Store it on encrypted disks or buckets, and restrict who can access it.
- In GitHub Actions, only run backups in **private** repositories, and keep artifact retention short.
- Delete exports you no longer need.

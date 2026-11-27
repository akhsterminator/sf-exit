# Authentication

sf-exit tries these methods in order and uses the first one that has enough information.

| # | Method | Flags / environment variables |
|---|---|---|
| 1 | Access token | `--access-token` + `--instance-url` (`SF_ACCESS_TOKEN`, `SF_INSTANCE_URL`) |
| 2 | Salesforce CLI | `--org ALIAS` (`SF_ORG` or `SF_TARGET_ORG`) |
| 3 | JWT bearer | `--jwt-key-file` + `--client-id` + `--username` |
| 4 | Username + password | `--username` + `--password` + `--client-id` (+ `--client-secret`) |
| 5 | Client credentials | `--client-id` + `--client-secret` + `--instance-url` |
| 6 | *(nothing given)* | The Salesforce CLI's default org, if `sf` is installed |

## Salesforce CLI (recommended for people)

```bash
# install: https://developer.salesforce.com/tools/salesforcecli
sf org login web --alias acme                     # production
sf org login web --alias acme-uat --instance-url https://test.salesforce.com   # sandbox
sf-exit export --org acme
```

sf-exit runs `sf org display --json` to borrow a fresh access token, and repeats this if the token expires during a long export. Your credentials never pass through sf-exit.

## Client credentials (recommended for servers and CI)

This needs no user password and no interactive login. A Salesforce admin sets it up once:

1. **Setup → External Client App Manager → New External Client App**. On older orgs use **App Manager → New Connected App**.
2. Enable OAuth. Use any callback URL (for example `https://localhost`), and select the scope **Manage user data via APIs (`api`)**.
3. Enable **Client Credentials Flow**.
4. Under the app's policies, set the **Run As** user to an integration user that has *View All Data* and *Query All Files*.
5. Copy the consumer key and secret.

```bash
export SF_INSTANCE_URL=https://acme.my.salesforce.com   # your My Domain URL
export SF_CLIENT_ID=3MVG9...
export SF_CLIENT_SECRET=...
sf-exit export
```

## JWT bearer

Use this when your security team prefers certificates to secrets. Upload the certificate to the connected app, pre-authorise the user's profile or permission set, then:

```bash
pip install 'sf-exit[jwt]'
sf-exit export --client-id 3MVG9... --username integration@acme.com --jwt-key-file server.key
# add --sandbox for sandboxes
```

The token's audience is `https://login.salesforce.com`, or `https://test.salesforce.com` for sandboxes (detected from `--instance-url`, including enhanced-domain sandbox URLs). If your org needs another one, set `SF_AUDIENCE_URL` (the Salesforce CLI's `SFDX_AUDIENCE_URL` also works).

## Access token

If you already have a session from another tool:

```bash
sf-exit export --instance-url https://acme.my.salesforce.com --access-token "00D...!AQ..."
```

## Username + password

This is the legacy OAuth username-password flow. Salesforce is phasing it out and it's disabled by default in new orgs, so prefer one of the methods above.

```bash
sf-exit export --client-id ... --client-secret ... -u you@acme.com --password 'Password123SecurityToken'
```

## Permissions checklist

Run `sf-exit doctor` to check these:

- **API Enabled**: required.
- **View All Data**: otherwise the export only contains records the user can see.
- **Query All Files**: otherwise files that aren't shared with the user are silently left out.
- **View Setup and Configuration**: lets `plan` read record counts up front and choose Bulk API for big objects before the first query. Without it, the export still works: it counts records from the first page of each query instead.

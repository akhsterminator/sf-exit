# Recipe: migrate from Salesforce to another CRM

This works for HubSpot, Pipedrive, Attio, Twenty, Zoho and anything else that imports CSV. The idea is simple: export everything, clean it with SQL, and write one import file per object in the shape the target expects. Original Salesforce Ids are kept in every file, so relationships survive.

## 1. Export and audit

```bash
sf-exit export --org prod -d ./sf --metadata --audit
open ./sf/audit/index.html
```

Fix what the audit finds **before** you migrate: duplicates, records owned by deactivated users, unused fields. Everything you leave behind is something you don't have to map. Keep `audit/data-dictionary.csv` open while you build the field mapping.

## 2. Add a "Salesforce ID" field in the target

Create a text property such as `salesforce_id` on Companies, Contacts and Deals in the target CRM, and make it unique if the CRM supports that. It's how you'll link records during import and trace them afterwards. Most CRMs, including HubSpot, Attio and Twenty, let you associate records during import by matching on a unique property.

## 3. Shape the import files with SQL

`sf-exit sql` runs DuckDB over the export, so a `COPY … TO` writes a ready-to-import CSV.

```bash
sf-exit sql ./sf "
COPY (
  SELECT Id AS salesforce_id, Name AS name, Website AS domain, Industry AS industry,
         BillingCity AS city, BillingCountry AS country, AnnualRevenue AS annual_revenue
  FROM Account
  WHERE NOT IsDeleted
) TO 'companies.csv' (HEADER);"

sf-exit sql ./sf "
COPY (
  SELECT c.Id AS salesforce_id, c.FirstName AS first_name, c.LastName AS last_name,
         lower(c.Email) AS email, c.Phone AS phone, c.Title AS job_title,
         c.AccountId AS company_salesforce_id,
         u.Email AS owner_email
  FROM Contact c LEFT JOIN \"User\" u ON u.Id = c.OwnerId
  QUALIFY row_number() OVER (PARTITION BY coalesce(lower(c.Email), c.Id) ORDER BY c.LastModifiedDate DESC) = 1
) TO 'contacts.csv' (HEADER);"

sf-exit sql ./sf "
COPY (
  SELECT o.Id AS salesforce_id, o.Name AS deal_name, o.Amount AS amount,
         o.CloseDate AS close_date, o.StageName AS stage,
         o.AccountId AS company_salesforce_id, u.Email AS owner_email
  FROM Opportunity o LEFT JOIN \"User\" u ON u.Id = o.OwnerId
) TO 'deals.csv' (HEADER);"
```

The `QUALIFY` line keeps only the most recently edited contact for each email address, which deduplicates people on the way out.

**Stage mapping.** Pipeline stages rarely match one to one. Find out what you have, then use a `CASE` expression:

```bash
sf-exit sql ./sf "SELECT StageName, count(*) FROM Opportunity GROUP BY 1 ORDER BY 2 DESC"
```

**Notes, tasks and emails.** These are in `Task`, `Event`, `EmailMessage`, `Note` and `ContentNote`. `WhoId` points at a Contact or Lead, and `WhatId` points at an Account, Opportunity or other record.

## 4. Files

`files/index.csv` maps every file to its record. For ContentVersion, join through `ContentDocumentLink`:

```bash
sf-exit sql ./sf "
COPY (
  SELECT l.LinkedEntityId AS salesforce_record_id, f.name, f.path
  FROM ContentDocumentLink l
  JOIN _files f ON f.parent_id = l.ContentDocumentId AND f.kind = 'ContentVersion'
  UNION ALL
  SELECT parent_id, name, path FROM _files WHERE kind = 'Attachment'
) TO 'files-to-attach.csv' (HEADER);"
```

Most CRMs need files uploaded through their API, using `salesforce_id` to find the record. The CSV above is the work list.

## 5. Check

- Count records in the target and compare: `sf-exit sql ./sf "SELECT count(*) FROM Contact"`.
- Spot-check a handful of records with many relationships.
- Keep the export. It's your proof of what existed and your rollback plan. `sf-exit verify ./sf` proves it hasn't been altered since.

## Field mapping cheatsheet

| Salesforce | HubSpot | Pipedrive | Attio / Twenty |
|---|---|---|---|
| Account | Company | Organization | Company |
| Contact | Contact | Person | Person |
| Lead | Contact (lifecycle = lead) | Lead | Person (status) |
| Opportunity | Deal | Deal | Deal / Opportunity |
| Task / Event | Task / Meeting | Activity | Task |
| Note / ContentNote | Note | Note | Note |
| Case | Ticket | – | custom object |
| Custom object | Custom object | – | custom object |

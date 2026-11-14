# Recipe: Salesforce data in Postgres (or DuckDB) for analytics

Reports in Salesforce are limited: two thousand rows in the UI, no real joins, and no window functions. Put the data in a real database and use any BI tool (Metabase, Superset, Grafana, Power BI, Tableau) or plain SQL.

## Load

```bash
pip install 'sf-exit[postgres]'
sf-exit export --org prod -d ./sf --no-files
sf-exit load ./sf --to postgresql://analytics@db.internal/crm --lowercase --replace
```

`--lowercase` gives you `select * from account` rather than `select * from "Account"`. Column types are real types: `timestamptz`, `date`, `boolean`, `numeric`.

To keep it fresh, run nightly:

```bash
sf-exit export --org prod -d ./sf --no-files --incremental && \
sf-exit load ./sf --to postgresql://analytics@db.internal/crm --lowercase --replace
```

## Useful queries

```sql
-- Pipeline by stage and month
select date_trunc('month', closedate) as month, stagename, count(*), sum(amount)
from opportunity where not isclosed group by 1, 2 order by 1, 2;

-- Win rate by owner over the last year
select u.name, avg(case when o.iswon then 1 else 0 end) as win_rate, count(*) as closed
from opportunity o join "user" u on u.id = o.ownerid
where o.isclosed and o.closedate > now() - interval '1 year'
group by 1 order by closed desc;

-- Accounts with no activity in 90 days
select a.id, a.name
from account a
left join task t on t.whatid = a.id and t.activitydate > now() - interval '90 days'
where t.id is null;

-- Field history: who changed which opportunity fields most
select field, count(*) from opportunityfieldhistory group by 1 order by 2 desc;
```

## DuckDB instead of a server

For a laptop or a notebook, skip Postgres:

```bash
sf-exit load ./sf --to duckdb:///crm.duckdb
duckdb crm.duckdb
```

Or query the export folder directly with no load step: `sf-exit sql ./sf`.

## dbt

Point a dbt source at the loaded schema. The `schema/*.json` describe files, or `audit/data-dictionary.csv`, give you labels and help text for documenting models.

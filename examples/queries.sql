-- Handy queries once an export is loaded:
--   sf-exit load ./export --to duckdb:///sf.duckdb   then   duckdb sf.duckdb < examples/queries.sql

-- Row counts per table
SELECT table_name, estimated_size AS rows FROM duckdb_tables() ORDER BY rows DESC;

-- Top accounts by open pipeline
SELECT a.Name, count(*) AS open_opps, sum(o.Amount) AS pipeline
FROM Opportunity o JOIN Account a ON a.Id = o.AccountId
WHERE NOT o.IsClosed
GROUP BY 1 ORDER BY pipeline DESC LIMIT 20;

-- Every file attached to an account, with its path on disk
SELECT a.Name AS account, f.name AS file, f.path, f.bytes
FROM ContentDocumentLink l
JOIN Account a ON a.Id = l.LinkedEntityId
JOIN _files f  ON f.parent_id = l.ContentDocumentId AND f.kind = 'ContentVersion'
ORDER BY account;

-- Contacts with no activity in the last year
SELECT c.Id, c.Name, c.Email
FROM Contact c
LEFT JOIN Task t ON t.WhoId = c.Id AND t.ActivityDate > current_date - INTERVAL 1 YEAR
WHERE t.Id IS NULL;

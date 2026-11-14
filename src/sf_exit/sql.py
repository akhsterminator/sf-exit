"""Query an export with SQL straight away: DuckDB views over the CSVs, with proper types."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Tuple

from sf_exit.loader import _duckdb, _read_csv_sql, discover_tables
from sf_exit.schema import quote_ident


def connect(export_dir: Path):
    """An in-memory DuckDB connection with one view per exported object (+ ``_files``)."""
    duckdb = _duckdb()
    con = duckdb.connect()
    for t in discover_tables(Path(export_dir)):
        cols = ", ".join(quote_ident(c) for c in t.columns)
        con.execute(f"CREATE VIEW {quote_ident(t.name)} AS SELECT {cols} FROM {_read_csv_sql(t)}")
    return con


def run(con, query: str, limit: int = 0) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    cur = con.execute(query)
    if cur.description is None:
        return [], []
    cols = [d[0] for d in cur.description]
    rows = cur.fetchmany(limit) if limit else cur.fetchall()
    return cols, rows


def tables(con) -> List[Tuple[str, int]]:
    names = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables ORDER BY lower(table_name)").fetchall()]
    return [(n, len(con.execute(f"DESCRIBE {quote_ident(n)}").fetchall())) for n in names]

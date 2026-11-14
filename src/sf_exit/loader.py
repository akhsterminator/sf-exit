"""Load an sf-exit export folder into a database or Parquet files.

Targets:
  sqlite:///path/to/file.db        (built in, no extra install)
  duckdb:///path/to/file.duckdb    (pip install 'sf-exit[duckdb]')
  postgresql://user:pass@host/db   (pip install 'sf-exit[postgres]')
  parquet:///path/to/folder        (pip install 'sf-exit[parquet]')
"""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from sf_exit.schema import create_table_sql, quote_ident, sql_type, table_name

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


class LoadError(Exception):
    pass


@dataclass
class Table:
    name: str
    csv_path: Path
    columns: List[str]
    field_types: Dict[str, str]


def discover_tables(export_dir: Path, only: Optional[List[str]] = None) -> List[Table]:
    data_dir = export_dir / "data"
    if not data_dir.is_dir():
        raise LoadError(f"{export_dir} doesn't look like an sf-exit export (no data/ folder)")
    tables = []
    for path in sorted(data_dir.glob("*.csv"), key=lambda p: p.stem.lower()):
        name = path.stem
        if only and name not in only:
            continue
        with open(path, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh), [])
        if not header:
            continue
        types: Dict[str, str] = {}
        schema = export_dir / "schema" / f"{name}.json"
        if schema.exists():
            describe = json.loads(schema.read_text("utf-8"))
            types = {f["name"]: f.get("type", "string") for f in describe.get("fields", [])}
        tables.append(Table(name, path, header, types))
    # Files index is useful to query too (which file belongs to which record)
    index = export_dir / "files" / "index.csv"
    if index.exists() and (not only or "_files" in only):
        with open(index, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh), [])
        tables.append(Table("_files", index, header, {"bytes": "long"}))
    return tables


def _rows(table: Table, convert: Callable[[str, str], object]) -> Iterator[Tuple]:
    types = [table.field_types.get(c, "string") for c in table.columns]
    with open(table.csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for row in reader:
            yield tuple(convert(v, t) for v, t in zip(row, types, strict=False))


def _sqlite_value(v: str, t: str) -> object:
    if v == "":
        return None
    if t == "boolean":
        return 1 if v.lower() == "true" else 0
    if t in ("int", "long"):
        try:
            return int(v)
        except ValueError:
            return float(v)
    if t in ("double", "currency", "percent"):
        return float(v)
    return v


def parse_target(target: str) -> Tuple[str, str]:
    for scheme in ("sqlite", "duckdb", "parquet"):
        prefix = f"{scheme}:///"
        if target.startswith(prefix):
            return scheme, target[len(prefix):] or "."
        if target.startswith(f"{scheme}://"):
            return scheme, target[len(scheme) + 3:]
    if target.startswith(("postgres://", "postgresql://")):
        return "postgres", target
    raise LoadError(
        f"Unknown target '{target}'. Use sqlite:///file.db, duckdb:///file.duckdb, "
        "postgresql://user:pass@host/db or parquet:///folder"
    )


def load(export_dir: Path, target: str, *, lowercase: bool = False, replace: bool = False,
         only: Optional[List[str]] = None,
         on_table: Optional[Callable[[str, int], None]] = None) -> Dict[str, int]:
    kind, where = parse_target(target)
    tables = discover_tables(Path(export_dir), only)
    loaders = {"sqlite": _load_sqlite, "duckdb": _load_duckdb, "postgres": _load_postgres,
               "parquet": _load_parquet}
    return loaders[kind](tables, where, lowercase, replace, on_table or (lambda n, r: None))


def _or_replace(t: Table) -> str:
    # Re-loading is a snapshot (tables are emptied first); OR REPLACE only guards against
    # the rare duplicate Id the Bulk API can return when records change mid-export.
    return "OR REPLACE " if "Id" in t.columns else ""


def _col(c: str, lowercase: bool) -> str:
    return quote_ident(c.lower() if lowercase else c)


# --------------------------------------------------------------------- sqlite
def _load_sqlite(tables, path, lowercase, replace, on_table) -> Dict[str, int]:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    counts = {}
    try:
        for t in tables:
            tname = quote_ident(table_name(t.name, lowercase))
            if replace:
                con.execute(f"DROP TABLE IF EXISTS {tname}")
            con.execute(create_table_sql(t.name, t.columns, t.field_types, "sqlite", lowercase))
            con.execute(f"DELETE FROM {tname}")
            cols = ", ".join(_col(c, lowercase) for c in t.columns)
            marks = ", ".join("?" for _ in t.columns)
            cur = con.executemany(
                f"INSERT {_or_replace(t)}INTO {tname} ({cols}) VALUES ({marks})",
                _rows(t, _sqlite_value),
            )
            con.commit()
            counts[t.name] = cur.rowcount if cur.rowcount >= 0 else 0
            on_table(t.name, counts[t.name])
    finally:
        con.close()
    return counts


# --------------------------------------------------------------------- duckdb
def _duckdb():
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise LoadError("DuckDB/Parquet output needs: pip install 'sf-exit[duckdb]'") from exc
    return duckdb


def _read_csv_sql(t: Table) -> str:
    cols = ", ".join(
        f"'{c.replace(chr(39), chr(39) * 2)}': '{sql_type(t.field_types.get(c, 'string'), 'duckdb')}'"
        for c in t.columns
    )
    path = str(t.csv_path).replace("'", "''")
    return (
        f"read_csv('{path}', header=true, columns={{{cols}}}, quote='\"', escape='\"', "
        "nullstr='', allow_quoted_nulls=true, strict_mode=false, max_line_size=104857600, "
        "parallel=false)"
    )


def _load_duckdb(tables, path, lowercase, replace, on_table) -> Dict[str, int]:
    duckdb = _duckdb()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(path)
    counts = {}
    try:
        for t in tables:
            tname = quote_ident(table_name(t.name, lowercase))
            if replace:
                con.execute(f"DROP TABLE IF EXISTS {tname}")
            con.execute(create_table_sql(t.name, t.columns, t.field_types, "duckdb", lowercase))
            con.execute(f"DELETE FROM {tname}")
            cols = ", ".join(_col(c, lowercase) for c in t.columns)
            src = ", ".join(quote_ident(c) for c in t.columns)
            con.execute(
                f"INSERT {_or_replace(t)}INTO {tname} ({cols}) SELECT {src} FROM {_read_csv_sql(t)}"
            )
            counts[t.name] = con.execute(f"SELECT count(*) FROM {tname}").fetchone()[0]
            on_table(t.name, counts[t.name])
    finally:
        con.close()
    return counts


def _load_parquet(tables, folder, lowercase, replace, on_table) -> Dict[str, int]:
    duckdb = _duckdb()
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    counts = {}
    for t in tables:
        dest = out / f"{table_name(t.name, lowercase)}.parquet"
        if dest.exists() and not replace:
            raise LoadError(f"{dest} exists (use --replace to overwrite)")
        sel = ", ".join(f"{quote_ident(c)} AS {_col(c, lowercase)}" for c in t.columns)
        target = str(dest).replace("'", "''")
        con.execute(
            f"COPY (SELECT {sel} FROM {_read_csv_sql(t)}) TO '{target}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        counts[t.name] = con.execute(f"SELECT count(*) FROM '{target}'").fetchone()[0]
        on_table(t.name, counts[t.name])
    con.close()
    return counts


# ------------------------------------------------------------------- postgres
def _load_postgres(tables, dsn, lowercase, replace, on_table) -> Dict[str, int]:
    try:
        import psycopg  # type: ignore
    except ImportError as exc:
        raise LoadError("Postgres output needs: pip install 'sf-exit[postgres]'") from exc
    counts = {}
    with psycopg.connect(dsn) as con:
        for t in tables:
            tname = quote_ident(table_name(t.name, lowercase))
            with con.cursor() as cur:
                if replace:
                    cur.execute(f"DROP TABLE IF EXISTS {tname}")
                cur.execute(create_table_sql(t.name, t.columns, t.field_types, "postgres", lowercase))
                cur.execute(f"TRUNCATE {tname}")
                cols = ", ".join(_col(c, lowercase) for c in t.columns)
                copy_sql = (
                    f"COPY {tname} ({cols}) FROM STDIN WITH (FORMAT csv, HEADER true, "
                    f"FORCE_NULL ({cols}))"
                )
                with cur.copy(copy_sql) as copy, open(t.csv_path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        copy.write(chunk)
                cur.execute(f"SELECT count(*) FROM {tname}")
                counts[t.name] = cur.fetchone()[0]
            con.commit()
            on_table(t.name, counts[t.name])
    return counts

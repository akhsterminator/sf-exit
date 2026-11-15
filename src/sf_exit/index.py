"""A local SQLite index of an export, shared by ``sf-exit browse`` and ``sf-exit mcp``.

Built once into ``<export>/.sf-exit/index.db`` and rebuilt automatically when the export
changes. Contains one typed table per object, a catalog (labels, fields, relationships),
indexes on every lookup column and a full-text search index over record names.
Only the standard library is needed.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from sf_exit.loader import _load_sqlite, discover_tables
from sf_exit.schema import quote_ident as q

INDEX_VERSION = "2"
TITLE_CANDIDATES = ("Name", "Subject", "Title", "CaseNumber", "DeveloperName", "MasterLabel",
                    "Label", "Username", "PathOnClient", "LastName")
SEARCH_TYPES = {"string", "email", "phone", "url"}
NOISY_REFS = {"CreatedById", "LastModifiedById"}


@dataclass
class Fld:
    name: str
    label: str
    type: str
    custom: bool = False
    help: str = ""
    refs: List[str] = field(default_factory=list)
    picklist: List[str] = field(default_factory=list)


@dataclass
class Obj:
    name: str
    label: str
    custom: bool
    rows: int
    title_field: Optional[str]
    key_prefix: str
    fields: Dict[str, Fld] = field(default_factory=dict)


def index_path(export_dir: Path) -> Path:
    return Path(export_dir) / ".sf-exit" / "index.db"


def _signature(export_dir: Path) -> str:
    parts = [INDEX_VERSION]
    for p in [export_dir / "manifest.json", export_dir / "files" / "index.csv"]:
        if p.exists():
            st = p.stat()
            parts.append(f"{p.name}:{st.st_mtime_ns}:{st.st_size}")
    data = sorted((export_dir / "data").glob("*.csv"))
    parts.append(f"n={len(data)}:{sum(p.stat().st_size for p in data)}")
    return "|".join(parts)


def ensure_index(export_dir: Path, force: bool = False,
                 on_progress: Optional[Callable[[str], None]] = None) -> Path:
    """Build the index if it is missing or older than the export. Returns its path."""
    export_dir = Path(export_dir)
    path = index_path(export_dir)
    sig = _signature(export_dir)
    if path.exists() and not force:
        with contextlib.suppress(sqlite3.Error), contextlib.closing(sqlite3.connect(path)) as con:
            row = con.execute("SELECT value FROM _meta WHERE key = 'signature'").fetchone()
            if row and row[0] == sig:
                return path
    say = on_progress or (lambda s: None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("index.db.building")
    for stale in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        stale.unlink(missing_ok=True)

    tables = discover_tables(export_dir)
    say(f"Indexing {len(tables)} tables")
    counts = _load_sqlite(tables, str(tmp), False, True, lambda name, n: say(f"Indexed {name}"))

    con = sqlite3.connect(tmp)
    try:
        con.execute("PRAGMA synchronous=OFF")
        con.executescript(
            "CREATE TABLE _meta(key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE _objects(name TEXT PRIMARY KEY, label TEXT, custom INT, rows INT,"
            " title_field TEXT, key_prefix TEXT);"
            "CREATE TABLE _fields(object TEXT, name TEXT, label TEXT, type TEXT, custom INT,"
            " help TEXT, refs TEXT, picklist TEXT, position INT, PRIMARY KEY(object, name));"
        )
        fts = _create_search(con)
        for t in tables:
            if t.name.startswith("_"):
                continue
            desc = {}
            schema = export_dir / "schema" / f"{t.name}.json"
            if schema.exists():
                desc = json.loads(schema.read_text("utf-8"))
            meta = {f["name"]: f for f in desc.get("fields", [])}
            title = next((f["name"] for f in desc.get("fields", [])
                          if f.get("nameField") and f["name"] in t.columns), None)
            if not title:
                title = next((c for c in TITLE_CANDIDATES if c in t.columns), None)
            rows = counts.get(t.name, 0)
            prefix = desc.get("keyPrefix") or ""
            if not prefix and rows and "Id" in t.columns:
                first = con.execute(f"SELECT {q('Id')} FROM {q(t.name)} LIMIT 1").fetchone()
                prefix = (first[0] or "")[:3] if first else ""
            con.execute("INSERT INTO _objects VALUES (?,?,?,?,?,?)",
                        (t.name, desc.get("label", t.name), int(bool(desc.get("custom"))), rows,
                         title, prefix))
            for pos, c in enumerate(t.columns):
                m = meta.get(c, {})
                picks = [p["value"] for p in m.get("picklistValues", []) if p.get("active", True)]
                con.execute(
                    "INSERT INTO _fields VALUES (?,?,?,?,?,?,?,?,?)",
                    (t.name, c, m.get("label", c), m.get("type", t.field_types.get(c, "string")),
                     int(bool(m.get("custom"))), m.get("inlineHelpText") or "",
                     "|".join(m.get("referenceTo") or []), "|".join(picks), pos))
            if not rows or "Id" not in t.columns:
                continue
            for c in t.columns:
                if meta.get(c, {}).get("type") == "reference" and c not in NOISY_REFS:
                    con.execute(f"CREATE INDEX {q('ix_' + t.name + '_' + c)} ON {q(t.name)}({q(c)})")
            if title:
                body_cols = [c for c in t.columns if c != title
                             and meta.get(c, {}).get("type") in SEARCH_TYPES
                             and (meta.get(c, {}).get("length") or 0) <= 255][:8]
                body = " || ' ' || ".join(f"coalesce({q(c)}, '')" for c in body_cols) or "''"
                con.execute(
                    f"INSERT INTO _search(object, id, title, body) "
                    f"SELECT ?, {q('Id')}, coalesce({q(title)}, ''), {body} FROM {q(t.name)}",
                    (t.name,))
        if "_files" in counts:
            con.execute("CREATE INDEX ix_files_parent ON _files(parent_id)")
        con.executemany("INSERT INTO _meta VALUES (?, ?)",
                        [("signature", sig), ("fts", "1" if fts else "0")])
        con.commit()
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    tmp.replace(path)
    return path


def _create_search(con: sqlite3.Connection) -> bool:
    try:
        con.execute("CREATE VIRTUAL TABLE _search USING fts5(object UNINDEXED, id UNINDEXED, "
                    "title, body, tokenize='unicode61 remove_diacritics 2', prefix='2 3 4')")
        return True
    except sqlite3.OperationalError:  # SQLite built without FTS5: fall back to LIKE
        con.execute("CREATE TABLE _search(object TEXT, id TEXT, title TEXT, body TEXT)")
        return False


def connect(export_dir: Path) -> sqlite3.Connection:
    """A read-only connection to the index: queries can never modify anything."""
    uri = index_path(Path(export_dir)).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


class Catalog:
    """Objects, fields and relationships of an indexed export."""

    def __init__(self, con: sqlite3.Connection):
        self.objects: Dict[str, Obj] = {}
        for r in con.execute("SELECT * FROM _objects"):
            self.objects[r["name"]] = Obj(r["name"], r["label"], bool(r["custom"]), r["rows"],
                                          r["title_field"], r["key_prefix"] or "")
        for r in con.execute("SELECT * FROM _fields ORDER BY object, position"):
            o = self.objects.get(r["object"])
            if o:
                o.fields[r["name"]] = Fld(
                    r["name"], r["label"], r["type"], bool(r["custom"]), r["help"] or "",
                    [x for x in (r["refs"] or "").split("|") if x],
                    [x for x in (r["picklist"] or "").split("|") if x])
        self.fts = (con.execute("SELECT value FROM _meta WHERE key='fts'").fetchone() or ["0"])[0] == "1"
        self.has_files = bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE name = '_files'").fetchone())
        self.by_prefix: Dict[str, List[str]] = {}
        for o in self.objects.values():
            if o.key_prefix and o.rows:
                self.by_prefix.setdefault(o.key_prefix, []).append(o.name)
        # parent object -> [(child object, lookup field)]
        self.children: Dict[str, List[Tuple[str, str]]] = {}
        for o in self.objects.values():
            if not o.rows:
                continue
            for f in o.fields.values():
                if f.type == "reference" and f.name not in NOISY_REFS:
                    for target in f.refs:
                        self.children.setdefault(target, []).append((o.name, f.name))

    def object_for_id(self, con: sqlite3.Connection, record_id: str) -> Optional[str]:
        for name in self.by_prefix.get(record_id[:3], []):
            if con.execute(f"SELECT 1 FROM {q(name)} WHERE {q('Id')} = ?", (record_id,)).fetchone():
                return name
        return None

    def titles(self, con: sqlite3.Connection, ids: List[str]) -> Dict[str, Dict[str, str]]:
        """Resolve record Ids to {object, title} in as few queries as possible."""
        out: Dict[str, Dict[str, str]] = {}
        groups: Dict[str, List[str]] = {}
        for i in {i for i in ids if isinstance(i, str) and len(i) >= 15}:
            for name in self.by_prefix.get(i[:3], []):
                groups.setdefault(name, []).append(i)
        for name, group in groups.items():
            o = self.objects[name]
            col = q(o.title_field) if o.title_field else q("Id")
            for start in range(0, len(group), 500):
                chunk = group[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                for r in con.execute(
                        f"SELECT {q('Id')}, {col} FROM {q(name)} WHERE {q('Id')} IN ({marks})", chunk):
                    out[r[0]] = {"object": name, "title": str(r[1]) if r[1] not in (None, "") else r[0]}
        return out


def fts_query(text: str) -> str:
    tokens = ["".join(ch for ch in t if ch.isalnum() or ch in "_@.-") for t in text.split()]
    tokens = [t.strip(".-") for t in tokens]
    return " ".join(f'"{t}"*' for t in tokens if t)


def search(con: sqlite3.Connection, catalog: Catalog, text: str, limit: int = 30,
           obj: Optional[str] = None) -> List[Dict[str, str]]:
    text = text.strip()
    if not text:
        return []
    params: List[object]
    if catalog.fts and fts_query(text):
        sql = ("SELECT object, id, title, snippet(_search, 3, '', '', '…', 12) AS extra "
               "FROM _search WHERE _search MATCH ?")
        params = [fts_query(text)]
        if obj:
            sql += " AND object = ?"
            params.append(obj)
        sql += " ORDER BY bm25(_search, 0, 0, 8.0, 1.0) LIMIT ?"
    else:
        sql = "SELECT object, id, title, body AS extra FROM _search WHERE (title LIKE ? OR body LIKE ?)"
        params = [f"%{text}%", f"%{text}%"]
        if obj:
            sql += " AND object = ?"
            params.append(obj)
        sql += " LIMIT ?"
    params.append(limit)
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"object": r["object"], "id": r["id"], "title": r["title"] or r["id"],
             "extra": (r["extra"] or "").strip()} for r in rows]

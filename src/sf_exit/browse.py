"""``sf-exit browse``: click through an exported org in your browser, offline and read-only.

A tiny local web app (standard library only) over the SQLite index: a dashboard, object
lists, record pages with lookups resolved, related lists, attached files, each record's
history across backups, and full-text search. It binds to 127.0.0.1, opens the index
read-only and never talks to Salesforce.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from sf_exit import __version__
from sf_exit import history as hist
from sf_exit.index import Catalog, Obj, connect, index_path, search
from sf_exit.schema import quote_ident as q

PAGE = 50
PREFERRED_COLUMNS = ("Type", "StageName", "Status", "Amount", "CloseDate", "Email", "Phone",
                     "Industry", "AccountId", "ContactId", "WhatId", "WhoId", "Priority",
                     "OwnerId", "CreatedDate")
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}


class NotFound(Exception):
    pass


def list_columns(o: Obj) -> List[str]:
    cols: List[str] = []
    if o.title_field:
        cols.append(o.title_field)
    for c in PREFERRED_COLUMNS:
        if c in o.fields and c not in cols:
            cols.append(c)
    for f in o.fields.values():  # then custom fields, which usually matter most
        if len(cols) >= 7:
            break
        if f.custom and f.name not in cols and f.type not in ("textarea", "base64"):
            cols.append(f.name)
    if not cols:
        cols = [c for c in o.fields if c != "Id"][:6]
    return cols[:7]


def _cells(con: sqlite3.Connection, cat: Catalog, o: Obj, cols: List[str],
           rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    ref_cols = [c for c in cols if o.fields[c].type == "reference"]
    titles = cat.titles(con, [r[c] for r in rows for c in ref_cols if r[c]])
    out = []
    for r in rows:
        values: Dict[str, Any] = {}
        for c in cols:
            v = r[c]
            if c in ref_cols and v:
                values[c] = {"id": v, **titles.get(v, {"title": v, "object": None})}
            else:
                values[c] = v
        out.append({"id": r["Id"], "values": values})
    return out


def _columns_meta(o: Obj, cols: List[str]) -> List[Dict[str, str]]:
    return [{"name": c, "label": o.fields[c].label, "type": o.fields[c].type} for c in cols]


def summary(con: sqlite3.Connection, cat: Catalog, export_dir: Path) -> Dict[str, Any]:
    manifest: Dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        manifest = json.loads((export_dir / "manifest.json").read_text("utf-8"))
    objs = sorted(cat.objects.values(), key=lambda o: (-o.rows, o.name.lower()))
    files = None
    if cat.has_files:
        r = con.execute("SELECT count(*), coalesce(sum(bytes), 0) FROM _files").fetchone()
        files = {"count": r[0], "bytes": r[1]}
    return {
        "org": manifest.get("org", {}),
        "exported_at": manifest.get("finished_at"),
        "version": __version__,
        "records": sum(o.rows for o in objs),
        "files": files,
        "has_audit": (export_dir / "audit" / "index.html").exists(),
        "has_history": bool(hist.runs(export_dir)),
        "objects": [{"name": o.name, "label": o.label, "rows": o.rows, "custom": o.custom}
                    for o in objs],
    }


def object_list(con: sqlite3.Connection, cat: Catalog, name: str, offset: int = 0,
                sort: Optional[str] = None, desc: bool = False, text: str = "",
                where: Optional[Tuple[str, str]] = None) -> Dict[str, Any]:
    o = cat.objects.get(name)
    if not o:
        raise NotFound(name)
    cols = [c for c in list_columns(o) if not (where and c == where[0])]
    clauses, params = [], []
    if where:
        if where[0] not in o.fields:
            raise NotFound(where[0])
        clauses.append(f"{q(where[0])} = ?")
        params.append(where[1])
    if text:
        target = o.title_field or "Id"
        clauses.append(f"{q(target)} LIKE ? ESCAPE '\\'")
        params.append("%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    wsql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = con.execute(f"SELECT count(*) FROM {q(name)}{wsql}", params).fetchone()[0]
    order = sort if sort in o.fields else None
    osql = f" ORDER BY {q(order)} {'DESC' if desc else 'ASC'} NULLS LAST" if order else ""
    select = ", ".join(q(c) for c in dict.fromkeys(["Id", *cols]))
    rows = con.execute(f"SELECT {select} FROM {q(name)}{wsql}{osql} LIMIT ? OFFSET ?",
                       [*params, PAGE, max(0, offset)]).fetchall()
    return {"object": name, "label": o.label, "total": total, "offset": offset, "page": PAGE,
            "title_field": o.title_field, "columns": _columns_meta(o, cols),
            "rows": _cells(con, cat, o, cols, rows)}


def record(con: sqlite3.Connection, cat: Catalog, record_id: str) -> Dict[str, Any]:
    name = cat.object_for_id(con, record_id)
    if not name:
        raise NotFound(record_id)
    o = cat.objects[name]
    row = con.execute(f"SELECT * FROM {q(name)} WHERE {q('Id')} = ?", (record_id,)).fetchone()
    refs = [f.name for f in o.fields.values() if f.type == "reference" and row[f.name]]
    titles = cat.titles(con, [row[c] for c in refs])
    fields = []
    for f in o.fields.values():
        v = row[f.name]
        item: Dict[str, Any] = {"name": f.name, "label": f.label, "type": f.type, "value": v,
                                "custom": f.custom, "help": f.help}
        if f.name in refs:
            item["ref"] = {"id": v, **titles.get(v, {"title": v, "object": None})}
        fields.append(item)
    related = []
    for child, fld in sorted(cat.children.get(name, [])):
        n = con.execute(f"SELECT count(*) FROM {q(child)} WHERE {q(fld)} = ?",
                        (record_id,)).fetchone()[0]
        if n:
            c = cat.objects[child]
            related.append({"object": child, "label": c.label, "field": fld,
                            "field_label": c.fields[fld].label, "count": n})
    related.sort(key=lambda r: -r["count"])
    title = row[o.title_field] if o.title_field and row[o.title_field] else record_id
    return {"object": name, "label": o.label, "id": record_id, "title": str(title),
            "fields": fields, "related": related, "files": record_files(con, cat, name, record_id)}


def record_files(con: sqlite3.Connection, cat: Catalog, name: str, rid: str) -> List[Dict[str, Any]]:
    if not cat.has_files:
        return []
    sql = ["SELECT kind, name, path, bytes FROM _files WHERE parent_id = ? AND status LIKE 'ok%'"]
    params: List[Any] = [rid]
    if "ContentDocumentLink" in cat.objects and name != "ContentDocument":
        sql.append("SELECT f.kind, f.name, f.path, f.bytes FROM ContentDocumentLink l "
                   "JOIN _files f ON f.parent_id = l.ContentDocumentId "
                   "WHERE l.LinkedEntityId = ? AND f.status LIKE 'ok%'")
        params.append(rid)
    if name == "ContentVersion":
        sql.append("SELECT kind, name, path, bytes FROM _files WHERE record_id = ? AND status LIKE 'ok%'")
        params.append(rid)
    rows = con.execute(" UNION ".join(sql) + " LIMIT 200", params).fetchall()
    return [{"kind": r[0], "name": r[1], "path": (r[2] or "").replace("\\", "/"), "bytes": r[3]}
            for r in rows]


def dashboard(con: sqlite3.Connection, cat: Catalog, export_dir: Path) -> Dict[str, Any]:
    """Charts for the home page: record growth, pipeline, files and backup history."""
    top = [o for o in sorted(cat.objects.values(), key=lambda o: -o.rows)
           if o.rows and "CreatedDate" in o.fields][:6]
    months: Dict[str, Dict[str, int]] = {}
    latest = ""
    for o in top:
        r = con.execute(f"SELECT max({q('CreatedDate')}) FROM {q(o.name)}").fetchone()
        latest = max(latest, (r[0] or "")[:7])
    if latest:
        y, m = int(latest[:4]), int(latest[5:7])
        keys = []
        for _ in range(24):
            keys.append(f"{y:04d}-{m:02d}")
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        keys.reverse()
        for k in keys:
            months[k] = {}
        for o in top:
            for month, n in con.execute(
                    f"SELECT substr({q('CreatedDate')}, 1, 7) AS mo, count(*) FROM {q(o.name)} "
                    f"WHERE {q('CreatedDate')} >= ? GROUP BY mo", (keys[0],)):
                if month in months:
                    months[month][o.label] = n
    pipeline = []
    opp = cat.objects.get("Opportunity")
    if opp and opp.rows and {"StageName", "IsClosed"} <= set(opp.fields):
        amount = q("Amount") if "Amount" in opp.fields else "0"
        pipeline = [{"stage": r[0] or "(none)", "count": r[1], "amount": r[2] or 0} for r in con.execute(
            f"SELECT {q('StageName')}, count(*), sum({amount}) FROM {q('Opportunity')} "
            f"WHERE {q('IsClosed')} = 0 GROUP BY 1 ORDER BY 3 DESC, 2 DESC LIMIT 12")]
    files = []
    if cat.has_files:
        files = [{"kind": r[0], "count": r[1], "bytes": r[2] or 0} for r in con.execute(
            "SELECT kind, count(*), sum(bytes) FROM _files WHERE status LIKE 'ok%' GROUP BY 1 ORDER BY 3 DESC")]
    return {"series": [o.label for o in top], "months": months, "pipeline": pipeline,
            "files": files, "runs": changes(export_dir)}


def changes(export_dir: Path) -> List[Dict[str, Any]]:
    return [{"stamp": r.stamp, "at": r.finished_at, "kind": r.kind, "objects": r.objects,
             "total": sum(r.objects.values())} for r in reversed(hist.runs(export_dir))]


def changed_in_run(con: sqlite3.Connection, cat: Catalog, export_dir: Path, stamp: str,
                   obj: str) -> Dict[str, Any]:
    if not any(r.stamp == stamp for r in hist.runs(export_dir)):
        raise NotFound(stamp)
    if obj not in cat.objects:
        raise NotFound(obj)
    items = hist.changed_records(export_dir, stamp, obj)
    titles = cat.titles(con, [i["id"] for i in items])
    for i in items:
        i["title"] = titles.get(i["id"], {}).get("title") or i["id"]
        i["exists"] = i["id"] in titles
    return {"object": obj, "label": cat.objects[obj].label, "stamp": stamp, "records": items}


def record_history(con: sqlite3.Connection, cat: Catalog, export_dir: Path, rid: str) -> Dict[str, Any]:
    try:
        h = hist.record_history(export_dir, rid)
    except hist.HistoryError:
        return {"versions": []}
    o = cat.objects.get(h["object"])
    flds = o.fields if o else {}
    lookups = {k for k, f in flds.items() if f.type == "reference"}
    titles = cat.titles(con, [x for v in h["versions"] for k, pair in v["fields"].items()
                              if k in lookups for x in pair if x])
    for v in h["versions"]:
        rows = []
        for k, (a, b) in sorted(v["fields"].items()):
            item = {"name": k, "label": flds[k].label if k in flds else k,
                    "type": flds[k].type if k in flds else "", "old": a, "new": b}
            for side, value in (("old", a), ("new", b)):
                if k in lookups and value in titles:  # show the owner's name, not 005…
                    item[side], item[side + "_id"] = titles[value]["title"], value
            rows.append(item)
        v["fields"] = rows
        v.pop("values", None)
    h.pop("current", None)
    return h


# ------------------------------------------------------------------ HTTP server
class BrowseServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, export_dir: Path, port: int = 8484, host: str = "127.0.0.1"):
        self.export_dir = Path(export_dir).resolve()
        self.allowed_hosts = {"127.0.0.1", "localhost", host}
        with contextlib.closing(connect(self.export_dir)) as con:
            self.catalog = Catalog(con)
        self._dashboard: Optional[Tuple[int, Dict[str, Any]]] = None
        super().__init__((host, port), _Handler)

    def dashboard(self, con: sqlite3.Connection) -> Dict[str, Any]:
        stamp = index_path(self.export_dir).stat().st_mtime_ns
        if not self._dashboard or self._dashboard[0] != stamp:  # big tables: compute once
            self._dashboard = (stamp, dashboard(con, self.catalog, self.export_dir))
        return self._dashboard[1]

    @property
    def url(self) -> str:
        host = self.server_address[0]
        return f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{self.server_address[1]}/"

    def serve_in_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t


class _Handler(BaseHTTPRequestHandler):
    server: BrowseServer
    server_version = "sf-exit"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        pass

    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, default=str).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in self.server.allowed_hosts:  # blocks DNS-rebinding attacks
            return self._json({"error": "forbidden host"}, 403)
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = unquote(url.path)
        try:
            if path == "/":
                csp = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                       "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
                return self._send(200, PAGE_HTML.encode(), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": csp})
            if path.startswith("/files/"):
                return self._file(self.server.export_dir / "files", path[len("/files/"):])
            if path.startswith("/audit/"):
                return self._file(self.server.export_dir / "audit", path[len("/audit/"):] or "index.html",
                                  inline_html=True)
            if path.startswith("/api/"):
                with contextlib.closing(connect(self.server.export_dir)) as con:
                    return self._json(self._api(con, path[5:], qs))
            raise NotFound(path)
        except NotFound as exc:
            self._json({"error": f"not found: {exc}"}, 404)
        except (sqlite3.Error, ValueError) as exc:
            self._json({"error": str(exc)}, 400)

    def _api(self, con: sqlite3.Connection, route: str, qs: Dict[str, str]) -> Any:
        cat = self.server.catalog
        if route == "summary":
            return summary(con, cat, self.server.export_dir)
        if route == "search":
            return {"results": search(con, cat, qs.get("q", ""), 40)}
        if route.startswith("object/"):
            where = (qs["field"], qs["id"]) if "field" in qs and "id" in qs else None
            return object_list(con, cat, route[7:], int(qs.get("offset", 0)), qs.get("sort"),
                               qs.get("dir") == "desc", qs.get("q", ""), where)
        if route.startswith("record/"):
            return record(con, cat, route[7:])
        if route == "dashboard":
            return self.server.dashboard(con)
        if route.startswith("history/"):
            return record_history(con, cat, self.server.export_dir, route[8:])
        if route == "changes":
            return {"runs": changes(self.server.export_dir)}
        if route.startswith("changes/") and route.count("/") == 2:
            _, stamp, obj = route.split("/")
            return changed_in_run(con, cat, self.server.export_dir, stamp, obj)
        raise NotFound(route)

    def _file(self, root: Path, rel: str, inline_html: bool = False) -> None:
        root = root.resolve()
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file():
            raise NotFound(rel)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        headers = {}
        if inline_html and ctype == "text/html":
            ctype = "text/html; charset=utf-8"
        elif ctype not in INLINE_TYPES:
            # never render uploaded HTML/SVG/etc. in the page's origin: always download
            safe = target.name.replace('"', "").encode("ascii", "replace").decode()
            headers["Content-Disposition"] = f'attachment; filename="{safe}"'
            ctype = "application/octet-stream"
        size = target.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        with open(target, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                self.wfile.write(chunk)


PAGE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>sf-exit browse</title>
<style>
:root{--bg:#f4f6fa;--panel:#fff;--ink:#16202f;--muted:#5f6f87;--line:#e1e6ef;--accent:#0b5cd5;--accent-bg:#e8f0fd;--side:#0f1b2d;--side-ink:#c6d2e6;--side-hi:#1c2c47;--ok:#15803d}
@media (prefers-color-scheme:dark){:root{--bg:#0a101c;--panel:#111a2b;--ink:#e5ebf5;--muted:#8fa0bb;--line:#22304a;--accent:#6aa8ff;--accent-bg:#17274a;--side:#070c15;--side-hi:#152238}}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;display:grid;grid-template-columns:260px 1fr;grid-template-rows:54px 1fr}
header{grid-column:1/3;background:var(--side);color:#fff;display:flex;align-items:center;gap:16px;padding:0 18px}
header a.brand{color:#fff;text-decoration:none;font-weight:700;font-size:16px;font-family:ui-monospace,Menlo,Consolas,monospace}header a.brand b{color:#6aa8ff}
header .org{color:var(--side-ink);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
header form{flex:1;max-width:560px;margin-left:auto}header input{width:100%;padding:8px 12px;border-radius:8px;border:1px solid #2a3b59;background:#16233a;color:#fff;font:inherit}header input::placeholder{color:#8194b3}
.ro{font-size:11px;border:1px solid #2a3b59;border-radius:999px;padding:2px 9px;color:var(--side-ink);white-space:nowrap}
nav{background:var(--side);color:var(--side-ink);overflow:auto;padding:10px 8px 24px}
nav input{width:100%;padding:6px 10px;border-radius:7px;border:1px solid #2a3b59;background:#16233a;color:#fff;font:inherit;margin-bottom:8px}
nav a{display:flex;justify-content:space-between;gap:8px;padding:5px 10px;border-radius:7px;color:var(--side-ink);text-decoration:none;font-size:13px}
nav a:hover,nav a.on{background:var(--side-hi);color:#fff}nav a span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}nav a i{font-style:normal;color:#7f92b2;font-variant-numeric:tabular-nums;font-size:12px}
nav h4{margin:14px 10px 4px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#6f83a5}
main{overflow:auto;padding:22px 26px 60px}
h1{font-size:22px;margin:0 0 2px;letter-spacing:-.3px}h2{font-size:15px;margin:26px 0 10px}.crumb{color:var(--muted);font-size:12.5px;margin-bottom:4px}.crumb a,.muted a{color:var(--muted)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.muted{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin:16px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;display:block;color:inherit}.card:hover{border-color:var(--accent);text-decoration:none}.card .v{font-size:22px;font-weight:700}.card .k{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
th{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;cursor:pointer;user-select:none}tr:last-child td{border-bottom:0}tbody tr:hover td{background:var(--accent-bg)}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.bar{display:flex;gap:10px;align-items:center;margin:12px 0}.bar label{display:flex;gap:6px;align-items:center}.bar input[type=search]{padding:7px 11px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font:inherit;min-width:260px}
button{padding:6px 12px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font:inherit;cursor:pointer}button:disabled{opacity:.4;cursor:default}
.fields{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:0 28px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:6px 18px}
.f{display:grid;grid-template-columns:42% 58%;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)}.f .l{color:var(--muted);font-size:12.5px}.f .v{word-break:break-word;white-space:pre-wrap}
.pill{font-size:10.5px;border:1px solid var(--line);border-radius:6px;padding:0 5px;color:var(--muted);margin-left:6px;vertical-align:1px}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 12px}.tabs button.on{background:var(--accent-bg);border-color:var(--accent);color:var(--accent);font-weight:600}
.files a{display:inline-flex;gap:8px;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:7px 12px;margin:0 8px 8px 0}
.hit{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:8px;display:block;color:inherit}.hit:hover{border-color:var(--accent);text-decoration:none}.hit b{color:var(--accent)}
.yes{color:var(--ok);font-weight:700}
.navl{display:block;padding:6px 10px;border-radius:7px;color:var(--side-ink);font-size:13px;font-weight:600}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px;margin:6px 0 10px}
.chart{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}.chart h3{margin:0 0 10px;font-size:13px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.chart svg{width:100%;height:auto;display:block}.chart svg text{fill:var(--muted);font-size:10px}
.legend{display:flex;flex-wrap:wrap;gap:4px 12px;margin-top:8px;font-size:12px;color:var(--muted)}.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.hbar{display:grid;grid-template-columns:150px 1fr 110px;gap:8px;align-items:center;margin:5px 0;font-size:12.5px}.hbar .t{height:14px;background:var(--accent-bg);border-radius:4px;overflow:hidden}.hbar .t i{display:block;height:100%;background:var(--accent)}.hbar span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.hbar b{text-align:right;font-weight:600;font-variant-numeric:tabular-nums}
.tl{border-left:2px solid var(--line);margin:6px 0 0 8px;padding-left:16px}.tl .ev{position:relative;margin:0 0 14px}.tl .ev:before{content:"";position:absolute;left:-23px;top:5px;width:10px;height:10px;border-radius:50%;background:var(--accent)}
.tl .ev.created:before{background:var(--ok)}.tl .ev.deleted:before{background:#dc2626}
.badge{font-size:10.5px;font-weight:700;text-transform:uppercase;border-radius:999px;padding:1px 8px;color:#fff;background:var(--accent);margin-right:6px}.badge.created{background:var(--ok)}.badge.deleted{background:#dc2626}
.chg{font-size:12.5px;margin-top:5px;table-layout:fixed}.chg td:first-child{width:24%}.chg td{white-space:normal;max-width:none;padding:5px 10px}.chg .old{color:#b91c1c;text-decoration:line-through}.chg .new{color:var(--ok)}.chg a{color:inherit}
</style></head><body>
<header><a class="brand" href="#/">sf<b>-</b>exit</a><span class="org" id="org"></span><span class="ro">read-only · offline</span>
<form id="sf"><input id="sq" type="search" placeholder="Search every record…  (press /)" autocomplete="off"></form></header>
<nav><a class="navl" href="#/">Home</a><a class="navl" id="navc" href="#/changes" hidden>Changes</a><a class="navl" id="nava" href="/audit/index.html" target="_blank" hidden>Audit report ↗</a>
<input id="of" type="search" placeholder="Filter objects…" style="margin-top:8px"><div id="objs"></div></nav>
<main id="main"></main>
<script>
const $=s=>document.querySelector(s),main=$("#main");let S=null;
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num=n=>Number(n).toLocaleString();
const bytes=n=>{n=Number(n||0);const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<4){n/=1024;i++}return(i?n.toFixed(1):n)+" "+u[i]};
const api=async p=>{const r=await fetch("/api/"+p);const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j};
const enc=encodeURIComponent;
function fmt(v,type){if(v===null||v===undefined||v==="")return'<span class="muted">–</span>';
 if(typeof v==="object")return v.object?`<a href="#/r/${enc(v.id)}">${esc(v.title)}</a>`:esc(v.id);
 if(type==="boolean")return v==1||v==="true"?'<span class="yes">✓</span>':'<span class="muted">✗</span>';
 if(type==="currency"||type==="double"||type==="percent"||type==="int"||type==="long")return isNaN(v)?esc(v):Number(v).toLocaleString(undefined,{maximumFractionDigits:2})+(type==="percent"?"%":"");
 if(type==="datetime")return esc(String(v).replace("T"," ").slice(0,16));
 if(type==="email")return`<a href="mailto:${esc(v)}">${esc(v)}</a>`;
 if(type==="url"&&/^https?:\/\//i.test(v))return`<a href="${esc(v)}" target="_blank" rel="noopener noreferrer">${esc(v)}</a>`;
 return esc(v)}
function nav(filter=""){const f=filter.toLowerCase();const mk=o=>`<a href="#/o/${enc(o.name)}" data-o="${esc(o.name)}" title="${esc(o.name)}"><span>${esc(o.label)}</span><i>${num(o.rows)}</i></a>`;
 const has=S.objects.filter(o=>o.rows&&(!f||o.name.toLowerCase().includes(f)||o.label.toLowerCase().includes(f)));
 const std=has.filter(o=>!o.custom),cus=has.filter(o=>o.custom);
 $("#objs").innerHTML=(cus.length?"<h4>Custom objects</h4>"+cus.map(mk).join(""):"")+"<h4>Standard objects</h4>"+std.map(mk).join("");mark()}
function mark(){const m=location.hash.match(/^#\/o\/([^?]+)/);document.querySelectorAll("nav a").forEach(a=>a.classList.toggle("on",!!m&&a.dataset.o===decodeURIComponent(m[1])))}
function table(d,sortable,cur){const th=d.columns.map(c=>`<th data-c="${esc(c.name)}">${esc(c.label)}${cur&&cur.sort===c.name?(cur.dir==="desc"?" ↓":" ↑"):""}</th>`).join("");
 const tr=d.rows.map(r=>"<tr>"+d.columns.map((c,i)=>{const v=r.values[c.name];const inner=i===0&&c.type!=="reference"?`<a href="#/r/${enc(r.id)}">${v===null||v===""?esc(r.id):fmt(v,c.type)}</a>`:fmt(v,c.type);return`<td class="${["currency","double","int","percent"].includes(c.type)?"n":""}">${inner}</td>`}).join("")+"</tr>").join("");
 return`<table><thead><tr>${th}</tr></thead><tbody>${tr||`<tr><td colspan="${d.columns.length}" class="muted">No records</td></tr>`}</tbody></table>`}
function pager(d,go){const a=d.offset+1,b=Math.min(d.offset+d.page,d.total);const el=document.createElement("div");el.className="bar";
 el.innerHTML=`<button ${d.offset?"":"disabled"} data-d="-1">← Prev</button><span class="muted">${d.total?num(a)+"–"+num(b):0} of ${num(d.total)}</span><button ${b<d.total?"":"disabled"} data-d="1">Next →</button>`;
 el.addEventListener("click",e=>{const x=e.target.dataset.d;if(x)go(d.offset+x*d.page)});return el}
const COLORS=["#2563eb","#16a34a","#d97706","#9333ea","#0891b2","#dc2626"];
function columns(months,series){const keys=Object.keys(months);if(!keys.length||!series.length)return"";
 const totals=keys.map(k=>series.reduce((a,s)=>a+(months[k][s]||0),0)),max=Math.max(1,...totals),W=640,H=190,P=26,bw=(W-P)/keys.length;
 let g="";keys.forEach((k,i)=>{let y=H-16;series.forEach((s,j)=>{const v=months[k][s]||0;if(!v)return;const h=(H-30)*v/max;y-=h;g+=`<rect x="${P+i*bw+2}" y="${y}" width="${Math.max(1,bw-4)}" height="${h}" fill="${COLORS[j%6]}" rx="2"><title>${esc(k)} · ${esc(s)}: ${num(v)}</title></rect>`});
  if(i%3===0)g+=`<text x="${P+i*bw+bw/2}" y="${H-3}" text-anchor="middle">${esc(k.slice(2))}</text>`});
 g+=`<text x="0" y="12">${num(max)}</text><line x1="${P}" x2="${W}" y1="${H-16}" y2="${H-16}" stroke="currentColor" opacity=".15"/>`;
 return`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Records created per month">${g}</svg><div class="legend">${series.map((s,j)=>`<span><i style="background:${COLORS[j%6]}"></i>${esc(s)}</span>`).join("")}</div>`}
function hbars(items,label,value,fmtv){const max=Math.max(1,...items.map(value));return items.map(it=>`<div class="hbar"><span title="${esc(label(it))}">${esc(label(it))}</span><div class="t"><i style="width:${100*value(it)/max}%"></i></div><b>${fmtv(it)}</b></div>`).join("")}
async function home(){document.title="sf-exit browse";const top=S.objects.filter(o=>o.rows).slice(0,8);
 main.innerHTML=`<h1>${esc(S.org.name||"Salesforce export")}</h1><div class="muted">${esc([S.org.edition,S.exported_at?"exported "+S.exported_at.slice(0,10):""].filter(Boolean).join(" · "))}</div>
 <div class="cards"><div class="card"><div class="v">${num(S.records)}</div><div class="k">records</div></div><div class="card"><div class="v">${S.objects.filter(o=>o.rows).length}</div><div class="k">objects with data</div></div>${S.files?`<div class="card"><div class="v">${num(S.files.count)}</div><div class="k">files · ${bytes(S.files.bytes)}</div></div>`:""}${S.has_audit?`<a class="card" href="/audit/index.html" target="_blank"><div class="v">Audit →</div><div class="k">health score &amp; data dictionary</div></a>`:""}</div>
 <div class="charts" id="charts"><div class="chart"><h3>Largest objects</h3>${hbars(top,o=>o.label,o=>o.rows,o=>num(o.rows))}</div></div>`;
 const d=await api("dashboard");const c=$("#charts");if(!c)return;
 if(Object.keys(d.months).length)c.insertAdjacentHTML("afterbegin",`<div class="chart"><h3>Records created per month</h3>${columns(d.months,d.series)}</div>`);
 if(d.pipeline.length)c.insertAdjacentHTML("beforeend",`<div class="chart"><h3>Open pipeline by stage</h3>${hbars(d.pipeline,p=>p.stage,p=>Number(p.amount)||p.count,p=>(Number(p.amount)?Number(p.amount).toLocaleString(undefined,{notation:"compact",maximumFractionDigits:1})+" · ":"")+num(p.count))}</div>`);
 if(d.files.length)c.insertAdjacentHTML("beforeend",`<div class="chart"><h3>Files</h3>${hbars(d.files,f=>f.kind,f=>f.bytes,f=>num(f.count)+" · "+bytes(f.bytes))}</div>`);
 if(d.runs.length)c.insertAdjacentHTML("beforeend",`<div class="chart"><h3>Recent changes (backup history)</h3>${hbars(d.runs.slice(0,8),r=>r.at.replace("T"," ").slice(0,16),r=>r.total,r=>`<a href="#/changes">${num(r.total)}</a>`)}</div>`)}
async function list(name,params){const cur={q:params.get("q")||"",sort:params.get("sort")||"",dir:params.get("dir")||"asc",offset:+(params.get("offset")||0)};
 const d=await api(`object/${enc(name)}?offset=${cur.offset}&q=${enc(cur.q)}&sort=${enc(cur.sort)}&dir=${cur.dir}`);document.title=d.label+" · sf-exit";
 const go=o=>{const p=new URLSearchParams();for(const[k,v]of Object.entries({...cur,...o}))if(v)p.set(k,v);location.hash=`#/o/${enc(name)}?${p}`};
 main.innerHTML=`<div class="crumb"><a href="#/">Home</a> / ${esc(name)}</div><h1>${esc(d.label)}</h1><div class="bar"><input id="lq" type="search" placeholder="Filter by ${esc(d.title_field||"Id")}…" value="${esc(cur.q)}"><span class="muted">${num(d.total)} records</span></div>${table(d,true,cur)}`;
 main.appendChild(pager(d,o=>go({offset:o})));
 $("#lq").addEventListener("change",e=>go({q:e.target.value,offset:0}));
 main.querySelector("thead").addEventListener("click",e=>{const c=e.target.dataset.c;if(c)go({sort:c,dir:cur.sort===c&&cur.dir==="asc"?"desc":"asc",offset:0})})}
async function rec(id){const d=await api("record/"+enc(id));document.title=d.title+" · sf-exit";let hideEmpty=true;
 const fields=()=>d.fields.filter(f=>!hideEmpty||!(f.value===null||f.value==="")).map(f=>`<div class="f"><div class="l" title="${esc(f.name+(f.help?" — "+f.help:""))}">${esc(f.label)}${f.custom?'<span class="pill">custom</span>':""}</div><div class="v">${fmt(f.ref||f.value,f.type)}</div></div>`).join("");
 main.innerHTML=`<div class="crumb"><a href="#/">Home</a> / <a href="#/o/${enc(d.object)}">${esc(d.label)}</a> / <span>${esc(d.id)}</span></div><h1>${esc(d.title)}</h1>
 <div class="bar"><label class="muted"><input type="checkbox" id="he" checked> Hide empty fields</label><span class="muted">${d.fields.length} fields</span></div><div class="fields" id="fl">${fields()}</div>
 ${d.files.length?`<h2>Files (${d.files.length})</h2><div class="files">${d.files.map(f=>`<a href="/files/${f.path.replace(/^files\//,"").split("/").map(enc).join("/")}" target="_blank">📎 ${esc(f.name)} <span class="muted">${bytes(f.bytes)}</span></a>`).join("")}</div>`:""}
 ${d.related.length?`<h2>Related</h2><div class="tabs" id="tabs">${d.related.map((r,i)=>`<button data-i="${i}">${esc(r.label)}${d.related.filter(x=>x.object===r.object).length>1?" ("+esc(r.field_label)+")":""} · ${num(r.count)}</button>`).join("")}</div><div id="rel"></div>`:""}`;
 $("#he").addEventListener("change",e=>{hideEmpty=e.target.checked;$("#fl").innerHTML=fields()});
 const show=async(i,offset=0)=>{const r=d.related[i];document.querySelectorAll("#tabs button").forEach(b=>b.classList.toggle("on",+b.dataset.i===i));
  const l=await api(`object/${enc(r.object)}?field=${enc(r.field)}&id=${enc(id)}&offset=${offset}`);const box=$("#rel");box.innerHTML=table(l);if(l.total>l.page)box.appendChild(pager(l,o=>show(i,o)))};
 if(d.related.length){$("#tabs").addEventListener("click",e=>{if(e.target.dataset.i)show(+e.target.dataset.i)});show(0)}
 const h=await api("history/"+enc(id));if(h.versions&&h.versions.length&&main.querySelector("h1"))main.insertAdjacentHTML("beforeend",`<h2>History (${h.versions.length})</h2>${timeline(h.versions)}<div class="muted">Recorded since ${esc((h.baseline||"").slice(0,10))} by sf-exit backups.</div>`)}
function val(f,s){return f[s+"_id"]?`<a href="#/r/${enc(f[s+"_id"])}">${esc(f[s])}</a>`:fmt(f[s],f.type)}
function timeline(vs){return`<div class="tl">${vs.slice().reverse().map(v=>`<div class="ev ${esc(v.change)}"><span class="badge ${esc(v.change)}">${esc(v.change)}</span><span class="muted">${esc(v.at.replace("T"," ").slice(0,16))}</span>${v.fields&&v.fields.length?`<table class="chg"><tbody>${v.fields.map(f=>`<tr><td>${esc(f.label)}</td><td class="old">${val(f,"old")}</td><td class="new">${val(f,"new")}</td></tr>`).join("")}</tbody></table>`:""}</div>`).join("")}</div>`}
async function changesPage(){const d=await api("changes");document.title="Changes · sf-exit";
 main.innerHTML=`<div class="crumb"><a href="#/">Home</a> / Changes</div><h1>What changed</h1><div class="muted" style="margin-bottom:12px">Every backup run into this folder records the records it saw created, changed or deleted.</div>`+(d.runs.length?d.runs.map(r=>`<div class="chart" style="margin-bottom:12px"><h3>${esc(r.at.replace("T"," ").slice(0,16))} · ${esc(r.kind)} · ${num(r.total)} records</h3>${hbars(Object.entries(r.objects).map(([o,n])=>({o,n})).sort((a,b)=>b.n-a.n),x=>(S.objects.find(o=>o.name===x.o)||{}).label||x.o,x=>x.n,x=>`<a href="#/changes/${enc(r.stamp)}/${enc(x.o)}">${num(x.n)}</a>`)}</div>`).join(""):'<p class="muted">No history yet: run the export again into the same folder (for example nightly with --incremental) and changes appear here.</p>')}
async function changedPage(stamp,obj){const d=await api(`changes/${enc(stamp)}/${enc(obj)}`);document.title=d.label+" changes · sf-exit";
 main.innerHTML=`<div class="crumb"><a href="#/">Home</a> / <a href="#/changes">Changes</a> / ${esc(d.label)}</div><h1>${esc(d.label)}: ${num(d.records.length)} changed</h1><table><thead><tr><th>Record</th><th>Change</th><th>Fields</th></tr></thead><tbody>${d.records.map(r=>`<tr><td>${r.exists?`<a href="#/r/${enc(r.id)}">${esc(r.title)}</a>`:esc(r.title)}</td><td><span class="badge ${esc(r.change)}">${esc(r.change)}</span></td><td class="muted">${esc(r.fields.join(", "))}</td></tr>`).join("")}</tbody></table>`}
async function find(text){$("#sq").value=text;const d=await api("search?q="+enc(text));document.title=text+" · search";
 main.innerHTML=`<h1>Search: ${esc(text)}</h1><div class="muted" style="margin-bottom:14px">${d.results.length} result${d.results.length===1?"":"s"}${d.results.length>=40?" (showing the best 40)":""}</div>`+d.results.map(r=>`<a class="hit" href="#/r/${enc(r.id)}"><b>${esc(r.title)}</b> <span class="pill">${esc((S.objects.find(o=>o.name===r.object)||{}).label||r.object)}</span><div class="muted">${esc(r.extra)}</div></a>`).join("")}
async function route(){const h=location.hash||"#/";mark();try{
 if(h==="#/changes"){await changesPage();return}
 if(h.startsWith("#/changes/")){const[st,ob]=h.slice(10).split("/").map(decodeURIComponent);await changedPage(st,ob);return}
 if(h.startsWith("#/o/")){const[p,qs]=h.slice(4).split("?");await list(decodeURIComponent(p),new URLSearchParams(qs||""))}
 else if(h.startsWith("#/r/"))await rec(decodeURIComponent(h.slice(4)));
 else if(h.startsWith("#/s/"))await find(decodeURIComponent(h.slice(4)));
 else await home();main.scrollTop=0}catch(e){main.innerHTML=`<h1>Not found</h1><p class="muted">${esc(e.message)}</p>`}}
(async()=>{S=await api("summary");$("#navc").hidden=!S.has_history;$("#nava").hidden=!S.has_audit;$("#org").textContent=[S.org.name,S.exported_at?"export of "+S.exported_at.slice(0,10):""].filter(Boolean).join(" · ");nav();
 $("#of").addEventListener("input",e=>nav(e.target.value));$("#sf").addEventListener("submit",e=>{e.preventDefault();const v=$("#sq").value.trim();if(v)location.hash="#/s/"+enc(v)});
 document.addEventListener("keydown",e=>{if(e.key==="/"&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName)){e.preventDefault();$("#sq").focus()}});
 window.addEventListener("hashchange",route);route()})();
</script></body></html>
"""

"""``sf-exit mcp``: let an AI assistant answer questions from an export, offline.

A Model Context Protocol server over stdio (newline-delimited JSON-RPC 2.0), implemented
with the standard library only. It exposes read-only tools over the local SQLite index:
nothing can be modified and nothing is sent to Salesforce.

Client configuration (any MCP client)::

    {"mcpServers": {"salesforce-export": {"command": "sf-exit", "args": ["mcp", "/path/to/export"]}}}
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import IO, Any, Callable, Dict, List, Optional

from sf_exit import __version__
from sf_exit.browse import NotFound, record
from sf_exit.index import Catalog, connect, search

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_ROWS = 200
MAX_CELL = 2000

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "list_objects",
        "description": "List the Salesforce objects (tables) in the export with labels and row counts. "
                       "Call this first to learn what data exists.",
        "inputSchema": {"type": "object", "properties": {
            "include_empty": {"type": "boolean", "description": "Also list objects with no records."}}},
    },
    {
        "name": "describe_object",
        "description": "Fields of one object: API name, label, type, help text, picklist values and "
                       "which objects a lookup points to. Use it before writing SQL.",
        "inputSchema": {"type": "object", "required": ["object"], "properties": {
            "object": {"type": "string", "description": "Object API name, e.g. Opportunity"}}},
    },
    {
        "name": "query",
        "description": "Run a read-only SQL query (SQLite dialect) over the export. Each object is a table "
                       "named by its API name; columns are field API names; Ids join tables "
                       "(Contact.AccountId = Account.Id). Booleans are 0/1, dates are ISO strings. "
                       f"At most {MAX_ROWS} rows are returned, so aggregate or LIMIT.",
        "inputSchema": {"type": "object", "required": ["sql"], "properties": {
            "sql": {"type": "string", "description": "A single SELECT (or WITH ... SELECT) statement."}}},
    },
    {
        "name": "search",
        "description": "Full-text search across record names and key text fields of every object.",
        "inputSchema": {"type": "object", "required": ["text"], "properties": {
            "text": {"type": "string"},
            "object": {"type": "string", "description": "Optional: only search this object."},
            "limit": {"type": "integer", "default": 20}}},
    },
    {
        "name": "get_record",
        "description": "One record by its Salesforce Id: all non-empty fields with lookups resolved to "
                       "names, counts of related records, and attached files.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "get_record_history",
        "description": "Every recorded version of one record across backup runs: when it was created, "
                       "which fields changed (old -> new) and when it was deleted.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "audit_findings",
        "description": "Data-quality findings from `sf-exit audit` (unused fields, duplicates, overdue "
                       "pipeline...) if the audit has been run for this export.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class ToolError(Exception):
    pass


class ExportTools:
    """The tool implementations, independent of the wire protocol (and easy to test)."""

    def __init__(self, export_dir: Path):
        self.export_dir = Path(export_dir)
        self.con = connect(self.export_dir)
        self.catalog = Catalog(self.con)

    def list_objects(self, include_empty: bool = False) -> Any:
        objs = sorted(self.catalog.objects.values(), key=lambda o: (-o.rows, o.name))
        return [{"object": o.name, "label": o.label, "rows": o.rows, "custom": o.custom}
                for o in objs if o.rows or include_empty]

    def describe_object(self, object: str) -> Any:  # noqa: A002 - matches the tool schema
        o = self._object(object)
        return {"object": o.name, "label": o.label, "rows": o.rows, "name_field": o.title_field,
                "fields": [{k: v for k, v in {
                    "name": f.name, "label": f.label, "type": f.type, "help": f.help,
                    "references": f.refs, "picklist_values": f.picklist}.items() if v}
                    for f in o.fields.values()]}

    def query(self, sql: str) -> Any:
        statement = sql.strip().rstrip(";").strip()
        if not statement.lower().startswith(("select", "with")):
            raise ToolError("Only SELECT statements are allowed: this export is read-only.")
        try:
            cur = self.con.execute(statement)
            rows = cur.fetchmany(MAX_ROWS + 1)
        except sqlite3.Error as exc:
            raise ToolError(f"SQL error: {exc}. Table names are object API names; call "
                            "describe_object for column names.") from exc
        cols = [d[0] for d in cur.description or []]
        truncated = len(rows) > MAX_ROWS
        out = [{c: _clip(v) for c, v in zip(cols, r, strict=True)} for r in rows[:MAX_ROWS]]
        return {"columns": cols, "rows": out, "row_count": len(out), "truncated": truncated}

    def search(self, text: str, object: Optional[str] = None, limit: int = 20) -> Any:  # noqa: A002
        if object:
            self._object(object)
        return search(self.con, self.catalog, text, max(1, min(int(limit), 100)), object)

    def get_record(self, id: str) -> Any:  # noqa: A002
        try:
            r = record(self.con, self.catalog, id)
        except NotFound as exc:
            raise ToolError(f"No record with Id {id} in this export.") from exc
        fields = {}
        for f in r["fields"]:
            if f["value"] in (None, ""):
                continue
            ref = f.get("ref")
            fields[f["name"]] = ({"id": ref["id"], "name": ref["title"], "object": ref["object"]}
                                 if ref else _clip(f["value"]))
        return {"object": r["object"], "id": r["id"], "name": r["title"], "fields": fields,
                "related": [{"object": x["object"], "via": x["field"], "count": x["count"]}
                            for x in r["related"]],
                "files": r["files"]}

    def get_record_history(self, id: str) -> Any:  # noqa: A002
        from sf_exit.history import HistoryError, record_history

        try:
            h = record_history(self.export_dir, id)
        except HistoryError as exc:
            raise ToolError(str(exc)) from exc
        return {"object": h["object"], "id": id, "exists_now": bool(h["current"]),
                "history_since": h["baseline"],
                "versions": [{"at": v["at"], "change": v["change"],
                              "fields": {k: {"old": _clip(a), "new": _clip(b)}
                                         for k, (a, b) in v["fields"].items()}}
                             for v in h["versions"]]}

    def audit_findings(self) -> Any:
        path = self.export_dir / "audit" / "audit.json"
        if not path.exists():
            raise ToolError(f"No audit yet. Run: sf-exit audit {self.export_dir}")
        report = json.loads(path.read_text("utf-8"))
        return {"summary": report["summary"],
                "findings": [{**f, "items": f["items"][:25]} for f in report["findings"]]}

    def _object(self, name: str):
        o = self.catalog.objects.get(name)
        if not o:
            lowered = {k.lower(): k for k in self.catalog.objects}
            if name.lower() in lowered:
                return self.catalog.objects[lowered[name.lower()]]
            raise ToolError(f"Unknown object '{name}'. Call list_objects to see what exists.")
        return o


def _clip(v: Any) -> Any:
    if isinstance(v, str) and len(v) > MAX_CELL:
        return v[:MAX_CELL] + f"… [{len(v) - MAX_CELL} more characters]"
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    return v


class McpServer:
    def __init__(self, tools: ExportTools):
        self.tools = tools
        self.handlers: Dict[str, Callable[..., Any]] = {t["name"]: getattr(tools, t["name"]) for t in TOOLS}

    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Process one JSON-RPC message; returns the response, or None for notifications."""
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # notification (e.g. notifications/initialized): nothing to send
            return None
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                wanted = params.get("protocolVersion")
                result: Any = {
                    "protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "sf-exit", "version": __version__},
                    "instructions": "Read-only access to a Salesforce export. Start with list_objects, "
                                    "then describe_object, then query with SQL.",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._call(params.get("name"), params.get("arguments") or {})
            elif method in ("resources/list", "prompts/list"):
                result = {method.split("/")[0]: []}
            else:
                return {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32601, "message": f"Method not found: {method}"}}
        except Exception as exc:  # noqa: BLE001 - never crash the server on one bad request
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _call(self, name: Optional[str], args: Dict[str, Any]) -> Dict[str, Any]:
        fn = self.handlers.get(name or "")
        if not fn:
            return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
        try:
            payload = fn(**args)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except TypeError as exc:
            return {"content": [{"type": "text", "text": f"Bad arguments: {exc}"}], "isError": True}
        text = json.dumps(payload, default=str, ensure_ascii=False, indent=1)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    def serve(self, stdin: Optional[IO[str]] = None, stdout: Optional[IO[str]] = None) -> None:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                resp: Optional[Dict[str, Any]] = {"jsonrpc": "2.0", "id": None,
                                                  "error": {"code": -32700, "message": "Parse error"}}
            else:
                resp = self.handle(msg) if isinstance(msg, dict) else None
            if resp is not None:
                stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                stdout.flush()


def serve(export_dir: Path) -> None:
    tools = ExportTools(export_dir)
    with contextlib.closing(tools.con):
        McpServer(tools).serve()

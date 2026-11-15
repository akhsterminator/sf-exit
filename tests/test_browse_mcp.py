"""The local index, `sf-exit browse` and the MCP server."""

import io
import json
import urllib.error
import urllib.request

import pytest

from sf_exit.browse import BrowseServer, NotFound, object_list, record
from sf_exit.exporter import Exporter, ExportOptions
from sf_exit.index import Catalog, connect, ensure_index, index_path, search
from sf_exit.mcp import ExportTools, McpServer, ToolError

from .fake_org import build_org, client_for

ACME = "001000000000001AAA"


@pytest.fixture(scope="module")
def export_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("exp")
    Exporter(client_for(build_org()), ExportOptions(out_dir=out, poll_interval=0)).run()
    ensure_index(out)
    return out


@pytest.fixture()
def db(export_dir):
    con = connect(export_dir)
    yield con, Catalog(con)
    con.close()


def test_index_is_reused_and_rebuilt_when_export_changes(export_dir):
    path = index_path(export_dir)
    before = path.stat().st_mtime_ns
    assert ensure_index(export_dir) == path and path.stat().st_mtime_ns == before
    manifest = export_dir / "manifest.json"
    manifest.write_text(manifest.read_text() + "\n")
    ensure_index(export_dir)
    assert path.stat().st_mtime_ns != before


def test_index_is_read_only(db):
    con, _ = db
    with pytest.raises(Exception, match="readonly|read-only|query_only"):
        con.execute('DELETE FROM "Account"')


def test_catalog_and_titles(db):
    con, cat = db
    assert cat.objects["Account"].title_field == "Name" and cat.objects["Account"].key_prefix == "001"
    assert cat.object_for_id(con, ACME) == "Account"
    assert ("Contact", "AccountId") in cat.children["Account"]
    assert cat.titles(con, [ACME, "005000000000002AAA"]) == {
        ACME: {"object": "Account", "title": "Account 1"},
        "005000000000002AAA": {"object": "User", "title": "User 2"}}


def test_search(db):
    con, cat = db
    hits = search(con, cat, "contract")
    assert hits and hits[0]["object"] == "ContentDocument"
    assert search(con, cat, "acc 12")[0]["title"] == "Account 12"  # prefix matching
    assert search(con, cat, 'weird "quote AND (', 5) == []  # never a syntax error
    assert all(h["object"] == "Opportunity" for h in search(con, cat, "deal", obj="Opportunity"))


def test_object_list_sort_filter_and_lookup_resolution(db):
    con, cat = db
    d = object_list(con, cat, "Opportunity", sort="Amount", desc=True)
    assert d["total"] == 30 and len(d["rows"]) == 30
    amounts = [r["values"]["Amount"] for r in d["rows"]]
    assert amounts == sorted(amounts, reverse=True)
    assert d["rows"][0]["values"]["AccountId"]["object"] == "Account"
    assert object_list(con, cat, "Account", text="Account 2")["total"] == 7  # 2, 20..25
    assert object_list(con, cat, "Account", text="100%")["total"] == 0  # LIKE wildcards escaped
    rel = object_list(con, cat, "Contact", where=("AccountId", ACME))
    assert rel["total"] == 1 and "AccountId" not in [c["name"] for c in rel["columns"]]
    with pytest.raises(NotFound):
        object_list(con, cat, "Nope")


def test_record_page(db):
    con, cat = db
    r = record(con, cat, ACME)
    assert r["object"] == "Account" and r["title"] == "Account 1"
    owner = next(f for f in r["fields"] if f["name"] == "OwnerId")
    assert owner["ref"]["title"] == "User 2"
    related = {(x["object"], x["field"]): x["count"] for x in r["related"]}
    assert related[("Contact", "AccountId")] == 1 and related[("Account", "ParentId")] == 1
    assert {f["kind"] for f in r["files"]} == {"Attachment", "ContentVersion"}
    with pytest.raises(NotFound):
        record(con, cat, "001000000000999AAA")


def test_http_server(export_dir):
    srv = BrowseServer(export_dir, 0)
    srv.serve_in_thread()
    try:
        def get(path, **headers):
            return urllib.request.urlopen(urllib.request.Request(srv.url + path, headers=headers))

        page = get("")
        assert b"sf-exit browse" in page.read()
        assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
        s = json.load(get("api/summary"))
        assert s["org"]["name"] == "Acme Corp" and s["files"]["count"] == 5
        assert json.load(get("api/record/" + ACME))["title"] == "Account 1"
        assert json.load(get("api/search?q=deal"))["results"]
        f = record(connect(export_dir), srv.catalog, ACME)["files"][0]
        resp = get("files/" + urllib.request.quote(f["path"][len("files/"):]))
        assert resp.status == 200 and len(resp.read()) == f["bytes"]
        for bad in ("files/../manifest.json", "files/%2e%2e/manifest.json",
                    "files/%2e%2e%2f.sf-exit%2findex.db", "api/object/Nope", "api/record/zzz", "nope"):
            with pytest.raises(urllib.error.HTTPError) as e:
                get(bad)
            assert e.value.code == 404, bad
        with pytest.raises(urllib.error.HTTPError) as e:  # DNS-rebinding protection
            get("api/summary", Host="evil.example")
        assert e.value.code == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_mcp_tools(export_dir):
    t = ExportTools(export_dir)
    names = [o["object"] for o in t.list_objects()]
    assert names[0] == "Contact" and "EmptyThing__c" not in names
    assert "EmptyThing__c" in [o["object"] for o in t.list_objects(include_empty=True)]
    d = t.describe_object("opportunity")  # case-insensitive
    stage = next(f for f in d["fields"] if f["name"] == "StageName")
    assert "Closed Won" in stage["picklist_values"]
    q = t.query('SELECT StageName, count(*) AS n FROM Opportunity GROUP BY 1 ORDER BY 1;')
    assert q["columns"] == ["StageName", "n"] and q["row_count"] == 4 and not q["truncated"]
    assert t.query("SELECT Id FROM Contact")["row_count"] == 40
    for bad in ('DELETE FROM "Account"', "PRAGMA writable_schema=1", "SELECT * FROM Nope"):
        with pytest.raises(ToolError):
            t.query(bad)
    rec = t.get_record(ACME)
    assert rec["name"] == "Account 1" and rec["fields"]["OwnerId"]["name"] == "User 2"
    with pytest.raises(ToolError):
        t.get_record("nope")
    with pytest.raises(ToolError, match="sf-exit audit"):
        t.audit_findings()
    t.con.close()


def test_mcp_protocol(export_dir):
    tools = ExportTools(export_dir)
    server = McpServer(tools)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "query", "arguments": {"sql": "SELECT count(*) AS n FROM Account"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "query", "arguments": {"sql": "DROP TABLE Account"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}},
        {"jsonrpc": "2.0", "id": 6, "method": "does/not/exist"},
        {"jsonrpc": "2.0", "id": 7, "method": "ping"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\nnot json\n")
    stdout = io.StringIO()
    server.serve(stdin, stdout)
    out = [json.loads(line) for line in stdout.getvalue().splitlines()]
    by_id = {o["id"]: o for o in out}
    assert len(out) == 8  # the notification gets no reply; the bad line gets a parse error
    assert by_id[1]["result"]["protocolVersion"] == "2025-03-26"
    assert by_id[1]["result"]["serverInfo"]["name"] == "sf-exit"
    assert {t["name"] for t in by_id[2]["result"]["tools"]} >= {"query", "search", "get_record"}
    assert all("inputSchema" in t for t in by_id[2]["result"]["tools"])
    payload = json.loads(by_id[3]["result"]["content"][0]["text"])
    assert payload["rows"] == [{"n": 24}] and by_id[3]["result"]["isError"] is False
    assert by_id[4]["result"]["isError"] is True and by_id[5]["result"]["isError"] is True
    assert by_id[6]["error"]["code"] == -32601 and by_id[7]["result"] == {}
    assert by_id[None]["error"]["code"] == -32700
    tools.con.close()

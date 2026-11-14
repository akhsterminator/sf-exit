import pytest

from sf_exit.client import SalesforceClient, SalesforceError, soql_quote
from sf_exit.discovery import plan_object, select_objects
from sf_exit.exporter import _fmt_value
from sf_exit.loader import LoadError, parse_target
from sf_exit.schema import create_table_sql
from sf_exit.utils import human_bytes, safe_filename

from .fake_org import FakeOrg, build_org, client_for


@pytest.mark.parametrize("raw,expected", [
    ("Contract 1/final", "Contract 1_final"),
    ('a<b>c:d"e|f?g*h', "a_b_c_d_e_f_g_h"),
    ("  .hidden. ", "hidden"),
    ("", "file"),
    ("CON", "_CON"),
    ("x" * 300 + ".pdf", "x" * 116 + ".pdf"),
])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_human_bytes():
    assert human_bytes(512) == "512 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(5 * 1024**3) == "5.0 GB"


def test_fmt_value_normalises_rest_to_bulk():
    assert _fmt_value(None) == ""
    assert _fmt_value(True) == "true"
    assert _fmt_value("2024-01-02T03:04:05.000+0000") == "2024-01-02T03:04:05.000Z"
    assert _fmt_value(12) == "12"


def test_soql_quote():
    assert soql_quote("O'Brien\\") == "'O\\'Brien\\\\'"


def test_select_objects_include_exclude():
    g = [{"name": n, "queryable": True} for n in
         ["Account", "AccountHistory", "AccountShare", "Invoice__c", "Vote", "Ext__x", "Big__b",
          "Thing__e", "ContentDocumentLink"]]
    chosen, skipped = select_objects(g, exclude=["*history", "*Share"])
    names = [o["name"] for o in chosen]
    assert names == ["Account", "Invoice__c"]
    assert "AccountHistory" in skipped and "Vote" in skipped and "Ext__x" in skipped
    chosen, _ = select_objects(g, include=["inv*"])
    assert [o["name"] for o in chosen] == ["Invoice__c"]


def test_plan_object_field_rules():
    p = plan_object({"name": "X", "fields": [
        {"name": "Name", "type": "string"}, {"name": "Id", "type": "id"},
        {"name": "Addr", "type": "address"}, {"name": "Blob", "type": "base64"},
        {"name": "Old", "type": "string", "deprecatedAndHidden": True}]})
    assert p.fields == ["Id", "Name"]
    assert p.soql("Name = 'a'") == "SELECT Id, Name FROM X WHERE Name = 'a'"


def test_create_table_sql():
    sql = create_table_sql("Account", ["Id", "Paid", "When"],
                           {"Id": "id", "Paid": "boolean", "When": "datetime"}, "postgres")
    assert '"Id" VARCHAR(18) PRIMARY KEY' in sql
    assert '"Paid" BOOLEAN' in sql and '"When" TIMESTAMPTZ' in sql
    assert '"account"' in create_table_sql("Account", ["Id"], {}, "sqlite", lowercase=True)


def test_parse_target():
    assert parse_target("sqlite:///a/b.db") == ("sqlite", "a/b.db")
    assert parse_target("sqlite:////abs/b.db") == ("sqlite", "/abs/b.db")
    assert parse_target("duckdb:///x.duckdb") == ("duckdb", "x.duckdb")
    assert parse_target("postgresql://u:p@h/db")[0] == "postgres"
    with pytest.raises(LoadError):
        parse_target("mysql://nope")


def test_client_pagination_and_api_version():
    org = build_org()
    sf = client_for(org)
    assert sf.api_version == "62.0"
    rows = list(sf.query("SELECT Id, Name FROM Account"))
    assert len(rows) == 24  # rest_page=3 → 8 pages
    assert "attributes" not in rows[0]
    assert sf.count("Account", include_deleted=True) == 25


def test_client_retries_then_raises():
    class Flaky(FakeOrg):
        n = 0

        def route(self, method, path, qs, body):
            Flaky.n += 1
            if Flaky.n < 3:
                return 503, {"message": "busy"}, {}
            return super().route(method, path, qs, body)

    org = Flaky()
    org.add_object("A", [{"name": "Id", "type": "id"}], [{"Id": "1"}])
    sf = client_for(org, api_version="62.0")
    assert sf.count("A") == 1
    with pytest.raises(SalesforceError) as e:
        sf.describe("Nope")
    assert e.value.code == "INVALID_TYPE"


def test_client_refreshes_expired_token():
    org = build_org()

    class Expiring(FakeOrg):
        pass

    sf = client_for(org, api_version="62.0")
    seen = []
    orig = org.send

    def send(request, **kw):
        seen.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer old":
            r = orig(request, **kw)
            r.status_code = 401
            r._content = b'[{"message":"Session expired","errorCode":"INVALID_SESSION_ID"}]'
            return r
        return orig(request, **kw)

    org.send = send
    sf.access_token = "old"
    sf.refresh = lambda: "new"
    assert sf.count("Account") == 24
    assert seen == ["Bearer old", "Bearer new"]


def test_instance_url_trailing_slash():
    assert SalesforceClient("https://x.my.salesforce.com/", "t", api_version="60.0").base == \
        "https://x.my.salesforce.com/services/data/v60.0"

import csv
import json
import os
import sqlite3

import pytest

from sf_exit.exporter import Exporter, ExportOptions
from sf_exit.verify import verify_live, verify_offline

from .fake_org import build_org, client_for


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture
def exported(tmp_path):
    org = build_org()
    sf = client_for(org)
    opts = ExportOptions(out_dir=tmp_path / "out", poll_interval=0, workers=3, file_workers=3)
    result = Exporter(sf, opts).run()
    return org, sf, result, tmp_path / "out"


def test_exports_every_queryable_object(exported):
    _, _, result, out = exported
    done = {n for n, o in result.objects.items() if o.status == "done"}
    assert {"Account", "Contact", "Invoice__c", "Task", "User", "ContentVersion",
            "ContentDocument", "ContentDocumentLink", "SomeSetting__mdt", "EmptyThing__c"} <= done
    assert not result.failed
    # filtered out
    assert "AccountChangeEvent" not in result.objects
    assert "NotQueryable" not in result.objects
    assert "Vote" in result.skipped


def test_deleted_rows_excluded_by_default(exported):
    _, _, result, out = exported
    rows = read_csv(out / "data" / "Account.csv")
    assert len(rows) == 24
    assert result.objects["Account"].rows == 24


def test_include_deleted(tmp_path):
    sf = client_for(build_org())
    opts = ExportOptions(out_dir=tmp_path, include=["Account"], include_deleted=True,
                         poll_interval=0, files=False)
    result = Exporter(sf, opts).run()
    assert result.objects["Account"].rows == 25


def test_csv_fidelity(exported):
    _, _, _, out = exported
    rows = read_csv(out / "data" / "Account.csv")
    header = list(rows[0].keys())
    assert header[0] == "Id"
    assert "BillingAddress" not in header  # compound field skipped
    assert "BillingCity" in header  # ...but its components kept
    assert "Legacy__c" not in header  # deprecated field skipped
    by_id = {r["Id"]: r for r in rows}
    assert by_id["001000000000001AAA"]["Description"] == "multi\nline, text"
    assert by_id["001000000000002AAA"]["Name"] == 'Account 2, "Ltd"'
    inv = read_csv(out / "data" / "Invoice__c.csv")
    assert "Scan__c" not in inv[0]  # base64 not in CSV


def test_rest_and_bulk_produce_identical_csv(tmp_path):
    outs = {}
    for engine in ("rest", "bulk"):
        sf = client_for(build_org())
        opts = ExportOptions(out_dir=tmp_path / engine, include=["Account", "Contact"],
                             engine=engine, poll_interval=0, files=False)
        result = Exporter(sf, opts).run()
        assert result.objects["Account"].engine == engine
        outs[engine] = (tmp_path / engine / "data" / "Contact.csv").read_text()
    assert outs["rest"] == outs["bulk"]


def test_bulk_unsupported_falls_back_to_rest(tmp_path):
    sf = client_for(build_org())
    opts = ExportOptions(out_dir=tmp_path, include=["Task"], engine="bulk", poll_interval=0, files=False)
    result = Exporter(sf, opts).run()
    assert result.objects["Task"].status == "done"
    assert result.objects["Task"].engine == "rest"
    assert result.objects["Task"].rows == 5


def test_content_document_links_exported_via_chunked_filter(exported):
    _, _, result, out = exported
    rows = read_csv(out / "data" / "ContentDocumentLink.csv")
    assert len(rows) == 5
    assert result.objects["ContentDocumentLink"].status == "done"


def test_files_downloaded_and_checksummed(exported):
    org, _, result, out = exported
    index = read_csv(out / "files" / "index.csv")
    kinds = sorted(r["kind"] for r in index)
    assert kinds == ["Attachment", "ContentVersion", "ContentVersion", "ContentVersion", "Document"]
    for r in index:
        assert r["status"] == "ok", r
        assert (out / r["path"]).exists()
    cv = [r for r in index if r["kind"] == "ContentVersion"]
    assert all(r["md5"] == r["sf_checksum"] for r in cv)
    assert "/" not in cv[0]["path"].split("/")[-1]
    assert cv[0]["path"].endswith(".pdf")
    assert result.files["ContentVersion"]["count"] == 3


def test_outputs_written(exported):
    _, _, _, out = exported
    m = json.loads((out / "manifest.json").read_text())
    assert m["org"]["name"] == "Acme Corp"
    assert m["totals"]["rows"] > 80
    assert (out / "schema" / "Account.json").exists()
    for d in ("postgres", "duckdb", "sqlite"):
        assert 'CREATE TABLE IF NOT EXISTS "Account"' in (out / f"schema.{d}.sql").read_text()
    erd = (out / "erd.mmd").read_text()
    assert "Contact }o--o| Account" in erd
    assert "Invoice_c }|--|| Account" in erd
    assert "Acme Corp" in (out / "REPORT.md").read_text()


def test_resume_skips_finished_objects(tmp_path):
    org = build_org()
    sf = client_for(org)
    opts = ExportOptions(out_dir=tmp_path, poll_interval=0, files=False)
    Exporter(sf, opts).run()
    first_calls = len(org.calls)
    org.calls.clear()
    Exporter(client_for(org), opts).run()
    queries = [c for c in org.calls if c.endswith("/query") or "jobs/query" in c]
    # only the ContentDocumentLink pass (which depends on fresh ContentDocument Ids) re-queries
    assert len(queries) <= 3, queries
    assert len(org.calls) < first_calls


def test_unexportable_object_is_skipped_not_failed(tmp_path):
    org = build_org()
    org.requires_filter.add("Contact")
    opts = ExportOptions(out_dir=tmp_path, poll_interval=0, files=False)
    result = Exporter(client_for(org), opts).run()
    assert result.objects["Contact"].status == "skipped"
    assert "requires a filter" in result.objects["Contact"].error
    assert not result.failed
    assert result.objects["Account"].status == "done"
    assert not (tmp_path / "data" / "Contact.csv").exists()
    report = (tmp_path / "REPORT.md").read_text()
    assert "Failed objects" not in report and "`Contact`" in report


def test_permission_error_is_a_real_failure(tmp_path):
    org = build_org()
    org.denied.add("Contact")
    opts = ExportOptions(out_dir=tmp_path, poll_interval=0, files=False)
    result = Exporter(client_for(org), opts).run()
    assert result.objects["Contact"].status == "failed"
    assert "INSUFFICIENT_ACCESS" in result.objects["Contact"].error
    assert "Failed objects" in (tmp_path / "REPORT.md").read_text()


def test_knowledge_articles_exported_per_publish_status(exported):
    _, _, result, out = exported
    assert result.objects["FAQ__kav"].status == "done"
    rows = read_csv(out / "data" / "FAQ__kav.csv")
    assert sorted(r["PublishStatus"] for r in rows) == ["Archived", "Draft", "Online", "Online"]


def test_composite_batching_saves_api_calls(tmp_path):
    calls = {}
    for composite in (True, False):
        org = build_org()
        org.composite = composite
        sf = client_for(org)
        opts = ExportOptions(out_dir=tmp_path / str(composite), poll_interval=0, files=False)
        result = Exporter(sf, opts).run()
        assert not result.failed
        calls[composite] = sf.api_calls
    # 15 objects: describe + first page batched -> far fewer calls
    assert calls[True] < calls[False] - 20, calls


def test_resume_restarts_when_options_change(tmp_path):
    org = build_org()
    opts = ExportOptions(out_dir=tmp_path, include=["Account"], poll_interval=0, files=False)
    Exporter(client_for(org), opts).run()
    opts.include_deleted = True
    result = Exporter(client_for(org), opts).run()
    assert result.objects["Account"].rows == 25  # re-exported, not resumed with 24


def test_verify_offline_and_live(exported):
    org, sf, _, out = exported
    checks = verify_offline(out)
    assert checks and all(c.ok for c in checks)
    live = verify_live(sf, out)
    assert all(c.ok for c in live), [c for c in live if not c.ok]
    # tamper
    p = out / "data" / "Contact.csv"
    p.write_text(p.read_text() + '"x","y"\n')
    assert not all(c.ok for c in verify_offline(out))
    # drift
    org.objects["Account"]["records"].append({"Id": "001NEW", "IsDeleted": False})
    assert any(c.name == "Account" and not c.ok for c in verify_live(sf, out))


def test_load_sqlite(exported, tmp_path):
    from sf_exit.loader import load

    _, _, result, out = exported
    db = tmp_path / "sf.db"
    counts = load(out, f"sqlite:///{db}")
    assert counts["Account"] == 24
    con = sqlite3.connect(db)
    paid = con.execute('SELECT count(*) FROM "Invoice__c" WHERE "Paid__c" = 1').fetchone()[0]
    assert paid == 2
    revenue = con.execute('SELECT typeof("AnnualRevenue") FROM "Account" LIMIT 1').fetchone()[0]
    assert revenue == "real"
    nulls = con.execute('SELECT count(*) FROM "Account" WHERE "Industry" IS NULL').fetchone()[0]
    assert nulls > 0
    joined = con.execute(
        'SELECT count(*) FROM "Contact" c JOIN "Account" a ON a."Id" = c."AccountId"').fetchone()[0]
    assert joined > 0
    assert con.execute('SELECT count(*) FROM "_files"').fetchone()[0] == 5
    # idempotent re-load
    load(out, f"sqlite:///{db}")
    assert con.execute('SELECT count(*) FROM "Account"').fetchone()[0] == 24


def test_load_duckdb_and_parquet(exported, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from sf_exit.loader import load

    _, _, _, out = exported
    db = tmp_path / "sf.duckdb"
    counts = load(out, f"duckdb:///{db}", lowercase=True)
    assert counts["Contact"] == 40
    con = duckdb.connect(str(db))
    t = con.execute("SELECT typeof(createddate), typeof(hasoptedoutofemail), typeof(birthdate) "
                    "FROM contact LIMIT 1").fetchone()
    assert t == ("TIMESTAMP WITH TIME ZONE", "BOOLEAN", "DATE")
    desc = con.execute("SELECT description FROM account WHERE id = '001000000000001AAA'").fetchone()[0]
    assert desc == "multi\nline, text"
    con.close()

    pq = tmp_path / "pq"
    load(out, f"parquet:///{pq}")
    assert (pq / "Account.parquet").exists()
    n = duckdb.connect().execute(f"SELECT count(*) FROM '{pq}/Account.parquet'").fetchone()[0]
    assert n == 24


@pytest.mark.skipif(not os.environ.get("SF_EXIT_TEST_PG"), reason="set SF_EXIT_TEST_PG=postgresql://...")
def test_load_postgres(exported):
    psycopg = pytest.importorskip("psycopg")
    from sf_exit.loader import load

    _, _, _, out = exported
    uri = os.environ["SF_EXIT_TEST_PG"]
    counts = load(out, uri, lowercase=True, replace=True)
    assert counts["Account"] == 24
    with psycopg.connect(uri) as con:
        types = con.execute("SELECT pg_typeof(createddate)::text, pg_typeof(hasoptedoutofemail)::text "
                            "FROM contact LIMIT 1").fetchone()
        assert types == ("timestamp with time zone", "boolean")
        assert con.execute("SELECT count(*) FROM account WHERE industry IS NULL").fetchone()[0] > 0
    assert load(out, uri, lowercase=True)["Account"] == 24  # re-load is idempotent

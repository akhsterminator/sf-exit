"""audit, sql and diff: the offline tools that work on an export folder."""

import csv
import json
import shutil

import pytest
from click.testing import CliRunner

from sf_exit import cli as cli_mod
from sf_exit.audit import run_audit
from sf_exit.diff import diff_exports
from sf_exit.exporter import Exporter, ExportOptions

from .fake_org import build_org, client_for


@pytest.fixture(scope="module")
def export_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("exp")
    Exporter(client_for(build_org()), ExportOptions(out_dir=out, poll_interval=0)).run()
    return out


def test_audit_finds_real_problems(export_dir, tmp_path):
    report = run_audit(export_dir, tmp_path / "audit")
    ids = {f["id"]: f for f in report["findings"]}
    assert "Opportunity.Tier__c (Tier)" in ids["unused-fields"]["items"]
    assert "Opportunity.Is_Partner__c (Partner deal)" in ids["never-checked"]["items"]
    assert any(i.startswith("Opportunity.Legacy_Code__c") for i in ids["rare-fields"]["items"])
    assert any("Qualification" in i for i in ids["unused-picklist-values"]["items"])
    assert ids["duplicate-emails"]["count"] == 4  # ada0..ada3 appear twice
    assert ids["overdue-opportunities"]["severity"] == "high"
    assert ids["inactive-owners"]["count"] > 0  # User 3 is inactive and owns records
    assert "EmptyThing__c (EmptyThing__c)" in ids["empty-objects"]["items"]
    assert report["findings"][0]["severity"] == "high"

    s = report["summary"]
    assert s["org"]["name"] == "Acme Corp" and s["records"] > 100

    html = (tmp_path / "audit" / "index.html").read_text()
    assert "<script>" in html and "Acme Corp" in html and "</script>" in html
    assert "<\\/" in html or "</script></body>" in html  # data can't break out of <script>
    with open(tmp_path / "audit" / "data-dictionary.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    tier = next(r for r in rows if r["object"] == "Opportunity" and r["field"] == "Tier__c")
    assert tier["help_text"] == "Customer tier at signature" and tier["fill_rate"] == "0.0000"
    assert tier["picklist_values"] == "Gold;Silver"
    md = (tmp_path / "audit" / "findings.md").read_text()
    assert "[HIGH]" in md


def test_sql_queries_typed_views(export_dir):
    pytest.importorskip("duckdb")
    from sf_exit import sql

    con = sql.connect(export_dir)
    cols, rows = sql.run(con, 'SELECT count(*) AS n, sum("Amount") > 0 AS ok FROM "Opportunity"')
    assert cols == ["n", "ok"] and rows == [(30, True)]
    _, rows = sql.run(con, 'SELECT typeof("CloseDate") FROM "Opportunity" LIMIT 1')
    assert rows == [("DATE",)]
    names = dict(sql.tables(con))
    assert "Account" in names and "_files" in names


def test_diff_detects_added_removed_changed(export_dir, tmp_path):
    new = tmp_path / "new"
    shutil.copytree(export_dir, new)
    path = new / "data" / "Contact.csv"
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    header = rows[0]
    li = header.index("LastName")
    rows[1][li] = "Changed"  # changed
    del rows[2]  # removed
    rows.append(["003NEW000000000AAA"] + [""] * (len(header) - 1))  # added
    with open(path, "w", newline="") as fh:
        csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n").writerows(rows)
    (new / "data" / "Brand_New__c.csv").write_text('"Id"\n"a00000000000001AAA"\n')

    diffs = {d.name: d for d in diff_exports(export_dir, new, details_dir=tmp_path / "details")}
    c = diffs["Contact"]
    assert (c.added, c.removed, c.changed) == (1, 1, 1)
    assert c.changed_fields == {"LastName": 1}
    assert diffs["Brand_New__c"].status == "new-object"
    assert not diffs["Account"].touched
    with open(tmp_path / "details" / "Contact.csv", newline="") as fh:
        detail = list(csv.DictReader(fh))
    assert {d["change"] for d in detail} == {"added", "removed", "changed"}
    changed = next(d for d in detail if d["change"] == "changed")
    assert changed["field"] == "LastName" and changed["new_value"] == "Changed"


def test_cli_audit_sql_diff(export_dir, tmp_path):
    runner = CliRunner()
    res = runner.invoke(cli_mod.cli, ["audit", str(export_dir), "-o", str(tmp_path / "a")], obj={})
    assert res.exit_code == 0, res.output
    assert "HIGH" in res.output and "index.html" in res.output

    pytest.importorskip("duckdb")
    res = runner.invoke(cli_mod.cli, ["sql", str(export_dir), 'SELECT count(*) AS n FROM "Account"',
                                      "--format", "json"], obj={})
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == [{"n": 24}]
    res = runner.invoke(cli_mod.cli, ["sql", str(export_dir)], input=".tables\nSELECT 1 AS x;\n.quit\n",
                        obj={})
    assert res.exit_code == 0, res.output

    res = runner.invoke(cli_mod.cli, ["diff", str(export_dir), str(export_dir)], obj={})
    assert res.exit_code == 0 and "No differences" in res.output


def test_cli_export_with_everything(monkeypatch, tmp_path):
    org = build_org()

    def fake(kw):
        kw.clear()
        return client_for(org)

    monkeypatch.setattr(cli_mod, "make_client", fake)
    out = tmp_path / "exp"
    args = ["export", "-d", str(out), "--metadata", "--redact-pii", "--audit"]
    res = CliRunner().invoke(cli_mod.cli, args, obj={})
    assert res.exit_code == 0, res.output
    assert "Metadata" in res.output and "Redacted" in res.output
    assert (out / "metadata" / "package.xml").exists()
    assert (out / "audit" / "index.html").exists()
    res = CliRunner().invoke(cli_mod.cli, args[:3] + ["--incremental", "--no-files"], obj={})
    assert res.exit_code == 0, res.output
    assert "Changed" in res.output

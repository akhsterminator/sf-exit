"""sf-exit restore: re-inserting records into another org with Ids remapped."""

import csv

import pytest

from sf_exit.exporter import Exporter, ExportOptions
from sf_exit.restore import RestoreError, Restorer, plan_restore

from .fake_org import build_org, client_for

DATA_OBJECTS = ("Account", "Contact", "Opportunity", "Invoice__c", "Task", "FAQ__kav", "Lead")


@pytest.fixture()
def export_dir(tmp_path):
    out = tmp_path / "exp"
    Exporter(client_for(build_org()), ExportOptions(out_dir=out, poll_interval=0, files=False)).run()
    return out


def sandbox():
    """An empty sandbox of the same org: same schema and users, no business data."""
    org = build_org()
    for name in DATA_OBJECTS:
        org.objects[name]["records"].clear()
    org.objects["Organization"]["records"][0].update(Id="00D000000000002AAA", IsSandbox=True)
    return org


def by(org, obj, key="Name"):
    return {r.get(key): r for r in org.objects[obj]["records"]}


def test_plan_orders_parents_before_children(export_dir):
    order, skipped = plan_restore(client_for(sandbox()), export_dir, ["*"])
    names = [p.name for p in order]
    assert names.index("Account") < names.index("Contact") < len(names)
    assert names.index("Account") < names.index("Invoice__c")
    assert names.index("Account") < names.index("Opportunity")
    assert "User" in skipped and "ContentVersion" in skipped and "Organization" in skipped
    account = next(p for p in order if p.name == "Account")
    assert account.deferred == ["ParentId"]  # self-reference: filled in by pass 2
    assert "Id" not in account.fields and "CreatedDate" not in account.fields
    assert next(p for p in order if p.name == "Invoice__c").required_refs == {"Account__c"}


def test_dry_run_writes_nothing(export_dir):
    org = sandbox()
    result = Restorer(client_for(org), export_dir, ["Account", "Contact"]).run()
    assert not result.executed and result.total_created == 0
    assert [p.name for p in result.order] == ["Account", "Contact"]
    assert org.writes == 0 and not (export_dir / "restore").exists()


def test_restore_remaps_lookups_and_self_references(export_dir):
    org = sandbox()
    result = Restorer(client_for(org), export_dir,
                      ["Account", "Contact", "Opportunity", "Invoice__c"], execute=True).run()
    assert result.created == {"Account": 24, "Contact": 40, "Opportunity": 30, "Invoice__c": 7}
    assert result.total_failed == 1  # the invoice whose (deleted) account wasn't exported
    accounts = by(org, "Account")
    assert len(accounts) == 24
    a1 = accounts["Account 1"]
    assert a1["Id"] != "001000000000001AAA" and a1["AnnualRevenue"] == 332000.0
    assert a1["Description"] == "multi\nline, text"
    # child lookups point at the NEW account ids
    new_ids = {a["Id"] for a in accounts.values()}
    contacts = org.objects["Contact"]["records"]
    assert all(c["AccountId"] in new_ids for c in contacts if c.get("AccountId"))
    inv = by(org, "Invoice__c")["INV-0001"]
    assert inv["Account__c"] == a1["Id"] and inv["Paid__c"] is False
    # self-references are fixed by the second pass (Account 1 -> 24, Account 3 -> 1)
    assert a1["ParentId"] == accounts["Account 24"]["Id"]
    assert accounts["Account 3"]["ParentId"] == a1["Id"]
    assert result.relinked == {"Account": 2}
    # owners are matched to the target org's users by username
    assert a1["OwnerId"] == "005000000000002AAA"
    # the id map and the error log are on disk
    with open(result.out_dir / "id-map.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 101 and {"object", "old_id", "new_id"} == set(rows[0])
    errors = (result.out_dir / "errors.csv").read_text()
    assert "REQUIRED_FIELD_MISSING" in errors or "Account__c" in errors


def test_restore_is_resumable(export_dir):
    org = sandbox()
    Restorer(client_for(org), export_dir, ["Account"], execute=True).run()
    writes = org.writes
    again = Restorer(client_for(org), export_dir, ["Account", "Contact"], execute=True).run()
    assert again.skipped_existing == {"Account": 24} and again.created == {"Account": 0, "Contact": 40}
    assert org.writes == writes + 40
    assert len(org.objects["Account"]["records"]) == 24  # no duplicates


def test_refuses_production_and_same_org(export_dir):
    prod = sandbox()
    prod.objects["Organization"]["records"][0]["IsSandbox"] = False
    with pytest.raises(RestoreError, match="production"):
        Restorer(client_for(prod), export_dir, ["Account"], execute=True).run()
    assert Restorer(client_for(prod), export_dir, ["Account"], allow_production=True).run().order
    source = build_org()
    source.objects["Organization"]["records"][0]["IsSandbox"] = True
    with pytest.raises(RestoreError, match="came from"):
        Restorer(client_for(source), export_dir, ["Account"]).run()
    with pytest.raises(RestoreError, match="--include"):
        Restorer(client_for(sandbox()), export_dir, []).run()


def test_fields_missing_in_target_are_skipped(export_dir):
    org = sandbox()
    fields = org.objects["Opportunity"]["describe"]["fields"]
    org.objects["Opportunity"]["describe"]["fields"] = [f for f in fields if f["name"] != "Tier__c"]
    result = Restorer(client_for(org), export_dir, ["Account", "Opportunity"], execute=True).run()
    opp = next(p for p in result.order if p.name == "Opportunity")
    assert opp.skipped_fields == ["Tier__c"] and result.created["Opportunity"] == 30


def test_cli_restore_browse_index_and_mcp(export_dir, monkeypatch):
    from click.testing import CliRunner

    from sf_exit import cli as cli_mod

    org = sandbox()
    monkeypatch.setattr(cli_mod, "make_client", lambda kw: (kw.clear(), client_for(org))[1])
    runner = CliRunner()
    args = ["restore", str(export_dir), "-i", "Account", "-i", "Contact"]
    res = runner.invoke(cli_mod.cli, args, obj={})
    assert res.exit_code == 0, res.output
    assert "Dry run" in res.output and org.writes == 0
    res = runner.invoke(cli_mod.cli, args + ["--execute"], obj={})
    assert res.exit_code == 0, res.output
    assert "Created 64 records" in res.output and "re-linked 2" in res.output

    prod = sandbox()
    prod.objects["Organization"]["records"][0]["IsSandbox"] = False
    monkeypatch.setattr(cli_mod, "make_client", lambda kw: (kw.clear(), client_for(prod))[1])
    res = runner.invoke(cli_mod.cli, args + ["--execute"], obj={})
    assert res.exit_code == 2 and "production" in res.output and prod.writes == 0

    request = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_objects"}}\n'
    res = runner.invoke(cli_mod.cli, ["mcp", str(export_dir)], input=request, obj={})
    assert res.exit_code == 0, res.output
    reply = next(line for line in res.stdout.splitlines() if line.startswith('{"jsonrpc"'))
    assert '\\"object\\": \\"Contact\\"' in reply

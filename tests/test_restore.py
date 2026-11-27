"""sf-exit restore: re-inserting records into another org with Ids remapped."""

import csv

import pytest

from sf_exit.exporter import Exporter, ExportOptions
from sf_exit.restore import RestoreError, Restorer, plan_restore

from .fake_org import F, build_org, client_for

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


def test_restore_files_are_uploaded_and_relinked(tmp_path):
    out = tmp_path / "exp"
    Exporter(client_for(build_org()), ExportOptions(out_dir=out, poll_interval=0)).run()
    org = sandbox()
    for name in ("ContentVersion", "ContentDocument", "ContentDocumentLink", "Attachment"):
        org.objects[name]["records"].clear()
    org.blobs.clear()
    dry = Restorer(client_for(org), out, ["Account"], files=True).run()
    assert dry.files_planned == 4 and org.writes == 0  # 3 documents on accounts + 1 attachment

    result = Restorer(client_for(org), out, ["Account"], files=True, execute=True).run()
    assert result.files_restored == 4 and result.files_failed == 0
    new_ids = {old: new for old, new in
               (line.split(",")[1:] for line in (result.out_dir / "id-map.csv").read_text().split()[1:])}
    att = org.objects["Attachment"]["records"][0]
    assert att["ParentId"] == new_ids["001000000000001AAA"] and att["Name"] == "logo.png"
    assert org.blobs[f"Attachment/{att['Id']}"] == b"\x89PNG fake image bytes"
    links = org.objects["ContentDocumentLink"]["records"]
    assert {lk["LinkedEntityId"] for lk in links} <= set(new_ids.values())
    # Contract 2 is linked to Account 1 and Account 4 in the export: one upload, two links
    contract2 = next(v for v in org.objects["ContentVersion"]["records"] if v["Title"] == "Contract 2/final")
    linked = {lk["LinkedEntityId"] for lk in links if lk["ContentDocumentId"] == contract2["ContentDocumentId"]}
    assert linked == {new_ids["001000000000001AAA"], new_ids["001000000000004AAA"]}
    assert result.links_created >= 1

    again = Restorer(client_for(org), out, ["Account"], files=True, execute=True).run()
    assert again.files_skipped == 4 and again.files_restored == 0  # resumable, no duplicates


def test_upsert_makes_restore_idempotent(export_dir):
    org = sandbox()
    org.objects["Account"]["describe"]["fields"].append(F("Legacy_Id__c", externalId=True))
    first = Restorer(client_for(org), export_dir, ["Account"], upsert=["Account.Legacy_Id__c"],
                     execute=True).run()
    assert first.created == {"Account": 24}
    a1 = by(org, "Account")["Account 1"]
    assert a1["Legacy_Id__c"] == "001000000000001AAA"  # the original Id, remembered
    (first.out_dir / "id-map.csv").unlink()  # even without the id map...
    second = Restorer(client_for(org), export_dir, ["Account"], upsert=["*.Legacy_Id__c"],
                      execute=True).run()
    assert second.updated == {"Account": 24} and second.created == {"Account": 0}
    assert len(org.objects["Account"]["records"]) == 24  # ...no duplicates
    by(org, "Account")["Account 1"]["Industry"] = "Changed in the sandbox"
    third = Restorer(client_for(org), export_dir, ["Account"], upsert=["*.Legacy_Id__c"],
                     execute=True).run()  # with the id map: brought up to date, not skipped
    assert third.updated == {"Account": 24} and not third.skipped_existing
    assert by(org, "Account")["Account 1"]["Industry"] != "Changed in the sandbox"
    with pytest.raises(RestoreError, match="External ID"):
        Restorer(client_for(org), export_dir, ["Contact"], upsert=["Contact.Nope__c"]).run()


def test_same_org_restore_keeps_links_to_surviving_parents(export_dir):
    org = build_org()  # the source org itself: contacts were deleted, accounts still exist
    org.objects["Organization"]["records"][0]["IsSandbox"] = True
    org.objects["Contact"]["records"].clear()
    result = Restorer(client_for(org), export_dir, ["Contact"], allow_same_org=True,
                      execute=True).run()
    assert result.same_org and result.created == {"Contact": 40}
    restored = org.objects["Contact"]["records"]
    assert all(c["AccountId"].startswith("001") and not c["AccountId"].startswith("001N")
               for c in restored)  # the original account Ids, not left empty
    assert result.kept_lookups["Contact.AccountId"] == 40 and not result.unmapped_lookups


def test_same_org_restore_brings_back_only_deleted_records(export_dir):
    org = build_org()  # the source org itself, where two contacts have since been deleted
    org.objects["Organization"]["records"][0]["IsSandbox"] = True
    contacts = org.objects["Contact"]["records"]
    gone = {contacts[3]["Id"], contacts[7]["Id"]}
    contacts[:] = [c for c in contacts if c["Id"] not in gone]
    plan = Restorer(client_for(org), export_dir, ["Contact"], allow_same_org=True).run()
    assert plan.still_present == {"Contact": 38} and len(contacts) == 38  # dry run: no writes
    result = Restorer(client_for(org), export_dir, ["Contact"], allow_same_org=True,
                      execute=True).run()
    assert result.created == {"Contact": 2} and result.still_present == {"Contact": 38}
    assert len(contacts) == 40  # no duplicates of the 38 that were never deleted
    again = Restorer(client_for(org), export_dir, ["Contact"], allow_same_org=True,
                     execute=True).run()
    assert sum(again.created.values()) == 0  # the two are in the Id map now


def test_unknown_outcome_stops_and_is_never_duplicated(export_dir):
    org = sandbox()
    org.drop_connection["/composite/sobjects"] = 1  # the first insert batch times out
    with pytest.raises(RestoreError, match="may or may not"):
        Restorer(client_for(org), export_dir, ["Account"], execute=True).run()
    unknown = (export_dir / "restore" / "00D000000000002AAA" / "unknown.csv").read_text()
    assert unknown.count("\n") == 25  # header + the 24 accounts of that batch
    again = Restorer(client_for(org), export_dir, ["Account"], execute=True).run()
    assert again.skipped_unknown == {"Account": 24} and again.created == {"Account": 0}

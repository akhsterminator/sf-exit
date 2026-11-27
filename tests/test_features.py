"""Incremental exports, redaction and metadata retrieval."""

import csv
import json
import re

from sf_exit.exporter import Exporter, ExportOptions

from .fake_org import build_org, client_for


def rows_by_id(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["Id"]: r for r in csv.DictReader(fh)}


NOW = "2099-01-01T00:00:00.000Z"  # always after the previous run's start time


def test_incremental_export_merges_changes_and_deletions(tmp_path):
    org = build_org()
    opts = ExportOptions(out_dir=tmp_path, poll_interval=0, files=False)
    first = Exporter(client_for(org), opts).run()
    assert first.objects["Contact"].rows == 40

    contacts = org.objects["Contact"]["records"]
    contacts[0].update(LastName="Hopper", SystemModstamp=NOW)  # updated
    contacts[1].update(IsDeleted=True, SystemModstamp=NOW)  # deleted (in recycle bin)
    contacts.append({"Id": "003NEW000000001AAA", "FirstName": "Grace", "LastName": "New",
                     "IsDeleted": False, "SystemModstamp": NOW})  # created

    org.calls.clear()
    opts.incremental = True
    sf = client_for(org)
    second = Exporter(sf, opts).run()

    c = second.objects["Contact"]
    assert c.changed == 3
    assert c.rows == 40  # 40 - 1 deleted + 1 created
    data = rows_by_id(tmp_path / "data" / "Contact.csv")
    assert data[contacts[0]["Id"]]["LastName"] == "Hopper"
    assert contacts[1]["Id"] not in data
    assert "003NEW000000001AAA" in data
    assert second.objects["Account"].changed == 0
    assert second.objects["Account"].rows == 24

    m = json.loads((tmp_path / "manifest.json").read_text())
    assert m["incremental_since"]
    snapshot = tmp_path / m["changes_dir"] / "Contact.csv"
    assert len(rows_by_id(snapshot)) == 3
    # every incremental query used queryAll + the SystemModstamp filter
    assert any("queryAll" in c for c in org.calls) or any("composite" in c for c in org.calls)


def test_incremental_only_requeries_links_of_changed_documents(tmp_path):
    org = build_org()
    opts = ExportOptions(out_dir=tmp_path, poll_interval=0, files=False)
    Exporter(client_for(org), opts).run()
    docs = org.objects["ContentDocument"]["records"]
    docs[0]["SystemModstamp"] = NOW
    links = org.objects["ContentDocumentLink"]["records"]
    links.append({"Id": "06ANEW000000001AAA", "ContentDocumentId": docs[0]["Id"],
                  "LinkedEntityId": "001000000000009AAA", "ShareType": "V"})
    org.calls.clear()
    opts.incremental = True
    result = Exporter(client_for(org), opts).run()
    assert result.objects["ContentDocumentLink"].rows == 6
    data = rows_by_id(tmp_path / "data" / "ContentDocumentLink.csv")
    assert "06ANEW000000001AAA" in data and len(data) == 6
    # nothing changed next time: no link queries at all
    org.calls.clear()
    result = Exporter(client_for(org), opts).run()
    assert result.objects["ContentDocumentLink"].rows == 6


def test_incremental_without_previous_export_is_full(tmp_path):
    opts = ExportOptions(out_dir=tmp_path, poll_interval=0, files=False, incremental=True,
                         include=["Account"])
    result = Exporter(client_for(build_org()), opts).run()
    assert result.objects["Account"].rows == 24
    assert result.objects["Account"].changed is None


def test_incremental_reexports_object_when_fields_change(tmp_path):
    org = build_org()
    opts = ExportOptions(out_dir=tmp_path, poll_interval=0, files=False, include=["Account"])
    Exporter(client_for(org), opts).run()
    org.objects["Account"]["describe"]["fields"].append({"name": "Tier__c", "type": "string"})
    opts.incremental = True
    exporter = Exporter(client_for(org), opts)
    result = exporter.run()
    assert "Account" not in exporter.incremental  # full re-export, not a merge
    assert result.objects["Account"].changed == 0  # ...and the new empty field isn't a change
    assert "Tier__c" in next(iter(rows_by_id(tmp_path / "data" / "Account.csv").values()))


def test_redaction_hashes_consistently(tmp_path):
    opts = ExportOptions(out_dir=tmp_path, poll_interval=0, files=False,
                         include=["Contact", "User"], redact=["Contact.LastName"],
                         redact_pii=True, redact_salt="pepper")
    result = Exporter(client_for(build_org()), opts).run()
    contacts = list(rows_by_id(tmp_path / "data" / "Contact.csv").values())
    assert all(r["Email"].startswith("redacted:") for r in contacts)
    assert all(r["LastName"].startswith("redacted:") for r in contacts)
    assert contacts[0]["LastName"] == contacts[1]["LastName"]  # same input -> same hash
    assert contacts[0]["FirstName"].startswith("redacted:")  # person names are PII
    assert contacts[0]["HasOptedOutOfEmail"] in ("true", "false")  # booleans never hashed
    assert contacts[0]["AccountId"].startswith("001")  # Ids/lookups never redacted
    assert set(result.objects["Contact"].redacted) == {"Email", "FirstName", "LastName", "Birthdate"}
    users = list(rows_by_id(tmp_path / "data" / "User.csv").values())
    assert users[0]["Email"].startswith("redacted:")
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert "Contact" in m["redacted"]


def test_metadata_export(tmp_path):
    org = build_org()
    opts = ExportOptions(out_dir=tmp_path, poll_interval=0, data=False, files=False,
                         metadata=True)
    result = Exporter(client_for(org), opts).run()
    md = tmp_path / "metadata"
    assert (md / "classes" / "InvoiceService.cls").read_text() == "public class InvoiceService {}"
    assert (md / "flows" / "Onboarding.flow-meta.xml").exists()
    assert (md / "objects" / "Account.object").exists()
    assert (md / "reports" / "SalesReports" / "Pipeline.report").exists()
    pkg = (md / "package.xml").read_text()
    assert "<name>ApexClass</name>" in pkg and "<members>SalesReports/Pipeline</members>" in pkg
    # the type that breaks retrieve is isolated and reported, the rest still succeeds
    assert "BrokenType" in result.metadata["failed_types"]
    warnings = (md / "retrieve-warnings.txt").read_text()
    assert "FAILED BrokenType" in warnings


def test_metadata_include_filter(tmp_path):
    opts = ExportOptions(out_dir=tmp_path, poll_interval=0, data=False, files=False,
                         metadata=True, metadata_include=["ApexClass", "Flow"])
    result = Exporter(client_for(build_org()), opts).run()
    assert result.metadata["types"] == 2
    assert not (tmp_path / "metadata" / "objects").exists()


def _md_export(tmp_path, org, **kw):
    opts = ExportOptions(out_dir=tmp_path, poll_interval=0, data=False, files=False, metadata=True, **kw)
    return Exporter(client_for(org), opts).run()


def test_metadata_profiles_carry_every_permission(tmp_path):
    org = build_org()
    org.metadata.pop("BrokenType")
    org.metadata["ApexClass"]["Billing"] = {"classes/Billing.cls": "public class Billing {}"}
    _md_export(tmp_path, org)
    # every retrieve included the profiles, so each permission section survived the merge
    assert all({"Profile", "PermissionSet"} <= set(r) for r in org.retrieve_requests)
    admin = (tmp_path / "metadata" / "profiles" / "Admin.profile").read_text()
    for needle in ("<apexClass>InvoiceService</apexClass>", "<apexClass>Billing</apexClass>",
                   "<field>Account.Name</field>", "<object>Invoice__c</object>",
                   "<layout>Account-Account Layout</layout>", "<name>ApiEnabled</name>"):
        assert admin.count(needle) == 1, needle
    sections = re.findall(r"^    <(\w+)>", admin, re.M)
    assert sections == sorted(sections)  # Metadata API order: alphabetical by element
    assert admin.count("<custom>false</custom>") == 1
    ps = (tmp_path / "metadata" / "permissionsets" / "Sales_Ops.permissionset").read_text()
    assert "<object>Account</object>" in ps and ps.count("<label>Sales Ops</label>") == 1


def test_metadata_oversized_types_are_split_by_component(tmp_path):
    org = build_org()
    org.metadata.pop("BrokenType")
    for i in range(6):
        org.metadata["ApexClass"][f"C{i}"] = {f"classes/C{i}.cls": "x", f"classes/C{i}.cls-meta.xml": "y"}
    org.max_files_per_retrieve = 5
    result = _md_export(tmp_path, org)
    assert result.metadata["failed_types"] == {}
    classes = sorted(p.name for p in (tmp_path / "metadata" / "classes").glob("*.cls"))
    assert classes == ["C0.cls", "C1.cls", "C2.cls", "C3.cls", "C4.cls", "C5.cls", "InvoiceService.cls"]
    assert "<members>C5</members>" in (tmp_path / "metadata" / "package.xml").read_text()


def test_metadata_email_templates_and_standard_picklists(tmp_path):
    org = build_org()
    org.metadata.pop("BrokenType")
    _md_export(tmp_path, org)
    md = tmp_path / "metadata"
    assert (md / "email" / "LightningFolder" / "Renewal.email").exists()  # EmailTemplateFolder
    assert (md / "email" / "ClassicFolder" / "Welcome.email").exists()
    assert (md / "standardValueSets" / "OpportunityStage.standardValueSet").exists()
    assert not (md / "retrieve-warnings.txt").exists()  # missing standard sets are expected


def test_metadata_rerun_drops_deleted_components_but_keeps_failed_types(tmp_path):
    org = build_org()
    org.metadata.pop("BrokenType")
    _md_export(tmp_path, org)
    assert (tmp_path / "metadata" / "flows" / "Onboarding.flow-meta.xml").exists()
    org.metadata["Flow"].pop("Onboarding")  # deleted in the org
    org.failing_types = {"Layout"}  # and Layouts fail to retrieve this time
    result = _md_export(tmp_path, org)
    md = tmp_path / "metadata"
    assert not (md / "flows" / "Onboarding.flow-meta.xml").exists()
    assert (md / "layouts" / "Account-Account Layout.layout").exists()  # previous version kept
    assert "Layout" in result.metadata["failed_types"]
    assert "previous version kept" in (md / "retrieve-warnings.txt").read_text()
    assert "<name>Layout</name>" in (md / "package.xml").read_text()
    assert not (tmp_path / ".sf-exit" / "metadata-new").exists()


def test_metadata_zip_cannot_write_outside_its_folder(tmp_path):
    org = build_org()
    org.metadata.pop("BrokenType")
    org.evil_zip_entries = [("../metadata-evil/pwned.cls", "x"), ("/abs/pwned.cls", "x"),
                            ("classes/../../escape.cls", "x")]
    _md_export(tmp_path, org)
    assert not (tmp_path / "metadata-evil").exists()
    assert not (tmp_path / "escape.cls").exists()
    assert not list(tmp_path.rglob("pwned.cls"))
    assert "unsafe path" in (tmp_path / "metadata" / "retrieve-warnings.txt").read_text()

"""Network failures, partial results and re-runs: an export must never silently lose data."""

import csv
import datetime as dt
import json
import threading

import pytest

from sf_exit.client import SalesforceError
from sf_exit.exporter import BULK_THRESHOLD, Exporter, ExportOptions
from sf_exit.verify import verify_offline

from .fake_org import build_org, client_for


def rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def export(tmp_path, org, **kw):
    kw.setdefault("poll_interval", 0)
    return Exporter(client_for(org), ExportOptions(out_dir=tmp_path, **kw)).run()


# ------------------------------------------------------------------ network failures
def test_connections_dropped_mid_body_are_retried_everywhere(tmp_path):
    org = build_org()
    org.break_stream = {"/VersionData": 2, "/Body": 1, "/results": 1, "/query": 1}
    result = export(tmp_path, org, engine="bulk")  # bulk: result pages stream too
    assert result.ok, (result.failed, result.file_errors)
    assert not org.break_stream  # every simulated reset happened...
    assert result.objects["Contact"].rows == 40  # ...and nothing was lost or doubled
    assert len(rows(tmp_path / "data" / "Contact.csv")) == 40
    index = rows(tmp_path / "files" / "index.csv")
    assert all(r["status"] == "ok" for r in index)
    assert not list(tmp_path.rglob("*.part"))


def test_persistent_failure_fails_one_object_not_the_run(tmp_path):
    org = build_org()
    org.drop_connection = {"/jobs/query": 100}  # bulk never works
    result = export(tmp_path, org, engine="bulk")
    # bulk failed for everything, so each object fell back to REST and still succeeded
    assert result.ok and result.objects["Account"].engine == "rest"


def test_unexpected_errors_are_contained(tmp_path, monkeypatch):
    org = build_org()
    real = type(client_for(org)).download

    def disk_full(self, path, dest):
        if "Attachment" in path:
            raise OSError(28, "No space left on device")
        return real(self, path, dest)

    monkeypatch.setattr(type(client_for(org)), "download", disk_full)
    result = export(tmp_path, org)
    assert result.file_errors == 1 and not result.ok
    bad = [r for r in rows(tmp_path / "files" / "index.csv") if r["status"] == "error"]
    assert bad and "No space left" in bad[0]["error"]
    assert (tmp_path / "manifest.json").exists()  # the run still finished and reported


def test_writes_with_unknown_outcome_are_not_retried(tmp_path):
    org = build_org()
    sf = client_for(org)
    org.drop_connection = {"/composite/sobjects": 1}
    with pytest.raises(SalesforceError) as exc:
        sf.request("POST", "composite/sobjects", retry=False,
                   json_body={"allOrNone": False, "records": []})
    assert exc.value.code == "OUTCOME_UNKNOWN"
    org.drop_connection = {"/limits": 2}  # reads are simply retried
    assert sf.limits()["DailyApiRequests"]["Max"] == 100000


def test_expired_token_is_refreshed_once_for_all_threads():
    org = build_org()
    sf = client_for(org)
    sf.access_token = "expired"
    refreshes = []

    def refresh():
        refreshes.append(1)
        return "fresh"

    sf.refresh = refresh
    send = org.send

    def guarded(request, **kw):
        r = send(request, **kw)
        if request.headers["Authorization"] == "Bearer expired":
            r.status_code = 401
        return r

    org.send = guarded
    threads = [threading.Thread(target=sf.limits) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(refreshes) == 1


# ------------------------------------------------------------------- API economics
def test_composite_bills_one_call_and_respects_its_query_limit(tmp_path):
    org = build_org()
    result = export(tmp_path, org, files=False)
    assert result.ok
    composite = [c for c in org.calls if c.endswith("/composite")]
    batch = [c for c in org.calls if c.endswith("/composite/batch")]
    assert composite and not batch  # /composite/batch bills every subrequest: never used
    # 18 objects: 1 describe call + 4 calls of 5 first pages + 1 for ContentDocumentLink
    assert len(composite) <= 6, composite


def test_unknown_counts_switch_big_objects_to_bulk(tmp_path, monkeypatch):
    org = build_org()
    org.record_count_denied = True
    monkeypatch.setattr("sf_exit.exporter.BULK_THRESHOLD", 30)  # Contact (40 rows) is "big"
    assert BULK_THRESHOLD == 10_000
    result = export(tmp_path, org, files=False)
    assert result.objects["Contact"].engine == "bulk"
    assert result.objects["Account"].engine == "rest"  # 24 rows: stays on REST
    assert any("View Setup and Configuration" in w for w in result.warnings)


def test_numbers_are_exact_and_never_scientific(tmp_path):
    org = build_org()
    org.objects["Account"]["records"][0]["AnnualRevenue"] = 1e16
    org.objects["Account"]["records"][1]["AnnualRevenue"] = 0.1 + 0.2
    export(tmp_path, org, include=["Account"], files=False)
    revenue = {r["Id"]: r["AnnualRevenue"] for r in rows(tmp_path / "data" / "Account.csv")}
    assert revenue["001000000000001AAA"] == "10000000000000000"
    assert revenue["001000000000002AAA"] == "0.30000000000000004"  # exactly what was sent


# ---------------------------------------------------------- partial results & re-runs
def test_partial_knowledge_export_is_reported_and_fails_verify(tmp_path):
    org = build_org()
    org.requires_filter.discard("FAQ__kav")
    real = org._where

    def no_archived(rec, where):
        if "Archived" in where:
            from .fake_org import _SfErr
            raise _SfErr(400, "MALFORMED_QUERY", "archived needs KnowledgeArticleId")
        return real(rec, where)

    org._where = no_archived
    result = export(tmp_path, org, include=["FAQ__kav"], files=False)
    faq = result.objects["FAQ__kav"]
    assert faq.status == "done" and faq.rows == 3 and faq.warnings
    assert "Warnings" in (tmp_path / "REPORT.md").read_text()
    assert not all(c.ok for c in verify_offline(tmp_path))


def test_file_listing_failure_keeps_previous_index_and_fails(tmp_path):
    org = build_org()
    export(tmp_path, org)
    real = org._select

    def timeout(soql, include_deleted):
        if "FROM ContentVersion WHERE IsLatest" in soql:
            from .fake_org import _SfErr
            raise _SfErr(400, "QUERY_TIMEOUT", "Your query request was running for too long.")
        return real(soql, include_deleted)

    org._select = timeout
    result = export(tmp_path, org)
    assert "ContentVersion" in result.listing_errors and not result.ok
    kinds = [r["kind"] for r in rows(tmp_path / "files" / "index.csv")]
    assert kinds.count("ContentVersion") == 3  # the previous rows are still there
    assert not all(c.ok for c in verify_offline(tmp_path))


def test_verify_fails_on_failed_objects_and_bad_files(tmp_path):
    org = build_org()
    org.denied.add("Contact")
    org.objects["ContentVersion"]["records"][0]["Checksum"] = "0" * 32
    export(tmp_path, org)
    checks = verify_offline(tmp_path)
    failed = {c.name: c.detail for c in checks if not c.ok}
    assert "Contact" in failed and "export failed" in failed["Contact"]
    assert "checksum" in failed["files/"]


def test_partial_reruns_keep_the_rest_of_the_export(tmp_path):
    org = build_org()
    export(tmp_path, org)
    export(tmp_path, org, include=["Contact"])
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert {"Account", "Opportunity", "Contact"} <= set(m["objects"])  # not just Contact
    assert len(rows(tmp_path / "files" / "index.csv")) == 5  # files of other kinds kept
    export(tmp_path, org, data=False)  # --files-only
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert m["objects"]["Account"]["rows"] == 24
    assert all(c.ok for c in verify_offline(tmp_path))


def test_incremental_uses_each_objects_own_export_time(tmp_path):
    org = build_org()
    export(tmp_path, org, files=False)
    m = json.loads((tmp_path / "manifest.json").read_text())
    m["objects"]["Account"]["started_at"] = "2020-01-01T00:00:00Z"  # exported long ago
    (tmp_path / "manifest.json").write_text(json.dumps(m))
    org.objects["Account"]["records"][0].update(Name="Changed in 2023", SystemModstamp="2023-01-01T00:00:00.000Z")
    exporter = Exporter(client_for(org), ExportOptions(out_dir=tmp_path, poll_interval=0, files=False,
                                                       incremental=True))
    result = exporter.run()
    assert "SystemModstamp > 2019-12-31T23:00:00Z" in exporter.plans["Account"].base_where
    # Every Account modified since 2020 was fetched again, but only one really changed.
    assert result.objects["Account"].changed == 1
    assert result.objects["Contact"].changed == 0  # Contact kept the run's own, recent watermark
    names = {r["Id"]: r["Name"] for r in rows(tmp_path / "data" / "Account.csv")}
    assert names["001000000000001AAA"] == "Changed in 2023"
    changes = tmp_path / result.changes_dir
    assert [r["Id"] for r in rows(changes / "Account.csv")] == ["001000000000001AAA"]
    assert [r["Name"] for r in rows(changes / "Account.before.csv")] == ["Account 1"]


def test_records_fetched_again_by_the_overlap_are_not_changes(tmp_path):
    """The watermark overlaps the previous run by an hour: rows seen then must not count twice."""
    org = build_org()
    export(tmp_path, org, files=False)
    accounts, contacts = org.objects["Account"]["records"], org.objects["Contact"]["records"]
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    accounts[0].update(Name="Edited", SystemModstamp=soon)
    contacts[1].update(IsDeleted=True, SystemModstamp=soon)
    first = Exporter(client_for(org), ExportOptions(out_dir=tmp_path, poll_interval=0, files=False,
                                                    incremental=True)).run()
    assert first.objects["Account"].changed == 1 and first.objects["Contact"].changed == 1
    again = Exporter(client_for(org), ExportOptions(out_dir=tmp_path, poll_interval=0, files=False,
                                                    incremental=True)).run()
    assert again.objects["Account"].changed == 0  # same rows again: nothing new
    assert again.objects["Contact"].changed == 0  # the deletion was recorded by the first run
    assert again.changes_dir is None and len(list((tmp_path / "changes").iterdir())) == 1
    assert all(c.ok for c in verify_offline(tmp_path))


def test_incremental_drops_rows_that_became_archived(tmp_path):
    org = build_org()
    tasks = org.objects["Task"]
    tasks["describe"]["fields"].append({"name": "IsArchived", "type": "boolean", "label": "Archived"})
    for t in tasks["records"]:
        t["IsArchived"] = False
    export(tmp_path, org, include=["Task"], files=False)
    tasks["records"][0].update(IsArchived=True, SystemModstamp="2099-01-01T00:00:00.000Z")
    result = export(tmp_path, org, include=["Task"], files=False, incremental=True)
    assert result.objects["Task"].rows == 4  # a normal export wouldn't include it either


def test_redaction_change_forces_a_full_export(tmp_path):
    org = build_org()
    export(tmp_path, org, include=["Contact"], files=False)
    exporter = Exporter(client_for(org), ExportOptions(
        out_dir=tmp_path, include=["Contact"], files=False, poll_interval=0, incremental=True,
        redact_pii=True, redact_salt="pepper"))
    exporter.run()
    assert not exporter.incremental  # every row re-fetched and hashed, none left in clear
    assert all(r["Email"].startswith("redacted:") for r in rows(tmp_path / "data" / "Contact.csv"))

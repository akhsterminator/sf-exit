"""Time machine: history recorded by every run, record timelines and point-in-time snapshots."""

import csv
import json

import pytest
from click.testing import CliRunner

from sf_exit import cli as cli_mod
from sf_exit.exporter import Exporter, ExportOptions
from sf_exit.history import HistoryError, parse_when, record_history, runs, snapshot
from sf_exit.index import ensure_index

from .fake_org import build_org, client_for

LATER = "2099-01-01T00:00:00.000Z"
A1, A3, C2 = "001000000000001AAA", "001000000000003AAA", "003000000000002AAA"


def rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["Id"]: r for r in csv.DictReader(fh)}


def stamp_times(out, *times):
    """Give the runs predictable finish times (tests run within one second)."""
    m = json.loads((out / "manifest.json").read_text())
    for run, t in zip(m["runs"], times, strict=False):
        run["finished_at"] = t
        if run.get("changes"):
            meta = out / run["changes"] / "run.json"
            d = json.loads(meta.read_text())
            d["finished_at"] = t
            meta.write_text(json.dumps(d))
    (out / "manifest.json").write_text(json.dumps(m))


@pytest.fixture(scope="module")
def backup(tmp_path_factory):
    out = tmp_path_factory.mktemp("backup")
    org = build_org()
    opts = dict(out_dir=out, poll_interval=0, files=False)
    Exporter(client_for(org), ExportOptions(**opts)).run()  # Monday: full
    accounts, contacts = org.objects["Account"]["records"], org.objects["Contact"]["records"]
    accounts[0].update(Name="Northwind Traders", AnnualRevenue=1.0, OwnerId="005000000000004AAA",
                       SystemModstamp=LATER)
    contacts[1].update(IsDeleted=True, SystemModstamp=LATER)
    contacts.append({"Id": "003NEW000000001AAA", "FirstName": "Grace", "LastName": "Hopper",
                     "Email": "grace@x.example", "IsDeleted": False, "SystemModstamp": LATER})
    Exporter(client_for(org), ExportOptions(incremental=True, **opts)).run()  # Tuesday: incremental
    accounts[2]["Name"] = "Renamed on Wednesday"  # no SystemModstamp bump: only a full run sees it
    Exporter(client_for(org), ExportOptions(**opts)).run()  # Wednesday: full
    stamp_times(out, "2026-11-02T22:00:00Z", "2026-11-03T22:00:00Z", "2026-11-04T22:00:00Z")
    return out


def test_every_run_records_its_changes(backup):
    rs = runs(backup)
    assert [r.kind for r in rs] == ["incremental", "full"]
    assert rs[0].objects == {"Account": 1, "Contact": 2}
    assert rs[1].objects["Account"] == 1


def test_record_timeline(backup):
    h = record_history(backup, A1)
    assert h["object"] == "Account" and h["current"]["Name"] == "Northwind Traders"
    (v,) = h["versions"]
    assert v["change"] == "updated" and v["at"] == "2026-11-03T22:00:00Z"
    assert v["fields"]["Name"] == ("Account 1", "Northwind Traders")
    assert "SystemModstamp" not in v["fields"]  # bookkeeping, not a change
    assert [v["change"] for v in record_history(backup, C2)["versions"]] == ["deleted"]
    assert record_history(backup, C2)["current"] is None
    assert [v["change"] for v in record_history(backup, "003NEW000000001AAA")["versions"]] == ["created"]
    # a full re-export compares with the previous CSV, so silent edits are caught too
    assert record_history(backup, A3)["versions"][0]["fields"]["Name"][1] == "Renamed on Wednesday"
    with pytest.raises(HistoryError):
        record_history(backup, "001000000000999AAA")


def test_snapshot_rebuilds_any_moment(backup, tmp_path):
    monday = snapshot(backup, parse_when("2026-11-03T10:00:00Z"), tmp_path / "mon")
    accounts, contacts = rows(tmp_path / "mon" / "data" / "Account.csv"), rows(tmp_path / "mon" / "data" / "Contact.csv")
    assert accounts[A1]["Name"] == "Account 1" and accounts[A3]["Name"] == "Account 3"
    assert C2 in contacts and "003NEW000000001AAA" not in contacts
    assert len(contacts) == 40 and monday["totals"]["rows"] > 0
    assert monday["snapshot"]["as_of"] == "2026-11-03T10:00:00Z"

    tuesday = tmp_path / "tue"
    snapshot(backup, parse_when("2026-11-04T10:00:00Z"), tuesday)
    accounts, contacts = rows(tuesday / "data" / "Account.csv"), rows(tuesday / "data" / "Contact.csv")
    assert accounts[A1]["Name"] == "Northwind Traders" and accounts[A3]["Name"] == "Account 3"
    assert C2 not in contacts and "003NEW000000001AAA" in contacts

    now = tmp_path / "now"
    snapshot(backup, parse_when("2030-01-01"), now)
    assert rows(now / "data" / "Account.csv") == rows(backup / "data" / "Account.csv")

    with pytest.raises(HistoryError, match="starts at"):
        snapshot(backup, parse_when("2026-11-01"), tmp_path / "too-early")
    from sf_exit.verify import verify_offline

    assert all(c.ok for c in verify_offline(tmp_path / "mon"))  # a valid export folder


def test_cli_history_and_snapshot(backup, tmp_path):
    runner = CliRunner()
    res = runner.invoke(cli_mod.cli, ["history", str(backup), A1], obj={})
    assert res.exit_code == 0, res.output
    assert "updated" in res.output and "Northwind Traders" in res.output
    res = runner.invoke(cli_mod.cli, ["history", str(backup), A1, "--json"], obj={})
    assert json.loads(res.output)["versions"][0]["change"] == "updated"
    res = runner.invoke(cli_mod.cli, ["snapshot", str(backup), "--as-of", "2026-11-03T10:00:00Z",
                                      "-o", str(tmp_path / "snap")], obj={})
    assert res.exit_code == 0, res.output
    assert rows(tmp_path / "snap" / "data" / "Account.csv")[A1]["Name"] == "Account 1"
    res = runner.invoke(cli_mod.cli, ["snapshot", str(backup), "--as-of", "last week",
                                      "-o", str(tmp_path / "x")], obj={})
    assert res.exit_code == 2 and "Can't read the date" in res.output


def test_history_in_browse_and_mcp(backup):
    from sf_exit.browse import BrowseServer, changed_in_run, dashboard
    from sf_exit.browse import record_history as browse_history
    from sf_exit.index import Catalog, connect
    from sf_exit.mcp import ExportTools

    ensure_index(backup)
    con = connect(backup)
    cat = Catalog(con)
    h = browse_history(con, cat, backup, A1)
    assert h["versions"][0]["fields"][0] == {"name": "AnnualRevenue", "label": "AnnualRevenue",
                                             "type": "currency", "old": "332000.0", "new": "1.0"}
    owner = next(f for f in h["versions"][0]["fields"] if f["name"] == "OwnerId")
    assert owner == {"name": "OwnerId", "label": "OwnerId", "type": "reference",
                     "old": "User 2", "new": "User 4",
                     "old_id": "005000000000002AAA", "new_id": "005000000000004AAA"}
    tuesday = runs(backup)[0].stamp
    changed = changed_in_run(con, cat, backup, tuesday, "Contact")["records"]
    assert {c["change"] for c in changed} == {"created", "deleted"}
    d = dashboard(con, cat, backup)
    assert d["runs"][0]["kind"] == "full" and d["pipeline"] and d["months"]
    srv = BrowseServer(backup, 0)
    assert srv.dashboard(con) is srv.dashboard(con)  # computed once, then cached
    srv.server_close()
    con.close()

    t = ExportTools(backup)
    mh = t.get_record_history(A1)
    assert mh["versions"][0]["fields"]["Name"] == {"old": "Account 1", "new": "Northwind Traders"}
    t.con.close()

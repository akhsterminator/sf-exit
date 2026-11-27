"""Regenerate the README's demo assets from real sf-exit runs against a simulated org.

    python scripts/record_demo.py          # needs duckdb; audit screenshot needs playwright

Writes:
  docs/demo.svg       export (records + files + metadata) and audit summary
  docs/tools.svg      sf-exit sql + incremental backup + diff
  docs/audit.png      the HTML audit report's findings (needs node + playwright)
  docs/dictionary.png the HTML audit report's data dictionary
  docs/browse.png     sf-exit browse: a record page with related lists and files
  docs/dashboard.png  sf-exit browse: the home page dashboard
  docs/history.png    sf-exit browse: one record's history across a month of nightly backups
  docs/restore.svg    sf-exit restore: dry run, then --execute into a sandbox
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from click.testing import CliRunner  # noqa: E402
from rich.console import Console  # noqa: E402

import sf_exit.cli as cli_mod  # noqa: E402
from tests.fake_org import F, build_org, client_for  # noqa: E402

COMPANIES = ["Northwind Traders", "Globex", "Initech", "Umbrella Health", "Stark Industries",
             "Wayne Logistics", "Hooli", "Vandelay Imports", "Soylent Foods", "Cyberdyne Systems",
             "Wonka Confectionery", "Tyrell Robotics", "Massive Dynamic", "Pied Piper", "Gringotts",
             "Oscorp", "Aperture Labs", "Black Mesa", "Monarch Solutions", "Dunder Mifflin"]
FIRST = ["Ada", "Grace", "Alan", "Katherine", "Linus", "Margaret", "Dennis", "Barbara", "Ken",
         "Radia", "Tim", "Frances", "John", "Hedy", "Edsger", "Sophie", "Guido", "Annie"]
LAST = ["Lovelace", "Hopper", "Turing", "Johnson", "Torvalds", "Hamilton", "Ritchie", "Liskov",
        "Thompson", "Perlman", "Berners-Lee", "Allen", "McCarthy", "Lamarr", "Dijkstra", "Wilson"]


OBJECT_LABELS = {"Invoice__c": "Invoice", "FAQ__kav": "FAQ", "SomeSetting__mdt": "Billing Setting",
                 "ContentDocumentLink": "Content Document Link", "ContentDocument": "File",
                 "ContentVersion": "File Version", "EmptyThing__c": "Legacy Import"}
FIELD_LABELS = {"AccountId": "Account", "OwnerId": "Owner", "ParentId": "Parent", "CreatedById": "Created By",
                "StageName": "Stage", "IsDeleted": "Deleted", "IsClosed": "Closed", "WhatId": "Related To",
                "HasOptedOutOfEmail": "Email Opt Out", "NumberOfEmployees": "Employees",
                "Account__c": "Account", "Id": "Record ID", "LinkedEntityId": "Linked To",
                "ContentDocumentId": "File", "IsConverted": "Converted", "IsActive": "Active"}


def _humanise(name: str) -> str:
    import re

    base = name[:-3] if name.endswith("__c") else name
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base.replace("_", " ")).strip()


def demo_org():
    rnd = random.Random(42)
    org = build_org(scale=420)  # ~10k Accounts, ~17k Contacts: big enough to use the Bulk API
    org.bulk_page, org.rest_page = 5000, 2000
    for i, a in enumerate(org.objects["Account"]["records"]):
        base = COMPANIES[i % len(COMPANIES)]
        suffix = ["Inc", "Ltd", "LLC", "GmbH", "Corp", ""][i % 6]
        a["Name"] = f"{base} {suffix}".strip() if i < 60 else f"{base} {i}"
        a["Region__c"] = rnd.choice(["EMEA", "AMER", "APAC", None])
        a["Old_CRM_Id__c"] = None
        a["Churn_Risk__c"] = None if i % 40 else "High"
    org.objects["Account"]["describe"]["fields"] += [
        F("Region__c", "picklist", custom=True, label="Region",
          picklistValues=[{"value": v, "active": True} for v in ["EMEA", "AMER", "APAC", "LATAM"]]),
        F("Old_CRM_Id__c", custom=True, label="Old CRM Id", inlineHelpText="Id from the 2017 migration"),
        F("Churn_Risk__c", custom=True, label="Churn risk"),
    ]
    for i, c in enumerate(org.objects["Contact"]["records"]):
        c["FirstName"], c["LastName"] = FIRST[i % len(FIRST)], LAST[(i // 3) % len(LAST)]
        c["Email"] = f"{c['FirstName'].lower()}.{c['LastName'].lower()}{i % 7000}@example.com"
    contacts = org.objects["Contact"]["records"]
    for i in range(150):  # the same people entered twice, as in every real CRM
        contacts[8000 + i]["Email"] = contacts[i]["Email"]
    for c in contacts:
        c["Name"] = f"{c['FirstName']} {c['LastName']}"
    org.objects["Contact"]["describe"]["fields"].insert(1, F("Name", nameField=True, createable=False))
    for i, u in enumerate(org.objects["User"]["records"]):
        u["Name"] = f"{FIRST[i * 3 % len(FIRST)]} {LAST[i * 5 % len(LAST)]}"
    first = org.objects["Account"]["records"][0]
    first.update(Description="Strategic account. Renewal due in Q3; expansion into APAC under discussion.",
                 Region__c="EMEA", Churn_Risk__c="Low", AnnualRevenue=332000.0, NumberOfEmployees=1200,
                 OwnerId="005000000000002AAA")
    # Two years of growth, so the dashboard has something to show.
    start = dt.datetime(2024, 12, 1, tzinfo=dt.timezone.utc)
    span = (dt.datetime(2026, 11, 24, tzinfo=dt.timezone.utc) - start).total_seconds()
    for name in ("Account", "Contact", "Opportunity", "Lead", "Invoice__c", "Task"):
        for r in org.objects[name]["records"]:
            r["CreatedDate"] = _stamp(start + dt.timedelta(seconds=span * rnd.random() ** 0.62))
    stages = ["Prospecting", "Qualification", "Needs Analysis", "Proposal", "Negotiation",
              "Closed Won", "Closed Lost"]
    for o in org.objects["Opportunity"]["records"]:
        stage = rnd.choices(stages, [14, 12, 9, 8, 6, 30, 21])[0]
        o.update(StageName=stage, IsClosed=stage.startswith("Closed"),
                 Amount=float(max(500, round(rnd.lognormvariate(9.6, 0.8) / 500) * 500)))
    for f in org.objects["Opportunity"]["describe"]["fields"]:
        if f["name"] == "StageName":
            f["picklistValues"] = [{"value": v, "active": True} for v in stages]
    for name, obj in org.objects.items():
        obj["describe"]["label"] = OBJECT_LABELS.get(name, obj["describe"]["label"])
        for f in obj["describe"]["fields"]:
            if f["label"] == f["name"]:
                f["label"] = FIELD_LABELS.get(f["name"]) or _humanise(f["name"])
    md = org.metadata
    md.pop("BrokenType", None)
    for i in range(48):
        md["ApexClass"][f"Service{i}"] = {f"classes/Service{i}.cls": "public class X {}"}
    for i in range(14):
        md["Flow"][f"Flow_{i}"] = {f"flows/Flow_{i}.flow-meta.xml": "<Flow/>"}
    md["PermissionSet"] = {f"PS_{i}": {} for i in range(9)}
    md["ApexTrigger"] = {f"T{i}": {f"triggers/T{i}.trigger": "trigger T on Account (before insert) {}"}
                         for i in range(6)}
    _add_files(org, 240)
    return org


def _add_files(org, n: int) -> None:
    rnd = random.Random(1)
    cv = org.objects["ContentVersion"]["records"]
    cd = org.objects["ContentDocument"]["records"]
    for i in range(10, 10 + n):
        data = os.urandom(rnd.randint(20_000, 400_000))
        vid, did = f"068{i:012d}AAA", f"069{i:012d}AAA"
        cv.append({"Id": vid, "ContentDocumentId": did, "Title": f"Proposal {i}", "FileExtension": "pdf",
                   "VersionNumber": "1", "ContentSize": len(data), "IsLatest": True, "IsDeleted": False,
                   "Checksum": hashlib.md5(data).hexdigest(), "SystemModstamp": "2024-06-01T00:00:00.000Z"})
        cd.append({"Id": did, "Title": f"Proposal {i}", "IsDeleted": False})
        org.blobs[f"ContentVersion/{vid}"] = data


def _stamp(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def restamp(out: Path, finished: str) -> None:
    """Pretend the last run into ``out`` finished at ``finished``: the demo's nightly runs
    happen seconds apart, and history, the dashboard and the next watermark use these times."""
    end = dt.datetime.fromisoformat(finished.replace("Z", "+00:00"))
    began = (end - dt.timedelta(minutes=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    m = json.loads((out / "manifest.json").read_text("utf-8"))
    m["started_at"], m["finished_at"] = began, finished
    for o in (m.get("objects") or {}).values():
        if o.get("started_at"):
            o["started_at"] = began
    run = m["runs"][-1]
    run["started_at"], run["finished_at"] = began, finished
    if run.get("changes"):
        meta = out / run["changes"] / "run.json"
        d = json.loads(meta.read_text("utf-8"))
        d["started_at"], d["finished_at"] = began, finished
        meta.write_text(json.dumps(d, indent=1), "utf-8")
    (out / "manifest.json").write_text(json.dumps(m, indent=1), "utf-8")


def edit(record: dict, when: str, **values) -> None:
    record.update(values, SystemModstamp=when, LastModifiedDate=when)


def a_month_of_backups(org, runner) -> None:
    """Three nightly runs' worth of edits, so history and the Changes page have a story."""
    rnd = random.Random(3)
    accounts = org.objects["Account"]["records"]
    contacts = org.objects["Contact"]["records"]
    opps = org.objects["Opportunity"]["records"]
    northwind = accounts[0]
    nights = {
        "2026-11-03T22:00:00Z": "2026-11-03T15:12:00.000Z",
        "2026-11-12T22:00:00Z": "2026-11-12T10:41:00.000Z",
        "2026-11-23T22:00:00Z": "2026-11-23T16:05:00.000Z",
    }
    for night, (finished, when) in enumerate(nights.items()):
        if night == 0:
            edit(northwind, when, Churn_Risk__c="Medium",
                 Description="Strategic account. Renewal at risk since the October reorg; "
                             "exec sponsor meeting booked.")
            for c in rnd.sample(contacts, 18):
                edit(c, when, Email=c["Email"].replace("@example.com", "@mail.example"))
            ladder = ["Prospecting", "Qualification", "Needs Analysis", "Proposal", "Negotiation"]
            for o in [o for o in opps if o["StageName"] in ladder[:-1]][:6]:
                edit(o, when, StageName=ladder[ladder.index(o["StageName"]) + 1])
            leads = org.objects["Lead"]["records"]
            for i in range(4):
                leads.append({"Id": f"00Q9{i:011d}AAA", "Name": f"Web lead {i + 1}",
                              "Company": COMPANIES[i + 5], "Email": f"lead{i}@web.example",
                              "IsConverted": False, "Status": "Open", "IsDeleted": False,
                              "CreatedDate": when, "SystemModstamp": when})
        elif night == 1:
            edit(northwind, when, OwnerId="005000000000004AAA", NumberOfEmployees=1450)
            for o in rnd.sample([o for o in opps if not o["IsClosed"]], 9):
                edit(o, when, Amount=o["Amount"] + 2500.0)
            for c in contacts[200:202]:
                edit(c, when, IsDeleted=True)
            for a in [a for a in accounts if not a.get("Region__c")][:25]:
                edit(a, when, Region__c=rnd.choice(["EMEA", "AMER", "APAC"]))
        else:
            edit(northwind, when, AnnualRevenue=410000.0, Churn_Risk__c="Low",
                 Description="Strategic account. Renewed for three years in November; "
                             "APAC expansion signed.")
            for c in rnd.sample(contacts, 30):
                edit(c, when, HasOptedOutOfEmail=not c["HasOptedOutOfEmail"])
            for i in range(7):
                opps.append({"Id": f"0069{i:011d}AAA", "Name": f"{COMPANIES[i]} renewal",
                             "AccountId": accounts[i]["Id"], "StageName": "Qualification",
                             "Amount": 12000.0 + 3500 * i, "CloseDate": "2027-03-31", "IsClosed": False,
                             "Is_Partner__c": False, "Source_System__c": "SFDC", "IsDeleted": False,
                             "OwnerId": "005000000000002AAA", "CreatedDate": when, "SystemModstamp": when})
        cli_mod.console = cli_mod.stdout = _console()  # not recorded
        runner.invoke(cli_mod.cli, ["export", "-d", "./acme", "--incremental", "--no-files"], obj={},
                      catch_exceptions=False)
        restamp(Path("acme"), finished)


def _console() -> Console:
    return Console(record=True, width=96, force_terminal=True, force_interactive=False,
                   highlight=False, color_system="truecolor")


def run(console, runner, args, shown=None):
    console.print(f"[bold green]$[/] sf-exit {shown or ' '.join(args)}")
    runner.invoke(cli_mod.cli, args, obj={}, catch_exceptions=False)


def main() -> None:
    org = demo_org()
    cli_mod.make_client = lambda kw: (kw.clear(), client_for(org))[1]
    tmp = Path(tempfile.mkdtemp())
    cwd = os.getcwd()
    os.chdir(tmp)
    runner = CliRunner()
    try:
        console = cli_mod.console = cli_mod.stdout = _console()
        run(console, runner, ["export", "-d", "./acme", "--metadata"],
            "export --org acme -d ./acme --metadata")
        console.print()
        run(console, runner, ["audit", "./acme"])
        (ROOT / "docs" / "demo.svg").write_text(console.export_svg(title="sf-exit export"), "utf-8")
        restamp(Path("acme"), "2026-10-31T22:00:00Z")
        a_month_of_backups(org, runner)

        shutil.copytree("acme", "acme-monday")
        tonight = "2026-11-24T14:30:00.000Z"
        contacts = org.objects["Contact"]["records"]
        for c in contacts[:37]:
            edit(c, tonight, Email=c["Email"].replace("@example.com", "@newco.example"))
        for c in contacts[100:105]:
            edit(c, tonight, LastName="Smith")
        for c in contacts[40:43]:
            edit(c, tonight, IsDeleted=True)
        for opp in [o for o in org.objects["Opportunity"]["records"] if not o["IsClosed"]][:12]:
            edit(opp, tonight, StageName="Closed Won", IsClosed=True)

        console = cli_mod.console = cli_mod.stdout = _console()
        run(console, runner, ["sql", "./acme",
                              'SELECT "StageName", count(*) AS deals, sum("Amount") AS pipeline '
                              'FROM "Opportunity" GROUP BY 1 ORDER BY pipeline DESC'],
            'sql ./acme "SELECT StageName, count(*) deals, sum(Amount) pipeline FROM Opportunity GROUP BY 1"')
        console.print()
        run(console, runner, ["export", "-d", "./acme", "--incremental", "--no-files"],
            "export --org acme -d ./acme --incremental   # tonight's backup")
        console.print()
        run(console, runner, ["diff", "./acme-monday", "./acme"])
        (ROOT / "docs" / "tools.svg").write_text(console.export_svg(title="sf-exit sql · backup · diff"),
                                                 "utf-8")
        restamp(Path("acme"), "2026-11-24T22:00:00Z")
        _screenshot(tmp / "acme-monday" / "audit" / "index.html", ROOT / "docs" / "audit.png")
        _browse_shots(tmp / "acme", ROOT / "docs")

        sandbox = demo_org()
        for name in ("Account", "Contact", "Opportunity", "Invoice__c", "Task", "Lead", "FAQ__kav"):
            sandbox.objects[name]["records"].clear()
        sandbox.objects["Organization"]["records"][0].update(
            Id="00D000000000002AAA", Name="Acme UAT", IsSandbox=True)
        cli_mod.make_client = lambda kw: (kw.clear(), client_for(sandbox))[1]
        console = cli_mod.console = cli_mod.stdout = _console()
        args = ["restore", "./acme", "-i", "Account", "-i", "Contact", "-i", "Opportunity",
                "-i", "Invoice__c"]
        shown = "restore ./acme --org uat-sandbox -i Account -i Contact -i Opportunity -i Invoice__c"
        run(console, runner, args, shown)
        console.print()
        run(console, runner, args + ["--execute"], shown + " --execute")
        (ROOT / "docs" / "restore.svg").write_text(console.export_svg(title="sf-exit restore"), "utf-8")
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def _screenshot(html: Path, dest: Path) -> None:
    dictionary = dest.with_name("dictionary.png")
    script = f"""
const {{chromium}} = require('playwright');
(async () => {{
  const b = await chromium.launch();
  const p = await b.newPage({{viewport: {{width: 1200, height: 900}}, deviceScaleFactor: 2}});
  await p.goto('file://{html}');
  await p.click('details.finding.high >> nth=1');
  await p.waitForTimeout(200);
  const dictTop = () => [...document.querySelectorAll('h2')].find(h => h.textContent === 'Data dictionary').getBoundingClientRect().top + window.scrollY;
  const dictY = await p.evaluate(dictTop);
  await p.screenshot({{path: '{dest}', clip: {{x: 0, y: 0, width: 1200, height: dictY - 10}}, fullPage: true}});
  await p.click('details.finding.high >> nth=1 >> summary');
  await p.click('#objs th[data-k=name]');
  await p.click('tr.obj[data-n=Account]');
  await p.waitForTimeout(200);
  const y = await p.evaluate(dictTop);
  await p.screenshot({{path: '{dictionary}', clip: {{x: 0, y: y - 16, width: 1200, height: 820}}, fullPage: true}});
  await b.close();
}})();"""
    try:
        subprocess.run(["node", "-e", script], check=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"skipped audit screenshot: {exc}")


def _browse_shots(export_dir: Path, docs: Path) -> None:
    from sf_exit.browse import BrowseServer
    from sf_exit.index import ensure_index

    ensure_index(export_dir)
    server = BrowseServer(export_dir, 0)
    server.serve_in_thread()
    script = f"""
const {{chromium}} = require('playwright');
(async () => {{
  const b = await chromium.launch();
  const p = await b.newPage({{viewport: {{width: 1280, height: 860}}, deviceScaleFactor: 2}});
  await p.goto('{server.url}#/');
  await p.waitForSelector('#charts svg');
  await p.waitForTimeout(300);
  const dash = await p.evaluate(() => document.querySelector('#charts').getBoundingClientRect().bottom + window.scrollY);
  await p.setViewportSize({{width: 1280, height: Math.ceil(dash + 24)}});
  await p.waitForTimeout(200);
  await p.screenshot({{path: '{docs / "dashboard.png"}'}});
  await p.setViewportSize({{width: 1280, height: 860}});
  await p.goto('{server.url}#/r/001000000000001AAA');
  await p.waitForSelector('#rel table');
  await p.waitForSelector('.tl');
  await p.screenshot({{path: '{docs / "browse.png"}'}});
  await p.setViewportSize({{width: 1120, height: 1700}});
  const hist = await p.evaluate(() => {{
    const h = [...document.querySelectorAll('h2')].find(e => e.textContent.startsWith('History'));
    h.scrollIntoView({{block: 'start'}});
    const m = document.querySelector('#main').getBoundingClientRect();
    const top = h.getBoundingClientRect().top - 14;
    const end = document.querySelector('#main').lastElementChild.getBoundingClientRect().bottom + 18;
    return {{x: m.left, y: Math.max(0, top), w: m.width, h: Math.min(end, 1700) - Math.max(0, top)}};
  }});
  await p.screenshot({{path: '{docs / "history.png"}', clip: {{x: hist.x, y: hist.y, width: hist.w, height: hist.h}}}});
  await p.setViewportSize({{width: 1280, height: 860}});
  await p.goto('{server.url}#/s/northwind');
  await p.waitForSelector('.hit');
  await p.screenshot({{path: '{docs / "browse-search.png"}', clip: {{x: 0, y: 0, width: 1280, height: 560}}}});
  await b.close();
}})();"""
    try:
        subprocess.run(["node", "-e", script], check=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"skipped browse screenshots: {exc}")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

"""Regenerate the README's demo assets from real sf-exit runs against a simulated org.

    python scripts/record_demo.py          # needs duckdb; audit screenshot needs playwright

Writes:
  docs/demo.svg       export (records + files + metadata) and audit summary
  docs/tools.svg      sf-exit sql + incremental backup + diff
  docs/audit.png      the HTML audit report's findings (needs node + playwright)
  docs/dictionary.png the HTML audit report's data dictionary
"""

from __future__ import annotations

import hashlib
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
from tests.fake_org import METADATA_TYPES, F, build_org, client_for  # noqa: E402

COMPANIES = ["Northwind Traders", "Globex", "Initech", "Umbrella Health", "Stark Industries",
             "Wayne Logistics", "Hooli", "Vandelay Imports", "Soylent Foods", "Cyberdyne Systems",
             "Wonka Confectionery", "Tyrell Robotics", "Massive Dynamic", "Pied Piper", "Gringotts",
             "Oscorp", "Aperture Labs", "Black Mesa", "Monarch Solutions", "Dunder Mifflin"]
FIRST = ["Ada", "Grace", "Alan", "Katherine", "Linus", "Margaret", "Dennis", "Barbara", "Ken",
         "Radia", "Tim", "Frances", "John", "Hedy", "Edsger", "Sophie", "Guido", "Annie"]
LAST = ["Lovelace", "Hopper", "Turing", "Johnson", "Torvalds", "Hamilton", "Ritchie", "Liskov",
        "Thompson", "Perlman", "Berners-Lee", "Allen", "McCarthy", "Lamarr", "Dijkstra", "Wilson"]


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
    METADATA_TYPES.pop("BrokenType", None)
    for i in range(48):
        METADATA_TYPES["ApexClass"][f"Service{i}"] = {f"classes/Service{i}.cls": "public class X {}"}
    for i in range(14):
        METADATA_TYPES["Flow"][f"Flow_{i}"] = {f"flows/Flow_{i}.flow-meta.xml": "<Flow/>"}
    METADATA_TYPES["PermissionSet"] = {f"PS_{i}": {f"permissionsets/PS_{i}.permissionset": "<x/>"}
                                       for i in range(9)}
    METADATA_TYPES["ApexTrigger"] = {f"T{i}": {f"triggers/T{i}.trigger": "trigger T on Account (before insert) {}"}
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

        shutil.copytree("acme", "acme-monday")
        contacts = org.objects["Contact"]["records"]
        for c in contacts[:37]:
            c.update(Email=c["Email"].replace("@example.com", "@newco.example"),
                     SystemModstamp="2099-01-01T00:00:00.000Z")
        for c in contacts[100:105]:
            c.update(LastName="Smith", SystemModstamp="2099-01-01T00:00:00.000Z")
        for c in contacts[40:43]:
            c.update(IsDeleted=True, SystemModstamp="2099-01-01T00:00:00.000Z")
        for opp in org.objects["Opportunity"]["records"][:12]:
            opp.update(StageName="Closed Won", IsClosed=True, SystemModstamp="2099-01-01T00:00:00.000Z")

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
        _screenshot(tmp / "acme-monday" / "audit" / "index.html", ROOT / "docs" / "audit.png")
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
  const dictY = await p.evaluate(() => document.querySelectorAll('h2')[1].getBoundingClientRect().top + window.scrollY);
  await p.screenshot({{path: '{dest}', clip: {{x: 0, y: 0, width: 1200, height: dictY - 10}}, fullPage: true}});
  await p.click('details.finding.high >> nth=1 >> summary');
  await p.click('#objs th[data-k=name]');
  await p.click('tr.obj[data-n=Account]');
  await p.waitForTimeout(200);
  const y = await p.evaluate(() => document.querySelectorAll('h2')[1].getBoundingClientRect().top + window.scrollY);
  await p.screenshot({{path: '{dictionary}', clip: {{x: 0, y: y - 16, width: 1200, height: 820}}, fullPage: true}});
  await b.close();
}})();"""
    try:
        subprocess.run(["node", "-e", script], check=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"skipped audit screenshot: {exc}")


if __name__ == "__main__":
    main()

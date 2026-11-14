"""Regenerate docs/demo.svg: a real sf-exit run against the test suite's simulated org.

    python scripts/record_demo.py
"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from click.testing import CliRunner  # noqa: E402
from rich.console import Console  # noqa: E402

import sf_exit.cli as cli_mod  # noqa: E402
from tests.fake_org import build_org, client_for  # noqa: E402


def _add_files(org, n: int) -> None:
    rnd = random.Random(1)
    cv = org.objects["ContentVersion"]["records"]
    cd = org.objects["ContentDocument"]["records"]
    for i in range(10, 10 + n):
        data = os.urandom(rnd.randint(20_000, 400_000))
        vid, did = f"068{i:012d}AAA", f"069{i:012d}AAA"
        cv.append({"Id": vid, "ContentDocumentId": did, "Title": f"Proposal {i}", "FileExtension": "pdf",
                   "VersionNumber": "1", "ContentSize": len(data), "IsLatest": True, "IsDeleted": False,
                   "Checksum": hashlib.md5(data).hexdigest()})
        cd.append({"Id": did, "Title": f"Proposal {i}", "IsDeleted": False})
        org.blobs[f"ContentVersion/{vid}"] = data


def main() -> None:
    org = build_org(scale=420)  # ~10k Accounts, ~17k Contacts: big enough to use the Bulk API
    org.bulk_page = 5000
    org.rest_page = 2000
    _add_files(org, 240)
    console = Console(record=True, width=92, force_terminal=True, force_interactive=False, highlight=False,
                      color_system="truecolor")
    cli_mod.console = console

    def fake_client(kw):
        kw.clear()
        c = client_for(org)
        return c

    cli_mod.make_client = fake_client
    tmp = Path(tempfile.mkdtemp())
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        runner = CliRunner()
        console.print("[bold green]$[/] sf-exit export --org acme -d ./acme-export")
        runner.invoke(cli_mod.cli, ["export", "-d", "./acme-export"], obj={}, catch_exceptions=False)
        console.print()
        console.print("[bold green]$[/] sf-exit load ./acme-export --to duckdb:///acme.duckdb")
        runner.invoke(cli_mod.cli, ["load", "./acme-export", "--to", "duckdb:///acme.duckdb"],
                      obj={}, catch_exceptions=False)
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)
    text = console.export_text(clear=False)
    svg = console.export_svg(title="sf-exit", clear=True)
    (ROOT / "docs" / "demo.svg").write_text(svg, "utf-8")
    print(text)


if __name__ == "__main__":
    main()

"""Prove the export is complete: re-count CSV rows, re-hash files, compare with the live org."""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from sf_exit.client import SalesforceClient, SalesforceError
from sf_exit.utils import file_sha256

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


@dataclass
class Check:
    name: str
    kind: str  # records | file
    expected: Optional[int]
    actual: Optional[int]
    ok: bool
    detail: str = ""


def load_manifest(export_dir: Path) -> Dict:
    path = export_dir / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"No manifest.json in {export_dir}: is this an sf-exit export?")
    return json.loads(path.read_text("utf-8"))


def count_csv_rows(path: Path) -> int:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        return sum(1 for _ in reader)


def verify_offline(export_dir: Path, on_check: Optional[Callable[[Check], None]] = None) -> List[Check]:
    m = load_manifest(export_dir)
    checks: List[Check] = []
    emit = on_check or (lambda c: None)
    for name, obj in m["objects"].items():
        if obj["status"] != "done":
            continue
        path = export_dir / obj["file"]
        if not path.exists():
            c = Check(name, "records", obj["rows"], None, False, "CSV file missing")
        else:
            rows = count_csv_rows(path)
            sha_ok = not obj.get("sha256") or file_sha256(path) == obj["sha256"]
            detail = "" if sha_ok else "SHA-256 differs from manifest (file changed since export)"
            c = Check(name, "records", obj["rows"], rows, rows == obj["rows"] and sha_ok, detail)
        checks.append(c)
        emit(c)
    index = export_dir / "files" / "index.csv"
    if index.exists():
        bad = 0
        total = 0
        with open(index, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if not row["status"].startswith("ok"):
                    continue
                total += 1
                p = export_dir / row["path"]
                if not p.exists() or file_sha256(p) != row["sha256"]:
                    bad += 1
        c = Check("files/", "file", total, total - bad, bad == 0,
                  f"{bad} file(s) missing or altered" if bad else "")
        checks.append(c)
        emit(c)
    return checks


def verify_live(client: SalesforceClient, export_dir: Path,
                on_check: Optional[Callable[[Check], None]] = None) -> List[Check]:
    """Compare exported row counts with a fresh COUNT() against the org.

    Records created/deleted after the export finished will show up as drift; that is
    expected for a live org and is reported, not treated as corruption.
    """
    m = load_manifest(export_dir)
    deleted = bool(m.get("include_deleted"))
    checks: List[Check] = []
    emit = on_check or (lambda c: None)
    for name, obj in m["objects"].items():
        if obj["status"] != "done" or name == "ContentDocumentLink":
            continue
        try:
            live = client.count(name, include_deleted=deleted)
            ok = live == obj["rows"]
            detail = "" if ok else f"{live - obj['rows']:+,} since export"
            c = Check(name, "records", obj["rows"], live, ok, detail)
        except SalesforceError as exc:
            c = Check(name, "records", obj["rows"], None, False, f"could not count: {exc}")
        checks.append(c)
        emit(c)
    return checks

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
    """Re-count every CSV and re-hash every file against manifest.json.

    Anything the export itself reported as incomplete fails here too: failed objects,
    objects with partial results, files that failed to download or whose MD5 didn't match
    Salesforce's checksum, and file kinds that could not be listed."""
    m = load_manifest(export_dir)
    checks: List[Check] = []
    emit = on_check or (lambda c: None)

    def add(c: Check) -> None:
        checks.append(c)
        emit(c)

    for name, obj in m["objects"].items():
        if obj["status"] == "failed":
            add(Check(name, "records", None, None, False, f"export failed: {obj.get('error', '')[:200]}"))
            continue
        if obj["status"] != "done":
            continue  # skipped: Salesforce doesn't allow exporting it (listed in REPORT.md)
        path = export_dir / obj["file"]
        if not path.exists():
            add(Check(name, "records", obj["rows"], None, False, "CSV file missing"))
            continue
        rows = count_csv_rows(path)
        sha_ok = not obj.get("sha256") or file_sha256(path) == obj["sha256"]
        problems = []
        if not sha_ok:
            problems.append("SHA-256 differs from manifest (file changed since export)")
        problems += [w for w in obj.get("warnings") or [] if w.startswith("partial")]
        add(Check(name, "records", obj["rows"], rows, rows == obj["rows"] and not problems,
                  "; ".join(problems)))
    for kind, err in (m.get("file_listing_errors") or {}).items():
        add(Check(f"files/{kind}", "file", None, None, False, f"could not be listed: {err[:200]}"))
    index = export_dir / "files" / "index.csv"
    if index.exists():
        altered = failed = mismatched = total = 0
        with open(index, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                total += 1
                status = row.get("status", "")
                if status == "checksum-mismatch":
                    mismatched += 1
                    continue
                if not status.startswith("ok"):
                    failed += 1
                    continue
                p = export_dir / row["path"]
                if not p.exists() or file_sha256(p) != row["sha256"]:
                    altered += 1
        bad = altered + failed + mismatched
        detail = ", ".join(f"{n} {what}" for n, what in (
            (altered, "missing or altered"), (failed, "failed to download"),
            (mismatched, "differ from Salesforce's checksum")) if n)
        add(Check("files/", "file", total, total - bad, bad == 0, detail))
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
            live = _live_count(client, export_dir, name, deleted)
            ok = live == obj["rows"]
            detail = "" if ok else f"{live - obj['rows']:+,} since export"
            c = Check(name, "records", obj["rows"], live, ok, detail)
        except SalesforceError as exc:
            c = Check(name, "records", obj["rows"], None, False, f"could not count: {exc}")
        checks.append(c)
        emit(c)
    return checks


def _live_count(client: SalesforceClient, export_dir: Path, name: str, deleted: bool) -> int:
    """COUNT() the object, using the same mandatory filters the export used (e.g. Knowledge)."""
    from sf_exit.discovery import plan_object

    schema = export_dir / "schema" / f"{name}.json"
    variants = []
    if schema.exists():
        variants = plan_object(json.loads(schema.read_text("utf-8"))).where_variants
    if not variants:
        return client.count(name, include_deleted=deleted)
    total = 0
    for where in variants:  # a partial count would hide a partial export: fail instead
        try:
            total += client.count(name, include_deleted=deleted, where=where)
        except SalesforceError as exc:
            raise SalesforceError(f"rows matching {where} could not be counted: {exc}",
                                  exc.status, exc.code) from exc
    return total

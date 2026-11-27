"""Time machine: the history of every record, and the whole org as it was at any moment.

Every export run into an existing folder records what changed in ``changes/<run>/``:
``<Object>.csv`` holds the new version of each record that was created, changed or deleted,
``<Object>.before.csv`` the version it replaced, and ``run.json`` when the run happened.
That is enough to answer "what did this record look like last Tuesday?" and to rebuild a
complete export as of any moment between two runs.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from sf_exit.utils import file_sha256, utcnow

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

NOISE = {"LastViewedDate", "LastReferencedDate", "SystemModstamp", "LastModifiedDate",
         "LastModifiedById"}


class HistoryError(Exception):
    pass


@dataclass
class Run:
    stamp: str
    kind: str
    finished_at: str
    started_at: str = ""
    objects: Dict[str, int] = field(default_factory=dict)

    @property
    def dir(self) -> str:
        return f"changes/{self.stamp}"


def _parse(ts: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def parse_when(text: str) -> dt.datetime:
    """'2026-11-20' (end of that day, UTC), '2026-11-20T09:30', '2026-11-20T09:30:00Z'."""
    text = text.strip()
    try:
        if len(text) == 10:
            day = dt.date.fromisoformat(text)
            return dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=dt.timezone.utc)
        return _parse(text)
    except ValueError as exc:
        raise HistoryError(f"Can't read the date '{text}'. Use e.g. 2026-11-20 or "
                           "2026-11-20T09:30:00Z.") from exc


def _manifest(export_dir: Path) -> Dict[str, Any]:
    path = Path(export_dir) / "manifest.json"
    if not path.exists():
        raise HistoryError(f"No manifest.json in {export_dir}: is this an sf-exit export?")
    return json.loads(path.read_text("utf-8"))


def runs(export_dir: Path) -> List[Run]:
    """Every run that recorded changes, oldest first."""
    out = []
    root = Path(export_dir) / "changes"
    for meta in sorted(root.glob("*/run.json")) if root.is_dir() else []:
        try:
            d = json.loads(meta.read_text("utf-8"))
        except ValueError:
            continue
        out.append(Run(d["stamp"], d.get("kind", "full"), d["finished_at"], d.get("started_at", ""),
                       d.get("objects") or {}))
    return sorted(out, key=lambda r: r.finished_at)


def baseline(export_dir: Path) -> Optional[str]:
    """When the oldest state this folder knows about was captured."""
    m = _manifest(export_dir)
    recorded = [r.get("finished_at") for r in m.get("runs") or [] if r.get("finished_at")]
    return min(recorded) if recorded else m.get("finished_at")


def _rows(path: Path) -> Iterator[Dict[str, str]]:
    if path.exists():
        with open(path, newline="", encoding="utf-8") as fh:
            yield from csv.DictReader(fh)


def _find(path: Path, record_id: str) -> Optional[Dict[str, str]]:
    found = None
    for row in _rows(path):
        if row.get("Id") == record_id:
            found = row  # keep the last occurrence, like the merge does
    return found


def _object_for(export_dir: Path, record_id: str) -> List[str]:
    prefix = record_id[:3]
    hits, others = [], []
    for schema in sorted((Path(export_dir) / "schema").glob("*.json")):
        try:
            key = json.loads(schema.read_text("utf-8")).get("keyPrefix")
        except ValueError:
            key = None
        (hits if key == prefix else others).append(schema.stem)
    return hits or others


def _gone(row: Dict[str, str]) -> bool:
    return any((row.get(c) or "").lower() == "true" for c in ("IsDeleted", "IsArchived"))


def diff_rows(before: Dict[str, str], after: Dict[str, str]) -> Dict[str, Tuple[str, str]]:
    return {k: (before.get(k, ""), after.get(k, "")) for k in after
            if k not in NOISE and k != "Id" and before.get(k, "") != after.get(k, "")}


def record_history(export_dir: Path, record_id: str) -> Dict[str, Any]:
    """Every recorded version of one record, oldest first."""
    export_dir = Path(export_dir)
    all_runs = runs(export_dir)
    for obj in _object_for(export_dir, record_id):
        current = _find(export_dir / "data" / f"{obj}.csv", record_id)
        versions = []
        for run in all_runs:
            if run.objects and obj not in run.objects:
                continue
            after = _find(export_dir / run.dir / f"{obj}.csv", record_id)
            before = _find(export_dir / run.dir / f"{obj}.before.csv", record_id)
            if not after and not before:
                continue
            if after and not _gone(after):
                change = "updated" if before else "created"
            else:
                change = "deleted"
            versions.append({
                "run": run.stamp, "at": run.finished_at, "change": change,
                "fields": diff_rows(before, after) if before and after and change == "updated" else {},
                "values": after or before,
            })
        if current or versions:
            return {"object": obj, "id": record_id, "current": current, "versions": versions,
                    "baseline": baseline(export_dir)}
    raise HistoryError(f"No record with Id {record_id} in this export or its history.")


def changed_records(export_dir: Path, stamp: str, obj: str, limit: int = 500) -> List[Dict[str, Any]]:
    """What changed in one object during one run."""
    base = Path(export_dir) / "changes" / stamp
    before = {r["Id"]: r for r in _rows(base / f"{obj}.before.csv") if r.get("Id")}
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for after in _rows(base / f"{obj}.csv"):
        rid = after.get("Id", "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        b = before.get(rid)
        change = "deleted" if _gone(after) else ("updated" if b else "created")
        out.append({"id": rid, "change": change,
                    "fields": sorted(diff_rows(b, after)) if b and change == "updated" else []})
    for rid in before:
        if rid not in seen:
            out.append({"id": rid, "change": "deleted", "fields": []})
    return out[:limit]


def snapshot(export_dir: Path, as_of: dt.datetime, out_dir: Path, with_files: bool = False) -> Dict[str, Any]:
    """Write a complete export folder as the org was at ``as_of``."""
    export_dir, out_dir = Path(export_dir), Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise HistoryError(f"{out_dir} already exists and isn't empty.")
    m = _manifest(export_dir)
    oldest = baseline(export_dir)
    if not oldest or as_of < _parse(oldest):
        raise HistoryError(f"This export's history starts at {oldest}: it can't show the org "
                           f"as it was at {as_of.strftime('%Y-%m-%dT%H:%M:%SZ')}.")
    later = [r for r in runs(export_dir) if _parse(r.finished_at) > as_of]

    (out_dir / "data").mkdir(parents=True)
    objects: Dict[str, Any] = {}
    for src in sorted((export_dir / "data").glob("*.csv")):
        obj = src.stem
        decided: Dict[str, Optional[Dict[str, str]]] = {}  # Id -> version at as_of (None = absent)
        for run in later:  # the earliest change after as_of holds the version from before it
            befores = {r["Id"]: r for r in _rows(export_dir / run.dir / f"{obj}.before.csv")}
            for r in _rows(export_dir / run.dir / f"{obj}.csv"):
                decided.setdefault(r["Id"], befores.get(r["Id"]))
            for rid, r in befores.items():
                decided.setdefault(rid, r)
        dest = out_dir / "data" / src.name
        rows = 0
        with open(src, newline="", encoding="utf-8") as fh, \
                open(dest, "w", newline="", encoding="utf-8") as out:
            reader = csv.DictReader(fh)
            header = reader.fieldnames or ["Id"]
            writer = csv.DictWriter(out, fieldnames=header, quoting=csv.QUOTE_ALL, lineterminator="\n",
                                    extrasaction="ignore", restval="")
            writer.writeheader()
            written: Set[str] = set()
            for row in reader:
                rid = row.get("Id", "")
                if rid in decided:
                    version = decided[rid]
                    if version is not None:
                        writer.writerow(version)
                        rows += 1
                    written.add(rid)
                else:
                    writer.writerow(row)
                    rows += 1
            for rid, version in decided.items():  # deleted since then: bring them back
                if rid not in written and version is not None:
                    writer.writerow(version)
                    rows += 1
        prev = (m.get("objects") or {}).get(obj, {})
        objects[obj] = {**prev, "name": obj, "status": prev.get("status", "done"), "rows": rows,
                        "file": f"data/{src.name}", "sha256": file_sha256(dest), "changed": None}

    if (export_dir / "schema").is_dir():
        shutil.copytree(export_dir / "schema", out_dir / "schema")
    for name in ("schema.postgres.sql", "schema.duckdb.sql", "schema.sqlite.sql", "erd.mmd"):
        if (export_dir / name).exists():
            shutil.copy2(export_dir / name, out_dir / name)
    if (export_dir / "files" / "index.csv").exists():
        if with_files:
            _link_tree(export_dir / "files", out_dir / "files")
        else:
            (out_dir / "files").mkdir()
            shutil.copy2(export_dir / "files" / "index.csv", out_dir / "files" / "index.csv")

    snap = dict(m)
    snap.update({
        "objects": objects,
        "snapshot": {"of": str(export_dir.resolve()), "as_of": as_of.astimezone(dt.timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ"), "created_at": utcnow(), "with_files": with_files},
        "runs": [r for r in m.get("runs") or [] if r.get("finished_at") and _parse(r["finished_at"]) <= as_of],
        "changes_dir": None, "incremental_since": None,
    })
    snap["totals"] = {**(m.get("totals") or {}), "objects": len(objects),
                      "rows": sum(o["rows"] for o in objects.values()), "changed_records": 0}
    (out_dir / "manifest.json").write_text(json.dumps(snap, indent=2), "utf-8")
    from sf_exit.exporter import render_report

    (out_dir / "REPORT.md").write_text(render_report(snap), "utf-8")
    return snap


def _link_tree(src: Path, dest: Path) -> None:
    """Hard-link the files (no extra disk space) where possible, else copy them."""
    for path in src.rglob("*"):
        target = dest / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, target)
        except OSError:
            shutil.copy2(path, target)

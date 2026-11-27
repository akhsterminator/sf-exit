"""Compare two exports: which records were added, removed or changed, and which fields."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# Fields that change without anyone editing the record.
NOISE = {"LastViewedDate", "LastReferencedDate", "SystemModstamp"}
# Change with every edit, so they say nothing about *what* changed: left out of the ranking.
AUDIT_FIELDS = {"LastModifiedDate", "LastModifiedById"}
NUMERIC_TYPES = {"int", "long", "double", "currency", "percent"}
PARALLEL_THRESHOLD_BYTES = 64 * 1024 * 1024


@dataclass
class ObjectDiff:
    name: str
    old_rows: int = 0
    new_rows: int = 0
    added: int = 0
    removed: int = 0
    changed: int = 0
    fields_added: List[str] = field(default_factory=list)
    fields_removed: List[str] = field(default_factory=list)
    changed_fields: Dict[str, int] = field(default_factory=dict)  # field -> rows changed
    status: str = "both"  # both | new-object | removed-object

    @property
    def touched(self) -> bool:
        return bool(self.added or self.removed or self.changed or self.fields_added
                    or self.fields_removed or self.status != "both")


def _header(path: Path) -> List[str]:
    with open(path, newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh), [])


def _canon(value: str, numeric: bool) -> str:
    """Numbers compare by value: '1000', '1000.0' and '1E+3' are the same amount."""
    if numeric and value:
        try:
            return format(Decimal(value).normalize(), "f")
        except InvalidOperation:
            return value
    return value


def _numeric_columns(export_dir: Path, name: str) -> Set[str]:
    schema = export_dir / "schema" / f"{name}.json"
    if not schema.exists():
        return set()
    try:
        fields = json.loads(schema.read_text("utf-8")).get("fields", [])
    except ValueError:
        return set()
    return {f["name"] for f in fields if f.get("type") in NUMERIC_TYPES}


def _hashes(path: Path, cols: List[str], numeric: Set[str] = frozenset()) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        idx = [(header.index(c), c in numeric) for c in cols]
        for row in reader:
            if row:
                h = hashlib.blake2b("\x1f".join(_canon(row[i], n) for i, n in idx).encode(),
                                    digest_size=10)
                out[row[0]] = h.digest()
    return out


def _rows_for(path: Path, ids: Set[str]) -> Dict[str, Dict[str, str]]:
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("Id") in ids:
                out[r["Id"]] = r
    return out


def _count(path: Path) -> int:
    with open(path, newline="", encoding="utf-8") as fh:
        return max(sum(1 for _ in csv.reader(fh)) - 1, 0)


def diff_object(old: Optional[Path], new: Optional[Path], name: str,
                details_dir: Optional[Path] = None) -> ObjectDiff:
    d = ObjectDiff(name)
    if old is None and new is not None:
        d.status, d.new_rows = "new-object", _count(new)
        d.added = d.new_rows
        return d
    if new is None and old is not None:
        d.status, d.old_rows = "removed-object", _count(old)
        d.removed = d.old_rows
        return d
    assert old is not None and new is not None
    oh, nh = _header(old), _header(new)
    if not oh or oh[0] != "Id" or not nh or nh[0] != "Id":
        return d
    d.fields_added = [c for c in nh if c not in oh]
    d.fields_removed = [c for c in oh if c not in nh]
    common = [c for c in nh if c in oh and c not in NOISE]
    numeric = _numeric_columns(new.parent.parent, name) | _numeric_columns(old.parent.parent, name)
    a, b = _hashes(old, common, numeric), _hashes(new, common, numeric)
    d.old_rows, d.new_rows = len(a), len(b)
    added = b.keys() - a.keys()
    removed = a.keys() - b.keys()
    changed = {k for k in a.keys() & b.keys() if a[k] != b[k]}
    d.added, d.removed, d.changed = len(added), len(removed), len(changed)

    if changed or details_dir:
        old_rows = _rows_for(old, changed | removed)
        new_rows = _rows_for(new, changed | added)
        counts: Counter = Counter()
        detail_rows = []
        for k in sorted(changed):
            diffs = [c for c in common if _canon(old_rows[k].get(c) or "", c in numeric)
                     != _canon(new_rows[k].get(c) or "", c in numeric)]
            counts.update(c for c in diffs if c not in AUDIT_FIELDS)
            for c in diffs:
                detail_rows.append([k, "changed", c, old_rows[k].get(c, ""), new_rows[k].get(c, "")])
        d.changed_fields = dict(counts.most_common())
        if details_dir and (added or removed or changed):
            details_dir.mkdir(parents=True, exist_ok=True)
            with open(details_dir / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh, lineterminator="\n")
                w.writerow(["Id", "change", "field", "old_value", "new_value"])
                w.writerows([k, "added", "", "", ""] for k in sorted(added))
                w.writerows([k, "removed", "", "", ""] for k in sorted(removed))
                w.writerows(detail_rows)
    return d


def diff_exports(old_dir: Path, new_dir: Path, details_dir: Optional[Path] = None,
                 only: Optional[List[str]] = None) -> List[ObjectDiff]:
    old_files = {p.stem: p for p in (Path(old_dir) / "data").glob("*.csv")}
    new_files = {p.stem: p for p in (Path(new_dir) / "data").glob("*.csv")}
    if not old_files and not new_files:
        raise FileNotFoundError("Neither folder contains data/*.csv: are these sf-exit exports?")
    names = sorted(set(old_files) | set(new_files), key=str.lower)
    if only:
        names = [n for n in names if n in only]
    jobs = [(old_files.get(n), new_files.get(n), n, details_dir) for n in names]
    size = sum(p.stat().st_size for p in list(old_files.values()) + list(new_files.values()))
    workers = min(os.cpu_count() or 1, 8, len(jobs))
    if workers > 1 and size > PARALLEL_THRESHOLD_BYTES:  # big exports: one object per core
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                return list(pool.map(_diff_job, jobs))
        except (OSError, RuntimeError, ImportError):
            pass
    return [_diff_job(j) for j in jobs]


def _diff_job(job: Tuple[Optional[Path], Optional[Path], str, Optional[Path]]) -> ObjectDiff:
    return diff_object(*job)

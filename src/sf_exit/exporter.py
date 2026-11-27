"""The export pipeline: discover → describe → export records → export files → write manifest.

Every run into an existing export folder also records *history*: for each record that was
created, changed or deleted since the previous run, ``changes/<run>/`` keeps the new version
(``<Object>.csv``) and the version it replaced (``<Object>.before.csv``). ``sf-exit history``
and ``sf-exit snapshot`` use that to show any record's timeline or rebuild the org as it was.
"""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

from sf_exit import __version__
from sf_exit.client import (
    COMPOSITE_MAX,
    COMPOSITE_MAX_QUERIES,
    SalesforceClient,
    SalesforceError,
    soql_quote,
)
from sf_exit.discovery import ObjectPlan, _matches, is_not_exportable, plan_object, select_objects
from sf_exit.schema import DIALECTS, mermaid_erd, schema_sql
from sf_exit.utils import file_sha256, human_bytes, safe_filename, utcnow

log = logging.getLogger(__name__)
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# Above this many rows the Bulk API is used; below it REST (batched via /composite) is faster.
BULK_THRESHOLD = 10_000
# Long SOQL can't go in a GET URL, so very wide objects always use Bulk (POST).
MAX_REST_SOQL = 12_000
CDL_CHUNK = 200
# Recording history for an object re-exported in full holds one small hash per row in memory.
HISTORY_MAX_BYTES = 2 * 1024**3
# Fields that change without anyone editing the record: not "changes" for history purposes.
HISTORY_NOISE = {"LastViewedDate", "LastReferencedDate", "SystemModstamp"}
NUMERIC_TYPES = {"int", "long", "double", "currency", "percent"}

PII_NAME = re.compile(
    r"(^|_)ssn(_|$)|social_?security|birth_?date|date_?of_?birth|(^|_)dob(_|$)|passport|"
    r"tax_?id|national_?id|(^|_)iban(_|$)|bank_?account|e_?mail|phone|mobile|(^|_)fax(_|$)")
PII_ADDRESS = re.compile(r"^(billing|shipping|mailing|other|home)?(street|city|postalcode|"
                         r"latitude|longitude)$")
PERSON_NAME_FIELDS = {"FirstName", "LastName", "MiddleName", "Suffix"}
PERSON_OBJECTS = {"Contact", "Lead", "User", "Individual"}
USER_PII_FIELDS = {"Username", "Alias", "CommunityNickname", "FederationIdentifier"}
TEXT_TYPES = {"string", "textarea", "email", "phone", "url", "combobox", "encryptedstring"}


class ExportError(Exception):
    """A configuration problem detected before any data is exported."""


class Progress:
    """Progress callbacks. The CLI plugs in a Rich implementation; tests use this no-op."""

    def phase(self, text: str) -> None: ...
    def start(self, key: str, label: str, total: Optional[int]) -> None: ...
    def advance(self, key: str, n: int) -> None: ...
    def finish(self, key: str, status: str, detail: str = "") -> None: ...
    def log(self, text: str) -> None: ...


@dataclass
class ExportOptions:
    out_dir: Path
    include: Sequence[str] = ()
    exclude: Sequence[str] = ()
    include_deleted: bool = False
    engine: str = "auto"  # auto | bulk | rest
    workers: int = 4
    file_workers: int = 8
    files: bool = True
    all_file_versions: bool = False
    data: bool = True
    fresh: bool = False  # discard the progress of an interrupted run instead of resuming it
    max_records: Optional[int] = None
    poll_interval: float = 2.0
    incremental: bool = False  # only fetch rows changed since the previous export
    redact: Sequence[str] = ()  # "Object.Field" globs to hash, e.g. "Contact.Email", "*.Phone"
    redact_pii: bool = False  # also hash emails, phones, person names, addresses, birthdates...
    redact_salt: Optional[str] = None
    metadata: bool = False  # also retrieve configuration via the Metadata API
    metadata_include: Sequence[str] = ()
    metadata_exclude: Sequence[str] = ()
    history: bool = True  # record changes/ when re-exporting into an existing export


@dataclass
class ObjectResult:
    name: str
    status: str  # done | failed | skipped (Salesforce won't export it)
    rows: int = 0
    engine: str = ""
    file: str = ""
    sha256: str = ""
    error: str = ""
    seconds: float = 0.0
    changed: Optional[int] = None  # rows created/updated/deleted since the previous run
    redacted: List[str] = field(default_factory=list)
    started_at: str = ""  # when this object's query started: the watermark for --incremental
    warnings: List[str] = field(default_factory=list)  # e.g. part of a Knowledge export failed

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ObjectResult:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class ExportResult:
    out_dir: Path
    objects: Dict[str, ObjectResult] = field(default_factory=dict)
    skipped: Dict[str, str] = field(default_factory=dict)
    files: Dict[str, Dict[str, int]] = field(default_factory=dict)
    file_errors: int = 0
    listing_errors: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    api_calls: int = 0
    incremental_since: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    changes_dir: Optional[str] = None
    changed_records: int = 0
    resumed: bool = False

    @property
    def total_rows(self) -> int:
        return sum(o.rows for o in self.objects.values() if o.status == "done")

    @property
    def failed(self) -> List[ObjectResult]:
        return [o for o in self.objects.values() if o.status == "failed"]

    @property
    def ok(self) -> bool:
        return not (self.failed or self.file_errors or self.listing_errors)


def new_stamp() -> str:
    now = dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"


class State:
    """The current run's progress, in <out>/.sf-exit/state.json.

    A run that was interrupted (Ctrl-C, crash, CI timeout) is resumed by running the same
    command again: finished objects are skipped. A run that completed is never reused, so
    the next run exports everything afresh. ``fresh=True`` discards an interrupted run.
    """

    def __init__(self, path: Path, fingerprint: Dict[str, Any], fresh: bool = False):
        self.path = path
        self.lock = threading.Lock()
        self.resumed = False
        loaded: Dict[str, Any] = {}
        if path.exists() and not fresh:
            with contextlib.suppress(ValueError, OSError):
                loaded = json.loads(path.read_text("utf-8"))
        run = loaded.get("run") or {}
        if run.get("status") == "in_progress" and loaded.get("options") == fingerprint:
            self.data = loaded
            self.resumed = True
        else:
            self.data = {"run": {"stamp": new_stamp(), "started_at": utcnow(),
                                 "status": "in_progress"},
                         "options": fingerprint, "objects": {}}
        self._write()

    @property
    def stamp(self) -> str:
        return self.data["run"]["stamp"]

    @property
    def started_at(self) -> str:
        return self.data["run"]["started_at"]

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        return self.data["objects"].get(name)

    def put(self, result: ObjectResult) -> None:
        with self.lock:
            self.data["objects"][result.name] = asdict(result)
            self._write()

    def complete(self) -> None:
        with self.lock:
            self.data["run"]["status"] = "complete"
            self.data["run"]["finished_at"] = utcnow()
            self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), "utf-8")
        tmp.replace(self.path)


def _fmt_value(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        return format(value, "f")  # exact, never scientific notation
    if isinstance(value, float):
        return format(Decimal(repr(value)), "f")
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), default=str)
    s = str(value)
    # REST returns 2024-01-31T09:00:00.000+0000, Bulk returns ...000Z. Normalise to Z.
    if len(s) == 28 and s.endswith("+0000") and s[10] == "T":
        s = s[:-5] + "Z"
    return s


def write_records_csv(records: Iterable[Dict[str, Any]], columns: List[str], dest: Path,
                      on_rows: Optional[Callable[[int], None]] = None) -> int:
    rows = 0
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
        writer.writerow(columns)
        batch = 0
        for rec in records:
            writer.writerow([_fmt_value(rec.get(c)) for c in columns])
            rows += 1
            batch += 1
            if on_rows and batch >= 2000:
                on_rows(batch)
                batch = 0
        if on_rows and batch:
            on_rows(batch)
    return rows


def _csv_header(path: Path) -> List[str]:
    with open(path, newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh), [])


def _count_rows(path: Path) -> int:
    with open(path, newline="", encoding="utf-8") as fh:
        return max(sum(1 for _ in csv.reader(fh)) - 1, 0)


def _canon(value: str, numeric: bool) -> str:
    """Numbers compare by value, whichever API wrote them: 1000, 1000.0 and 1E+3 are equal."""
    if numeric and value:
        try:
            return format(Decimal(value).normalize(), "f")
        except ArithmeticError:
            return value
    return value


def _row_digest(row: List[str], idx: List[Tuple[int, bool]]) -> bytes:
    return hashlib.blake2b("\x1f".join(_canon(row[i], n) if 0 <= i < len(row) else ""
                                       for i, n in idx).encode(), digest_size=12).digest()


def _parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _soql_ts(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Exporter:
    def __init__(self, client: SalesforceClient, options: ExportOptions,
                 progress: Optional[Progress] = None):
        self.sf = client
        self.opt = options
        self.progress = progress or Progress()
        self.out = Path(options.out_dir)
        self.data_dir = self.out / "data"
        self.schema_dir = self.out / "schema"
        self.files_dir = self.out / "files"
        self.redaction = self._redaction_fingerprint()
        if self.redaction and options.incremental and not options.redact_salt:
            raise ExportError(
                "Incremental exports with --redact/--redact-pii need a fixed salt, or the same "
                "value would hash differently from one night to the next. Set SF_EXIT_REDACT_SALT "
                "to a long random secret (and keep it out of the export folder).")
        self.salt = options.redact_salt or self._random_salt
        self.state = State(self.out / ".sf-exit" / "state.json", self._fingerprint(),
                           fresh=options.fresh)
        self.run_stamp = self.state.stamp
        self.incremental: Set[str] = set()
        self.result = ExportResult(out_dir=self.out, resumed=self.state.resumed)
        self.plans: Dict[str, ObjectPlan] = {}
        self.prev_manifest: Dict[str, Any] = {}
        self._changes_lock = threading.Lock()

    # ------------------------------------------------------------ fingerprints
    _random_salt = secrets.token_hex(16)

    def _redaction_fingerprint(self) -> Optional[Dict[str, Any]]:
        if not (self.opt.redact or self.opt.redact_pii):
            return None
        salt = self.opt.redact_salt or self._random_salt
        tag = hmac.new(salt.encode(), b"sf-exit redaction", hashlib.sha256).hexdigest()[:16]
        return {"fields": sorted(self.opt.redact), "pii": self.opt.redact_pii, "salt": tag}

    def _fingerprint(self) -> Dict[str, Any]:
        """Options that change what ends up in a CSV: a run is only resumed if they match."""
        return {"include_deleted": self.opt.include_deleted, "incremental": self.opt.incremental,
                "redaction": self.redaction, "all_file_versions": self.opt.all_file_versions}

    # ------------------------------------------------------------------ public
    def discover(self) -> Dict[str, ObjectPlan]:
        self.progress.phase("Discovering objects")
        chosen, skipped = select_objects(
            self.sf.describe_global(), self.opt.include, self.opt.exclude
        )
        self.result.skipped.update(skipped)
        names = [o["name"] for o in chosen]
        counts = self.sf.record_counts(names)
        if self.sf.record_counts_available is False:
            self.result.warnings.append(
                "Record counts were unavailable (the user lacks 'View Setup and Configuration'), "
                "so engines were chosen from each object's first page instead.")

        self.progress.phase(f"Describing {len(names)} objects")
        self.progress.start("describe", "Reading schema", len(names))
        plans: Dict[str, ObjectPlan] = {}
        chunks = [names[i:i + COMPOSITE_MAX] for i in range(0, len(names), COMPOSITE_MAX)]
        with ThreadPoolExecutor(max_workers=max(1, self.opt.workers)) as pool:
            for described in pool.map(self.sf.describe_many, chunks):
                for name, body in described.items():
                    if isinstance(body, SalesforceError):
                        self.result.skipped[name] = f"describe failed: {body}"
                    else:
                        plan = plan_object(body)
                        plan.approx_rows = counts.get(name)
                        plans[name] = plan
                self.progress.advance("describe", len(described))
        self.progress.finish("describe", "done")
        self.plans = dict(sorted(plans.items(), key=lambda kv: kv[0].lower()))
        return self.plans

    def run(self) -> ExportResult:
        if self.state.resumed:
            self.progress.phase(f"Resuming the interrupted run started {self.state.started_at}")
        for d in (self.data_dir, self.schema_dir):
            d.mkdir(parents=True, exist_ok=True)
        manifest = self.out / "manifest.json"
        if manifest.exists():
            with contextlib.suppress(ValueError, OSError):
                self.prev_manifest = json.loads(manifest.read_text("utf-8"))
        org = self.sf.org_info()
        self.org = org

        plans = self.discover()
        for name, plan in plans.items():
            (self.schema_dir / f"{name}.json").write_text(
                json.dumps(plan.describe, indent=1, sort_keys=True, default=str), "utf-8"
            )
        if self.opt.incremental and self.opt.data:
            self._plan_incremental(plans)

        if self.opt.data:
            self.export_records(plans)
            if "ContentDocument" in plans and self._wanted("ContentDocumentLink"):
                self.export_content_document_links()
        if self.opt.files:
            self.export_files()
        if self.opt.metadata:
            self.export_metadata()

        self.result.api_calls = self.sf.api_calls
        self.write_outputs()
        self.state.complete()
        return self.result

    # ------------------------------------------------------------- incremental
    def _plan_incremental(self, plans: Dict[str, ObjectPlan]) -> None:
        """Turn plans into 'changes since that object was last exported' queries."""
        prev = self.prev_manifest
        if not prev:
            self.progress.log("[yellow]No previous export here, so this run is a full export.[/]")
            return
        if bool(prev.get("include_deleted")) != self.opt.include_deleted:
            self.progress.log("[yellow]--include-deleted changed since the last run: full export.[/]")
            return
        if prev.get("redaction") != self.redaction:
            self.progress.log("[yellow]Redaction settings changed since the last run: full export.[/]")
            return
        prev_objs = prev.get("objects", {})
        earliest: Optional[dt.datetime] = None
        for name, plan in plans.items():
            po = prev_objs.get(name) or {}
            dest = self.data_dir / f"{name}.csv"
            if (po.get("status") != "done" or not dest.exists()
                    or "SystemModstamp" not in plan.field_types or plan.where_variants):
                continue
            if _csv_header(dest) != plan.fields:
                continue  # fields were added/removed: re-export this object in full
            # Each object's own export time, minus an hour of overlap for clock skew and
            # long transactions; rows seen twice are de-duplicated by Id when merging.
            since = _parse_ts(po.get("started_at") or prev["started_at"]) - dt.timedelta(hours=1)
            plan.base_where = f"SystemModstamp > {_soql_ts(since)}"
            plan.query_all = True  # also returns rows deleted since then (IsDeleted = true)
            plan.approx_rows = None  # unknown delta size: the first page decides REST or Bulk
            self.incremental.add(name)
            earliest = since if earliest is None else min(earliest, since)
        if earliest:
            self.result.incremental_since = _soql_ts(earliest)
        self.progress.phase(f"Incremental: {len(self.incremental)} objects fetch only changes, "
                            f"{len(plans) - len(self.incremental)} in full")

    def _changes_dir(self) -> Path:
        d = self.out / "changes" / self.run_stamp
        with self._changes_lock:
            d.mkdir(parents=True, exist_ok=True)
        return d

    def _gone(self, header: List[str]) -> Callable[[List[str]], bool]:
        """Rows a normal (non --include-deleted) export would not contain."""
        if self.opt.include_deleted:
            return lambda row: False
        cols = [header.index(c) for c in ("IsDeleted", "IsArchived") if c in header]
        return lambda row: any(i < len(row) and row[i].lower() == "true" for i in cols)

    def _merge_delta(self, plan: ObjectPlan, delta: Path, dest: Path) -> Tuple[int, int]:
        """Apply changed/deleted rows from ``delta`` onto ``dest``, streaming both files.

        Keeps the changed rows in ``changes/<run>/<Object>.csv`` and the rows they replaced in
        ``<Object>.before.csv``. Rows identical to the version already in ``dest`` (fetched
        again because the watermark overlaps the previous run by an hour) are merged but not
        recorded as changes. Returns (rows now in dest, records changed)."""
        with open(delta, newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
            gone = self._gone(header)
            didx = self._compare_columns(plan, header, header)
            latest: Dict[str, Tuple[int, bytes, bool]] = {}  # Id -> (row index, digest, gone)
            for i, row in enumerate(reader):
                if row:
                    latest[row[0]] = (i, _row_digest(row, didx), gone(row))
        if not latest:
            delta.unlink()
            return _count_rows(dest), 0
        merged = dest.with_name(dest.name + ".merge")
        after = dest.with_name(dest.name + ".after")
        before = dest.with_name(dest.name + ".before")
        rows = replaced = 0
        same: Set[str] = set()  # re-fetched, nothing changed
        seen: Set[str] = set()
        with open(dest, newline="", encoding="utf-8") as src, \
                open(merged, "w", newline="", encoding="utf-8") as out, \
                open(before, "w", newline="", encoding="utf-8") as bfh:
            reader = csv.reader(src)
            writer = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\n")
            bwriter = csv.writer(bfh, quoting=csv.QUOTE_ALL, lineterminator="\n")
            base_header = next(reader, header)
            bidx = self._compare_columns(plan, header, base_header)
            writer.writerow(base_header)
            bwriter.writerow(base_header)
            for row in reader:
                if not row:
                    continue
                hit = latest.get(row[0])
                if hit is None:
                    writer.writerow(row)
                    rows += 1
                    continue
                seen.add(row[0])
                if hit[1] == _row_digest(row, bidx):
                    same.add(row[0])
                else:
                    bwriter.writerow(row)
                    replaced += 1
            # Deleted before the previous run and fetched again: already recorded then.
            same.update(k for k, (_, _, g) in latest.items() if g and k not in seen)
            with open(delta, newline="", encoding="utf-8") as fh, \
                    open(after, "w", newline="", encoding="utf-8") as afh:
                dreader = csv.reader(fh)
                next(dreader, None)
                awriter = csv.writer(afh, quoting=csv.QUOTE_ALL, lineterminator="\n")
                awriter.writerow(header)
                for i, row in enumerate(dreader):
                    if not row or latest[row[0]][0] != i:
                        continue
                    if not latest[row[0]][2]:
                        writer.writerow(row)
                        rows += 1
                    if row[0] not in same:
                        awriter.writerow(row)
        merged.replace(dest)
        delta.unlink()
        changed = len(latest) - len(same)
        if changed:
            changes = self._changes_dir()
            after.replace(changes / f"{plan.name}.csv")
            if replaced:
                before.replace(changes / f"{plan.name}.before.csv")
        for leftover in (after, before):
            leftover.unlink(missing_ok=True)
        return rows, changed

    @staticmethod
    def _compare_columns(plan: ObjectPlan, columns: List[str],
                         header: List[str]) -> List[Tuple[int, bool]]:
        """Positions in ``header`` of the ``columns`` that count when deciding whether a record
        changed (not formula or noise fields), flagged when numeric so 1000 == 1000.0."""
        fields = {f["name"]: f for f in plan.describe.get("fields", [])}
        out = []
        for c in columns:
            f = fields.get(c, {})
            if c in HISTORY_NOISE or f.get("calculated"):
                continue
            out.append((header.index(c) if c in header else -1, f.get("type") in NUMERIC_TYPES))
        return out

    def _record_full_changes(self, plan: ObjectPlan, old: Path, new: Path) -> Optional[int]:
        """For an object re-exported in full, write what changed since the previous run to
        changes/<run>/ (same layout as incremental runs). Returns records changed, or None
        when there was nothing to compare with."""
        if not (self.opt.history and old.exists() and self.prev_manifest):
            return None
        prev = (self.prev_manifest.get("objects") or {}).get(plan.name) or {}
        if prev.get("status") != "done":
            return None
        if old.stat().st_size > HISTORY_MAX_BYTES:
            self.result.warnings.append(
                f"{plan.name}: too large to record history for this full re-export.")
            return None
        old_header, new_header = _csv_header(old), _csv_header(new)
        if not old_header or old_header[0] != "Id" or not new_header or new_header[0] != "Id":
            return None
        common = [c for c in new_header if c in old_header]
        oi = self._compare_columns(plan, common, old_header)
        ni = self._compare_columns(plan, common, new_header)
        digest = _row_digest
        old_hashes: Dict[str, bytes] = {}
        with open(old, newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            next(reader, None)
            for row in reader:
                if row:
                    old_hashes[row[0]] = digest(row, oi)
        changed: Set[str] = set()
        after = old.with_name(old.name + ".after")
        with open(new, newline="", encoding="utf-8") as fh, \
                open(after, "w", newline="", encoding="utf-8") as afh:
            reader = csv.reader(fh)
            w = csv.writer(afh, quoting=csv.QUOTE_ALL, lineterminator="\n")
            w.writerow(next(reader, new_header))
            for row in reader:
                if not row:
                    continue
                h = old_hashes.pop(row[0], None)
                if h is None or h != digest(row, ni):
                    changed.add(row[0])
                    w.writerow(row)
        removed = set(old_hashes)
        if not changed and not removed:
            after.unlink()
            return 0
        changes = self._changes_dir()
        if changed:
            after.replace(changes / f"{plan.name}.csv")
        else:
            after.unlink()
        touched = (changed | removed)
        with open(old, newline="", encoding="utf-8") as src, \
                open(changes / f"{plan.name}.before.csv", "w", newline="", encoding="utf-8") as fh:
            reader = csv.reader(src)
            w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
            w.writerow(next(reader, old_header))
            w.writerows(row for row in reader if row and row[0] in touched)
        return len(touched)

    # --------------------------------------------------------------- redaction
    def _is_pii(self, obj: str, name: str, ftype: str) -> bool:
        if ftype in ("email", "phone"):
            return True
        if ftype not in TEXT_TYPES and ftype != "date":
            return False
        base = name[:-3] if name.endswith("__c") else name
        if name in PERSON_NAME_FIELDS or (obj in PERSON_OBJECTS and name == "Name"):
            return True
        if obj == "User" and name in USER_PII_FIELDS:
            return True
        lowered = base.lower()
        if ftype == "date":
            return bool(re.search(r"birth|(^|_)dob(_|$)", lowered))
        return bool(PII_NAME.search(lowered) or PII_ADDRESS.match(lowered))

    def _redacted_fields(self, plan: ObjectPlan) -> List[str]:
        if not self.redaction:
            return []
        out = []
        for f in plan.fields:
            ftype = plan.field_types.get(f, "string")
            if f == "Id" or ftype in ("id", "reference"):
                continue  # Ids and lookups are never hashed: joins must keep working
            explicit = bool(self.opt.redact) and _matches(f"{plan.name}.{f}", self.opt.redact)
            if explicit or (self.opt.redact_pii and self._is_pii(plan.name, f, ftype)):
                out.append(f)
        return out

    def _redact(self, plan: ObjectPlan, path: Path) -> List[str]:
        cols = self._redacted_fields(plan)
        if not cols:
            return []
        tmp = path.with_name(path.name + ".redact")
        with open(path, newline="", encoding="utf-8") as src, \
                open(tmp, "w", newline="", encoding="utf-8") as out:
            reader = csv.reader(src)
            writer = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\n")
            header = next(reader, [])
            writer.writerow(header)
            idx = [header.index(c) for c in cols if c in header]
            for row in reader:
                for i in idx:
                    if i < len(row) and row[i]:
                        digest = hashlib.sha256((self.salt + row[i].strip().lower()).encode())
                        row[i] = "redacted:" + digest.hexdigest()[:16]
                writer.writerow(row)
        tmp.replace(path)
        return cols

    # ---------------------------------------------------------------- metadata
    def export_metadata(self) -> None:
        from sf_exit.metadata import export_metadata

        self.progress.phase("Retrieving metadata (Apex, Flows, layouts, reports, permissions…)")
        try:
            res = export_metadata(self.sf, self.out, self.opt.metadata_include,
                                  self.opt.metadata_exclude, poll_interval=self.opt.poll_interval,
                                  on_progress=self.progress.phase)
        except Exception as exc:  # noqa: BLE001 - metadata must never sink the data export
            self.progress.log(f"[red]Metadata export failed:[/] {exc}")
            self.result.metadata = {"error": str(exc)}
            return
        self.result.metadata = {"types": res.types, "components": res.components,
                                "files": res.files, "failed_types": res.failed_types,
                                "warnings": len(res.warnings)}

    # ----------------------------------------------------------------- records
    def _wanted(self, name: str) -> bool:
        if self.opt.include and not _matches(name, self.opt.include):
            return False
        return not (self.opt.exclude and _matches(name, self.opt.exclude))

    def export_records(self, plans: Dict[str, ObjectPlan]) -> None:
        todo = []
        for name, plan in plans.items():
            prev = self.state.get(name)
            dest = self.data_dir / f"{name}.csv"
            if prev and prev.get("status") == "done" and dest.exists():
                self.result.objects[name] = ObjectResult.from_dict(prev)
                continue
            todo.append(plan)
        resumed = len(plans) - len(todo)
        self.progress.phase(
            f"Exporting records from {len(todo)} objects"
            + (f" ({resumed} already done, resuming)" if resumed else "")
        )
        # Small objects: first pages of 5 objects per API call (/composite), then each
        # object pages on its own. Big objects: one Bulk API job each, largest first.
        batchable = [p for p in todo if self._batchable(p)]
        singles = sorted((p for p in todo if not self._batchable(p)),
                         key=lambda p: -(p.approx_rows or 0))
        batches = []
        for flag in (False, True):  # one composite call uses one endpoint (query or queryAll)
            group = [p for p in batchable if self._all(p) == flag]
            batches += [group[i:i + COMPOSITE_MAX_QUERIES]
                        for i in range(0, len(group), COMPOSITE_MAX_QUERIES)]
        self.progress.start("records", "Objects", len(todo))
        with ThreadPoolExecutor(max_workers=max(1, self.opt.workers)) as pool:
            futures = {pool.submit(self.export_object, plan): [plan] for plan in singles}
            futures.update({pool.submit(self.export_batch, batch): batch for batch in batches})
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001 - defensive: never lose the others
                    res = [ObjectResult(p.name, "failed", error=f"{type(exc).__name__}: {exc}")
                           for p in futures[fut]]
                for r in res if isinstance(res, list) else [res]:
                    self.result.objects[r.name] = r
                    self.state.put(r)
                    self.progress.advance("records", 1)
        self.progress.finish("records", "done")

    def _all(self, plan: ObjectPlan) -> bool:
        """Whether to use queryAll (deleted + archived rows) for this plan."""
        return plan.query_all if plan.query_all is not None else self.opt.include_deleted

    def _batchable(self, plan: ObjectPlan) -> bool:
        return (self._choose_engine(plan) == "rest" and not plan.where_variants
                and len(plan.soql()) <= MAX_REST_SOQL)

    def export_batch(self, plans: List[ObjectPlan]) -> List[ObjectResult]:
        """Fetch the first page of up to 5 small objects in ONE API call (/composite), then
        follow pagination per object. An object whose first page reveals it is big switches
        to the Bulk API; anything that errors goes the normal route."""
        endpoint = "queryAll" if self._all(plans[0]) else "query"
        started = utcnow()
        try:
            pages = self.sf.composite_get(
                [f"{endpoint}?q={quote(p.soql(), safe='')}" for p in plans], decimals=True)
        except Exception:  # noqa: BLE001
            return [self.export_object(p) for p in plans]
        results = []
        for plan, (ok, body) in zip(plans, pages, strict=True):
            if not ok:
                results.append(self.export_object(plan))
                continue
            total = int(body.get("totalSize") or 0)
            if self.opt.engine == "auto" and not body.get("done", True) and total >= BULK_THRESHOLD:
                plan.approx_rows = total
                results.append(self.export_object(plan, force="bulk"))
                continue
            t0 = dt.datetime.now()
            key = f"obj:{plan.name}"
            dest = self.data_dir / f"{plan.name}.csv"
            tmp = dest.with_name(dest.name + ".part")
            self.progress.start(key, plan.name, total or plan.approx_rows)
            try:
                rows = write_records_csv(
                    self.sf.query(plan.soql(), self._all(plan), first_page=body),
                    plan.fields, tmp, on_rows=lambda n, k=key: self.progress.advance(k, n))
                results.append(self._done(plan, tmp, dest, key, rows, "rest", t0, started))
            except Exception:  # noqa: BLE001 - try again on its own (REST, then Bulk)
                tmp.unlink(missing_ok=True)
                self.progress.finish(key, "retry")
                results.append(self.export_object(plan))
        return results

    def _done(self, plan: ObjectPlan, tmp: Path, dest: Path, key: str, rows: int,
              engine: str, t0: dt.datetime, started: str,
              warnings: Optional[List[str]] = None) -> ObjectResult:
        redacted = self._redact(plan, tmp)
        changed: Optional[int] = None
        if plan.name in self.incremental:
            rows, changed = self._merge_delta(plan, tmp, dest)
            detail = f"{changed:,} changed · {rows:,} total"
        else:
            try:
                changed = self._record_full_changes(plan, dest, tmp)
            except (OSError, ValueError) as exc:  # history is a bonus: never fail the export
                self.result.warnings.append(f"{plan.name}: could not record history ({exc})")
            tmp.replace(dest)
            detail = f"{rows:,} row{'' if rows == 1 else 's'} via {engine}"
        if changed:
            self.result.changed_records += changed
        quiet = "0 rows " if (changed == 0 and plan.name in self.incremental) else ""
        self.progress.finish(key, "done", quiet + detail)
        return ObjectResult(
            plan.name, "done", rows=rows, engine=engine,
            file=dest.relative_to(self.out).as_posix(), sha256=file_sha256(dest),
            seconds=(dt.datetime.now() - t0).total_seconds(), changed=changed,
            redacted=redacted, started_at=started, warnings=list(warnings or []),
        )

    def _choose_engine(self, plan: ObjectPlan) -> str:
        if plan.where_variants:
            return "rest"
        if self.opt.engine in ("bulk", "rest"):
            engine = self.opt.engine
        else:
            engine = "bulk" if (plan.approx_rows or 0) >= BULK_THRESHOLD else "rest"
        if engine == "rest" and len(plan.soql()) > MAX_REST_SOQL:
            engine = "bulk"
        return engine

    def export_object(self, plan: ObjectPlan, force: Optional[str] = None) -> ObjectResult:
        t0 = dt.datetime.now()
        started = utcnow()
        dest = self.data_dir / f"{plan.name}.csv"
        tmp = dest.with_name(dest.name + ".part")
        key = f"obj:{plan.name}"
        self.progress.start(key, plan.name, plan.approx_rows)
        try:
            engine = force or self._choose_engine(plan)
            errors: List[SalesforceError] = []
            warnings: List[str] = []
            rows = None
            attempts = [engine, "rest"] if engine == "bulk" else ["rest", "bulk"]
            if plan.where_variants:
                attempts = ["rest"]
            for attempt_engine in attempts:
                if attempt_engine == "rest" and len(plan.soql()) > MAX_REST_SOQL:
                    continue
                try:
                    rows = self._run_engine(attempt_engine, plan, tmp, key, warnings)
                    engine = attempt_engine
                    break
                except SalesforceError as exc:
                    exc.message = f"{attempt_engine}: {exc.message}"
                    errors.append(exc)
                    if not _worth_retrying_other_engine(exc):
                        break
            if rows is not None:
                return self._done(plan, tmp, dest, key, rows, engine, t0, started, warnings)
            tmp.unlink(missing_ok=True)
            secs = (dt.datetime.now() - t0).total_seconds()
            msg = " | ".join(f"[{e.code}] {e.message}" if e.code else e.message for e in errors)
            msg = msg or "unknown error"
            if errors and all(is_not_exportable(f"{e.code} {e.message}") for e in errors):
                self.progress.finish(key, "skipped", msg)
                return ObjectResult(plan.name, "skipped", error=msg, seconds=secs)
            self.progress.finish(key, "failed", msg)
            return ObjectResult(plan.name, "failed", error=msg, seconds=secs)
        except Exception as exc:  # noqa: BLE001 - disk full, permissions, bugs: fail this object only
            tmp.unlink(missing_ok=True)
            msg = f"{type(exc).__name__}: {exc}"
            self.progress.finish(key, "failed", msg)
            return ObjectResult(plan.name, "failed", error=msg,
                                seconds=(dt.datetime.now() - t0).total_seconds())

    def _run_engine(self, engine: str, plan: ObjectPlan, tmp: Path, key: str,
                    warnings: List[str]) -> int:
        def bump(n: int) -> None:
            self.progress.advance(key, n)

        if engine == "bulk":
            return self.sf.bulk_query_to_file(
                plan.soql(), tmp, include_deleted=self._all(plan),
                poll_interval=self.opt.poll_interval, max_records=self.opt.max_records,
                on_rows=bump,
            )
        if plan.where_variants:
            failures: List[Tuple[str, SalesforceError]] = []
            rows = write_records_csv(self._variant_records(plan, failures), plan.fields, tmp,
                                     on_rows=bump)
            if len(failures) == len(plan.where_variants):
                raise failures[0][1]
            for where, exc in failures:  # some PublishStatus queries failed: say so, loudly
                warnings.append(f"partial: rows matching {where} could not be exported ({exc})")
            return rows
        records = self.sf.query(plan.soql(), include_deleted=self._all(plan))
        return write_records_csv(records, plan.fields, tmp, on_rows=bump)

    def _variant_records(self, plan: ObjectPlan,
                         failures: List[Tuple[str, SalesforceError]]) -> Iterator[Dict[str, Any]]:
        for where in plan.where_variants:
            try:
                yield from self.sf.query(plan.soql(where), include_deleted=self._all(plan))
            except SalesforceError as exc:
                failures.append((where, exc))

    def _composite_query(self, plan: ObjectPlan, wheres: List[str]) -> Iterator[Dict[str, Any]]:
        """Run many filtered queries of one object, 5 first pages per API call."""
        for i in range(0, len(wheres), COMPOSITE_MAX_QUERIES):
            group = wheres[i:i + COMPOSITE_MAX_QUERIES]
            pages = self.sf.composite_get(
                [f"query?q={quote(plan.soql(w), safe='')}" for w in group], decimals=True)
            for ok, body in pages:
                if not ok:
                    raise body
                yield from self.sf.query(plan.soql(), first_page=body)

    def export_content_document_links(self) -> None:
        """ContentDocumentLink (which file is attached to which record) can't be queried
        without a filter, so it is queried in chunks of the exported ContentDocument Ids.
        Every run refreshes it completely: links can change without the document changing."""
        src = self.data_dir / "ContentDocument.csv"
        res = self.result.objects.get("ContentDocument")
        if not src.exists() or not res or res.status != "done":
            return
        prev = self.state.get("ContentDocumentLink")
        dest = self.data_dir / "ContentDocumentLink.csv"
        if prev and prev.get("status") == "done" and dest.exists():
            self.result.objects["ContentDocumentLink"] = ObjectResult.from_dict(prev)
            return
        try:
            desc = self.sf.describe("ContentDocumentLink")
        except SalesforceError as exc:
            self.result.objects["ContentDocumentLink"] = ObjectResult(
                "ContentDocumentLink", "failed", error=str(exc))
            return
        plan = plan_object(desc)
        self.plans["ContentDocumentLink"] = plan
        (self.schema_dir / "ContentDocumentLink.json").write_text(
            json.dumps(desc, indent=1, sort_keys=True, default=str), "utf-8")
        tmp = dest.with_name(dest.name + ".part")
        with open(src, newline="", encoding="utf-8") as fh:
            ids = [row["Id"] for row in csv.DictReader(fh) if row.get("Id")]
        wheres = ["ContentDocumentId IN (" + ",".join(soql_quote(x) for x in ids[i:i + CDL_CHUNK]) + ")"
                  for i in range(0, len(ids), CDL_CHUNK)]
        key = "obj:ContentDocumentLink"
        self.progress.start(key, "ContentDocumentLink", None)
        t0 = dt.datetime.now()
        started = utcnow()
        try:
            rows = write_records_csv(self._composite_query(plan, wheres), plan.fields, tmp,
                                     on_rows=lambda n: self.progress.advance(key, n))
            r = self._done(plan, tmp, dest, key, rows, "rest", t0, started)
        except Exception as exc:  # noqa: BLE001 - keep the previous links file
            tmp.unlink(missing_ok=True)
            self.progress.finish(key, "failed", str(exc))
            r = ObjectResult("ContentDocumentLink", "failed", error=str(exc))
        self.result.objects["ContentDocumentLink"] = r
        self.state.put(r)

    # ------------------------------------------------------------------- files
    def _file_jobs(self) -> List[Dict[str, Any]]:
        jobs: List[Dict[str, Any]] = []
        latest = "" if self.opt.all_file_versions else " WHERE IsLatest = true"
        specs = [
            ("ContentVersion",
             "SELECT Id, ContentDocumentId, Title, FileExtension, VersionNumber, ContentSize, "
             f"Checksum FROM ContentVersion{latest}"),
            ("Attachment", "SELECT Id, ParentId, Name, BodyLength FROM Attachment"),
            ("Document", "SELECT Id, FolderId, Name, Type, BodyLength, Url FROM Document"),
        ]
        self.listed_kinds: Set[str] = set()
        for kind, soql in specs:
            if not self._wanted(kind):
                continue
            try:
                recs = list(self.sf.query(soql))
            except SalesforceError as exc:
                self.progress.log(f"[red]Could not list {kind} files:[/] {exc}")
                self.result.listing_errors[kind] = str(exc)
                continue
            self.listed_kinds.add(kind)
            for r in recs:
                if kind == "ContentVersion":
                    title = safe_filename(r.get("Title") or r["Id"])
                    ext = (r.get("FileExtension") or "").strip(".")
                    name = f"v{r.get('VersionNumber') or 1}_{title}"
                    if ext and not name.lower().endswith("." + ext.lower()):
                        name += "." + safe_filename(ext)
                    jobs.append(dict(kind=kind, id=r["Id"], parent=r.get("ContentDocumentId") or "",
                                     name=r.get("Title") or "", size=r.get("ContentSize"),
                                     checksum=(r.get("Checksum") or "").lower(), rel=name,
                                     url=f"sobjects/ContentVersion/{r['Id']}/VersionData"))
                elif kind == "Attachment":
                    jobs.append(dict(kind=kind, id=r["Id"], parent=r.get("ParentId") or "",
                                     name=r.get("Name") or "", size=r.get("BodyLength"),
                                     checksum="", rel=f"{r['Id']}_{safe_filename(r.get('Name') or 'file')}",
                                     url=f"sobjects/Attachment/{r['Id']}/Body"))
                else:
                    if r.get("Url") and not r.get("BodyLength"):
                        continue  # a link to an external URL, no body stored in Salesforce
                    name = safe_filename(r.get("Name") or "document")
                    typ = (r.get("Type") or "").strip(".")
                    if typ and not name.lower().endswith("." + typ.lower()):
                        name += "." + safe_filename(typ)
                    jobs.append(dict(kind=kind, id=r["Id"], parent=r.get("FolderId") or "",
                                     name=r.get("Name") or "", size=r.get("BodyLength"),
                                     checksum="", rel=f"{r['Id']}_{name}",
                                     url=f"sobjects/Document/{r['Id']}/Body"))
        return jobs

    def export_files(self) -> None:
        self.progress.phase("Listing files, attachments and documents")
        jobs = self._file_jobs()
        total_bytes = sum(int(j.get("size") or 0) for j in jobs)
        self.progress.phase(f"Downloading {len(jobs):,} files ({human_bytes(total_bytes)})")
        self.progress.start("files", "Files", total_bytes or None)

        def work(job: Dict[str, Any]) -> Dict[str, Any]:
            folder = self.files_dir / job["kind"] / (job["parent"] or "_")
            dest = folder / job["rel"]
            row = dict(kind=job["kind"], record_id=job["id"], parent_id=job["parent"],
                       name=job["name"], path=dest.relative_to(self.out).as_posix(), bytes=0,
                       sha256="", md5="", sf_checksum=job["checksum"], status="ok", error="")
            try:
                folder.mkdir(parents=True, exist_ok=True)
                expected = int(job["size"]) if job.get("size") not in (None, "") else None
                if dest.exists() and expected is not None and dest.stat().st_size == expected:
                    size = expected
                    sha, md5 = file_sha256(dest), file_sha256(dest, "md5")
                    row["status"] = "ok (resumed)"
                else:
                    size, sha, md5 = self.sf.download(job["url"], dest)
                row.update(bytes=size, sha256=sha, md5=md5)
                if job["checksum"] and job["checksum"] != md5:
                    row.update(status="checksum-mismatch",
                               error="MD5 differs from Salesforce's ContentVersion.Checksum")
            except Exception as exc:  # noqa: BLE001 - network, disk full, path too long...
                row.update(status="error", error=f"{type(exc).__name__}: {exc}"
                           if not isinstance(exc, SalesforceError) else str(exc))
            self.progress.advance("files", int(job.get("size") or 0) if total_bytes else 1)
            return row

        with ThreadPoolExecutor(max_workers=max(1, self.opt.file_workers)) as pool:
            rows = list(pool.map(work, jobs))
        self.progress.finish("files", "done")

        # Kinds not listed in this run (not selected, or the listing failed) keep their rows
        # from the previous index, so a partial run never erases what is already on disk.
        index = self.files_dir / "index.csv"
        cols = ["kind", "record_id", "parent_id", "name", "path", "bytes", "sha256", "md5",
                "sf_checksum", "status", "error"]
        if index.exists():
            with open(index, newline="", encoding="utf-8") as fh:
                rows += [r for r in csv.DictReader(fh) if r.get("kind") not in self.listed_kinds
                         and (self.out / r.get("path", "")).exists()]
        if rows or index.exists():
            self.files_dir.mkdir(parents=True, exist_ok=True)
            tmp = index.with_suffix(".tmp")
            with open(tmp, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
                w.writeheader()
                w.writerows(sorted(rows, key=lambda r: (r["kind"], r["path"])))
            tmp.replace(index)
        summary: Dict[str, Dict[str, int]] = {}
        for r in rows:
            s = summary.setdefault(r["kind"], {"count": 0, "bytes": 0, "errors": 0})
            s["count"] += 1
            s["bytes"] += int(r["bytes"] or 0)
            if not str(r["status"]).startswith("ok"):
                s["errors"] += 1
                self.result.file_errors += 1
        self.result.files = summary

    # ----------------------------------------------------------------- outputs
    def _carry_over(self) -> None:
        """Objects in the folder from earlier runs but not selected this time (-i/-x,
        --files-only) stay in the manifest, so it always describes what is on disk."""
        prev = self.prev_manifest
        if not prev or bool(prev.get("include_deleted")) != self.opt.include_deleted:
            return
        for name, o in (prev.get("objects") or {}).items():
            if name in self.result.objects or name in self.plans and self.opt.data:
                continue
            if o.get("status") == "done" and (self.out / o.get("file", "")).is_file():
                self.result.objects[name] = ObjectResult.from_dict(o)
                schema = self.schema_dir / f"{name}.json"
                if name not in self.plans and schema.exists():
                    with contextlib.suppress(ValueError, KeyError):
                        self.plans[name] = plan_object(json.loads(schema.read_text("utf-8")))

    def write_outputs(self) -> None:
        self._carry_over()
        exported = {n: p for n, p in self.plans.items()
                    if self.result.objects.get(n) and self.result.objects[n].status == "done"}
        org = self.org
        for dialect in DIALECTS:
            (self.out / f"schema.{dialect}.sql").write_text(
                schema_sql(exported, dialect, comment=f"Salesforce org {org.name} ({org.id})"),
                "utf-8")
        counts = {n: r.rows for n, r in self.result.objects.items()}
        (self.out / "erd.mmd").write_text(mermaid_erd(exported, counts), "utf-8")

        finished = utcnow()
        changes = self.out / "changes" / self.run_stamp
        kind = "incremental" if self.incremental else "full"
        if changes.is_dir() and not any(changes.iterdir()):
            changes.rmdir()
        if changes.is_dir():
            self.result.changes_dir = f"changes/{self.run_stamp}"
            (changes / "run.json").write_text(json.dumps({
                "stamp": self.run_stamp, "kind": kind, "started_at": self.state.started_at,
                "finished_at": finished, "since": self.result.incremental_since,
                "objects": {n: r.changed for n, r in sorted(self.result.objects.items())
                            if r.changed}}, indent=1), "utf-8")
        runs = list(self.prev_manifest.get("runs") or [])
        if self.prev_manifest and not runs:  # an export made before history existed
            runs.append({"stamp": None, "kind": "full", "started_at": self.prev_manifest.get(
                "started_at"), "finished_at": self.prev_manifest.get("finished_at"),
                "changes": None})
        runs.append({"stamp": self.run_stamp, "kind": kind, "started_at": self.state.started_at,
                     "finished_at": finished, "changes": self.result.changes_dir})

        manifest = {
            "tool": "sf-exit",
            "version": __version__,
            "started_at": self.state.started_at,
            "finished_at": finished,
            "org": asdict(org),
            "instance_url": self.sf.instance_url,
            "api_version": self.sf.api_version,
            "include_deleted": self.opt.include_deleted,
            "incremental_since": self.result.incremental_since,
            "changes_dir": self.result.changes_dir,
            "redaction": self.redaction,
            "redacted": {n: r.redacted for n, r in self.result.objects.items() if r.redacted},
            "metadata": self.result.metadata,
            "all_file_versions": self.opt.all_file_versions,
            "api_calls": self.sf.api_calls,
            "totals": {
                "objects": len(exported),
                "rows": self.result.total_rows,
                "files": sum(v["count"] for v in self.result.files.values()),
                "file_bytes": sum(v["bytes"] for v in self.result.files.values()),
                "changed_records": self.result.changed_records,
            },
            "objects": {n: asdict(r) for n, r in sorted(self.result.objects.items())},
            "skipped": dict(sorted(self.result.skipped.items())),
            "files": self.result.files,
            "file_listing_errors": self.result.listing_errors,
            "warnings": self.result.warnings,
            "runs": runs,
        }
        tmp = self.out / "manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2, default=str), "utf-8")
        tmp.replace(self.out / "manifest.json")
        (self.out / "REPORT.md").write_text(render_report(manifest), "utf-8")


def _worth_retrying_other_engine(exc: SalesforceError) -> bool:
    msg = (exc.message or "").lower()
    fatal = ("invalid_session", "insufficient_access", "request_limit_exceeded",
             "api_disabled", "api_currently_disabled")
    return not any(f in msg or f in (exc.code or "").lower() for f in fatal)


def render_report(m: Dict[str, Any]) -> str:
    org = m["org"]
    t = m["totals"]
    objs = [o for o in m["objects"].values() if o["status"] == "done"]
    failed = [o for o in m["objects"].values() if o["status"] == "failed"]
    partial = [o for o in objs if o.get("warnings")]
    unexportable = [o for o in m["objects"].values() if o["status"] == "skipped"]
    lines = [
        f"# Salesforce export: {org['name']}",
        "",
        f"- **Org ID:** `{org['id']}` ({org['edition']}{', sandbox' if org['is_sandbox'] else ''})",
        f"- **Exported:** {m['finished_at']} with sf-exit {m['version']} (API v{m['api_version']})",
        f"- **Records:** {t['rows']:,} across {t['objects']:,} objects"
        + (" (including deleted/archived)" if m["include_deleted"] else ""),
        f"- **Files:** {t['files']:,} ({human_bytes(t['file_bytes'])})",
        f"- **API calls used:** {m['api_calls']:,}",
    ]
    if m.get("incremental_since"):
        lines.append(f"- **Incremental:** changes since {m['incremental_since']} were merged in. "
                     "Formula fields on unchanged rows keep the value from when each row was "
                     "last fetched; a periodic full export refreshes them.")
    if m.get("changes_dir"):
        lines.append(f"- **History:** {t.get('changed_records', 0):,} records changed since the "
                     f"previous run (`{m['changes_dir']}`, see `sf-exit history`).")
    if m.get("redaction"):
        lines.append("- **Redacted:** chosen fields hold salted hashes, not values (see manifest.json).")
    lines += ["", "## Largest objects", "", "| Object | Rows | Engine |", "|---|---:|---|"]
    for o in sorted(objs, key=lambda o: -o["rows"])[:25]:
        lines.append(f"| `{o['name']}` | {o['rows']:,} | {o['engine']} |")
    if m["files"]:
        lines += ["", "## Files", "", "| Kind | Count | Size | Errors |", "|---|---:|---:|---:|"]
        for kind, s in m["files"].items():
            lines.append(f"| {kind} | {s['count']:,} | {human_bytes(s['bytes'])} | {s['errors']} |")
    if failed or m.get("file_listing_errors"):
        lines += ["", "## Failed", "", "Re-run the same command to retry: finished work is kept.", ""]
        for o in failed:
            lines.append(f"- `{o['name']}`: {o['error']}")
        for kind, err in (m.get("file_listing_errors") or {}).items():
            lines.append(f"- {kind} files could not be listed: {err}")
    if partial or m.get("warnings"):
        lines += ["", "## Warnings", ""]
        for o in partial:
            for w in o["warnings"]:
                lines.append(f"- `{o['name']}`: {w}")
        for w in m.get("warnings") or []:
            lines.append(f"- {w}")
    if m["skipped"] or unexportable:
        n = len(m["skipped"]) + len(unexportable)
        lines += ["", f"<details><summary>{n} objects skipped (Salesforce doesn't allow a plain "
                  "export of them, or you excluded them)</summary>", ""]
        for name, why in m["skipped"].items():
            lines.append(f"- `{name}`: {why}")
        for o in unexportable:
            lines.append(f"- `{o['name']}`: {o['error'][:300]}")
        lines += ["", "</details>"]
    lines += [
        "",
        "## What's in this folder",
        "",
        "- `data/<Object>.csv`: one CSV per object, every field, Salesforce Ids preserved",
        "- `files/`: binary files (ContentVersion, Attachment, Document) plus `files/index.csv`"
        " mapping each file to its record",
        "- `schema/<Object>.json`: full Salesforce describe metadata (labels, picklists, relationships)",
        "- `schema.{postgres,duckdb,sqlite}.sql`: ready-to-run DDL",
        "- `erd.mmd`: Mermaid entity-relationship diagram",
        "- `changes/<run>/`: what changed in each run, for `sf-exit history` and `sf-exit snapshot`",
        "- `manifest.json`: machine-readable summary with SHA-256 checksums",
        "",
        "## Next steps",
        "",
        "```bash",
        "sf-exit browse .                                  # click through it offline",
        "sf-exit audit .                                   # health score + data dictionary",
        "sf-exit load . --to duckdb:///salesforce.duckdb   # or sqlite:///, postgresql://, parquet:///",
        "sf-exit verify .                                  # re-check counts and checksums",
        "```",
        "",
    ]
    return "\n".join(lines)

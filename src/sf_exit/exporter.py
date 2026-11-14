"""The export pipeline: discover → describe → export records → export files → write manifest."""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence
from urllib.parse import quote

from sf_exit import __version__
from sf_exit.client import COMPOSITE_BATCH_SIZE, SalesforceClient, SalesforceError, soql_quote
from sf_exit.discovery import ObjectPlan, _matches, is_not_exportable, plan_object, select_objects
from sf_exit.schema import DIALECTS, mermaid_erd, schema_sql
from sf_exit.utils import file_sha256, human_bytes, safe_filename, utcnow

# Above this many (approximate) rows the Bulk API is used; below it the REST API is faster.
BULK_THRESHOLD = 10_000
# Long SOQL can't go in a GET URL, so very wide objects always use Bulk (POST).
MAX_REST_SOQL = 12_000
CDL_CHUNK = 150


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
    fresh: bool = False
    max_records: Optional[int] = None
    poll_interval: float = 2.0


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


@dataclass
class ExportResult:
    out_dir: Path
    objects: Dict[str, ObjectResult] = field(default_factory=dict)
    skipped: Dict[str, str] = field(default_factory=dict)
    files: Dict[str, Dict[str, int]] = field(default_factory=dict)
    file_errors: int = 0
    api_calls: int = 0

    @property
    def total_rows(self) -> int:
        return sum(o.rows for o in self.objects.values() if o.status == "done")

    @property
    def failed(self) -> List[ObjectResult]:
        return [o for o in self.objects.values() if o.status == "failed"]


class State:
    """Resumable progress, stored in <out>/.sf-exit/state.json."""

    def __init__(self, path: Path, fresh: bool = False, fingerprint: Optional[Dict] = None):
        self.path = path
        self.lock = threading.Lock()
        self.fingerprint = fingerprint or {}
        self.data: Dict[str, Any] = {"objects": {}, "options": self.fingerprint}
        if path.exists() and not fresh:
            with contextlib.suppress(ValueError):
                loaded = json.loads(path.read_text("utf-8"))
                # Resuming with different options (e.g. --include-deleted) would mix
                # incompatible files, so start over in that case.
                if loaded.get("options", {}) == self.fingerprint:
                    self.data = loaded

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        return self.data["objects"].get(name)

    def put(self, result: ObjectResult) -> None:
        with self.lock:
            self.data["objects"][result.name] = asdict(result)
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
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
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
        self.state = State(self.out / ".sf-exit" / "state.json", fresh=options.fresh,
                           fingerprint={"include_deleted": options.include_deleted})
        self.result = ExportResult(out_dir=self.out)
        self.plans: Dict[str, ObjectPlan] = {}

    # ------------------------------------------------------------------ public
    def discover(self) -> Dict[str, ObjectPlan]:
        self.progress.phase("Discovering objects")
        chosen, skipped = select_objects(
            self.sf.describe_global(), self.opt.include, self.opt.exclude
        )
        self.result.skipped.update(skipped)
        names = [o["name"] for o in chosen]
        counts = self.sf.record_counts(names)

        self.progress.phase(f"Describing {len(names)} objects")
        self.progress.start("describe", "Reading schema", len(names))
        plans: Dict[str, ObjectPlan] = {}
        chunks = [names[i:i + COMPOSITE_BATCH_SIZE] for i in range(0, len(names), COMPOSITE_BATCH_SIZE)]
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
        started = utcnow()
        for d in (self.data_dir, self.schema_dir):
            d.mkdir(parents=True, exist_ok=True)
        org = self.sf.org_info()
        self.org = org

        plans = self.discover()
        for name, plan in plans.items():
            (self.schema_dir / f"{name}.json").write_text(
                json.dumps(plan.describe, indent=1, sort_keys=True), "utf-8"
            )

        if self.opt.data:
            self.export_records(plans)
            if "ContentDocument" in plans and self._wanted("ContentDocumentLink"):
                self.export_content_document_links()
        if self.opt.files:
            self.export_files()

        self.result.api_calls = self.sf.api_calls
        self.write_outputs(started)
        return self.result

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
                self.result.objects[name] = ObjectResult(**prev)
                continue
            todo.append(plan)
        resumed = len(plans) - len(todo)
        self.progress.phase(
            f"Exporting records from {len(todo)} objects"
            + (f" ({resumed} already done, resuming)" if resumed else "")
        )
        # Small objects: first page of up to 25 objects per API call (composite batch).
        # Big objects: one Bulk API job each, largest first so long jobs start early.
        batchable = [p for p in todo if self._batchable(p)]
        singles = sorted((p for p in todo if not self._batchable(p)),
                         key=lambda p: -(p.approx_rows or 0))
        batches = [batchable[i:i + COMPOSITE_BATCH_SIZE]
                   for i in range(0, len(batchable), COMPOSITE_BATCH_SIZE)]
        self.progress.start("records", "Objects", len(todo))
        with ThreadPoolExecutor(max_workers=max(1, self.opt.workers)) as pool:
            futures = [pool.submit(self.export_object, plan) for plan in singles]
            futures += [pool.submit(self.export_batch, batch) for batch in batches]
            for fut in as_completed(futures):
                res = fut.result()
                for r in res if isinstance(res, list) else [res]:
                    self.result.objects[r.name] = r
                    self.state.put(r)
                    self.progress.advance("records", 1)
        self.progress.finish("records", "done")

    def _batchable(self, plan: ObjectPlan) -> bool:
        return (self._choose_engine(plan) == "rest" and not plan.where_variants
                and len(plan.soql()) <= MAX_REST_SOQL)

    def export_batch(self, plans: List[ObjectPlan]) -> List[ObjectResult]:
        """Fetch the first page of up to 25 small objects in a single API call, then
        follow pagination per object. Anything that errors goes the normal route."""
        endpoint = "queryAll" if self.opt.include_deleted else "query"
        try:
            pages = self.sf.composite_get(
                [f"{endpoint}?q={quote(p.soql(), safe='')}" for p in plans])
        except SalesforceError:
            return [self.export_object(p) for p in plans]
        results = []
        for plan, (ok, body) in zip(plans, pages, strict=True):
            if not ok:
                results.append(self.export_object(plan))
                continue
            t0 = dt.datetime.now()
            key = f"obj:{plan.name}"
            dest = self.data_dir / f"{plan.name}.csv"
            tmp = dest.with_name(dest.name + ".part")
            self.progress.start(key, plan.name, plan.approx_rows)
            try:
                rows = write_records_csv(
                    self.sf.query(plan.soql(), self.opt.include_deleted, first_page=body),
                    plan.fields, tmp, on_rows=lambda n, k=key: self.progress.advance(k, n))
            except SalesforceError:
                tmp.unlink(missing_ok=True)
                self.progress.finish(key, "retry")
                results.append(self.export_object(plan))
                continue
            results.append(self._done(plan, tmp, dest, key, rows, "rest", t0))
        return results

    def _done(self, plan: ObjectPlan, tmp: Path, dest: Path, key: str, rows: int,
              engine: str, t0: dt.datetime) -> ObjectResult:
        tmp.replace(dest)
        self.progress.finish(key, "done", f"{rows:,} row{'' if rows == 1 else 's'} via {engine}")
        return ObjectResult(
            plan.name, "done", rows=rows, engine=engine,
            file=str(dest.relative_to(self.out)), sha256=file_sha256(dest),
            seconds=(dt.datetime.now() - t0).total_seconds(),
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

    def export_object(self, plan: ObjectPlan) -> ObjectResult:
        t0 = dt.datetime.now()
        dest = self.data_dir / f"{plan.name}.csv"
        tmp = dest.with_name(dest.name + ".part")
        key = f"obj:{plan.name}"
        self.progress.start(key, plan.name, plan.approx_rows)
        engine = self._choose_engine(plan)
        errors: List[SalesforceError] = []
        rows = None
        attempts = [engine, "rest"] if engine == "bulk" else ["rest", "bulk"]
        if plan.where_variants:
            attempts = ["rest"]
        for attempt_engine in attempts:
            if attempt_engine == "rest" and len(plan.soql()) > MAX_REST_SOQL:
                continue
            try:
                rows = self._run_engine(attempt_engine, plan, tmp, key)
                engine = attempt_engine
                break
            except SalesforceError as exc:
                exc.message = f"{attempt_engine}: {exc.message}"
                errors.append(exc)
                if not _worth_retrying_other_engine(exc):
                    break
        if rows is not None:
            return self._done(plan, tmp, dest, key, rows, engine, t0)
        tmp.unlink(missing_ok=True)
        secs = (dt.datetime.now() - t0).total_seconds()
        msg = " | ".join(f"[{e.code}] {e.message}" if e.code else e.message for e in errors)
        msg = msg or "unknown error"
        if errors and all(is_not_exportable(f"{e.code} {e.message}") for e in errors):
            self.progress.finish(key, "skipped", msg)
            return ObjectResult(plan.name, "skipped", error=msg, seconds=secs)
        self.progress.finish(key, "failed", msg)
        return ObjectResult(plan.name, "failed", error=msg, seconds=secs)

    def _run_engine(self, engine: str, plan: ObjectPlan, tmp: Path, key: str) -> int:
        def bump(n: int) -> None:
            self.progress.advance(key, n)

        if engine == "bulk":
            return self.sf.bulk_query_to_file(
                plan.soql(), tmp, include_deleted=self.opt.include_deleted,
                poll_interval=self.opt.poll_interval, max_records=self.opt.max_records,
                on_rows=bump,
            )
        if plan.where_variants:
            failures: List[SalesforceError] = []
            rows = write_records_csv(self._variant_records(plan, failures), plan.fields, tmp,
                                     on_rows=bump)
            if len(failures) == len(plan.where_variants):
                raise failures[0]
            return rows
        records = self.sf.query(plan.soql(), include_deleted=self.opt.include_deleted)
        return write_records_csv(records, plan.fields, tmp, on_rows=bump)

    def _variant_records(self, plan: ObjectPlan,
                         failures: List[SalesforceError]) -> Iterator[Dict[str, Any]]:
        for where in plan.where_variants:
            try:
                yield from self.sf.query(plan.soql(where), include_deleted=self.opt.include_deleted)
            except SalesforceError as exc:
                failures.append(exc)

    def export_content_document_links(self) -> None:
        """ContentDocumentLink (which file is attached to which record) can't be queried
        without a filter, so query it in chunks of the ContentDocument Ids we exported."""
        src = self.data_dir / "ContentDocument.csv"
        res = self.result.objects.get("ContentDocument")
        if not src.exists() or not res or res.status != "done":
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
            json.dumps(desc, indent=1, sort_keys=True), "utf-8")
        with open(src, newline="", encoding="utf-8") as fh:
            ids = [row["Id"] for row in csv.DictReader(fh) if row.get("Id")]
        key = "obj:ContentDocumentLink"
        self.progress.start(key, "ContentDocumentLink", None)
        dest = self.data_dir / "ContentDocumentLink.csv"
        tmp = dest.with_name(dest.name + ".part")

        def records() -> Iterable[Dict[str, Any]]:
            for i in range(0, len(ids), CDL_CHUNK):
                chunk = ids[i:i + CDL_CHUNK]
                where = "ContentDocumentId IN (" + ",".join(soql_quote(x) for x in chunk) + ")"
                yield from self.sf.query(plan.soql(where))

        t0 = dt.datetime.now()
        try:
            rows = write_records_csv(records(), plan.fields, tmp,
                                     on_rows=lambda n: self.progress.advance(key, n))
        except SalesforceError as exc:
            tmp.unlink(missing_ok=True)
            self.progress.finish(key, "failed", str(exc))
            r = ObjectResult("ContentDocumentLink", "failed", error=str(exc))
        else:
            tmp.replace(dest)
            self.progress.finish(key, "done", f"{rows:,} row{'' if rows == 1 else 's'} via rest")
            r = ObjectResult("ContentDocumentLink", "done", rows=rows, engine="rest",
                             file=str(dest.relative_to(self.out)), sha256=file_sha256(dest),
                             seconds=(dt.datetime.now() - t0).total_seconds())
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
        for kind, soql in specs:
            if not self._wanted(kind):
                continue
            try:
                recs = list(self.sf.query(soql))
            except SalesforceError as exc:
                self.progress.log(f"[yellow]Skipping {kind} files:[/] {exc}")
                continue
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
        rows: List[Dict[str, Any]] = []
        lock = threading.Lock()

        def work(job: Dict[str, Any]) -> Dict[str, Any]:
            folder = self.files_dir / job["kind"] / (job["parent"] or "_")
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / job["rel"]
            row = dict(kind=job["kind"], record_id=job["id"], parent_id=job["parent"],
                       name=job["name"], path=str(dest.relative_to(self.out)), bytes=0,
                       sha256="", md5="", sf_checksum=job["checksum"], status="ok", error="")
            try:
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
            except SalesforceError as exc:
                row.update(status="error", error=str(exc))
            self.progress.advance("files", int(job.get("size") or 0) if total_bytes else 1)
            return row

        with ThreadPoolExecutor(max_workers=max(1, self.opt.file_workers)) as pool:
            for row in pool.map(work, jobs):
                with lock:
                    rows.append(row)
        self.progress.finish("files", "done")

        if jobs or self.files_dir.exists():
            self.files_dir.mkdir(parents=True, exist_ok=True)
            cols = ["kind", "record_id", "parent_id", "name", "path", "bytes", "sha256", "md5",
                    "sf_checksum", "status", "error"]
            with open(self.files_dir / "index.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
                w.writeheader()
                w.writerows(sorted(rows, key=lambda r: (r["kind"], r["path"])))
        summary: Dict[str, Dict[str, int]] = {}
        for r in rows:
            s = summary.setdefault(r["kind"], {"count": 0, "bytes": 0, "errors": 0})
            s["count"] += 1
            s["bytes"] += int(r["bytes"] or 0)
            if not r["status"].startswith("ok"):
                s["errors"] += 1
                self.result.file_errors += 1
        self.result.files = summary

    # ----------------------------------------------------------------- outputs
    def write_outputs(self, started: str) -> None:
        exported = {n: p for n, p in self.plans.items()
                    if self.result.objects.get(n) and self.result.objects[n].status == "done"}
        org = self.org
        for dialect in DIALECTS:
            (self.out / f"schema.{dialect}.sql").write_text(
                schema_sql(exported, dialect, comment=f"Salesforce org {org.name} ({org.id})"),
                "utf-8")
        counts = {n: r.rows for n, r in self.result.objects.items()}
        (self.out / "erd.mmd").write_text(mermaid_erd(exported, counts), "utf-8")

        manifest = {
            "tool": "sf-exit",
            "version": __version__,
            "started_at": started,
            "finished_at": utcnow(),
            "org": asdict(org),
            "instance_url": self.sf.instance_url,
            "api_version": self.sf.api_version,
            "include_deleted": self.opt.include_deleted,
            "all_file_versions": self.opt.all_file_versions,
            "api_calls": self.sf.api_calls,
            "totals": {
                "objects": len(exported),
                "rows": self.result.total_rows,
                "files": sum(v["count"] for v in self.result.files.values()),
                "file_bytes": sum(v["bytes"] for v in self.result.files.values()),
            },
            "objects": {n: asdict(r) for n, r in sorted(self.result.objects.items())},
            "skipped": dict(sorted(self.result.skipped.items())),
            "files": self.result.files,
        }
        (self.out / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")
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
        "",
        "## Largest objects",
        "",
        "| Object | Rows | Engine |",
        "|---|---:|---|",
    ]
    for o in sorted(objs, key=lambda o: -o["rows"])[:25]:
        lines.append(f"| `{o['name']}` | {o['rows']:,} | {o['engine']} |")
    if m["files"]:
        lines += ["", "## Files", "", "| Kind | Count | Size | Errors |", "|---|---:|---:|---:|"]
        for kind, s in m["files"].items():
            lines.append(f"| {kind} | {s['count']:,} | {human_bytes(s['bytes'])} | {s['errors']} |")
    if failed:
        lines += ["", "## Failed objects", "", "Re-run the same command to retry just these.", ""]
        for o in failed:
            lines.append(f"- `{o['name']}`: {o['error']}")
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
        "- `manifest.json`: machine-readable summary with SHA-256 checksums",
        "",
        "## Next steps",
        "",
        "```bash",
        "sf-exit load . --to duckdb:///salesforce.duckdb   # or sqlite:///, postgresql://, parquet:///",
        "sf-exit verify .                                  # re-check counts and checksums",
        "```",
        "",
    ]
    return "\n".join(lines)

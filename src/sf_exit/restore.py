"""``sf-exit restore``: put records from an export back into a Salesforce org.

Typical uses: seed a sandbox with real data, rebuild an org, or recover deleted records.

How it works:

* objects are inserted in dependency order (parents before children);
* every new record's Id is mapped to its old Id (``restore/<org id>/id-map.csv``) and all
  lookups are rewritten through that map, so relationships survive;
* lookups that point "backwards" (self-references such as Account.ParentId, or cycles) are
  filled in by a second pass;
* users and record types are matched to the target org by username/email and developer name;
* restoring into the org the export came from keeps lookups to records that still exist;
* ``upsert`` (Object.ExternalIdField) makes a restore idempotent: records are matched on an
  External ID field (filled with the old Salesforce Id unless the export has a value), so
  re-running updates them instead of creating duplicates;
* ``files=True`` uploads Salesforce Files and Attachments and links them to the restored
  records;
* it is resumable: records already in the Id map are skipped. A batch whose outcome is
  unknown (the connection dropped after it was sent) is recorded in ``unknown.csv`` and the
  restore stops, so a re-run can never silently create duplicates.

This is the only sf-exit command that writes to Salesforce. It is a dry run unless
``execute=True``, and refuses production orgs unless ``allow_production=True``.
Metadata is restored with the standard tools (see the docs).
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from sf_exit.client import SalesforceClient, SalesforceError
from sf_exit.discovery import _matches

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

BATCH = 200  # sObject Collections limit
BULK_PRESENCE = 10_000  # same org: check which records still exist with Bulk API above this
# Never created by a restore: identity, sharing, history and file plumbing.
NEVER_RESTORE = ("User", "Group", "GroupMember", "Profile", "PermissionSet*", "UserRole",
                 "Organization", "RecordType", "*Share", "*History", "*Feed", "ContentDocument",
                 "ContentDocumentLink", "ContentVersion", "Attachment", "Document", "*__mdt",
                 "*ChangeEvent", "LoginHistory", "SetupAuditTrail", "ApexClass", "ApexTrigger")
NUMERIC = {"int", "long", "double", "currency", "percent"}


class RestoreError(Exception):
    pass


@dataclass
class ObjectPlan:
    name: str
    rows: int
    fields: List[str]  # createable fields present in the CSV
    types: Dict[str, str]
    refs: Dict[str, List[str]]  # lookup field -> target objects
    required_refs: Set[str] = field(default_factory=set)
    deferred: List[str] = field(default_factory=list)  # lookups filled in by pass 2
    skipped_fields: List[str] = field(default_factory=list)
    upsert_field: Optional[str] = None  # External ID field to upsert on, if any


@dataclass
class RestoreResult:
    order: List[ObjectPlan]
    not_restorable: Dict[str, str]
    created: Dict[str, int] = field(default_factory=dict)
    skipped_existing: Dict[str, int] = field(default_factory=dict)
    failed: Dict[str, int] = field(default_factory=dict)
    relinked: Dict[str, int] = field(default_factory=dict)
    unmapped_lookups: Dict[str, int] = field(default_factory=dict)
    unmatched_users: Dict[str, int] = field(default_factory=dict)
    updated: Dict[str, int] = field(default_factory=dict)  # upserts that matched a record
    kept_lookups: Dict[str, int] = field(default_factory=dict)  # same-org: parent still exists
    still_present: Dict[str, int] = field(default_factory=dict)  # same-org: not deleted, left alone
    skipped_unknown: Dict[str, int] = field(default_factory=dict)
    files_planned: int = 0
    files_restored: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    links_created: int = 0
    same_org: bool = False
    executed: bool = False
    out_dir: Optional[Path] = None

    @property
    def total_created(self) -> int:
        return sum(self.created.values())

    @property
    def total_failed(self) -> int:
        return sum(self.failed.values())


def _header(path: Path) -> List[str]:
    with open(path, newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh), [])


def _count(path: Path) -> int:
    with open(path, newline="", encoding="utf-8") as fh:
        return max(sum(1 for _ in csv.reader(fh)) - 1, 0)


def _records(path: Path) -> Iterator[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def plan_restore(target: SalesforceClient, export_dir: Path, include: Sequence[str],
                 exclude: Sequence[str] = (), upsert: Sequence[str] = ()
                 ) -> Tuple[List[ObjectPlan], Dict[str, str]]:
    """Decide which objects and fields can be restored, and in which order."""
    data = Path(export_dir) / "data"
    candidates = [p.stem for p in sorted(data.glob("*.csv")) if _matches(p.stem, include)
                  and not (exclude and _matches(p.stem, exclude))]
    not_restorable: Dict[str, str] = {}
    names = []
    for n in candidates:
        if _matches(n, NEVER_RESTORE):
            not_restorable[n] = "not restored by design (identity, sharing, history or file object)"
        else:
            names.append(n)
    described = target.describe_many(names) if names else {}
    plans: Dict[str, ObjectPlan] = {}
    for n in names:
        d = described.get(n)
        if isinstance(d, SalesforceError) or d is None:
            not_restorable[n] = f"does not exist in the target org ({d})"
            continue
        if not d.get("createable", True):
            not_restorable[n] = "the target org does not allow creating records of this object"
            continue
        meta = {f["name"]: f for f in d.get("fields", [])}
        header = _header(data / f"{n}.csv")
        fields, skipped = [], []
        for c in header:
            m = meta.get(c)
            if c == "Id":
                continue
            if m is None:
                skipped.append(c)  # field doesn't exist in the target
            elif m.get("createable", True) and not m.get("calculated") and not m.get("autoNumber"):
                fields.append(c)
        refs = {c: list(meta[c].get("referenceTo") or []) for c in fields
                if meta[c].get("type") == "reference"}
        required = {c for c in refs if not meta[c].get("nillable", True)
                    and not meta[c].get("defaultedOnCreate")}
        plans[n] = ObjectPlan(n, _count(data / f"{n}.csv"), fields,
                              {c: meta[c].get("type", "string") for c in fields}, refs, required,
                              skipped_fields=skipped)
        for spec in upsert:
            obj_pattern, _, ext_field = spec.rpartition(".")
            if not obj_pattern or not _matches(n, [obj_pattern]):
                continue
            m = meta.get(ext_field) or {}
            if m.get("externalId"):
                plans[n].upsert_field = ext_field
                break
            if obj_pattern == n:  # named explicitly: a mistake worth stopping for
                raise RestoreError(f"--upsert {spec}: {n}.{ext_field} is not an External ID field "
                                   "in the target org. Create it (Text, External ID, Unique) first.")

    # Order: an object goes after every restored object it has a lookup to, when possible.
    order: List[ObjectPlan] = []
    placed: Set[str] = set()
    remaining = dict(plans)

    def deps(p: ObjectPlan, required_only: bool) -> Set[str]:
        out: Set[str] = set()
        for f, targets in p.refs.items():
            if required_only and f not in p.required_refs:
                continue
            out |= {t for t in targets if t in plans and t != p.name}
        return out

    while remaining:
        ready = [p for p in remaining.values() if deps(p, False) <= placed]
        if not ready:  # a cycle: fall back to required lookups only, then to anything
            ready = [p for p in remaining.values() if deps(p, True) <= placed]
        if not ready:
            ready = [min(remaining.values(), key=lambda p: (len(deps(p, True) - placed), p.name))]
        for p in sorted(ready, key=lambda p: p.name):
            order.append(p)
            placed.add(p.name)
            del remaining[p.name]
    seen: Set[str] = set()
    for p in order:
        seen.add(p.name)
        for f, targets in p.refs.items():
            in_set = [t for t in targets if t in plans]
            if in_set and any(t not in seen or t == p.name for t in in_set):
                p.deferred.append(f)
    return order, not_restorable


def check_target(target: SalesforceClient, export_dir: Path, allow_production: bool,
                 allow_same_org: bool) -> Dict[str, Any]:
    rows = list(target.query("SELECT Id, Name, OrganizationType, IsSandbox FROM Organization"))
    org = rows[0] if rows else {}
    safe = bool(org.get("IsSandbox")) or org.get("OrganizationType") == "Developer Edition"
    if not safe and not allow_production:
        raise RestoreError(
            f"'{org.get('Name')}' is a production org. Restore into a sandbox or Developer Edition "
            "org first; pass --allow-production only if you are sure.")
    manifest = Path(export_dir) / "manifest.json"
    if manifest.exists():
        source = json.loads(manifest.read_text("utf-8")).get("org", {})
        if source.get("id") and source["id"][:15] == (org.get("Id") or "")[:15] and not allow_same_org:
            raise RestoreError(
                "The target is the org this export came from. Restoring creates NEW records, so "
                "anything that still exists would be duplicated. Use --include to pick only what "
                "was lost and pass --allow-same-org to proceed.")
    return org


class Restorer:
    def __init__(self, target: SalesforceClient, export_dir: Path, include: Sequence[str],
                 exclude: Sequence[str] = (), execute: bool = False, allow_production: bool = False,
                 allow_same_org: bool = False, upsert: Sequence[str] = (), files: bool = False,
                 on_progress: Optional[Callable[[str, int, int], None]] = None):
        self.sf = target
        self.export_dir = Path(export_dir)
        self.include, self.exclude = include, exclude
        self.execute = execute
        self.allow_production, self.allow_same_org = allow_production, allow_same_org
        self.upsert, self.files = upsert, files
        self.progress = on_progress or (lambda name, done, total: None)
        self.id_map: Dict[str, str] = {}
        self.external: Dict[str, str] = {}  # users, record types: old Id -> target Id
        self.unknown: Set[str] = set()
        self.present: Set[str] = set()  # same org: exported records that still exist there
        self.same_org = False

    def run(self) -> RestoreResult:
        if not self.include:
            raise RestoreError("Choose what to restore with --include, e.g. -i Account -i Contact")
        org = check_target(self.sf, self.export_dir, self.allow_production, self.allow_same_org)
        source = _source_org(self.export_dir)
        self.same_org = bool(source) and source[:15] == (org.get("Id") or "")[:15]
        order, not_restorable = plan_restore(self.sf, self.export_dir, self.include, self.exclude,
                                             self.upsert)
        result = RestoreResult(order, not_restorable, executed=self.execute, same_org=self.same_org)
        if self.same_org:  # bring back only what's missing: never duplicate a surviving record
            for plan in list(order):
                try:
                    found = self._present_ids(plan)
                except SalesforceError as exc:
                    order.remove(plan)
                    not_restorable[plan.name] = f"can't check which records still exist ({exc})"
                    continue
                self.present |= found
                result.still_present[plan.name] = len(found)
        if self.files:
            result.files_planned = len(self._file_jobs(
                {o for p in order for o in self._ids(p) if o not in self.present}))
        if not self.execute or not order:
            return result
        out = self.export_dir / "restore" / (org.get("Id") or "target")
        out.mkdir(parents=True, exist_ok=True)
        result.out_dir = out
        self._load_id_map(out / "id-map.csv")
        self._load_unknown(out / "unknown.csv")
        self._map_users_and_record_types()
        with open(out / "id-map.csv", "a", newline="", encoding="utf-8") as map_fh, \
                open(out / "errors.csv", "a", newline="", encoding="utf-8") as err_fh:
            self.map_writer = csv.writer(map_fh, lineterminator="\n")
            self.err_writer = csv.writer(err_fh, lineterminator="\n")
            self.map_fh = map_fh
            if map_fh.tell() == 0:
                self.map_writer.writerow(["object", "old_id", "new_id"])
            if err_fh.tell() == 0:
                self.err_writer.writerow(["object", "old_id", "step", "error"])
            for plan in order:
                self._insert(plan, result, out)
                map_fh.flush()
            for plan in order:
                if plan.deferred:
                    self._relink(plan, result)
            if self.files:
                self._restore_files(result, out)
        return result

    def _present_ids(self, plan: ObjectPlan) -> Set[str]:
        """Same org: which of the exported records still exist (not deleted) in the org."""
        wanted = set(self._ids(plan))
        if not wanted:
            return set()
        soql = f"SELECT Id FROM {plan.name}"
        if plan.rows >= BULK_PRESENCE:  # one Bulk job instead of hundreds of REST pages
            tmp = self.export_dir / ".sf-exit" / f"present-{plan.name}.csv"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.sf.bulk_query_to_file(soql, tmp)
                return {i for i in (r.get("Id") for r in _records(tmp)) if i in wanted}
            except SalesforceError:
                pass  # e.g. an object Bulk API doesn't support: ask REST instead
            finally:
                tmp.unlink(missing_ok=True)
        return {i for i in (r.get("Id") for r in self.sf.query(soql)) if i in wanted}

    def _ids(self, plan: ObjectPlan) -> Iterator[str]:
        for row in _records(self.export_dir / "data" / f"{plan.name}.csv"):
            if row.get("Id"):
                yield row["Id"]

    # ------------------------------------------------------------------ mapping
    def _load_id_map(self, path: Path) -> None:
        if path.exists():
            for r in _records(path):
                self.id_map[r["old_id"]] = r["new_id"]

    def _load_unknown(self, path: Path) -> None:
        if path.exists():
            self.unknown = {r["old_id"] for r in _records(path)}

    def _map_users_and_record_types(self) -> None:
        data = self.export_dir / "data"
        if (data / "User.csv").exists():
            by_username, by_email = {}, {}
            for u in self.sf.query("SELECT Id, Username, Email FROM User WHERE IsActive = true"):
                by_username[(u.get("Username") or "").lower()] = u["Id"]
                by_email.setdefault((u.get("Email") or "").lower(), u["Id"])
            for u in _records(data / "User.csv"):
                hit = by_username.get((u.get("Username") or "").lower()) \
                    or by_email.get((u.get("Email") or "").lower())
                if hit:
                    self.external[u["Id"]] = hit
        if (data / "RecordType.csv").exists():
            target = {(r.get("SobjectType"), r.get("DeveloperName")): r["Id"] for r in
                      self.sf.query("SELECT Id, SobjectType, DeveloperName FROM RecordType")}
            for r in _records(data / "RecordType.csv"):
                hit = target.get((r.get("SobjectType"), r.get("DeveloperName")))
                if hit:
                    self.external[r["Id"]] = hit

    def _convert(self, value: str, ftype: str) -> Any:
        if ftype == "boolean":
            return value.lower() == "true"
        if ftype in NUMERIC:
            try:
                return int(value) if ftype in ("int", "long") else float(value)
            except ValueError:
                return value
        return value

    def _body(self, plan: ObjectPlan, row: Dict[str, str], result: RestoreResult) -> Dict[str, Any]:
        body: Dict[str, Any] = {"attributes": {"type": plan.name}}
        for f in plan.fields:
            v = row.get(f, "")
            if v == "":
                continue
            if f in plan.refs:
                mapped = self.id_map.get(v) or self.external.get(v)
                if mapped:
                    body[f] = mapped
                elif f in plan.deferred:
                    continue  # pass 2
                elif self.same_org:  # same org: the parent may still exist under its own Id
                    body[f] = v
                    key = f"{plan.name}.{f}"
                    result.kept_lookups[key] = result.kept_lookups.get(key, 0) + 1
                else:
                    key = f"{plan.name}.{f}"
                    bucket = result.unmatched_users if "User" in plan.refs[f] else result.unmapped_lookups
                    bucket[key] = bucket.get(key, 0) + 1
                continue
            body[f] = self._convert(v, plan.types.get(f, "string"))
        if plan.upsert_field and not body.get(plan.upsert_field):
            body[plan.upsert_field] = row["Id"]  # remember the original Id in the External ID
        return body

    # ------------------------------------------------------------------- writes
    def _insert(self, plan: ObjectPlan, result: RestoreResult, out: Path) -> None:
        path = self.export_dir / "data" / f"{plan.name}.csv"
        batch: List[Tuple[str, Dict[str, Any]]] = []
        done = 0
        result.created.setdefault(plan.name, 0)

        def flush() -> None:
            nonlocal done
            if not batch:
                return
            try:
                if plan.upsert_field:  # idempotent: safe to retry after a network failure
                    resp = self.sf.request(
                        "PATCH", f"composite/sobjects/{plan.name}/{plan.upsert_field}",
                        json_body={"allOrNone": False, "records": [b for _, b in batch]}).json()
                else:
                    resp = self.sf.request("POST", "composite/sobjects", retry=False, json_body={
                        "allOrNone": False, "records": [b for _, b in batch]}).json()
            except SalesforceError as exc:
                if exc.code == "OUTCOME_UNKNOWN":
                    self._stop_unknown(plan, [old for old, _ in batch], out, exc)
                resp = [{"success": False, "errors": [{"message": str(exc)}]}] * len(batch)
            for (old, _), res in zip(batch, resp, strict=False):
                if res.get("success"):
                    if self.id_map.get(old) != res["id"]:  # upsert re-runs: already mapped
                        self.id_map[old] = res["id"]
                        self.map_writer.writerow([plan.name, old, res["id"]])
                    if res.get("created", True):
                        result.created[plan.name] += 1
                    else:
                        result.updated[plan.name] = result.updated.get(plan.name, 0) + 1
                else:
                    msg = "; ".join(f"{e.get('statusCode', '')} {e.get('message', '')}".strip()
                                    for e in res.get("errors", [])) or "unknown error"
                    self.err_writer.writerow([plan.name, old, "insert", msg])
                    result.failed[plan.name] = result.failed.get(plan.name, 0) + 1
            done += len(batch)
            batch.clear()
            self.progress(plan.name, done, plan.rows)

        for row in _records(path):
            old = row.get("Id", "")
            if not old or row.get("IsDeleted", "").lower() == "true":
                continue
            if old in self.present:  # same org and never deleted: nothing to bring back
                continue
            if old in self.id_map and not plan.upsert_field:  # restored by an earlier run
                # (with --upsert it's sent again, so the target is brought up to date)
                result.skipped_existing[plan.name] = result.skipped_existing.get(plan.name, 0) + 1
                continue
            if old in self.unknown:  # may already exist: never risk a duplicate
                result.skipped_unknown[plan.name] = result.skipped_unknown.get(plan.name, 0) + 1
                continue
            batch.append((old, self._body(plan, row, result)))
            if len(batch) >= BATCH:
                flush()
        flush()

    def _stop_unknown(self, plan: ObjectPlan, old_ids: List[str], out: Path, exc: Exception) -> None:
        path = out / "unknown.csv"
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            if new:
                w.writerow(["object", "old_id"])
            w.writerows([plan.name, i] for i in old_ids)
        self.map_fh.flush()
        raise RestoreError(
            f"The connection failed after sending {len(old_ids)} {plan.name} records, so Salesforce "
            f"may or may not have created them ({exc}). Their old Ids are in {path}. Check the "
            "target org: re-running skips them, and deleting that file retries them. Use --upsert "
            "with an External ID field to make restores safe to retry.")

    def _relink(self, plan: ObjectPlan, result: RestoreResult) -> None:
        """Second pass: fill lookups whose target didn't exist yet during the insert."""
        path = self.export_dir / "data" / f"{plan.name}.csv"
        batch: List[Dict[str, Any]] = []

        def flush() -> None:
            if not batch:
                return
            try:  # an update sets the same values again: safe to retry
                resp = self.sf.request("PATCH", "composite/sobjects", json_body={
                    "allOrNone": False, "records": list(batch)}).json()
            except SalesforceError as exc:
                resp = [{"success": False, "errors": [{"message": str(exc)}]}] * len(batch)
            for body, res in zip(batch, resp, strict=False):
                if res.get("success"):
                    result.relinked[plan.name] = result.relinked.get(plan.name, 0) + 1
                else:
                    msg = "; ".join(e.get("message", "") for e in res.get("errors", []))
                    self.err_writer.writerow([plan.name, body["id"], "relink", msg])
            batch.clear()

        for row in _records(path):
            new_id = self.id_map.get(row.get("Id", ""))
            if not new_id:
                continue
            body: Dict[str, Any] = {"attributes": {"type": plan.name}, "id": new_id}
            for f in plan.deferred:
                old = row.get(f, "")
                mapped = self.id_map.get(old) or self.external.get(old)
                if mapped:
                    body[f] = mapped
                elif old and self.same_org:
                    body[f] = old
            if len(body) > 2:
                batch.append(body)
                if len(batch) >= BATCH:
                    flush()
        flush()

    # -------------------------------------------------------------------- files
    def _file_jobs(self, restored: Set[str]) -> List[Dict[str, Any]]:
        """Files attached to records in ``restored`` (old Ids): the newest version of each
        Salesforce File, plus Attachments."""
        index = self.export_dir / "files" / "index.csv"
        if not index.exists():
            return []
        links: Dict[str, List[str]] = defaultdict(list)
        cdl = self.export_dir / "data" / "ContentDocumentLink.csv"
        if cdl.exists():
            for r in _records(cdl):
                if r.get("LinkedEntityId") in restored:
                    links[r["ContentDocumentId"]].append(r["LinkedEntityId"])
        newest: Dict[str, Tuple[int, Dict[str, str]]] = {}
        jobs: List[Dict[str, Any]] = []
        for r in _records(index):
            if not r.get("status", "").startswith("ok"):
                continue
            if r["kind"] == "ContentVersion" and r["parent_id"] in links:
                m = re.match(r"v(\d+)_", Path(r["path"]).name)
                version = int(m.group(1)) if m else 1
                if r["parent_id"] not in newest or version > newest[r["parent_id"]][0]:
                    newest[r["parent_id"]] = (version, r)
            elif r["kind"] == "Attachment" and r["parent_id"] in restored:
                jobs.append({"kind": "Attachment", "row": r, "parents": [r["parent_id"]]})
        for doc, (_v, r) in newest.items():
            jobs.append({"kind": "ContentVersion", "row": r, "parents": links[doc]})
        return jobs

    def _upload(self, sobject: str, fields: Dict[str, Any], path: Path) -> str:
        part = "VersionData" if sobject == "ContentVersion" else "Body"

        def files() -> Dict[str, Any]:  # re-opened on every attempt
            return {"entity_content": (None, json.dumps(fields), "application/json"),
                    part: (path.name, open(path, "rb"), "application/octet-stream")}  # noqa: SIM115

        resp = self.sf.request("POST", f"sobjects/{sobject}", files=files, retry=False).json()
        return resp["id"]

    def _restore_files(self, result: RestoreResult, out: Path) -> None:
        done_path = out / "files-map.csv"
        done = {r["old_id"]: r["new_id"] for r in _records(done_path)} if done_path.exists() else {}
        jobs = self._file_jobs(set(self.id_map))
        with open(done_path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            if fh.tell() == 0:
                w.writerow(["kind", "old_id", "new_id"])
            for n, job in enumerate(jobs, start=1):
                r = job["row"]
                parents = [self.id_map[p] for p in job["parents"] if p in self.id_map]
                local = self.export_dir / r["path"]
                if r["record_id"] in done:
                    result.files_skipped += 1
                    continue
                if not parents or not local.is_file():
                    result.files_failed += 1
                    self.err_writer.writerow([job["kind"], r["record_id"], "file",
                                              "file missing from the export" if parents else "no parent"])
                    continue
                try:
                    if job["kind"] == "ContentVersion":
                        name = re.sub(r"^v\d+_", "", Path(r["path"]).name)
                        new_id = self._upload("ContentVersion", {
                            "Title": r.get("name") or name, "PathOnClient": name,
                            "FirstPublishLocationId": parents[0]}, local)
                        if len(parents) > 1:
                            doc = next(iter(self.sf.query(
                                f"SELECT ContentDocumentId FROM ContentVersion WHERE Id = '{new_id}'")))
                            for p in parents[1:]:
                                self.sf.request("POST", "sobjects/ContentDocumentLink", retry=False,
                                                json_body={"ContentDocumentId": doc["ContentDocumentId"],
                                                           "LinkedEntityId": p, "ShareType": "V"})
                                result.links_created += 1
                    else:
                        new_id = self._upload("Attachment", {"Name": r.get("name") or local.name,
                                                             "ParentId": parents[0]}, local)
                except SalesforceError as exc:
                    if exc.code == "OUTCOME_UNKNOWN":
                        fh.flush()
                        raise RestoreError(
                            f"The connection failed while uploading {local.name}, so it may or may "
                            f"not exist in the target ({exc}). Check it, then re-run.") from exc
                    result.files_failed += 1
                    self.err_writer.writerow([job["kind"], r["record_id"], "file", str(exc)])
                    continue
                w.writerow([job["kind"], r["record_id"], new_id])
                fh.flush()
                result.files_restored += 1
                self.progress("Files", n, len(jobs))


def _source_org(export_dir: Path) -> str:
    manifest = Path(export_dir) / "manifest.json"
    if not manifest.exists():
        return ""
    try:
        return (json.loads(manifest.read_text("utf-8")).get("org") or {}).get("id") or ""
    except ValueError:
        return ""

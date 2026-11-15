"""``sf-exit restore``: put records from an export back into a Salesforce org.

Typical uses: seed a sandbox with real data, rebuild an org, or recover deleted records.

How it works:

* objects are inserted in dependency order (parents before children);
* every new record's Id is mapped to its old Id (``restore/<org id>/id-map.csv``) and all
  lookups are rewritten through that map, so relationships survive;
* lookups that point "backwards" (self-references such as Account.ParentId, or cycles) are
  filled in by a second pass;
* users and record types are matched to the target org by username/email and developer name;
* it is resumable: records already in the Id map are skipped.

This is the only sf-exit command that writes to Salesforce. It is a dry run unless
``execute=True``, and refuses production orgs unless ``allow_production=True``.
Records only: files and metadata are restored with the standard tools (see the docs).
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from sf_exit.client import SalesforceClient, SalesforceError
from sf_exit.discovery import _matches

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

BATCH = 200  # sObject Collections limit
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
                 exclude: Sequence[str] = ()) -> Tuple[List[ObjectPlan], Dict[str, str]]:
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
                 allow_same_org: bool = False,
                 on_progress: Optional[Callable[[str, int, int], None]] = None):
        self.sf = target
        self.export_dir = Path(export_dir)
        self.include, self.exclude = include, exclude
        self.execute = execute
        self.allow_production, self.allow_same_org = allow_production, allow_same_org
        self.progress = on_progress or (lambda name, done, total: None)
        self.id_map: Dict[str, str] = {}
        self.external: Dict[str, str] = {}  # users, record types: old Id -> target Id

    def run(self) -> RestoreResult:
        if not self.include:
            raise RestoreError("Choose what to restore with --include, e.g. -i Account -i Contact")
        org = check_target(self.sf, self.export_dir, self.allow_production, self.allow_same_org)
        order, not_restorable = plan_restore(self.sf, self.export_dir, self.include, self.exclude)
        result = RestoreResult(order, not_restorable, executed=self.execute)
        if not self.execute or not order:
            return result
        out = self.export_dir / "restore" / (org.get("Id") or "target")
        out.mkdir(parents=True, exist_ok=True)
        result.out_dir = out
        self._load_id_map(out / "id-map.csv")
        self._map_users_and_record_types()
        with open(out / "id-map.csv", "a", newline="", encoding="utf-8") as map_fh, \
                open(out / "errors.csv", "a", newline="", encoding="utf-8") as err_fh:
            self.map_writer = csv.writer(map_fh, lineterminator="\n")
            self.err_writer = csv.writer(err_fh, lineterminator="\n")
            if map_fh.tell() == 0:
                self.map_writer.writerow(["object", "old_id", "new_id"])
            if err_fh.tell() == 0:
                self.err_writer.writerow(["object", "old_id", "step", "error"])
            for plan in order:
                self._insert(plan, result)
                map_fh.flush()
            for plan in order:
                if plan.deferred:
                    self._relink(plan, result)
        return result

    # ------------------------------------------------------------------ mapping
    def _load_id_map(self, path: Path) -> None:
        if path.exists():
            for r in _records(path):
                self.id_map[r["old_id"]] = r["new_id"]

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
                elif f not in plan.deferred:
                    key = f"{plan.name}.{f}"
                    bucket = result.unmatched_users if "User" in plan.refs[f] else result.unmapped_lookups
                    bucket[key] = bucket.get(key, 0) + 1
                continue
            body[f] = self._convert(v, plan.types.get(f, "string"))
        return body

    # ------------------------------------------------------------------- writes
    def _insert(self, plan: ObjectPlan, result: RestoreResult) -> None:
        path = self.export_dir / "data" / f"{plan.name}.csv"
        batch: List[Tuple[str, Dict[str, Any]]] = []
        done = 0
        result.created.setdefault(plan.name, 0)

        def flush() -> None:
            nonlocal done
            if not batch:
                return
            try:
                resp = self.sf.request("POST", "composite/sobjects", retry=False, json_body={
                    "allOrNone": False, "records": [b for _, b in batch]}).json()
            except SalesforceError as exc:
                resp = [{"success": False, "errors": [{"message": str(exc)}]}] * len(batch)
            for (old, _), res in zip(batch, resp, strict=False):
                if res.get("success"):
                    self.id_map[old] = res["id"]
                    self.map_writer.writerow([plan.name, old, res["id"]])
                    result.created[plan.name] += 1
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
            if old in self.id_map:  # restored by an earlier (interrupted) run
                result.skipped_existing[plan.name] = result.skipped_existing.get(plan.name, 0) + 1
                continue
            batch.append((old, self._body(plan, row, result)))
            if len(batch) >= BATCH:
                flush()
        flush()

    def _relink(self, plan: ObjectPlan, result: RestoreResult) -> None:
        """Second pass: fill lookups whose target didn't exist yet during the insert."""
        path = self.export_dir / "data" / f"{plan.name}.csv"
        batch: List[Dict[str, Any]] = []

        def flush() -> None:
            if not batch:
                return
            try:
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
                mapped = self.id_map.get(row.get(f, ""))
                if mapped:
                    body[f] = mapped
            if len(body) > 2:
                batch.append(body)
                if len(batch) >= BATCH:
                    flush()
        flush()

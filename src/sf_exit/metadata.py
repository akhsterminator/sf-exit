"""Export org *configuration* (Apex, Flows, layouts, reports, permission sets...) with the
Metadata API, so nothing an admin or developer built is left behind.

The result is a standard Metadata API folder (``metadata/package.xml`` + one directory per
type) that can be deployed to another org with ``sf project deploy start --metadata-dir``
or converted to source format with ``sf project convert mdapi``.

Things the Metadata API makes subtle, and how they are handled:

* Profiles and permission sets only contain the permissions for components retrieved in
  the *same* request. They are therefore added to every retrieve and their XML is merged.
* A retrieve is limited to 10,000 files and ~39 MB zipped. A batch that is too big is split
  by type, and a single type that is too big is listed and split by component.
* Some types don't support ``*`` (standard picklists, folder-based types), so their members
  are named explicitly.
* The new retrieve replaces the previous ``metadata/`` folder only once it has finished, so
  components deleted from the org don't linger in the backup.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import shutil
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple
from xml.sax.saxutils import escape

from sf_exit.client import TRANSIENT_ERRORS, SalesforceClient, SalesforceError

log = logging.getLogger(__name__)

NS = "http://soap.sforce.com/2006/04/metadata"

# Folder-based types: members must be listed folder by folder (wildcards don't work).
# Lightning email templates live in EmailTemplateFolder, Classic ones in EmailFolder.
FOLDER_TYPES = {
    "Report": ["ReportFolder"],
    "Dashboard": ["DashboardFolder"],
    "Document": ["DocumentFolder"],
    "EmailTemplate": ["EmailFolder", "EmailTemplateFolder"],
}
# Types where "*" misses standard components, so members are listed explicitly too.
LIST_EXPLICITLY = {"CustomObject", "Layout"}
# Their content depends on what else is in the request: merged across every retrieve.
SECURITY_TYPES = ("Profile", "PermissionSet")
SECURITY_DIRS = {"profiles": ".profile", "permissionsets": ".permissionset"}
# StandardValueSet doesn't support "*" and isn't reliably listed: name them all.
STANDARD_VALUE_SETS = [
    "AccountContactMultiRoles", "AccountContactRole", "AccountOwnership", "AccountRating",
    "AccountType", "AssetStatus", "CampaignMemberStatus", "CampaignStatus", "CampaignType",
    "CaseContactRole", "CaseOrigin", "CasePriority", "CaseReason", "CaseStatus", "CaseType",
    "ContactRole", "ContractContactRole", "ContractStatus", "EntitlementType", "EventSubject",
    "EventType", "FiscalYearPeriodName", "FiscalYearPeriodPrefix", "FiscalYearQuarterName",
    "FiscalYearQuarterPrefix", "IdeaCategory", "IdeaMultiCategory", "IdeaStatus",
    "IdeaThemeStatus", "Industry", "LeadSource", "LeadStatus", "OpportunityCompetitor",
    "OpportunityStage", "OpportunityType", "OrderStatus", "OrderType", "PartnerRole",
    "Product2Family", "QuestionOrigin", "QuickTextCategory", "QuickTextChannel", "QuoteStatus",
    "RoleInTerritory2", "SalesTeamRole", "Salutation", "ServiceContractApprovalStatus",
    "SocialPostClassification", "SocialPostEngagementLevel", "SocialPostReviewedStatus",
    "SolutionStatus", "TaskPriority", "TaskStatus", "TaskSubject", "TaskType",
    "WorkOrderLineItemStatus", "WorkOrderPriority", "WorkOrderStatus",
]
# Retrieved elsewhere or not useful in a backup.
SKIP_TYPES = {"Document"}  # Document bodies are already exported as files
MAX_MEMBERS_PER_RETRIEVE = 4000
MAX_TYPES_PER_RETRIEVE = 40
SIZE_ERROR = re.compile(r"limit|exceed|too many|too large|maximum size|file size", re.I)
EXPECTED_WARNING = re.compile(r"No package\.xml found|StandardValueSet.*cannot be found", re.I)


@dataclass
class MetadataResult:
    types: int = 0
    components: int = 0
    files: int = 0
    warnings: List[str] = field(default_factory=list)
    failed_types: Dict[str, str] = field(default_factory=dict)
    kept_previous: List[str] = field(default_factory=list)


def _strip(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _find_all(el: ET.Element, name: str) -> List[ET.Element]:
    return [e for e in el.iter() if _strip(e.tag) == name]


def _text(el: ET.Element, name: str, default: str = "") -> str:
    for child in el:
        if _strip(child.tag) == name:
            return child.text or default
    return default


class MetadataClient:
    """Minimal SOAP client for the read-only Metadata API calls sf-exit needs."""

    def __init__(self, sf: SalesforceClient, poll_interval: float = 2.0, max_wait: float = 3600):
        self.sf = sf
        self.poll_interval = poll_interval
        self.max_wait = max_wait

    @property
    def url(self) -> str:
        return f"{self.sf.instance_url}/services/Soap/m/{self.sf.api_version}"

    def call(self, operation: str, body: str) -> ET.Element:
        template = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            f'xmlns:met="{NS}">'
            "<soapenv:Header><met:SessionHeader><met:sessionId>"
            "{token}</met:sessionId></met:SessionHeader></soapenv:Header>"
            f"<soapenv:Body><met:{operation}>{body}</met:{operation}></soapenv:Body>"
            "</soapenv:Envelope>"
        )
        for attempt in range(5):
            token = self.sf.access_token
            self.sf._count_call()
            try:
                envelope = template.replace("{token}", escape(token))
                resp = self.sf.session.post(
                    self.url, data=envelope.encode("utf-8"), timeout=self.sf.timeout,
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'},
                )
            except TRANSIENT_ERRORS as exc:  # read-only calls: safe to repeat
                if attempt == 4:
                    raise SalesforceError(f"Network error calling Metadata API: {exc}",
                                          code="NETWORK_ERROR") from exc
                time.sleep(2**attempt)
                continue
            if resp.status_code in (429, 502, 503, 504) and attempt < 4:
                time.sleep(2**attempt)
                continue
            try:
                root = ET.fromstring(resp.content)
            except ET.ParseError as exc:
                raise SalesforceError(f"Unexpected Metadata API response ({resp.status_code})",
                                      resp.status_code) from exc
            faults = _find_all(root, "Fault")
            if faults:
                code = _text(faults[0], "faultcode")
                msg = _text(faults[0], "faultstring") or "SOAP fault"
                if "INVALID_SESSION_ID" in code and self.sf.refresh and attempt == 0:
                    self.sf._refresh_token(token)
                    continue
                raise SalesforceError(msg, resp.status_code, code.split(":")[-1])
            results = _find_all(root, f"{operation}Response")
            return results[0] if results else root
        raise SalesforceError("Metadata API kept failing")  # pragma: no cover

    def describe(self) -> List[Dict[str, object]]:
        resp = self.call("describeMetadata", f"<met:asOfVersion>{self.sf.api_version}</met:asOfVersion>")
        out = []
        for mo in _find_all(resp, "metadataObjects"):
            out.append({
                "name": _text(mo, "xmlName"),
                "in_folder": _text(mo, "inFolder") == "true",
                "directory": _text(mo, "directoryName"),
                "children": [c.text for c in mo if _strip(c.tag) == "childXmlNames" and c.text],
            })
        return out

    def list(self, queries: Sequence[tuple]) -> List[str]:
        """listMetadata for up to 3 (type, folder) queries per call; returns fullNames."""
        names: List[str] = []
        for i in range(0, len(queries), 3):
            body = "".join(
                "<met:queries>"
                + (f"<met:folder>{escape(folder)}</met:folder>" if folder else "")
                + f"<met:type>{escape(t)}</met:type></met:queries>"
                for t, folder in queries[i:i + 3]
            ) + f"<met:asOfVersion>{self.sf.api_version}</met:asOfVersion>"
            resp = self.call("listMetadata", body)
            names += [_text(r, "fullName") for r in _find_all(resp, "result") if _text(r, "fullName")]
        return names

    def retrieve(self, members: Dict[str, List[str]]) -> tuple:
        types_xml = "".join(
            "<met:types>" + "".join(f"<met:members>{escape(m)}</met:members>" for m in mems)
            + f"<met:name>{escape(t)}</met:name></met:types>"
            for t, mems in sorted(members.items())
        )
        body = (
            "<met:retrieveRequest>"
            f"<met:apiVersion>{self.sf.api_version}</met:apiVersion>"
            "<met:singlePackage>true</met:singlePackage>"
            f"<met:unpackaged>{types_xml}<met:version>{self.sf.api_version}</met:version></met:unpackaged>"
            "</met:retrieveRequest>"
        )
        job_id = _text(_find_all(self.call("retrieve", body), "result")[0], "id")
        deadline = time.monotonic() + self.max_wait
        delay = min(1.0, self.poll_interval)
        while True:
            res = _find_all(
                self.call("checkRetrieveStatus",
                          f"<met:asyncProcessId>{job_id}</met:asyncProcessId>"
                          "<met:includeZip>true</met:includeZip>"),
                "result",
            )[0]
            if _text(res, "done") == "true":
                status = _text(res, "status")
                if status != "Succeeded":
                    raise SalesforceError(_text(res, "errorMessage") or f"retrieve {status}",
                                          code=_text(res, "errorStatusCode") or "RETRIEVE_FAILED")
                warnings = [
                    f"{_text(m, 'fileName')}: {_text(m, 'problem')}"
                    for m in res if _strip(m.tag) == "messages"
                ]
                zip_b64 = _text(res, "zipFile")
                return base64.b64decode(zip_b64) if zip_b64 else b"", warnings
            if time.monotonic() > deadline:
                raise SalesforceError("Metadata retrieve timed out", code="RETRIEVE_TIMEOUT")
            time.sleep(delay)
            delay = min(delay * 1.5, max(self.poll_interval, 0.5) * 5)


def plan_members(mc: MetadataClient, include: Sequence[str] = (), exclude: Sequence[str] = (),
                 warnings: Optional[List[str]] = None) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """Work out what to retrieve. Returns ({type: [members]}, {type: directoryName})."""
    from sf_exit.discovery import _matches

    warn = warnings if warnings is not None else []
    described = mc.describe()
    members: Dict[str, List[str]] = {}
    directories: Dict[str, str] = {}
    folder_types = []
    for mo in described:
        name = str(mo["name"])
        if not name or name in SKIP_TYPES:
            continue
        if include and not _matches(name, include):
            continue
        if exclude and _matches(name, exclude):
            continue
        directories[name] = str(mo.get("directory") or "")
        if name in FOLDER_TYPES or mo["in_folder"]:
            folder_types.append(name)
        elif name == "StandardValueSet":
            members[name] = list(STANDARD_VALUE_SETS)
        else:
            members[name] = ["*"]

    for t in sorted(LIST_EXPLICITLY & set(members)):
        try:
            members[t] = sorted({"*", *mc.list([(t, None)])})
        except SalesforceError as exc:  # keep "*" (custom components only) and say so
            warn.append(f"Could not list {t} ({exc}): only custom {t} components were retrieved.")

    for t in folder_types:
        listed: Set[str] = set()
        folders: List[str] = []
        errors = []
        for folder_type in FOLDER_TYPES.get(t, [f"{t}Folder"]):
            try:
                folders += mc.list([(folder_type, None)])
            except SalesforceError as exc:
                errors.append(f"{folder_type}: {exc}")
        try:
            items = mc.list([(t, f) for f in sorted(set(folders))]) if folders else []
            if t in ("Report", "EmailTemplate"):  # unfiled public items live in a pseudo-folder
                items += mc.list([(t, "unfiled$public")])
            listed.update(folders, items)
        except SalesforceError as exc:
            errors.append(f"{t}: {exc}")
        if errors and not listed:
            warn.append(f"Could not list {t} folders: {'; '.join(errors)}")
        if listed:
            members[t] = sorted(listed)
    return members, directories


def _batches(members: Dict[str, List[str]]) -> List[Dict[str, List[str]]]:
    batches: List[Dict[str, List[str]]] = []
    current: Dict[str, List[str]] = {}
    size = 0
    for t, mems in sorted(members.items()):
        for i in range(0, len(mems), MAX_MEMBERS_PER_RETRIEVE):
            chunk = mems[i:i + MAX_MEMBERS_PER_RETRIEVE]
            if current and (size + len(chunk) > MAX_MEMBERS_PER_RETRIEVE
                            or len(current) >= MAX_TYPES_PER_RETRIEVE):
                batches.append(current)
                current, size = {}, 0
            current.setdefault(t, []).extend(chunk)
            size += len(chunk)
    if current:
        batches.append(current)
    return batches


def _clean(el: ET.Element) -> ET.Element:
    """Drop formatting whitespace so identical sections compare equal."""
    for e in el.iter():
        e.tail = None
        if e.text is not None and not e.text.strip():
            e.text = None
    return el


def merge_permission_xml(docs: List[bytes]) -> bytes:
    """Union of the sections of several retrieved copies of one Profile/PermissionSet.

    Each retrieve only includes permissions for the components in that request, so the
    complete profile is the union across retrieves. Sections are written in alphabetical
    order of their element name, which is the order the Metadata API itself uses."""
    ET.register_namespace("", NS)
    root: Optional[ET.Element] = None
    sections: Dict[str, Dict[bytes, ET.Element]] = {}
    for data in docs:
        parsed = ET.fromstring(data)
        if root is None:
            root = ET.Element(parsed.tag)
        for child in parsed:
            key = ET.tostring(_clean(child))
            sections.setdefault(child.tag, {}).setdefault(key, child)
    assert root is not None
    for tag in sorted(sections, key=_strip):
        root.extend(sections[tag].values())
    ET.indent(root, space="    ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8",
                                                                       xml_declaration=False) + b"\n"


def _safe_target(base: Path, name: str) -> Optional[Path]:
    rel = PurePosixPath(name)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        return None
    target = (base / rel).resolve()
    return target if target.is_relative_to(base) else None


def export_metadata(sf: SalesforceClient, out_dir: Path, include: Sequence[str] = (),
                    exclude: Sequence[str] = (), poll_interval: float = 2.0,
                    on_progress: Optional[Callable[[str], None]] = None) -> MetadataResult:
    out_dir = Path(out_dir)
    final = out_dir / "metadata"
    work = out_dir / ".sf-exit" / "metadata-new"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    base = work.resolve()
    mc = MetadataClient(sf, poll_interval=poll_interval)
    say = on_progress or (lambda s: None)
    result = MetadataResult()
    members, directories = plan_members(mc, include, exclude, result.warnings)
    security = {t: ["*"] for t in SECURITY_TYPES if t in members}
    for t in security:
        del members[t]
    result.types = len(members) + len(security)
    retrieved: Dict[str, Set[str]] = {}
    permission_docs: Dict[str, List[bytes]] = {}

    def extract(data: bytes, keep_security: bool) -> None:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.split("/", 1)[1] if info.filename.startswith("unpackaged/") \
                    else info.filename
                if name == "package.xml":
                    continue
                top = name.split("/", 1)[0]
                if top in SECURITY_DIRS:
                    if keep_security:
                        permission_docs.setdefault(name, []).append(zf.read(info))
                    continue
                target = _safe_target(base, name)
                if target is None:
                    result.warnings.append(f"Ignored unsafe path in retrieve: {name}")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
                result.files += 1

    def fetch(batch: Dict[str, List[str]], with_security: bool) -> Optional[bytes]:
        data, warnings = mc.retrieve({**batch, **(security if with_security else {})})
        result.warnings += [w for w in warnings if not EXPECTED_WARNING.search(w)]
        return data

    # Probe the security types on their own first: if they can't be retrieved, they must not
    # be added to every other batch (one broken type would sink the whole backup).
    if security:
        say("Retrieving profiles and permission sets")
        try:
            data = fetch({}, True)
            if data:
                extract(data, True)
            for t in security:
                retrieved.setdefault(t, set()).add("*")
        except SalesforceError as exc:
            for t in security:
                result.failed_types[t] = str(exc)
            security = {}

    def run(batch: Dict[str, List[str]]) -> None:
        try:
            data = fetch(batch, bool(security))
        except SalesforceError as exc:
            if len(batch) > 1:  # isolate the type that breaks the retrieve
                keys = sorted(batch)
                half = len(keys) // 2
                run({k: batch[k] for k in keys[:half]})
                run({k: batch[k] for k in keys[half:]})
                return
            (t, mems), = batch.items()
            if SIZE_ERROR.search(f"{exc.code} {exc.message}"):
                if "*" in mems:  # too big as a wildcard: name every component instead
                    try:
                        mems = sorted(set(mems) - {"*"} | set(mc.list([(t, None)])))
                    except SalesforceError:
                        mems = [m for m in mems if m != "*"]
                if len(mems) > 1:
                    half = len(mems) // 2
                    run({t: mems[:half]})
                    run({t: mems[half:]})
                    return
            result.failed_types[t] = str(exc)
            return
        if data:
            extract(data, bool(security))
        for t, mems in batch.items():
            retrieved.setdefault(t, set()).update(mems)

    batches = _batches(members)
    for i, batch in enumerate(batches, start=1):
        say(f"Retrieving metadata batch {i}/{len(batches)} ({', '.join(list(batch)[:4])}"
            f"{'…' if len(batch) > 4 else ''})")
        run(batch)

    for name, docs in permission_docs.items():
        target = _safe_target(base, name)
        if target is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_bytes(merge_permission_xml(docs))
        except ET.ParseError:
            target.write_bytes(docs[0])
        result.files += 1
    if security and not members:
        result.warnings.append("Profiles and permission sets were retrieved on their own, so they "
                               "only contain user permissions. Retrieve all types for complete ones.")

    # Types that failed this time keep their files from the previous backup, so a transient
    # failure never erases part of the configuration.
    if final.is_dir():
        old_pkg = _read_package_xml(final / "package.xml")
        for t in sorted(result.failed_types):
            d = directories.get(t)
            if d and (final / d).is_dir() and not (work / d).exists():
                shutil.copytree(final / d, work / d)
                retrieved.setdefault(t, set()).update(old_pkg.get(t, {"*"}))
                result.kept_previous.append(t)

    result.components = sum(len([m for m in v if m != "*"]) for v in retrieved.values())
    _write_package_xml(work / "package.xml", retrieved, sf.api_version)
    if result.warnings or result.failed_types:
        lines = [f"FAILED {t}: {e}" + (" (previous version kept)" if t in result.kept_previous else "")
                 for t, e in sorted(result.failed_types.items())]
        lines += result.warnings
        (work / "retrieve-warnings.txt").write_text("\n".join(lines) + "\n", "utf-8")

    old = out_dir / ".sf-exit" / "metadata-old"
    shutil.rmtree(old, ignore_errors=True)
    if final.exists():
        final.replace(old)
    work.replace(final)
    shutil.rmtree(old, ignore_errors=True)
    return result


def _read_package_xml(path: Path) -> Dict[str, Set[str]]:
    if not path.exists():
        return {}
    try:
        root = ET.fromstring(path.read_bytes())
    except ET.ParseError:
        return {}
    out: Dict[str, Set[str]] = {}
    for t in _find_all(root, "types"):
        out[_text(t, "name")] = {m.text or "" for m in t if _strip(m.tag) == "members"}
    return out


def _write_package_xml(path: Path, members: Dict[str, Set[str]], version: str) -> None:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', f'<Package xmlns="{NS}">']
    for t in sorted(members):
        parts.append("    <types>")
        for m in sorted(members[t]):
            parts.append(f"        <members>{escape(m)}</members>")
        parts.append(f"        <name>{escape(t)}</name>")
        parts.append("    </types>")
    parts += [f"    <version>{version}</version>", "</Package>", ""]
    path.write_text("\n".join(parts), "utf-8")

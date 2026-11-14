"""Export org *configuration* (Apex, Flows, layouts, reports, permission sets...) with the
Metadata API, so nothing an admin or developer built is left behind.

The result is a standard Metadata API folder (``metadata/package.xml`` + one directory per
type) that can be deployed to another org with ``sf project deploy start --metadata-dir``
or converted to source format with ``sf project convert mdapi``.
"""

from __future__ import annotations

import base64
import io
import logging
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence
from xml.sax.saxutils import escape

import requests

from sf_exit.client import SalesforceClient, SalesforceError

log = logging.getLogger(__name__)

NS = "http://soap.sforce.com/2006/04/metadata"

# Folder-based types: members must be listed folder by folder (wildcards don't work).
FOLDER_TYPES = {
    "Report": "ReportFolder",
    "Dashboard": "DashboardFolder",
    "Document": "DocumentFolder",
    "EmailTemplate": "EmailFolder",
}
# Types where "*" misses standard components, so members are listed explicitly too.
LIST_EXPLICITLY = {"CustomObject", "StandardValueSet", "Layout", "Profile"}
# Retrieved elsewhere or not useful in a backup.
SKIP_TYPES = {"Document"}  # Document bodies are already exported as files
MAX_MEMBERS_PER_RETRIEVE = 4000
MAX_TYPES_PER_RETRIEVE = 40


@dataclass
class MetadataResult:
    types: int = 0
    components: int = 0
    files: int = 0
    warnings: List[str] = field(default_factory=list)
    failed_types: Dict[str, str] = field(default_factory=dict)


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
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            f'xmlns:met="{NS}">'
            "<soapenv:Header><met:SessionHeader><met:sessionId>"
            f"{escape(self.sf.access_token)}</met:sessionId></met:SessionHeader></soapenv:Header>"
            f"<soapenv:Body><met:{operation}>{body}</met:{operation}></soapenv:Body>"
            "</soapenv:Envelope>"
        )
        for attempt in range(5):
            self.sf._count_call()
            try:
                resp = self.sf.session.post(
                    self.url, data=envelope.encode("utf-8"), timeout=self.sf.timeout,
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'},
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == 4:
                    raise SalesforceError(f"Network error calling Metadata API: {exc}") from exc
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
                    self.sf.access_token = self.sf.refresh()
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


def plan_members(mc: MetadataClient, include: Sequence[str] = (),
                 exclude: Sequence[str] = ()) -> Dict[str, List[str]]:
    """Work out what to retrieve: {type: [members]}."""
    from sf_exit.discovery import _matches

    described = mc.describe()
    members: Dict[str, List[str]] = {}
    folder_queries = []
    explicit = []
    for mo in described:
        name = str(mo["name"])
        if not name or name in SKIP_TYPES:
            continue
        if include and not _matches(name, include):
            continue
        if exclude and _matches(name, exclude):
            continue
        if name in FOLDER_TYPES or mo["in_folder"]:
            folder_queries.append(name)
        else:
            members[name] = ["*"]
            if name in LIST_EXPLICITLY:
                explicit.append(name)

    if explicit:
        try:
            listed = {t: [] for t in explicit}
            for t in explicit:
                listed[t] = mc.list([(t, None)])
            for t, names in listed.items():
                members[t] = sorted(set(["*"] + names))
        except SalesforceError as exc:
            log.info("listMetadata failed (%s); using wildcards only", exc)

    for t in folder_queries:
        folder_type = FOLDER_TYPES.get(t, f"{t}Folder")
        try:
            folders = mc.list([(folder_type, None)])
            items = mc.list([(t, f) for f in folders]) if folders else []
            # unfiled public items live in a pseudo-folder
            if t in ("Report", "EmailTemplate"):
                items += mc.list([(t, "unfiled$public")])
            if folders or items:
                members[t] = sorted(set(folders + items))
        except SalesforceError as exc:
            log.info("Could not list %s: %s", t, exc)
    return members


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


def export_metadata(sf: SalesforceClient, out_dir: Path, include: Sequence[str] = (),
                    exclude: Sequence[str] = (), poll_interval: float = 2.0,
                    on_progress: Optional[Callable[[str], None]] = None) -> MetadataResult:
    out = Path(out_dir) / "metadata"
    out.mkdir(parents=True, exist_ok=True)
    mc = MetadataClient(sf, poll_interval=poll_interval)
    say = on_progress or (lambda s: None)
    members = plan_members(mc, include, exclude)
    result = MetadataResult(types=len(members))
    retrieved: Dict[str, set] = {}

    def run(batch: Dict[str, List[str]]) -> None:
        try:
            data, warnings = mc.retrieve(batch)
        except SalesforceError as exc:
            if len(batch) > 1:  # isolate the type that breaks the retrieve
                keys = sorted(batch)
                half = len(keys) // 2
                run({k: batch[k] for k in keys[:half]})
                run({k: batch[k] for k in keys[half:]})
                return
            (t,) = batch
            result.failed_types[t] = str(exc)
            return
        result.warnings += [w for w in warnings if "No package.xml found" not in w]
        if data:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename.split("/", 1)[1] if info.filename.startswith("unpackaged/") \
                        else info.filename
                    if name == "package.xml":
                        continue
                    target = (out / name).resolve()
                    if not str(target).startswith(str(out.resolve())):
                        continue  # never write outside the export folder
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(info))
                    result.files += 1
        for t, mems in batch.items():
            retrieved.setdefault(t, set()).update(mems)

    batches = _batches(members)
    for i, batch in enumerate(batches, start=1):
        say(f"Retrieving metadata batch {i}/{len(batches)} ({', '.join(list(batch)[:4])}"
            f"{'…' if len(batch) > 4 else ''})")
        run(batch)

    result.components = sum(len([m for m in v if m != "*"]) for v in retrieved.values())
    _write_package_xml(out / "package.xml", retrieved, sf.api_version)
    if result.warnings or result.failed_types:
        lines = [f"FAILED {t}: {e}" for t, e in sorted(result.failed_types.items())]
        lines += result.warnings
        (out / "retrieve-warnings.txt").write_text("\n".join(lines) + "\n", "utf-8")
    return result


def _write_package_xml(path: Path, members: Dict[str, set], version: str) -> None:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', f'<Package xmlns="{NS}">']
    for t in sorted(members):
        parts.append("    <types>")
        for m in sorted(members[t]):
            parts.append(f"        <members>{escape(m)}</members>")
        parts.append(f"        <name>{escape(t)}</name>")
        parts.append("    </types>")
    parts += [f"    <version>{version}</version>", "</Package>", ""]
    path.write_text("\n".join(parts), "utf-8")

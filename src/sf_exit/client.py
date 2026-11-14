"""A small, dependency-light Salesforce REST + Bulk API 2.0 client.

Everything here is read-only: sf-exit never creates, updates or deletes records
in your org. The only POST requests it makes are to create Bulk API *query* jobs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import requests

from sf_exit import __version__

log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
USER_AGENT = f"sf-exit/{__version__} (+https://github.com/akhsterminator/sf-exit)"
FALLBACK_API_VERSION = "62.0"


class SalesforceError(Exception):
    """An error returned by the Salesforce API."""

    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        prefix = f"[{self.code}] " if self.code else ""
        return f"{prefix}{self.message}"


@dataclass
class OrgInfo:
    id: str
    name: str
    edition: str
    instance: str
    is_sandbox: bool
    username: Optional[str] = None


def _parse_error(resp: requests.Response) -> SalesforceError:
    try:
        body = resp.json()
    except ValueError:
        return SalesforceError(resp.text[:500] or resp.reason, resp.status_code)
    if isinstance(body, list) and body:
        body = body[0]
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error_description") or json.dumps(body)
        code = body.get("errorCode") or body.get("error")
        return SalesforceError(msg, resp.status_code, code)
    return SalesforceError(str(body), resp.status_code)


class SalesforceClient:
    def __init__(
        self,
        instance_url: str,
        access_token: str,
        api_version: Optional[str] = None,
        session: Optional[requests.Session] = None,
        max_retries: int = 6,
        timeout: float = 300.0,
        refresh: Optional[Callable[[], str]] = None,
    ) -> None:
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.timeout = timeout
        self.refresh = refresh
        self.api_calls = 0
        self._api_version = api_version

    # ------------------------------------------------------------------ basics
    @property
    def api_version(self) -> str:
        if not self._api_version:
            self._api_version = self.latest_api_version()
        return self._api_version

    @property
    def base(self) -> str:
        return f"{self.instance_url}/services/data/v{self.api_version}"

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if path.startswith("/services/"):
            return self.instance_url + path
        return f"{self.base}/{path.lstrip('/')}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        stream: bool = False,
    ) -> requests.Response:
        url = self._url(path)
        refreshed = False
        attempt = 0
        while True:
            hdrs = {
                "Authorization": f"Bearer {self.access_token}",
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "gzip",
            }
            if headers:
                hdrs.update(headers)
            try:
                self.api_calls += 1
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=hdrs,
                    stream=stream,
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self.max_retries:
                    raise SalesforceError(f"Network error talking to Salesforce: {exc}") from exc
                self._sleep(attempt)
                attempt += 1
                continue

            if resp.status_code == 401 and self.refresh and not refreshed:
                log.info("Access token expired, refreshing")
                self.access_token = self.refresh()
                refreshed = True
                continue

            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                self._sleep(attempt, retry_after)
                attempt += 1
                continue

            if resp.status_code >= 400:
                err = _parse_error(resp)
                resp.close()
                raise err
            return resp

    @staticmethod
    def _sleep(attempt: int, retry_after: Optional[str] = None) -> None:
        if retry_after and retry_after.isdigit():
            delay = float(retry_after)
        else:
            delay = min(60.0, (2**attempt) * 0.5) + random.uniform(0, 0.5)
        time.sleep(delay)

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", path, params=params).json()

    # ---------------------------------------------------------------- metadata
    def latest_api_version(self) -> str:
        try:
            versions = self.request("GET", f"{self.instance_url}/services/data/").json()
            return max((v["version"] for v in versions), key=lambda v: float(v))
        except (SalesforceError, ValueError, KeyError):
            return FALLBACK_API_VERSION

    def org_info(self) -> OrgInfo:
        rows = list(
            self.query(
                "SELECT Id, Name, OrganizationType, InstanceName, IsSandbox FROM Organization"
            )
        )
        r = rows[0] if rows else {}
        username = None
        try:
            ident = self.get_json(f"{self.instance_url}/services/oauth2/userinfo")
            username = ident.get("preferred_username") or ident.get("email")
        except SalesforceError:
            pass
        return OrgInfo(
            id=r.get("Id", ""),
            name=r.get("Name", ""),
            edition=r.get("OrganizationType", ""),
            instance=r.get("InstanceName", ""),
            is_sandbox=bool(r.get("IsSandbox")),
            username=username,
        )

    def limits(self) -> Dict[str, Any]:
        return self.get_json("limits")

    def describe_global(self) -> List[Dict[str, Any]]:
        return self.get_json("sobjects")["sobjects"]

    def describe(self, sobject: str) -> Dict[str, Any]:
        return self.get_json(f"sobjects/{sobject}/describe")

    def record_counts(self, names: List[str]) -> Dict[str, int]:
        """Approximate record counts (from Salesforce's storage stats) in very few API calls."""
        counts: Dict[str, int] = {}
        chunk: List[str] = []
        length = 0
        for name in names:
            if chunk and length + len(name) > 1500:
                counts.update(self._record_counts_chunk(chunk))
                chunk, length = [], 0
            chunk.append(name)
            length += len(name) + 1
        if chunk:
            counts.update(self._record_counts_chunk(chunk))
        return counts

    def _record_counts_chunk(self, names: List[str]) -> Dict[str, int]:
        try:
            body = self.get_json("limits/recordCount", params={"sObjects": ",".join(names)})
        except SalesforceError:
            return {}
        return {s["name"]: int(s["count"]) for s in body.get("sObjects", [])}

    # ------------------------------------------------------------------- query
    def query(self, soql: str, include_deleted: bool = False) -> Iterator[Dict[str, Any]]:
        endpoint = "queryAll" if include_deleted else "query"
        body = self.get_json(endpoint, params={"q": soql})
        while True:
            for rec in body.get("records", []):
                rec.pop("attributes", None)
                yield rec
            next_url = body.get("nextRecordsUrl")
            if not next_url or body.get("done", True):
                break
            body = self.get_json(next_url)

    def count(self, sobject: str, include_deleted: bool = False, where: str = "") -> int:
        endpoint = "queryAll" if include_deleted else "query"
        soql = f"SELECT COUNT() FROM {sobject}" + (f" WHERE {where}" if where else "")
        return int(self.get_json(endpoint, params={"q": soql})["totalSize"])

    # ---------------------------------------------------------------- bulk 2.0
    def bulk_query_to_file(
        self,
        soql: str,
        dest: Path,
        include_deleted: bool = False,
        poll_interval: float = 2.0,
        max_records: Optional[int] = None,
        on_rows: Optional[Callable[[int], None]] = None,
    ) -> int:
        """Run a Bulk API 2.0 query job and stream the CSV result into ``dest``.

        Returns the number of data rows written.
        """
        job = self.request(
            "POST",
            "jobs/query",
            json_body={
                "operation": "queryAll" if include_deleted else "query",
                "query": soql,
                "contentType": "CSV",
                "columnDelimiter": "COMMA",
                "lineEnding": "LF",
            },
        ).json()
        job_id = job["id"]

        delay = min(0.5, poll_interval)
        while True:
            info = self.get_json(f"jobs/query/{job_id}")
            state = info.get("state")
            if state == "JobComplete":
                break
            if state in ("Failed", "Aborted"):
                raise SalesforceError(
                    info.get("errorMessage") or f"Bulk job {job_id} {state.lower()}",
                    code="BULK_JOB_" + state.upper(),
                )
            time.sleep(delay)
            delay = min(delay * 1.5, max(poll_interval, 0.5) * 5)

        rows = 0
        locator: Optional[str] = None
        first = True
        with open(dest, "wb") as fh:
            while True:
                params: Dict[str, Any] = {}
                if locator:
                    params["locator"] = locator
                if max_records:
                    params["maxRecords"] = max_records
                resp = self.request(
                    "GET", f"jobs/query/{job_id}/results", params=params, stream=True
                )
                skip_header = not first
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if not chunk:
                        continue
                    if skip_header:
                        nl = chunk.find(b"\n")
                        if nl == -1:
                            continue
                        chunk = chunk[nl + 1 :]
                        skip_header = False
                    fh.write(chunk)
                page_rows = int(resp.headers.get("Sforce-NumberOfRecords", "0") or 0)
                rows += page_rows
                if on_rows:
                    on_rows(page_rows)
                first = False
                locator = resp.headers.get("Sforce-Locator")
                resp.close()
                if not locator or locator == "null":
                    break
        return rows

    # --------------------------------------------------------------- binaries
    def download(self, path: str, dest: Path) -> Tuple[int, str, str]:
        """Stream a blob (file body) to disk. Returns (bytes, sha256, md5)."""
        resp = self.request("GET", path, stream=True)
        sha = hashlib.sha256()
        md5 = hashlib.md5()  # noqa: S324 - matches Salesforce's ContentVersion.Checksum
        size = 0
        tmp = dest.with_name(dest.name + ".part")
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    fh.write(chunk)
                    sha.update(chunk)
                    md5.update(chunk)
                    size += len(chunk)
        resp.close()
        tmp.replace(dest)
        return size, sha.hexdigest(), md5.hexdigest()


def soql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

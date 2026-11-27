"""A small, dependency-light Salesforce REST + Bulk API 2.0 client.

Reads are retried with backoff, including connections that drop while a response body is
being streamed. Writes (only ever made by ``sf-exit restore``) are never replayed blindly:
a write whose outcome is unknown raises ``SalesforceError`` with code ``OUTCOME_UNKNOWN``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter

from sf_exit import __version__

log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
USER_AGENT = f"sf-exit/{__version__} (+https://github.com/akhsterminator/sf-exit)"
FALLBACK_API_VERSION = "62.0"
# /composite counts as ONE API call. It takes up to 25 subrequests, of which at most 5 may
# be queries. (/composite/batch is not used: each of its subrequests is billed separately.)
COMPOSITE_MAX = 25
COMPOSITE_MAX_QUERIES = 5
COMPOSITE_BATCH_SIZE = COMPOSITE_MAX  # kept for backwards compatibility
TRANSIENT_ERRORS = (
    requests.ConnectionError,  # includes SSLError, ConnectTimeout and read timeouts mid-body
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,  # connection dropped while reading the body
    requests.exceptions.ContentDecodingError,  # truncated gzip stream
)


class SalesforceError(Exception):
    """An error returned by the Salesforce API (or a network failure talking to it)."""

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
    return _error_from_body(body, resp.status_code)


def _error_from_body(body: Any, status: Optional[int]) -> SalesforceError:
    if isinstance(body, list) and body:
        body = body[0]
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error_description") or json.dumps(body, default=str)
        code = body.get("errorCode") or body.get("error")
        return SalesforceError(msg, status, code)
    return SalesforceError(str(body), status)


def _never_sent(exc: Exception) -> bool:
    """True when the request certainly never reached Salesforce (safe to retry a write)."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    if isinstance(exc, requests.ConnectionError) and exc.args:
        reason = getattr(exc.args[0], "reason", exc.args[0])
        return type(reason).__name__ in ("NewConnectionError", "NameResolutionError",
                                         "ConnectTimeoutError")
    return False


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
        if session is None:
            session = requests.Session()
            # Enough pooled connections for parallel object exports + file downloads.
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=64)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
        self.session = session
        self.max_retries = max_retries
        self.timeout = timeout
        self.refresh = refresh
        self._api_version = api_version
        self._calls = 0
        self._count_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self.composite_supported = True
        self.record_counts_available: Optional[bool] = None  # None until record_counts() runs

    @property
    def api_calls(self) -> int:
        return self._calls

    def _count_call(self) -> None:
        with self._count_lock:
            self._calls += 1

    def _refresh_token(self, used: str) -> None:
        """Refresh once per expiry: threads that saw the same expired token wait for one refresh."""
        with self._refresh_lock:
            if self.access_token == used and self.refresh:
                log.info("Access token expired, refreshing")
                self.access_token = self.refresh()

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
        retry: bool = True,
        files: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> requests.Response:
        """Send a request. Reads (``retry=True``) are retried on network errors and 429/5xx.
        Writes (``retry=False``) are only retried when the request never left this machine;
        otherwise a network failure raises ``SalesforceError(code="OUTCOME_UNKNOWN")``."""
        url = self._url(path)
        refreshed = False
        attempt = 0
        while True:
            token = self.access_token
            hdrs = {
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "gzip",
            }
            if headers:
                hdrs.update(headers)
            parts = files() if files else None  # a multipart upload is rebuilt on every attempt
            try:
                self._count_call()
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    files=parts,
                    headers=hdrs,
                    stream=stream,
                    timeout=self.timeout,
                )
            except TRANSIENT_ERRORS as exc:
                if not retry and not _never_sent(exc):
                    raise SalesforceError(
                        f"The connection failed after the request was sent, so Salesforce may or "
                        f"may not have processed it: {exc}", code="OUTCOME_UNKNOWN") from exc
                if attempt >= self.max_retries:
                    raise SalesforceError(f"Network error talking to Salesforce: {exc}",
                                          code="NETWORK_ERROR") from exc
                self._sleep(attempt)
                attempt += 1
                continue
            finally:
                for part in (parts or {}).values():
                    handle = part[1] if isinstance(part, tuple) else None
                    if hasattr(handle, "close"):
                        handle.close()

            if resp.status_code == 401 and self.refresh and not refreshed:
                resp.close()
                self._refresh_token(token)
                refreshed = True
                continue

            if retry and resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                resp.close()
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
            delay = min(float(retry_after), 300.0)
        else:
            delay = min(60.0, (2**attempt) * 0.5) + random.uniform(0, 0.5)
        time.sleep(delay)

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None,
                 decimals: bool = False) -> Any:
        """GET and decode JSON. ``decimals=True`` keeps numbers exact (Decimal, not float)."""
        return self.request("GET", path, params=params).json(
            **({"parse_float": Decimal} if decimals else {}))

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

    def composite_get(self, paths: List[str], decimals: bool = False) -> List[Tuple[bool, Any]]:
        """GET up to 25 resources (at most 5 of them queries) in ONE API call via /composite.

        Returns one (ok, body_or_error) per path, in order. If the org rejects composite
        requests, falls back to individual GETs.
        """
        if not paths:
            return []
        queries = sum(1 for p in paths if p.lstrip("/").startswith(("query", "queryAll")))
        if len(paths) > COMPOSITE_MAX or queries > COMPOSITE_MAX_QUERIES:
            raise ValueError("composite_get takes at most 25 paths, of which at most 5 queries")
        if self.composite_supported:
            try:
                body = self.request(
                    "POST",
                    "composite",
                    json_body={
                        "allOrNone": False,
                        "compositeRequest": [
                            {"method": "GET", "referenceId": f"r{i}",
                             "url": f"/services/data/v{self.api_version}/{p.lstrip('/')}"}
                            for i, p in enumerate(paths)
                        ],
                    },
                ).json(**({"parse_float": Decimal} if decimals else {}))
                by_ref = {r.get("referenceId"): r for r in body.get("compositeResponse", [])}
                out: List[Tuple[bool, Any]] = []
                for i in range(len(paths)):
                    res = by_ref.get(f"r{i}")
                    if res is None:
                        break
                    status = int(res.get("httpStatusCode", 500))
                    if status < 400:
                        out.append((True, res.get("body")))
                    else:
                        out.append((False, _error_from_body(res.get("body"), status)))
                if len(out) == len(paths):
                    return out
            except SalesforceError as exc:
                if exc.status in (400, 404, 405, 501):
                    log.info("/composite unavailable (%s), using single requests", exc)
                    self.composite_supported = False
                else:  # transient: fall back for this call only
                    log.info("/composite failed (%s), retrying as single requests", exc)
        single: List[Tuple[bool, Any]] = []
        for p in paths:
            try:
                single.append((True, self.get_json(p, decimals=decimals)))
            except SalesforceError as exc:
                single.append((False, exc))
        return single

    def describe_many(self, names: List[str]) -> Dict[str, Any]:
        """Describe many objects, 25 per API call. Values are dicts or SalesforceError."""
        out: Dict[str, Any] = {}
        for i in range(0, len(names), COMPOSITE_MAX):
            chunk = names[i:i + COMPOSITE_MAX]
            for name, (_ok, body) in zip(
                chunk, self.composite_get([f"sobjects/{n}/describe" for n in chunk]), strict=True
            ):
                out[name] = body
        return out

    def record_counts(self, names: List[str]) -> Dict[str, int]:
        """Approximate record counts (Salesforce's cached storage stats) in very few calls.

        Needs the "View Setup and Configuration" permission. When unavailable this returns {}
        and sets ``record_counts_available = False``; callers must not treat missing as zero.
        """
        counts: Dict[str, int] = {}
        chunk: List[str] = []
        length = 0
        self.record_counts_available = True
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
        except SalesforceError as exc:
            if self.record_counts_available:
                log.warning("Record counts unavailable (%s). Grant 'View Setup and "
                            "Configuration' for better engine choices and plan estimates.", exc)
            self.record_counts_available = False
            return {}
        return {s["name"]: int(s["count"]) for s in body.get("sObjects", [])}

    # ------------------------------------------------------------------- query
    def query(self, soql: str, include_deleted: bool = False,
              first_page: Optional[Dict[str, Any]] = None) -> Iterator[Dict[str, Any]]:
        """Yield records, following pagination. Numbers are exact ``Decimal`` values.
        ``first_page`` lets a caller that already fetched page one continue from there."""
        if first_page is not None:
            body = first_page
        else:
            endpoint = "queryAll" if include_deleted else "query"
            body = self.get_json(endpoint, params={"q": soql}, decimals=True)
        while True:
            for rec in body.get("records", []):
                rec.pop("attributes", None)
                yield rec
            next_url = body.get("nextRecordsUrl")
            if not next_url or body.get("done", True):
                break
            body = self.get_json(next_url, decimals=True)

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
        max_wait: float = 6 * 3600,
    ) -> int:
        """Run a Bulk API 2.0 query job and stream the CSV result into ``dest``.

        Each result page is re-requested (same locator) if the connection drops mid-page,
        so one network blip doesn't throw away a multi-hour job. Returns the data row count.
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
        deadline = time.monotonic() + max_wait
        while True:
            if time.monotonic() > deadline:
                with contextlib.suppress(SalesforceError):
                    self.request("PATCH", f"jobs/query/{job_id}", json_body={"state": "Aborted"})
                raise SalesforceError(
                    f"Bulk job {job_id} did not finish within {max_wait / 3600:.1f}h; aborted",
                    code="BULK_JOB_TIMEOUT",
                )
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
                start = fh.tell()
                attempt = 0
                while True:
                    try:
                        resp = self.request(
                            "GET", f"jobs/query/{job_id}/results", params=params, stream=True
                        )
                        try:
                            skip_header = not first
                            for chunk in resp.iter_content(chunk_size=1 << 16):
                                if not chunk:
                                    continue
                                if skip_header:
                                    nl = chunk.find(b"\n")
                                    if nl == -1:
                                        continue
                                    chunk = chunk[nl + 1:]
                                    skip_header = False
                                fh.write(chunk)
                        finally:
                            resp.close()
                        break
                    except TRANSIENT_ERRORS as exc:  # dropped mid-page: rewind and re-fetch
                        fh.seek(start)
                        fh.truncate()
                        if attempt >= self.max_retries:
                            raise SalesforceError(
                                f"Bulk results page kept failing: {exc}", code="NETWORK_ERROR") from exc
                        self._sleep(attempt)
                        attempt += 1
                page_rows = int(resp.headers.get("Sforce-NumberOfRecords", "0") or 0)
                rows += page_rows
                if on_rows:
                    on_rows(page_rows)
                first = False
                locator = resp.headers.get("Sforce-Locator")
                if not locator or locator == "null":
                    break
        return rows

    # --------------------------------------------------------------- binaries
    def download(self, path: str, dest: Path) -> Tuple[int, str, str]:
        """Stream a blob (file body) to disk, retrying from scratch if the connection drops.
        Returns (bytes, sha256, md5)."""
        tmp = dest.with_name(dest.name + ".part")
        attempt = 0
        while True:
            sha = hashlib.sha256()
            md5 = hashlib.md5()  # noqa: S324 - matches Salesforce's ContentVersion.Checksum
            size = 0
            resp = self.request("GET", path, stream=True)
            try:
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        if chunk:
                            fh.write(chunk)
                            sha.update(chunk)
                            md5.update(chunk)
                            size += len(chunk)
                break
            except TRANSIENT_ERRORS as exc:
                if attempt >= self.max_retries:
                    tmp.unlink(missing_ok=True)
                    raise SalesforceError(f"Download kept failing: {exc}",
                                          code="NETWORK_ERROR") from exc
                self._sleep(attempt)
                attempt += 1
            finally:
                resp.close()
        tmp.replace(dest)
        return size, sha.hexdigest(), md5.hexdigest()


def soql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

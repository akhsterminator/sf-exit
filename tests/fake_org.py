"""An in-process fake Salesforce org that speaks enough REST + Bulk API 2.0 for sf-exit.

Mounted on a ``requests.Session`` as a transport adapter, so the real client code runs
unchanged: pagination, Bulk job polling, result locators, blob downloads and errors.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import re
from typing import Any, Dict, List
from urllib.parse import parse_qs, unquote, urlparse

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

INSTANCE = "https://acme.my.salesforce.com"
API = "62.0"


def F(name, type_="string", label=None, **kw):
    d = {"name": name, "type": type_, "label": label or name, "deprecatedAndHidden": False}
    d.update(kw)
    return d


STD = [
    F("Id", "id"),
    F("IsDeleted", "boolean"),
    F("CreatedDate", "datetime"),
    F("CreatedById", "reference", referenceTo=["User"]),
    F("LastModifiedDate", "datetime"),
]


class FakeOrg(BaseAdapter):
    def __init__(self, rest_page: int = 3, bulk_page: int = 4):
        super().__init__()
        self.rest_page = rest_page
        self.bulk_page = bulk_page
        self.objects: Dict[str, Dict[str, Any]] = {}
        self.blobs: Dict[str, bytes] = {}
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.calls: List[str] = []
        self.bulk_unsupported = set()
        self.requires_filter = set()
        self.polls_before_complete = 1

    # ------------------------------------------------------------ fixtures
    def add_object(self, name: str, fields: List[Dict], records: List[Dict], custom=None,
                   queryable=True, label=None):
        self.objects[name] = {
            "describe": {
                "name": name,
                "label": label or name,
                "custom": name.endswith("__c") if custom is None else custom,
                "queryable": queryable,
                "fields": fields,
            },
            "records": records,
        }

    def session(self) -> requests.Session:
        s = requests.Session()
        s.mount(INSTANCE, self)
        return s

    # ----------------------------------------------------------- transport
    def close(self):
        pass

    def send(self, request, **kwargs):
        url = urlparse(request.url)
        path = unquote(url.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append(f"{request.method} {path}")
        body = json.loads(request.body) if request.body else None
        try:
            status, payload, headers = self.route(request.method, path, qs, body)
        except _SfErr as e:
            status, payload, headers = e.status, [{"message": e.msg, "errorCode": e.code}], {}
        resp = requests.Response()
        resp.status_code = status
        resp.request = request
        resp.url = request.url
        resp.headers = CaseInsensitiveDict(headers or {})
        if isinstance(payload, (bytes, bytearray)):
            resp._content = bytes(payload)
        else:
            resp._content = json.dumps(payload).encode()
            resp.headers.setdefault("Content-Type", "application/json")
        resp._content_consumed = True
        resp.reason = "OK" if status < 400 else "Error"
        resp.encoding = "utf-8"
        return resp

    def route(self, method, path, qs, body):
        base = f"/services/data/v{API}"
        if path == "/services/data/":
            return 200, [{"version": "61.0"}, {"version": API}], {}
        if path == "/services/oauth2/userinfo":
            return 200, {"user_id": "005000000000001AAA", "preferred_username": "admin@acme.example"}, {}
        if not path.startswith(base):
            raise _SfErr(404, "NOT_FOUND", path)
        rest = path[len(base):]
        if rest == "/sobjects":
            return 200, {"sobjects": [
                {"name": n, "label": o["describe"]["label"], "queryable": o["describe"]["queryable"],
                 "custom": o["describe"]["custom"]} for n, o in self.objects.items()]}, {}
        m = re.fullmatch(r"/sobjects/(\w+)/describe", rest)
        if m:
            return 200, self._obj(m.group(1))["describe"], {}
        m = re.fullmatch(r"/sobjects/(\w+)/(\w+)/(VersionData|Body)", rest)
        if m:
            key = f"{m.group(1)}/{m.group(2)}"
            if key not in self.blobs:
                raise _SfErr(404, "NOT_FOUND", "blob not found")
            return 200, self.blobs[key], {"Content-Type": "application/octet-stream"}
        if rest == "/limits":
            return 200, {"DailyApiRequests": {"Max": 100000, "Remaining": 99000},
                         "DailyBulkV2QueryJobs": {"Max": 10000, "Remaining": 9990}}, {}
        if rest == "/limits/recordCount":
            names = qs.get("sObjects", "").split(",")
            return 200, {"sObjects": [
                {"name": n, "count": len(self._live(n, False))}
                for n in names if n in self.objects and self._live(n, False)]}, {}
        if rest in ("/query", "/queryAll") and "q" in qs:
            return 200, self._query(qs["q"], rest == "/queryAll", 0), {}
        m = re.fullmatch(r"/(query|queryAll)/cursor-(\d+)-(.+)", rest)
        if m:
            return 200, self._query(unquote(m.group(3)), m.group(1) == "queryAll", int(m.group(2))), {}
        if rest == "/jobs/query" and method == "POST":
            return 200, self._create_job(body), {}
        m = re.fullmatch(r"/jobs/query/(\w+)", rest)
        if m:
            job = self.jobs[m.group(1)]
            job["polls"] += 1
            if job["polls"] > self.polls_before_complete:
                job["state"] = "JobComplete"
            return 200, {"id": job["id"], "state": job["state"]}, {}
        m = re.fullmatch(r"/jobs/query/(\w+)/results", rest)
        if m:
            return self._results(self.jobs[m.group(1)], qs)
        raise _SfErr(404, "NOT_FOUND", f"no route for {method} {rest}")

    # ---------------------------------------------------------------- soql
    def _obj(self, name):
        if name not in self.objects:
            raise _SfErr(400, "INVALID_TYPE", f"sObject type '{name}' is not supported.")
        return self.objects[name]

    def _live(self, name, include_deleted):
        recs = self.objects[name]["records"]
        return [r for r in recs if include_deleted or not r.get("IsDeleted")]

    def _parse(self, soql):
        m = re.fullmatch(r"SELECT (.+?) FROM (\w+)(?: WHERE (.+))?", soql.strip(), re.S)
        if not m:
            raise _SfErr(400, "MALFORMED_QUERY", soql)
        return [f.strip() for f in m.group(1).split(",")], m.group(2), m.group(3)

    def _select(self, soql, include_deleted):
        fields, name, where = self._parse(soql)
        obj = self._obj(name)
        if name in self.requires_filter and not where:
            raise _SfErr(400, "MALFORMED_QUERY",
                         f"Implementation restriction: {name} requires a filter by Id")
        known = {f["name"] for f in obj["describe"]["fields"]}
        for f in fields:
            base = f.split(" ")[0]
            if not base.startswith(("COUNT(", "SUM(")) and base not in known:
                raise _SfErr(400, "INVALID_FIELD", f"No such column '{f}' on entity '{name}'")
        recs = self._live(name, include_deleted)
        if where:
            recs = [r for r in recs if self._where(r, where)]
        return fields, recs

    @staticmethod
    def _where(rec, where):
        m = re.fullmatch(r"(\w+) = (true|false)", where)
        if m:
            return bool(rec.get(m.group(1))) == (m.group(2) == "true")
        m = re.fullmatch(r"(\w+) IN \((.*)\)", where, re.S)
        if m and not m.group(2).startswith("SELECT"):
            ids = {x.strip().strip("'") for x in m.group(2).split(",")}
            return rec.get(m.group(1)) in ids
        return True

    def _query(self, soql, include_deleted, offset):
        fields, recs = self._select(soql, include_deleted)
        if fields == ["COUNT()"]:
            return {"totalSize": len(recs), "done": True, "records": []}
        if any(f.startswith(("COUNT(", "SUM(")) for f in fields):
            out = {}
            for f in fields:
                func, alias = f.split(" ")
                col = func[func.index("(") + 1:-1]
                out[alias] = len(recs) if func.startswith("COUNT") else sum(r.get(col) or 0 for r in recs)
            return {"totalSize": 1, "done": True, "records": [out]}
        page = recs[offset:offset + self.rest_page]
        done = offset + self.rest_page >= len(recs)
        out = {"totalSize": len(recs), "done": done,
               "records": [{"attributes": {"type": "x"}, **{f: _rest_value(r.get(f)) for f in fields}}
                           for r in page]}
        if not done:
            kind = "queryAll" if include_deleted else "query"
            out["nextRecordsUrl"] = (f"/services/data/v{API}/{kind}/cursor-{offset + self.rest_page}-"
                                     + requests.utils.quote(soql, safe=""))
        return out

    def _create_job(self, body):
        _, name, _ = self._parse(body["query"])
        if name in self.bulk_unsupported:
            raise _SfErr(400, "INVALIDENTITY", f"Entity '{name}' is not supported by the Bulk API.")
        self._select(body["query"], body["operation"] == "queryAll")  # validate early
        job_id = f"750{len(self.jobs):015d}"
        self.jobs[job_id] = {"id": job_id, "state": "UploadComplete", "polls": 0,
                             "query": body["query"], "all": body["operation"] == "queryAll"}
        return {"id": job_id, "state": "UploadComplete"}

    def _results(self, job, qs):
        if job["state"] != "JobComplete":
            raise _SfErr(400, "INVALIDJOBSTATE", "job not complete")
        fields, recs = self._select(job["query"], job["all"])
        size = int(qs.get("maxRecords") or self.bulk_page)
        start = int(qs.get("locator") or 0)
        page = recs[start:start + size]
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writerow(fields)
        for r in page:
            w.writerow([_bulk_value(r.get(f)) for f in fields])
        nxt = start + size
        return 200, buf.getvalue().encode(), {
            "Content-Type": "text/csv",
            "Sforce-NumberOfRecords": str(len(page)),
            "Sforce-Locator": str(nxt) if nxt < len(recs) else "null",
        }


class _SfErr(Exception):
    def __init__(self, status, code, msg):
        self.status, self.code, self.msg = status, code, msg


def _rest_value(v):
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", v):
        return v[:-1] + "+0000"
    return v


def _bulk_value(v):
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


# --------------------------------------------------------------------------- data
def _id(prefix, n):
    return f"{prefix}{n:012d}AAA"


def build_org(scale: int = 1, seed: int = 7) -> FakeOrg:
    """A small but realistic org: standard + custom objects, files, deleted rows, quirks."""
    rnd = random.Random(seed)
    org = FakeOrg()
    ts = lambda d: f"2024-{1 + d % 12:02d}-{1 + d % 28:02d}T09:{d % 60:02d}:00.000Z"  # noqa: E731

    org.add_object("Organization", [F("Id", "id"), F("Name"), F("OrganizationType"),
                                    F("InstanceName"), F("IsSandbox", "boolean")],
                   [{"Id": "00D000000000001AAA", "Name": "Acme Corp", "OrganizationType": "Enterprise Edition",
                     "InstanceName": "EU45", "IsSandbox": False}])
    org.add_object("User", STD + [F("Name"), F("Email", "email"), F("IsActive", "boolean")],
                   [{"Id": _id("005", i), "Name": f"User {i}", "Email": f"user{i}@acme.example",
                     "IsActive": i != 3, "CreatedDate": ts(i), "IsDeleted": False} for i in range(1, 6)])
    industries = ["Technology", "Retail", "Healthcare", "Finance", None]
    accounts = []
    for i in range(1, 25 * scale + 1):
        accounts.append({
            "Id": _id("001", i), "Name": f"Account {i}, \"Ltd\"" if i == 2 else f"Account {i}",
            "Industry": industries[i % 5], "AnnualRevenue": float(rnd.randint(1, 900) * 1000),
            "NumberOfEmployees": rnd.randint(1, 5000), "BillingCity": "London",
            "Description": "multi\nline, text" if i == 1 else None,
            "CreatedDate": ts(i), "LastModifiedDate": ts(i + 1), "CreatedById": _id("005", 1),
            "IsDeleted": i == 5, "OwnerId": _id("005", 1 + i % 5),
        })
    org.add_object("Account", STD + [
        F("Name"), F("Industry", "picklist"), F("AnnualRevenue", "currency"),
        F("NumberOfEmployees", "int"), F("BillingCity"), F("BillingAddress", "address"),
        F("Description", "textarea"), F("OwnerId", "reference", referenceTo=["User"]),
        F("Legacy__c", deprecatedAndHidden=True),
    ], accounts)
    org.add_object("Contact", STD + [
        F("FirstName"), F("LastName"), F("Email", "email"), F("Birthdate", "date"),
        F("AccountId", "reference", referenceTo=["Account"]), F("HasOptedOutOfEmail", "boolean"),
    ], [{"Id": _id("003", i), "FirstName": f"Ada{i}", "LastName": "Lovelace", "Email": f"ada{i}@x.example",
         "Birthdate": f"1990-01-{1 + i % 28:02d}", "AccountId": _id("001", 1 + i % 24),
         "HasOptedOutOfEmail": i % 2 == 0, "CreatedDate": ts(i), "IsDeleted": False}
        for i in range(1, 40 * scale + 1)])
    org.add_object("Invoice__c", STD + [
        F("Name"), F("Amount__c", "currency"), F("Paid__c", "boolean"), F("Due__c", "date"),
        F("Account__c", "reference", referenceTo=["Account"], cascadeDelete=True),
        F("Scan__c", "base64"),
    ], [{"Id": _id("a01", i), "Name": f"INV-{i:04d}", "Amount__c": 99.5 * i, "Paid__c": i % 3 == 0,
         "Due__c": "2025-06-30", "Account__c": _id("001", i), "CreatedDate": ts(i), "IsDeleted": False}
        for i in range(1, 8 * scale + 1)])
    org.add_object("Task", STD + [F("Subject"), F("WhatId", "reference", referenceTo=["Account", "Invoice__c"])],
                   [{"Id": _id("00T", i), "Subject": f"Call {i}", "WhatId": _id("001", i), "IsDeleted": False}
                    for i in range(1, 6)])
    org.bulk_unsupported.add("Task")
    org.add_object("EmptyThing__c", STD + [F("Name")], [])

    # Files
    docs, versions = [], []
    for i in range(1, 4):
        data = f"PDF bytes for file {i} ".encode() * (50 * i)
        vid, did = _id("068", i), _id("069", i)
        versions.append({"Id": vid, "ContentDocumentId": did, "Title": f"Contract {i}/final",
                         "FileExtension": "pdf", "VersionNumber": "1", "ContentSize": len(data),
                         "Checksum": hashlib.md5(data).hexdigest(), "IsLatest": True,
                         "IsDeleted": False, "VersionData": None})
        docs.append({"Id": did, "Title": f"Contract {i}/final", "IsDeleted": False})
        org.blobs[f"ContentVersion/{vid}"] = data
    org.add_object("ContentDocument", STD + [F("Title")], docs)
    org.add_object("ContentVersion", STD + [
        F("ContentDocumentId", "reference", referenceTo=["ContentDocument"]), F("Title"),
        F("FileExtension"), F("VersionNumber"), F("ContentSize", "int"), F("Checksum"),
        F("IsLatest", "boolean"), F("VersionData", "base64")], versions)
    org.add_object("ContentDocumentLink", [F("Id", "id"),
                                           F("ContentDocumentId", "reference", referenceTo=["ContentDocument"]),
                                           F("LinkedEntityId", "reference", referenceTo=["Account"]),
                                           F("ShareType", "picklist")],
                   [{"Id": _id("06A", i), "ContentDocumentId": _id("069", 1 + i % 3),
                     "LinkedEntityId": _id("001", i), "ShareType": "V"} for i in range(1, 6)])
    org.requires_filter.add("ContentDocumentLink")
    att = b"\x89PNG fake image bytes"
    org.add_object("Attachment", STD + [F("ParentId", "reference", referenceTo=["Account"]), F("Name"),
                                        F("BodyLength", "int"), F("Body", "base64")],
                   [{"Id": _id("00P", 1), "ParentId": _id("001", 1), "Name": "logo.png",
                     "BodyLength": len(att), "IsDeleted": False}])
    org.blobs[f"Attachment/{_id('00P', 1)}"] = att
    org.add_object("Document", STD + [F("FolderId", "reference", referenceTo=["Folder"]), F("Name"),
                                      F("Type"), F("BodyLength", "int"), F("Url", "url"), F("Body", "base64")],
                   [{"Id": _id("015", 1), "FolderId": _id("00l", 1), "Name": "Letterhead", "Type": "docx",
                     "BodyLength": 4, "IsDeleted": False},
                    {"Id": _id("015", 2), "FolderId": _id("00l", 1), "Name": "Website", "Type": None,
                     "BodyLength": 0, "Url": "https://acme.example", "IsDeleted": False}])
    org.blobs[f"Document/{_id('015', 1)}"] = b"DOCX"

    # Quirky objects sf-exit has to cope with
    org.add_object("Vote", STD, [{"Id": _id("0Vo", 1)}])  # requires a filter: on the deny-list
    org.add_object("AccountChangeEvent", STD, [], queryable=True)  # streaming only
    org.add_object("SomeSetting__mdt", [F("Id", "id"), F("DeveloperName")],
                   [{"Id": _id("m00", 1), "DeveloperName": "Default"}], custom=True)
    org.add_object("NotQueryable", STD, [], queryable=False)
    return org


def client_for(org: FakeOrg, **kw):
    from sf_exit.client import SalesforceClient

    c = SalesforceClient(INSTANCE, "fake-token", session=org.session(), **kw)
    c._sleep = lambda *a, **k: None  # type: ignore[assignment]
    return c

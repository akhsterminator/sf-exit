"""Offline org audit: a data dictionary plus data-quality findings, built from an export.

No API calls: everything is computed from ``data/*.csv`` and ``schema/*.json``.
Outputs (in ``<export>/audit/`` by default):

* ``index.html``           self-contained interactive report (open it in any browser)
* ``data-dictionary.csv``  every object and field with label, type, fill rate and help text
* ``findings.md``          the findings as Markdown (paste into a ticket or a PR)
* ``audit.json``           everything, machine-readable
"""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import html
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from sf_exit import __version__

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

DATE_TYPES = {"date", "datetime"}
PICKLIST_TYPES = {"picklist", "multipicklist"}
MIN_ROWS_FOR_USAGE = 20  # don't call a field "unused" on an object with a handful of rows
RARE_FILL = 0.05
LARGE_OBJECT_FIELDS = 300
PARALLEL_THRESHOLD_BYTES = 32 * 1024 * 1024
SEVERITY_WEIGHT = {"high": 14.0, "medium": 7.0, "low": 3.0, "info": 0.0}
_ACCOUNT_SUFFIX = re.compile(
    r"\b(inc|incorporated|ltd|limited|llc|plc|gmbh|corp|corporation|co|company|sa|bv|ag|pty)\b\.?")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass
class FieldStats:
    name: str
    label: str
    type: str
    custom: bool
    filled: int = 0
    fill_rate: float = 0.0
    formula: bool = False
    help: str = ""
    length: Optional[int] = None
    references: List[str] = field(default_factory=list)
    picklist_defined: List[str] = field(default_factory=list)
    picklist_counts: Dict[str, int] = field(default_factory=dict)
    true_count: Optional[int] = None
    distinct: Optional[int] = None  # custom fields only: 1 = every value identical, 2 = varied
    min: Optional[str] = None
    max: Optional[str] = None


@dataclass
class ObjectStats:
    name: str
    label: str
    custom: bool
    rows: int
    fields: List[FieldStats]
    created_by_year: Dict[str, int] = field(default_factory=dict)
    last_modified: Optional[str] = None
    owner_inactive: int = 0


@dataclass
class Finding:
    id: str
    severity: str  # high | medium | low | info
    title: str
    detail: str
    items: List[str] = field(default_factory=list)
    count: int = 0
    share: float = 0.0  # how much of the relevant population is affected (0..1), for the score


def _n(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _norm_company(name: str) -> str:
    n = _ACCOUNT_SUFFIX.sub(" ", name.lower())
    return _NON_ALNUM.sub("", n)


def _describe(export_dir: Path, name: str) -> Dict[str, Any]:
    p = export_dir / "schema" / f"{name}.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {"name": name, "fields": []}


def analyse_object(export_dir: Path, csv_path: Path,
                   inactive_users: set) -> Tuple[ObjectStats, Dict[str, Any]]:
    """One pass over one CSV. Returns the stats plus raw material for cross-object checks."""
    name = csv_path.stem
    dup_keys: Dict[str, Any] = {"email": Counter(), "account": Counter(),
                                "contact_emails": set(), "lead_emails": []}
    desc = _describe(export_dir, name)
    meta = {f["name"]: f for f in desc.get("fields", [])}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        n = len(header)
        filled = [0] * n
        types = [meta.get(c, {}).get("type", "string") for c in header]
        pick_idx = [i for i, t in enumerate(types) if t in PICKLIST_TYPES]
        bool_idx = [i for i, t in enumerate(types) if t == "boolean"]
        date_idx = [i for i, t in enumerate(types) if t in DATE_TYPES]
        pick_counts: Dict[int, Counter] = {i: Counter() for i in pick_idx}
        true_counts = dict.fromkeys(bool_idx, 0)
        mins: Dict[int, str] = {}
        maxs: Dict[int, str] = {}
        col = {c: i for i, c in enumerate(header)}
        created_i = col.get("CreatedDate")
        lm_i = col.get("LastModifiedDate")
        owner_i = col.get("OwnerId")
        email_i = col.get("Email") if name in ("Contact", "Lead") else None
        converted_i = col.get("IsConverted") if name == "Lead" else None
        acct_name_i = col.get("Name") if name == "Account" else None
        cust_idx = [i for i, c in enumerate(header) if meta.get(c, {}).get("custom")
                    and types[i] != "boolean" and not meta[c].get("calculated")]
        distinct: Dict[int, set] = {i: set() for i in cust_idx}
        by_year: Counter = Counter()
        owner_inactive = 0
        rows = 0
        for row in reader:
            rows += 1
            if len(row) > n:
                row = row[:n]
            for i, v in enumerate(row):
                if v:
                    filled[i] += 1
            for i in cust_idx:
                seen = distinct[i]
                if len(seen) < 2 and i < len(row) and row[i]:
                    seen.add(row[i])
            for i in pick_idx:
                v = row[i] if i < len(row) else ""
                if v:
                    for part in v.split(";"):
                        pick_counts[i][part] += 1
            for i in bool_idx:
                if i < len(row) and row[i] == "true":
                    true_counts[i] += 1
            for i in date_idx:
                v = row[i] if i < len(row) else ""
                if v:
                    if i not in mins or v < mins[i]:
                        mins[i] = v
                    if i not in maxs or v > maxs[i]:
                        maxs[i] = v
            if created_i is not None and created_i < len(row) and row[created_i]:
                by_year[row[created_i][:4]] += 1
            if owner_i is not None and inactive_users and owner_i < len(row) \
                    and row[owner_i] in inactive_users:
                owner_inactive += 1
            if email_i is not None and email_i < len(row) and row[email_i]:
                email = row[email_i].strip().lower()
                dup_keys["email"][email] += 1
                if name == "Contact":
                    dup_keys["contact_emails"].add(email)
                elif converted_i is None or row[converted_i].lower() != "true":
                    dup_keys["lead_emails"].append(email)
            if acct_name_i is not None and acct_name_i < len(row) and row[acct_name_i]:
                key = _norm_company(row[acct_name_i])
                if key:
                    dup_keys["account"][key] += 1

    fields = []
    for i, c in enumerate(header):
        m = meta.get(c, {})
        fs = FieldStats(
            name=c, label=m.get("label", c), type=types[i], custom=bool(m.get("custom")),
            filled=filled[i], fill_rate=(filled[i] / rows) if rows else 0.0,
            formula=bool(m.get("calculated")), help=m.get("inlineHelpText") or "",
            length=m.get("length") or None, references=list(m.get("referenceTo") or []),
        )
        if i in pick_counts:
            fs.picklist_defined = [p["value"] for p in m.get("picklistValues", []) if p.get("active", True)]
            fs.picklist_counts = dict(pick_counts[i].most_common(200))
        if i in true_counts:
            fs.true_count = true_counts[i]
        if i in distinct:
            fs.distinct = len(distinct[i])
        if i in mins:
            fs.min, fs.max = mins[i], maxs[i]
        fields.append(fs)
    stats = ObjectStats(
        name=name, label=desc.get("label", name), custom=bool(desc.get("custom")), rows=rows,
        fields=fields, created_by_year=dict(sorted(by_year.items())),
        last_modified=maxs.get(lm_i) if lm_i is not None else None, owner_inactive=owner_inactive,
    )
    return stats, dup_keys


def _analyse_job(args: Tuple[str, str, frozenset]) -> Tuple[ObjectStats, Dict[str, Any]]:
    return analyse_object(Path(args[0]), Path(args[1]), set(args[2]))


def analyse_all(export_dir: Path, paths: List[Path], inactive: set,
                on_object: Optional[Callable[[str, int], None]] = None,
                ) -> Tuple[List[ObjectStats], Dict[str, Any]]:
    """Analyse every CSV, using all CPU cores when the export is big enough to be worth it."""
    merged: Dict[str, Any] = {"email": Counter(), "account": Counter(),
                              "contact_emails": set(), "lead_emails": []}
    objects: List[ObjectStats] = []

    def take(result: Tuple[ObjectStats, Dict[str, Any]]) -> None:
        stats, dups = result
        objects.append(stats)
        merged["email"].update(dups["email"])
        merged["account"].update(dups["account"])
        merged["contact_emails"] |= dups["contact_emails"]
        merged["lead_emails"] += dups["lead_emails"]
        if on_object:
            on_object(stats.name, stats.rows)

    workers = min(os.cpu_count() or 1, 8, len(paths))
    big = sum(p.stat().st_size for p in paths) > PARALLEL_THRESHOLD_BYTES
    if workers > 1 and big:
        try:
            jobs = [(str(export_dir), str(p), frozenset(inactive))
                    for p in sorted(paths, key=lambda p: -p.stat().st_size)]
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for result in pool.map(_analyse_job, jobs):
                    take(result)
            objects.sort(key=lambda o: o.name.lower())
            return objects, merged
        except (OSError, RuntimeError, ImportError):  # no multiprocessing here: go serial
            objects.clear()
            for key in ("email", "account", "contact_emails", "lead_emails"):
                merged[key].clear()
    for p in paths:
        take(analyse_object(export_dir, p, inactive))
    return objects, merged


def _inactive_users(export_dir: Path) -> set:
    p = export_dir / "data" / "User.csv"
    if not p.exists():
        return set()
    with open(p, newline="", encoding="utf-8") as fh:
        return {r["Id"] for r in csv.DictReader(fh) if r.get("IsActive", "true").lower() == "false"}


def findings_for(objects: List[ObjectStats], dup_keys: Dict[str, Any],
                 as_of: dt.datetime) -> List[Finding]:
    out: List[Finding] = []
    unused, rare, never_true, unused_values, empty_custom, stale, wide = [], [], [], [], [], [], []
    single: List[str] = []
    picklists_with_data = 0
    for o in objects:
        if o.custom and o.rows == 0:
            empty_custom.append(f"{o.name} ({o.label})")
        if sum(1 for f in o.fields if f.custom) >= LARGE_OBJECT_FIELDS:
            wide.append(f"{o.name}: {sum(1 for f in o.fields if f.custom)} custom fields")
        if o.custom and o.rows and o.last_modified:
            try:
                last = dt.datetime.fromisoformat(o.last_modified[:19])
                if (as_of.replace(tzinfo=None) - last).days > 365:
                    stale.append(f"{o.name}: last change {o.last_modified[:10]} ({o.rows:,} rows)")
            except ValueError:
                pass
        if o.rows < MIN_ROWS_FOR_USAGE:
            continue
        for f in o.fields:
            if f.formula:
                continue
            q = f"{o.name}.{f.name}"
            if f.custom:
                if f.type == "boolean":
                    if f.true_count == 0:
                        never_true.append(f"{q} ({f.label})")
                elif f.filled == 0:
                    unused.append(f"{q} ({f.label})")
                elif f.fill_rate < RARE_FILL:
                    rare.append(f"{q}: {f.fill_rate:.1%} filled")
                elif f.distinct == 1 and f.fill_rate > 0.5:
                    single.append(f"{q} ({f.label}): same value on {f.filled:,} records")
            if f.picklist_defined and f.filled:
                picklists_with_data += 1
                missing = [v for v in f.picklist_defined if v not in f.picklist_counts]
                if missing:
                    unused_values.append(f"{q}: {', '.join(missing[:8])}"
                                         + (f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""))
    total_custom = sum(1 for o in objects if o.rows >= MIN_ROWS_FOR_USAGE
                       for f in o.fields if f.custom and not f.formula)
    custom_objects = sum(1 for o in objects if o.custom)
    if unused:
        pct = f" ({len(unused) / total_custom:.0%} of custom fields on objects with data)" if total_custom else ""
        out.append(Finding("unused-fields", "high", _n(len(unused), "custom field is", "custom fields are") + " never filled in",
                           "Not one record has a value. These are candidates for deletion, which "
                           "simplifies page layouts and reports." + pct, unused, len(unused),
                           len(unused) / max(total_custom, 1)))
    if never_true:
        out.append(Finding("never-checked", "medium", _n(len(never_true), "custom checkbox is", "custom checkboxes are") + " never ticked",
                           "Every record has the default value (false).", never_true, len(never_true),
                           len(never_true) / max(total_custom, 1)))
    if rare:
        out.append(Finding("rare-fields", "medium", _n(len(rare), "custom field is", "custom fields are") + " filled on fewer than 5% of records",
                           "Often abandoned fields, or fields that belong on a different object.", rare, len(rare),
                           len(rare) / max(total_custom, 1)))
    if single:
        out.append(Finding("single-value-fields", "low",
                           _n(len(single), "custom field always holds", "custom fields always hold") + " the same value",
                           "Filled in, but identical on every record, so it carries no information.",
                           single, len(single), len(single) / max(total_custom, 1)))
    if unused_values:
        out.append(Finding("unused-picklist-values", "low",
                           _n(len(unused_values), "picklist has", "picklists have") + " values nobody uses",
                           "Active picklist values that appear on no record. Removing them makes data entry faster.",
                           unused_values, len(unused_values),
                           len(unused_values) / max(picklists_with_data, 1)))
    if empty_custom:
        out.append(Finding("empty-objects", "medium", _n(len(empty_custom), "custom object has", "custom objects have") + " no records",
                           "Built but never used, or used only by a retired process.", empty_custom, len(empty_custom),
                           len(empty_custom) / max(custom_objects, 1)))
    if stale:
        out.append(Finding("stale-objects", "low", _n(len(stale), "custom object hasn't", "custom objects haven't") + " changed in over a year",
                           "Possibly abandoned. Check before migrating them.", stale, len(stale),
                           len(stale) / max(custom_objects, 1)))
    dupe_emails = {k: v for k, v in dup_keys["email"].items() if v > 1}
    if dupe_emails:
        extra = sum(v - 1 for v in dupe_emails.values())
        items = [f"{k}: {v} records" for k, v in sorted(dupe_emails.items(), key=lambda kv: -kv[1])[:500]]
        out.append(Finding("duplicate-emails", "high",
                           _n(len(dupe_emails), "email address appears", "email addresses appear") + " on more than one Contact/Lead",
                           f"{extra:,} likely duplicate people. Deduplicate before migrating, or the "
                           "duplicates move with you.", items, len(dupe_emails),
                           extra / max(sum(dup_keys["email"].values()), 1)))
    contact_emails = dup_keys.get("contact_emails") or set()
    lead_hits = Counter(e for e in dup_keys.get("lead_emails") or [] if e in contact_emails)
    if lead_hits:
        n_leads = sum(lead_hits.values())
        items = [f"{k}: {v} open lead{'s' if v > 1 else ''}" for k, v in lead_hits.most_common(500)]
        out.append(Finding("leads-matching-contacts", "medium",
                           _n(n_leads, "open lead already exists", "open leads already exist") + " as a contact",
                           "Unconverted leads with the email address of an existing contact: the same "
                           "person is being worked twice.", items, n_leads,
                           n_leads / max(len(dup_keys["lead_emails"]), 1)))
    dupe_accounts = {k: v for k, v in dup_keys["account"].items() if v > 1}
    if dupe_accounts:
        items = [f"{k}: {v} accounts" for k, v in sorted(dupe_accounts.items(), key=lambda kv: -kv[1])[:500]]
        out.append(Finding("duplicate-accounts", "medium",
                           _n(len(dupe_accounts), "company name appears", "company names appear") + " on more than one Account",
                           "Matched ignoring case, punctuation and suffixes like Inc/Ltd/LLC.",
                           items, len(dupe_accounts),
                           sum(v - 1 for v in dupe_accounts.values()) / max(sum(dup_keys["account"].values()), 1)))
    orphaned = [f"{o.name}: {o.owner_inactive:,} records" for o in objects if o.owner_inactive]
    if orphaned:
        total = sum(o.owner_inactive for o in objects)
        out.append(Finding("inactive-owners", "medium",
                           _n(total, "record is", "records are") + " owned by deactivated users",
                           "Nobody is responsible for them. Reassign them before a migration.",
                           orphaned, total,
                           total / max(sum(o.rows for o in objects if any(f.name == "OwnerId" for f in o.fields)), 1)))
    contact = next((o for o in objects if o.name == "Contact"), None)
    if contact and contact.rows:
        acc = next((f for f in contact.fields if f.name == "AccountId"), None)
        if acc and acc.filled < contact.rows:
            n = contact.rows - acc.filled
            out.append(Finding("contacts-without-account", "low",
                               _n(n, "contact isn't", "contacts aren't") + " linked to an account",
                               "Private contacts are invisible to most users and are easy to lose in a migration.",
                               [], n, n / contact.rows))
    if wide:
        out.append(Finding("wide-objects", "info", _n(len(wide), "object has", "objects have") + f" {LARGE_OBJECT_FIELDS}+ custom fields",
                           "Approaching Salesforce's per-object field limit.", wide, len(wide)))
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    return sorted(out, key=lambda f: (order[f.severity], -f.count))


def overdue_opportunities(export_dir: Path, as_of: dt.datetime) -> Optional[Finding]:
    p = export_dir / "data" / "Opportunity.csv"
    if not p.exists():
        return None
    today = as_of.strftime("%Y-%m-%d")
    n = 0
    open_opps = 0
    amount = 0.0
    with open(p, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("IsClosed", "").lower() == "false":
                open_opps += 1
            if r.get("IsClosed", "").lower() == "false" and r.get("CloseDate") and r["CloseDate"] < today:
                n += 1
                with contextlib.suppress(ValueError):
                    amount += float(r.get("Amount") or 0)
    if not n:
        return None
    return Finding("overdue-opportunities", "high", _n(n, "open opportunity is past its", "open opportunities are past their") + " close date",
                   f"Worth {amount:,.0f} in total. Stale pipeline makes forecasts unreliable.", [], n,
                   n / max(open_opps, 1))


def health_score(findings: List[Finding]) -> Tuple[int, str]:
    """0-100. Each finding costs between half and all of its severity weight (high 14,
    medium 7, low 3), depending on how much of the data it affects (full weight at 20%+)."""
    penalty = sum(SEVERITY_WEIGHT[f.severity] * (0.5 + 0.5 * min(1.0, f.share * 5)) for f in findings)
    score = max(0, round(100 - penalty))
    grade = next(g for limit, g in ((90, "A"), (75, "B"), (60, "C"), (40, "D"), (-1, "F")) if score >= limit)
    return score, grade


def metadata_inventory(export_dir: Path) -> Dict[str, int]:
    """What has been built in the org, counted from a `--metadata` export (empty if none)."""
    root = Path(export_dir) / "metadata"
    if not root.is_dir():
        return {}
    kinds = [("Apex classes", "classes", "*.cls"), ("Apex triggers", "triggers", "*.trigger"),
             ("Flows", "flows", "*"), ("Custom objects & settings", "objects", "*"),
             ("Page layouts", "layouts", "*"), ("Lightning pages", "flexipages", "*"),
             ("Reports", "reports", "**/*.report"), ("Dashboards", "dashboards", "**/*.dashboard"),
             ("Email templates", "email", "**/*.email"), ("Permission sets", "permissionsets", "*"),
             ("Profiles", "profiles", "*"), ("Validation & workflow rules", "workflows", "*"),
             ("Visualforce pages", "pages", "*.page"), ("Lightning components", "lwc", "*"),
             ("Aura components", "aura", "*")]
    out: Dict[str, int] = {}
    for label, folder, pattern in kinds:
        d = root / folder
        if d.is_dir():
            n = sum(1 for p in d.glob(pattern) if not p.name.endswith("-meta.xml") or pattern == "*")
            if n:
                out[label] = n
    loc = 0
    for folder, pattern in (("classes", "*.cls"), ("triggers", "*.trigger")):
        for p in (root / folder).glob(pattern) if (root / folder).is_dir() else []:
            with contextlib.suppress(OSError), open(p, encoding="utf-8", errors="ignore") as fh:
                loc += sum(1 for line in fh if line.strip())
    if loc:
        out["Lines of Apex"] = loc
    return out


def run_audit(export_dir: Path, out_dir: Optional[Path] = None,
              on_object: Optional[Callable[[str, int], None]] = None) -> Dict[str, Any]:
    export_dir = Path(export_dir)
    out = Path(out_dir) if out_dir else export_dir / "audit"
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = export_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else {}
    as_of_s = manifest.get("finished_at") or dt.datetime.now(dt.timezone.utc).isoformat()
    as_of = dt.datetime.fromisoformat(as_of_s.replace("Z", "+00:00"))

    inactive = _inactive_users(export_dir)
    paths = sorted((export_dir / "data").glob("*.csv"), key=lambda p: p.stem.lower())
    if not paths:
        raise FileNotFoundError(f"No data/*.csv in {export_dir}: is this an sf-exit export?")
    objects, dup_keys = analyse_all(export_dir, paths, inactive, on_object)
    findings = findings_for(objects, dup_keys, as_of)
    overdue = overdue_opportunities(export_dir, as_of)
    if overdue:
        findings.insert(0, overdue)
    score, grade = health_score(findings)

    with_data = [o for o in objects if o.rows]
    custom_fields = [f for o in objects for f in o.fields if f.custom]
    summary = {
        "org": manifest.get("org", {}),
        "as_of": as_of_s,
        "objects": len(objects),
        "objects_with_data": len(with_data),
        "records": sum(o.rows for o in objects),
        "fields": sum(len(o.fields) for o in objects),
        "custom_fields": len(custom_fields),
        "custom_objects": sum(1 for o in objects if o.custom),
        "files": manifest.get("totals", {}).get("files"),
        "file_bytes": manifest.get("totals", {}).get("file_bytes"),
        "findings": {s: sum(1 for f in findings if f.severity == s) for s in ("high", "medium", "low", "info")},
        "score": score,
        "grade": grade,
        "configuration": metadata_inventory(export_dir),
    }
    report = {"tool": "sf-exit", "version": __version__, "summary": summary,
              "findings": [asdict(f) for f in findings], "objects": [asdict(o) for o in objects]}

    (out / "audit.json").write_text(json.dumps(report, indent=1), "utf-8")
    _write_dictionary(out / "data-dictionary.csv", objects)
    (out / "findings.md").write_text(_findings_md(summary, findings), "utf-8")
    (out / "index.html").write_text(_html(report), "utf-8")
    return report


def _write_dictionary(path: Path, objects: List[ObjectStats]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["object", "object_label", "field", "label", "type", "custom", "formula",
                    "references", "rows", "filled", "fill_rate", "help_text", "picklist_values"])
        for o in objects:
            for f in o.fields:
                w.writerow([o.name, o.label, f.name, f.label, f.type, f.custom, f.formula,
                            ";".join(f.references), o.rows, f.filled, f"{f.fill_rate:.4f}", f.help,
                            ";".join(f.picklist_defined)])


def _findings_md(summary: Dict[str, Any], findings: List[Finding]) -> str:
    org = summary.get("org") or {}
    lines = [f"# Salesforce org audit: {org.get('name', '')}".rstrip(": "), "",
             f"{summary['records']:,} records · {summary['objects_with_data']:,} objects with data · "
             f"{summary['custom_fields']:,} custom fields · as of {summary['as_of'][:10]}", "",
             f"**Health score: {summary['score']}/100 (grade {summary['grade']})**", ""]
    if summary.get("configuration"):
        lines += ["Configuration: " + " · ".join(f"{v:,} {k.lower()}" for k, v in
                                                 summary["configuration"].items()), ""]
    if not findings:
        lines.append("No findings. Tidy org!")
    for f in findings:
        lines += [f"## [{f.severity.upper()}] {f.title}", "", f.detail, ""]
        for item in f.items[:50]:
            lines.append(f"- {item}")
        if len(f.items) > 50:
            lines.append(f"- … and {len(f.items) - 50:,} more (see audit.json)")
        lines.append("")
    lines.append(f"_Generated by [sf-exit](https://github.com/akhsterminator/sf-exit) {__version__}._")
    return "\n".join(lines) + "\n"


def _html(report: Dict[str, Any]) -> str:
    data = json.dumps(report, separators=(",", ":")).replace("</", "<\\/")
    org = report["summary"].get("org") or {}
    title = html.escape(f"Salesforce audit · {org.get('name') or 'export'}")
    return _TEMPLATE.replace("__TITLE__", title).replace("__DATA__", data)


_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f7f8fb;--panel:#fff;--ink:#141a26;--muted:#5d6b82;--line:#e3e7ef;--accent:#2563eb;--high:#dc2626;--medium:#d97706;--low:#2563eb;--info:#64748b;--bar:#e8edf6;--ok:#16a34a}
@media (prefers-color-scheme:dark){:root{--bg:#0b1020;--panel:#121a2d;--ink:#e6ebf5;--muted:#93a1ba;--line:#24304a;--accent:#60a5fa;--bar:#1c2740}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif}
header{padding:28px 32px 8px;max-width:1240px;margin:0 auto}h1{margin:0;font-size:26px;letter-spacing:-.4px}.sub{color:var(--muted);margin-top:4px}
main{max-width:1240px;margin:0 auto;padding:8px 32px 48px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0 26px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}.card .v{font-size:22px;font-weight:700;letter-spacing:-.5px;white-space:nowrap}.card .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}.card .of{font-size:13px;color:var(--muted);font-weight:500;margin-left:3px}.cards.small{grid-template-columns:repeat(auto-fit,minmax(118px,1fr));margin-top:0}.cards.small .v{font-size:18px}
h2{font-size:18px;margin:30px 0 12px}
.finding{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--info);border-radius:10px;margin:0 0 10px;padding:12px 16px}
.finding.high{border-left-color:var(--high)}.finding.medium{border-left-color:var(--medium)}.finding.low{border-left-color:var(--low)}
.finding summary{cursor:pointer;font-weight:600;list-style:none}.finding summary::-webkit-details-marker{display:none}
.sev{display:inline-block;font-size:11px;font-weight:700;text-transform:uppercase;border-radius:999px;padding:1px 8px;margin-right:8px;color:#fff;background:var(--info)}
.sev.high{background:var(--high)}.sev.medium{background:var(--medium)}.sev.low{background:var(--low)}
.finding p{margin:6px 0;color:var(--muted)}.finding ul{margin:6px 0 0;padding-left:20px;max-height:280px;overflow:auto;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
input[type=search]{flex:1;min-width:220px;padding:9px 12px;border-radius:9px;border:1px solid var(--line);background:var(--panel);color:var(--ink);font:inherit}
label.chk{color:var(--muted);font-size:13px;display:flex;gap:6px;align-items:center}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;cursor:pointer;user-select:none;white-space:nowrap}
tr.obj{cursor:pointer}tr.obj:hover td{background:color-mix(in srgb,var(--accent) 6%,transparent)}
td.n{text-align:right;font-variant-numeric:tabular-nums}code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
.bar{position:relative;height:8px;background:var(--bar);border-radius:99px;min-width:90px}.bar i{position:absolute;inset:0 auto 0 0;background:var(--accent);border-radius:99px}
.bar.zero i{background:var(--high)}.fr{display:flex;align-items:center;gap:8px}.fr .bar{flex:1}.fr span{width:38px;text-align:right;font-size:12px}
.detail th,.detail td{padding:6px 10px}.bar.zero{background:color-mix(in srgb,var(--high) 25%,var(--bar))}.tag{font-size:11px;border:1px solid var(--line);border-radius:6px;padding:0 6px;color:var(--muted);margin-left:4px}
.detail td{background:var(--bg);padding:14px 16px}.detail table{font-size:13px}.muted{color:var(--muted)}
.spark{display:flex;gap:2px;align-items:flex-end;height:28px}.spark i{width:6px;background:var(--accent);border-radius:2px 2px 0 0;opacity:.8}
footer{color:var(--muted);text-align:center;font-size:12px;padding:24px}footer a{color:var(--accent)}
.ok{color:var(--ok);font-weight:600}
</style></head><body>
<header><h1 id="title"></h1><div class="sub" id="sub"></div></header>
<main>
<div class="cards" id="cards"></div><div id="cfg"></div>
<h2>Findings</h2><div id="findings"></div>
<h2>Data dictionary</h2>
<div class="toolbar"><input type="search" id="q" placeholder="Search objects and fields (name, label, help text)…">
<label class="chk"><input type="checkbox" id="onlyData" checked> Only objects with data</label>
<label class="chk"><input type="checkbox" id="onlyCustom"> Only custom</label></div>
<table id="objs"><thead><tr><th data-k="name">Object</th><th data-k="rows" class="n">Rows</th><th data-k="nf" class="n">Fields</th><th data-k="nc" class="n">Custom</th><th data-k="unused" class="n">Unused custom</th><th>Records created per year</th></tr></thead><tbody></tbody></table>
</main>
<footer>Generated offline by <a href="https://github.com/akhsterminator/sf-exit">sf-exit</a> <span id="ver"></span>. Nothing in this report left your machine.</footer>
<script>
const D=__DATA__;const S=D.summary;const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=n=>n==null?"–":Number(n).toLocaleString();
const bytes=n=>{if(n==null)return"–";const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<4){n/=1024;i++}return(i?n.toFixed(1):n)+" "+u[i]};
$("#title").textContent="Salesforce org audit"+(S.org&&S.org.name?": "+S.org.name:"");
$("#sub").textContent=[S.org&&S.org.edition,"export of "+String(S.as_of).slice(0,10),S.org&&S.org.is_sandbox?"sandbox":""].filter(Boolean).join(" · ");
$("#ver").textContent=D.version;
const unusedPct=(()=>{const f=D.findings.find(x=>x.id==="unused-fields");const tot=D.objects.filter(o=>o.rows>=20).reduce((a,o)=>a+o.fields.filter(f=>f.custom&&!f.formula).length,0);return f&&tot?Math.round(100*f.count/tot)+"%":"0%"})();
const sc=S.score>=75?"var(--ok)":S.score>=60?"var(--medium)":"var(--high)";
const cards=[["Health score",`<span style="color:${sc}">${S.score}</span><span class="of">/100 · ${S.grade}</span>`],["Records",fmt(S.records)],["Objects with data",fmt(S.objects_with_data)+" / "+fmt(S.objects)],["Custom fields",fmt(S.custom_fields)+` <span class="of">${unusedPct} unused</span>`],["Files",S.files?fmt(S.files)+" · "+bytes(S.file_bytes):"–"],["Findings",`<span style="color:var(--high)">${S.findings.high}</span> · <span style="color:var(--medium)">${S.findings.medium}</span> · <span style="color:var(--low)">${S.findings.low}</span>`]];
$("#cards").innerHTML=cards.map(([k,v])=>`<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
const cfg=Object.entries(S.configuration||{});if(cfg.length){$("#cfg").innerHTML="<h2>What's been built</h2><div class=\"cards small\">"+cfg.map(([k,v])=>`<div class="card"><div class="k">${esc(k)}</div><div class="v">${fmt(v)}</div></div>`).join("")+"</div>"}
$("#findings").innerHTML=D.findings.length?D.findings.map(f=>`<details class="finding ${f.severity}"><summary><span class="sev ${f.severity}">${f.severity}</span>${esc(f.title)}</summary><p>${esc(f.detail)}</p>${f.items.length?"<ul>"+f.items.slice(0,1000).map(i=>`<li>${esc(i)}</li>`).join("")+"</ul>":""}</details>`).join(""):'<p class="ok">No findings. This org is in good shape.</p>';
const objs=D.objects.map(o=>({...o,nf:o.fields.length,nc:o.fields.filter(f=>f.custom).length,unused:o.rows>=20?o.fields.filter(f=>f.custom&&!f.formula&&(f.type==="boolean"?f.true_count===0:f.filled===0)).length:0}));
let sortK="rows",sortD=-1,open=null;
function spark(y){const v=Object.values(y);if(!v.length)return"";const m=Math.max(...v);return`<div class="spark" title="${esc(Object.entries(y).map(([k,n])=>k+": "+n).join(", "))}">`+v.map(n=>`<i style="height:${Math.max(2,26*n/m)}px"></i>`).join("")+"</div>"}
function fieldRows(o){return [...o.fields].sort((a,b)=>(b.custom-a.custom)).map(f=>{const pct=o.rows?Math.round(100*f.fill_rate):0;const pctTxt=!o.rows?"":(f.filled&&pct===0?"<1%":pct+"%");const extra=[f.custom?'<span class="tag">custom</span>':"",f.formula?'<span class="tag">formula</span>':"",f.references.length?`<span class="tag">→ ${esc(f.references.join(" | "))}</span>`:""].join("");
const pv=f.picklist_defined.length?`<div class="muted">${f.picklist_defined.slice(0,30).map(v=>`${esc(v)} <b>${fmt(f.picklist_counts[v]||0)}</b>`).join(" · ")}</div>`:"";
return`<tr><td><code>${esc(f.name)}</code>${extra}<div class="muted">${esc(f.label)}</div></td><td>${esc(f.type)}</td><td class="n">${fmt(f.filled)}</td><td><div class="fr"><div class="bar ${o.rows&&!f.filled?"zero":""}"><i style="width:${pct}%"></i></div><span class="muted">${pctTxt}</span></div></td><td>${esc(f.help)}${pv}${f.min?`<div class="muted">${esc(f.min.slice(0,10))} → ${esc(f.max.slice(0,10))}</div>`:""}</td></tr>`}).join("")}
function render(){const q=$("#q").value.toLowerCase().trim(),od=$("#onlyData").checked,oc=$("#onlyCustom").checked;
const rows=objs.filter(o=>(!od||o.rows)&&(!oc||o.custom)&&(!q||o.name.toLowerCase().includes(q)||o.label.toLowerCase().includes(q)||o.fields.some(f=>f.name.toLowerCase().includes(q)||f.label.toLowerCase().includes(q)||f.help.toLowerCase().includes(q))))
.sort((a,b)=>(a[sortK]>b[sortK]?1:a[sortK]<b[sortK]?-1:0)*sortD);
$("#objs tbody").innerHTML=rows.map(o=>`<tr class="obj" data-n="${esc(o.name)}"><td><code>${esc(o.name)}</code>${o.custom?'<span class="tag">custom</span>':""}<div class="muted">${esc(o.label)}</div></td><td class="n">${fmt(o.rows)}</td><td class="n">${o.nf}</td><td class="n">${o.nc}</td><td class="n">${o.unused||""}</td><td>${spark(o.created_by_year)}</td></tr>`+(open===o.name?`<tr class="detail"><td colspan="6"><table><thead><tr><th>Field</th><th>Type</th><th class="n">Filled</th><th>Fill rate</th><th>Help text · picklist usage · range <span class="muted" style="text-transform:none">(custom fields first)</span></th></tr></thead><tbody>${fieldRows(o)}</tbody></table></td></tr>`:"")).join("")||'<tr><td colspan="6" class="muted">No matches</td></tr>'}
$("#objs tbody").addEventListener("click",e=>{const tr=e.target.closest("tr.obj");if(!tr)return;open=open===tr.dataset.n?null:tr.dataset.n;render()});
document.querySelectorAll("#objs th[data-k]").forEach(th=>th.addEventListener("click",()=>{const k=th.dataset.k;sortD=sortK===k?-sortD:(k==="name"?1:-1);sortK=k;render()}));
["#q","#onlyData","#onlyCustom"].forEach(s=>$(s).addEventListener("input",render));render();
</script></body></html>
"""

"""Command-line interface."""

from __future__ import annotations

import functools
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.progress import Progress as RichProgressBar
from rich.table import Table

from sf_exit import __version__
from sf_exit.auth import SANDBOX_LOGIN, AuthConfig, AuthError, login
from sf_exit.client import SalesforceClient, SalesforceError
from sf_exit.exporter import Exporter, ExportOptions, Progress
from sf_exit.loader import LoadError, load
from sf_exit.utils import human_bytes

console = Console(stderr=True, highlight=False)
stdout = Console(highlight=False)  # query results / diffs: data goes to stdout, chatter to stderr

BANNER = "[bold cyan]sf-exit[/] [dim]v{v}[/]  [dim]·  your Salesforce data, out.[/]"


# ----------------------------------------------------------------- helpers
def auth_options(fn):
    opts = [
        click.option("--org", "-o", envvar=["SF_ORG", "SF_TARGET_ORG"],
                     help="Salesforce CLI org alias or username (uses `sf org display`)."),
        click.option("--instance-url", envvar="SF_INSTANCE_URL",
                     help="e.g. https://acme.my.salesforce.com"),
        click.option("--access-token", envvar="SF_ACCESS_TOKEN", help="An existing session/access token."),
        click.option("--client-id", envvar="SF_CLIENT_ID", help="Connected/External Client App consumer key."),
        click.option("--client-secret", envvar="SF_CLIENT_SECRET", help="Consumer secret."),
        click.option("--username", "-u", envvar="SF_USERNAME", help="Username (password or JWT flow)."),
        click.option("--password", envvar="SF_PASSWORD",
                     help="Password + security token (username-password flow)."),
        click.option("--jwt-key-file", envvar="SF_JWT_KEY_FILE", type=click.Path(exists=True),
                     help="Private key for the JWT bearer flow."),
        click.option("--login-url", envvar="SF_LOGIN_URL", default="https://login.salesforce.com",
                     show_default=True, help="Login host for password/JWT flows."),
        click.option("--sandbox", is_flag=True, help="Shortcut for --login-url https://test.salesforce.com"),
        click.option("--api-version", envvar="SF_API_VERSION",
                     help="Pin an API version like 62.0 (default: newest the org supports)."),
    ]
    for opt in reversed(opts):
        fn = opt(fn)
    return fn


def make_client(kw: Dict) -> SalesforceClient:
    cfg = AuthConfig(
        access_token=kw.pop("access_token"), instance_url=kw.pop("instance_url"), org=kw.pop("org"),
        client_id=kw.pop("client_id"), client_secret=kw.pop("client_secret"),
        username=kw.pop("username"), password=kw.pop("password"),
        jwt_key_file=kw.pop("jwt_key_file"), login_url=kw.pop("login_url"),
    )
    if kw.pop("sandbox"):
        cfg.login_url = SANDBOX_LOGIN
    api_version = kw.pop("api_version")
    session = login(cfg)
    return SalesforceClient(session.instance_url, session.access_token, api_version=api_version,
                            refresh=session.refresh)


def handle_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (AuthError, LoadError, FileNotFoundError) as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(2)
        except SalesforceError as exc:
            console.print(f"[bold red]Salesforce error:[/] {exc}")
            sys.exit(2)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/] Run the same command again to resume where you left off.")
            sys.exit(130)
    return wrapper


class RichProgress(Progress):
    def __init__(self, con: Console, verbose: bool = False):
        self.console = con
        self.verbose = verbose
        self.bar = RichProgressBar(
            SpinnerColumn(),
            TextColumn("[bold]{task.description:<32}"),
            BarColumn(bar_width=28),
            TextColumn("{task.fields[detail]}"),
            TimeElapsedColumn(),
            console=con,
            transient=True,
        )
        self.tasks: Dict[str, TaskID] = {}
        self.done: Dict[str, int] = {}
        self.totals: Dict[str, Optional[int]] = {}
        self.byte_tasks = set()

    def __enter__(self):
        self.bar.start()
        return self

    def __exit__(self, *exc):
        self.bar.stop()

    def phase(self, text: str) -> None:
        self.console.print(f"[cyan]›[/] {text}")

    def start(self, key: str, label: str, total: Optional[int]) -> None:
        self.done[key] = 0
        self.totals[key] = total
        if key.startswith("obj:"):
            detail = f"~{total:,} rows" if total else ""
            self.tasks[key] = self.bar.add_task(f"  {label[:30]}", total=total or None, detail=detail)
        else:
            if key == "files":
                self.byte_tasks.add(key)
            detail = human_bytes(total) if key == "files" and total else ""
            self.tasks[key] = self.bar.add_task(label, total=total, detail=detail)

    def advance(self, key: str, n: int) -> None:
        tid = self.tasks.get(key)
        if tid is None:
            return
        self.done[key] = self.done.get(key, 0) + n
        if key.startswith("obj:"):
            self.bar.update(tid, advance=n, detail=f"{self.done[key]:,} rows")
        elif key in self.byte_tasks:
            self.bar.update(tid, advance=n,
                            detail=f"{human_bytes(self.done[key])} / {human_bytes(self.totals.get(key) or 0)}")
        else:
            self.bar.update(tid, advance=n)

    def finish(self, key: str, status: str, detail: str = "") -> None:
        tid = self.tasks.pop(key, None)
        if tid is None:
            return
        if key.startswith("obj:"):
            self.bar.remove_task(tid)
            name = key[4:]
            if status == "failed":
                self.console.print(f"  [red]✗[/] {name:<40} [red]{detail[:160]}[/]")
            elif status in ("skipped", "retry"):
                if self.verbose and status == "skipped":
                    self.console.print(f"  [dim]– {name:<40} not exportable: {detail[:120]}[/]")
            elif self.verbose or not detail.startswith("0 rows"):
                self.console.print(f"  [green]✓[/] {name:<40} [dim]{detail}[/]")
        else:
            done = self.done.get(key) or 1
            self.bar.update(tid, total=done, completed=done)

    def log(self, text: str) -> None:
        self.console.print(text)


def _print_org(sf: SalesforceClient) -> None:
    org = sf.org_info()
    sandbox = " [yellow](sandbox)[/]" if org.is_sandbox else ""
    who = f" · {org.username}" if org.username else ""
    console.print(
        f"[green]Connected[/] to [bold]{org.name}[/]{sandbox} · {org.edition}{who} · API v{sf.api_version}"
    )


# -------------------------------------------------------------------- root
@click.group(context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100})
@click.version_option(__version__, "-V", "--version", prog_name="sf-exit")
@click.option("-v", "--verbose", is_flag=True, help="Show every object, including empty ones, and debug logs.")
@click.pass_context
def cli(ctx, verbose):
    """sf-exit: move all of an organisation's data out of Salesforce.

    \b
    Quick start:
      sf org login web --alias my-org            # log in once with the Salesforce CLI
      sf-exit plan   --org my-org                # what will be exported (read-only)
      sf-exit export --org my-org -d ./export    # records + files (+ --metadata)
      sf-exit audit  ./export                    # data dictionary + org health report
      sf-exit sql    ./export "SELECT ..."       # query it instantly
      sf-exit load   ./export --to duckdb:///salesforce.duckdb

    \b
    Backups:
      sf-exit export --org my-org -d ./backup --incremental   # only what changed
      sf-exit diff ./backup-monday ./backup-tuesday            # what changed between two
    """
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)


# ------------------------------------------------------------------ export
@cli.command()
@auth_options
@click.option("-d", "--out", "out_dir", default="./sf-export", show_default=True,
              type=click.Path(file_okay=False), help="Folder to write the export into.")
@click.option("--include", "-i", multiple=True, metavar="PATTERN",
              help="Only these objects (glob, repeatable): -i Account -i 'Invoice*__c'.")
@click.option("--exclude", "-x", multiple=True, metavar="PATTERN",
              help="Skip these objects (glob, repeatable): -x '*History' -x '*Share'.")
@click.option("--include-deleted", is_flag=True,
              help="Also export deleted (Recycle Bin) and archived records (uses queryAll).")
@click.option("--no-files", is_flag=True, help="Skip downloading files, attachments and documents.")
@click.option("--files-only", is_flag=True, help="Only download files (skip record CSVs).")
@click.option("--all-file-versions", is_flag=True, help="Download every ContentVersion, not only the latest.")
@click.option("--engine", type=click.Choice(["auto", "bulk", "rest"]), default="auto", show_default=True,
              help="auto = Bulk API 2.0 for big objects, REST for small ones.")
@click.option("--workers", default=4, show_default=True, help="Objects exported in parallel.")
@click.option("--file-workers", default=8, show_default=True, help="Parallel file downloads.")
@click.option("--fresh", is_flag=True, help="Ignore previous progress and re-export everything.")
@click.option("--incremental", is_flag=True,
              help="Only fetch records changed or deleted since the last export into this folder.")
@click.option("--metadata", is_flag=True,
              help="Also export configuration (Apex, Flows, layouts, reports, permissions…).")
@click.option("--metadata-type", "metadata_types", multiple=True, metavar="TYPE",
              help="Only these metadata types (glob, repeatable): --metadata-type 'Apex*'.")
@click.option("--redact", multiple=True, metavar="OBJECT.FIELD",
              help="Hash these fields (glob, repeatable): --redact 'Contact.Email' --redact '*.Phone'.")
@click.option("--redact-pii", is_flag=True,
              help="Hash every email and phone field plus common PII (birthdate, SSN, street…).")
@click.option("--audit", "run_audit_after", is_flag=True,
              help="Build the audit report (data dictionary + findings) when the export finishes.")
@click.option("--load", "load_to", metavar="TARGET",
              help="After exporting, load into e.g. duckdb:///sf.duckdb (see `sf-exit load`).")
@click.pass_context
@handle_errors
def export(ctx, out_dir, include, exclude, include_deleted, no_files, files_only, all_file_versions,
           engine, workers, file_workers, fresh, incremental, metadata, metadata_types, redact,
           redact_pii, run_audit_after, load_to, **auth):
    """Export every record, file and attachment to a folder of CSVs + files.

    Safe to interrupt: re-running the same command resumes and skips finished objects.
    """
    console.print(BANNER.format(v=__version__))
    sf = make_client(auth)
    _print_org(sf)
    opts = ExportOptions(
        out_dir=Path(out_dir), include=include, exclude=exclude, include_deleted=include_deleted,
        engine=engine, workers=workers, file_workers=file_workers, files=not no_files,
        data=not files_only, all_file_versions=all_file_versions, fresh=fresh,
        incremental=incremental, metadata=metadata or bool(metadata_types),
        metadata_include=metadata_types, redact=redact, redact_pii=redact_pii,
        redact_salt=os.environ.get("SF_EXIT_REDACT_SALT"),
    )
    t0 = time.time()
    with RichProgress(console, verbose=ctx.obj["verbose"]) as progress:
        result = Exporter(sf, opts, progress).run()
    elapsed = time.time() - t0

    done = [o for o in result.objects.values() if o.status == "done"]
    files = sum(v["count"] for v in result.files.values())
    fbytes = sum(v["bytes"] for v in result.files.values())
    lines = [f"[bold green]Export complete[/] in {_dur(elapsed)}", "",
             f"  Records   [bold]{result.total_rows:,}[/] from {len(done):,} objects"]
    if result.incremental_since:
        changed = sum(o.changed or 0 for o in done)
        lines.append(f"  Changed   [bold]{changed:,}[/] records since {result.incremental_since}")
    if opts.files:
        lines.append(f"  Files     [bold]{files:,}[/] ({human_bytes(fbytes)})")
    if result.metadata and "components" in result.metadata:
        lines.append(f"  Metadata  [bold]{result.metadata['components']:,}[/] components, "
                     f"{result.metadata['files']:,} files in metadata/")
    redacted = sum(len(o.redacted) for o in done)
    if redacted:
        lines.append(f"  Redacted  {redacted:,} fields hashed")
    lines += [f"  API calls {result.api_calls:,}", f"  Output    [bold]{out_dir}[/]  (see REPORT.md)"]
    summary = "\n".join(lines)
    unexportable = sum(1 for o in result.objects.values() if o.status == "skipped")
    if unexportable:
        summary += (f"\n  [dim]{unexportable + len(result.skipped)} system objects skipped "
                    "(Salesforce doesn't allow exporting them), listed in REPORT.md[/]")
    if result.failed:
        summary += f"\n\n  [red]{len(result.failed)} object(s) failed[/]; re-run to retry, details in REPORT.md"
    if result.file_errors:
        summary += f"\n  [red]{result.file_errors} file(s) failed[/]; see files/index.csv"
    console.print(Panel.fit(summary, border_style="green" if not result.failed else "yellow"))

    if run_audit_after and opts.data:
        ctx.invoke(audit, export_dir=out_dir, out=None, open_report=False)
    if load_to:
        ctx.invoke(load_cmd, export_dir=out_dir, to=load_to, lowercase=False, replace=True, only=())
    elif not result.failed and opts.data:
        console.print(f"Next: [bold]sf-exit audit {out_dir}[/] · [bold]sf-exit sql {out_dir}[/] · "
                      f"[bold]sf-exit load {out_dir} --to duckdb:///sf.duckdb[/]")
    if result.failed:
        sys.exit(1)


def _dur(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    m, s = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


# -------------------------------------------------------------------- plan
@cli.command()
@auth_options
@click.option("--include", "-i", multiple=True, metavar="PATTERN")
@click.option("--exclude", "-x", multiple=True, metavar="PATTERN")
@click.option("--top", default=20, show_default=True, help="How many of the largest objects to list.")
@handle_errors
def plan(include, exclude, top, **auth):
    """Dry run: what would be exported, how big it is, and whether your API limits can take it."""
    from sf_exit.discovery import select_objects

    console.print(BANNER.format(v=__version__))
    sf = make_client(auth)
    _print_org(sf)
    chosen, skipped = select_objects(sf.describe_global(), include, exclude)
    names = [o["name"] for o in chosen]
    counts = sf.record_counts(names)
    total_rows = sum(counts.values())

    table = Table(title=f"Largest of {len(names):,} exportable objects", title_justify="left")
    table.add_column("Object")
    table.add_column("≈ Rows", justify="right")
    table.add_column("Engine")
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:top]:
        table.add_row(name, f"{n:,}", "bulk" if n >= 10_000 else "rest")
    console.print(table)

    files = Table(title="Files", title_justify="left")
    files.add_column("Kind")
    files.add_column("Count", justify="right")
    files.add_column("Size", justify="right")
    for kind, soql in [
        ("ContentVersion (latest)", "SELECT COUNT(Id) c, SUM(ContentSize) s FROM ContentVersion WHERE IsLatest = true"),
        ("Attachment", "SELECT COUNT(Id) c, SUM(BodyLength) s FROM Attachment"),
        ("Document", "SELECT COUNT(Id) c, SUM(BodyLength) s FROM Document"),
    ]:
        try:
            row = next(iter(sf.query(soql)), {})
            files.add_row(kind, f"{int(row.get('c') or 0):,}", human_bytes(float(row.get("s") or 0)))
        except SalesforceError as exc:
            files.add_row(kind, "?", f"[dim]{exc.message[:50]}[/]")
    console.print(files)

    bulk_objs = sum(1 for n in counts.values() if n >= 10_000)
    est_calls = len(names) * 2 + bulk_objs * 10 + sum(
        n // 2000 + 1 for n in counts.values() if n < 10_000)
    lim = _limits(sf)
    console.print(
        f"\n≈ [bold]{total_rows:,}[/] records in {len(names):,} objects "
        f"([dim]{len(skipped)} skipped, run with -v for reasons[/]).\n"
        f"≈ [bold]{est_calls:,}[/] API calls for records + 1 per file. {lim}"
    )
    if logging.getLogger().level == logging.DEBUG:
        for n, why in sorted(skipped.items()):
            console.print(f"  [dim]skip {n}: {why}[/]")
    console.print("\nNothing was changed or exported. Run [bold]sf-exit export[/] with the same options to go.")


def _limits(sf: SalesforceClient) -> str:
    try:
        lim = sf.limits()
    except SalesforceError:
        return ""
    api = lim.get("DailyApiRequests", {})
    bulk = lim.get("DailyBulkV2QueryJobs", {})
    parts = []
    if api:
        parts.append(f"Daily API calls remaining: [bold]{api.get('Remaining', 0):,}[/] / {api.get('Max', 0):,}.")
    if bulk:
        parts.append(f"Bulk query jobs remaining: {bulk.get('Remaining', 0):,}.")
    return " ".join(parts)


# -------------------------------------------------------------------- load
@cli.command("load")
@click.argument("export_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--to", required=True, metavar="TARGET",
              help="sqlite:///sf.db · duckdb:///sf.duckdb · postgresql://user:pw@host/db · parquet:///dir")
@click.option("--lowercase", is_flag=True, help="Use lowercase table/column names (nicer in Postgres).")
@click.option("--replace", is_flag=True, help="Drop and recreate tables that already exist.")
@click.option("--only", multiple=True, metavar="OBJECT", help="Only load these objects (repeatable).")
@handle_errors
def load_cmd(export_dir, to, lowercase, replace, only):
    """Load an export folder into SQLite, DuckDB, Postgres or Parquet, with proper column types."""
    console.print(f"[cyan]›[/] Loading [bold]{export_dir}[/] into [bold]{to}[/]")
    total = {"rows": 0, "tables": 0}

    def on_table(name: str, rows: int) -> None:
        total["rows"] += rows
        total["tables"] += 1
        if rows:
            console.print(f"  [green]✓[/] {name:<40} [dim]{_rows(rows)}[/]")

    t0 = time.time()
    load(Path(export_dir), to, lowercase=lowercase, replace=replace, only=list(only) or None,
         on_table=on_table)
    console.print(
        f"[bold green]Loaded[/] {total['rows']:,} rows into {total['tables']:,} tables in {_dur(time.time() - t0)}."
    )


# ------------------------------------------------------------------ verify
@cli.command()
@click.argument("export_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--live", is_flag=True, help="Also compare row counts against the org right now.")
@auth_options
@handle_errors
def verify(export_dir, live, **auth):
    """Check an export: CSV row counts, SHA-256 of every file, and (with --live) counts in the org."""
    from sf_exit.verify import verify_live, verify_offline

    path = Path(export_dir)
    checks = verify_offline(path)
    table = Table(title="Export integrity", title_justify="left")
    for col, j in (("Check", "left"), ("Expected", "right"), ("Found", "right"), ("", "left")):
        table.add_column(col, justify=j)
    bad = [c for c in checks if not c.ok]
    for c in (bad or [])[:50]:
        table.add_row(c.name, _n(c.expected), _n(c.actual), f"[red]✗ {c.detail}[/]")
    if bad:
        console.print(table)
    console.print(
        f"{'[green]✓[/]' if not bad else '[red]✗[/]'} {len(checks) - len(bad):,}/{len(checks):,} "
        "offline checks passed (row counts + SHA-256)."
    )
    drift = []
    if live:
        sf = make_client(auth)
        _print_org(sf)
        lc = verify_live(sf, path)
        drift = [c for c in lc if not c.ok]
        if drift:
            t = Table(title="Differences vs live org", title_justify="left")
            for col in ("Object", "Exported", "Live now", "Note"):
                t.add_column(col)
            for c in drift[:100]:
                t.add_row(c.name, _n(c.expected), _n(c.actual), c.detail)
            console.print(t)
        console.print(
            f"{'[green]✓[/]' if not drift else '[yellow]![/]'} {len(lc) - len(drift):,}/{len(lc):,} "
            "objects match the live org exactly."
        )
    sys.exit(1 if bad else 0)


def _rows(n: int) -> str:
    return f"{n:,} row" + ("" if n == 1 else "s")


def _n(v: Optional[int]) -> str:
    return "–" if v is None else f"{v:,}"


# ------------------------------------------------------------------ doctor
@cli.command()
@auth_options
@handle_errors
def doctor(**auth):
    """Check your login has the permissions a complete export needs."""
    console.print(BANNER.format(v=__version__))
    sf = make_client(auth)
    _print_org(sf)
    rows = []

    try:
        ident = sf.get_json(f"{sf.instance_url}/services/oauth2/userinfo")
        user_id = ident.get("user_id")
    except SalesforceError:
        user_id = None
    perms: Dict[str, bool] = {}
    if user_id:
        try:
            soql = (
                "SELECT PermissionsViewAllData, PermissionsQueryAllFiles, PermissionsApiEnabled "
                "FROM PermissionSet WHERE Id IN (SELECT PermissionSetId FROM PermissionSetAssignment "
                f"WHERE AssigneeId = '{user_id}')"
            )
            for r in sf.query(soql):
                for k, v in r.items():
                    perms[k] = perms.get(k, False) or bool(v)
        except SalesforceError:
            pass

    def check(label, ok, why):
        rows.append((label, ok, why))

    check("API access", True, "Connected and queried the org.")
    if perms:
        check("View All Data", perms.get("PermissionsViewAllData", False),
              "Without it you only export records your user can see.")
        check("Query All Files", perms.get("PermissionsQueryAllFiles", False),
              "Without it, files not shared with you are silently skipped.")
    else:
        check("Permissions", None, "Could not read your permission sets; ask an admin to confirm "
              "'View All Data' and 'Query All Files'.")
    try:
        lim = sf.limits()
        api = lim.get("DailyApiRequests", {})
        check("Daily API calls", api.get("Remaining", 0) > 1000,
              f"{api.get('Remaining', 0):,} of {api.get('Max', 0):,} remaining today.")
        bulk = lim.get("DailyBulkV2QueryJobs")
        if bulk:
            check("Bulk API 2.0 query jobs", bulk.get("Remaining", 0) > 50,
                  f"{bulk.get('Remaining', 0):,} of {bulk.get('Max', 0):,} remaining.")
    except SalesforceError as exc:
        check("Limits", None, f"Could not read limits: {exc}")

    table = Table(show_header=False, box=None, padding=(0, 1))
    for label, ok, why in rows:
        icon = "[green]✓[/]" if ok else ("[yellow]?[/]" if ok is None else "[red]✗[/]")
        table.add_row(icon, f"[bold]{label}[/]", f"[dim]{why}[/]")
    console.print(table)
    if any(ok is False for _, ok, _ in rows):
        sys.exit(1)


# ------------------------------------------------------------------- audit
@cli.command()
@click.argument("export_dir", type=click.Path(exists=True, file_okay=False))
@click.option("-o", "--out", type=click.Path(file_okay=False),
              help="Where to write the report (default: <export>/audit).")
@click.option("--open", "open_report", is_flag=True, help="Open the HTML report in your browser.")
@handle_errors
def audit(export_dir, out, open_report):
    """Data dictionary + org health report (unused fields, duplicates, stale pipeline…).

    Works offline on an export folder: no Salesforce connection, nothing leaves your machine.
    """
    from sf_exit.audit import run_audit

    console.print(f"[cyan]›[/] Auditing [bold]{export_dir}[/]")
    with console.status("Reading every record…"):
        report = run_audit(Path(export_dir), Path(out) if out else None)
    s = report["summary"]
    target = Path(out) if out else Path(export_dir) / "audit"
    sev_style = {"high": "red", "medium": "yellow", "low": "blue", "info": "dim"}
    table = Table(show_header=False, box=None, padding=(0, 1))
    for f in report["findings"][:12]:
        st = sev_style[f["severity"]]
        table.add_row(f"[{st}]●[/]", f"[{st}]{f['severity'].upper():<6}[/]", f["title"])
    console.print(
        f"[bold]{s['records']:,}[/] records · [bold]{s['objects_with_data']:,}[/] objects with data · "
        f"[bold]{s['custom_fields']:,}[/] custom fields"
    )
    if report["findings"]:
        console.print(table)
    else:
        console.print("[green]No findings. Tidy org![/]")
    console.print(
        f"\n[bold green]Report:[/] {target / 'index.html'}\n"
        f"        [dim]{target / 'data-dictionary.csv'} · {target / 'findings.md'} · "
        f"{target / 'audit.json'}[/]"
    )
    if open_report:
        import webbrowser

        webbrowser.open((target / "index.html").resolve().as_uri())


# --------------------------------------------------------------------- sql
@cli.command("sql")
@click.argument("export_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("query", required=False)
@click.option("--format", "fmt", type=click.Choice(["table", "csv", "json"]), default="table",
              show_default=True)
@click.option("--limit", default=100, show_default=True, help="Max rows to show in table format (0 = all).")
@handle_errors
def sql_cmd(export_dir, query, fmt, limit):
    """Query an export with SQL immediately (DuckDB, no loading step).

    \b
      sf-exit sql ./export "SELECT StageName, sum(Amount) FROM Opportunity GROUP BY 1"
      sf-exit sql ./export                      # interactive prompt; .tables lists tables
    """
    from sf_exit import sql as sqlmod

    con = sqlmod.connect(Path(export_dir))
    if query:
        try:
            result = sqlmod.run(con, query, 0 if fmt != "table" else limit)
        except Exception as exc:  # noqa: BLE001 - DuckDB raises many error types
            console.print(f"[bold red]SQL error:[/] {exc}")
            console.print("[dim]Tables are named after objects (quote mixed case: \"User\"). "
                          f"List them with: sf-exit sql {export_dir} then .tables[/]")
            sys.exit(2)
        _print_result(*result, fmt=fmt, limit=limit)
        return
    console.print("[bold cyan]sf-exit sql[/] · DuckDB over your export. End statements with [bold];[/]"
                  " · [bold].tables[/] · [bold].quit[/]")
    buf = ""
    while True:
        try:
            line = input("sf-exit> " if not buf else "    ...> ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        cmd = line.strip()
        if not buf and cmd in (".quit", ".exit", "quit", "exit"):
            return
        if not buf and cmd == ".tables":
            t = Table(show_header=True)
            t.add_column("Table")
            t.add_column("Columns", justify="right")
            for name, ncols in sqlmod.tables(con):
                t.add_row(name, str(ncols))
            console.print(t)
            continue
        buf += line + "\n"
        if cmd.endswith(";"):
            try:
                _print_result(*sqlmod.run(con, buf, limit), fmt="table", limit=limit)
            except Exception as exc:  # noqa: BLE001 - show any SQL error and keep the prompt
                console.print(f"[red]{exc}[/]")
            buf = ""


def _cell(v) -> str:
    if v is None:
        return "[dim]NULL[/]"
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.0f}" if v.is_integer() else f"{v:,.2f}"
    return str(v).replace("[", "\\[")


def _print_result(cols, rows, fmt="table", limit=100):
    if fmt == "csv":
        import csv as _csv

        w = _csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(cols)
        w.writerows(rows)
        return
    if fmt == "json":
        click.echo(json.dumps([dict(zip(cols, r, strict=False)) for r in rows], default=str, indent=1))
        return
    if not cols:
        console.print("[dim]OK[/]")
        return
    t = Table(show_header=True, header_style="bold")
    numeric = [bool(rows) and all(isinstance(r[i], (int, float)) and not isinstance(r[i], bool)
                                  for r in rows if r[i] is not None) for i in range(len(cols))]
    for c, num in zip(cols, numeric, strict=False):
        t.add_column(str(c), justify="right" if num else "left")
    for r in rows:
        t.add_row(*[_cell(v) for v in r])
    stdout.print(t)
    if limit and len(rows) >= limit:
        console.print(f"[dim](first {limit} rows; use --limit 0 for all)[/]")


# -------------------------------------------------------------------- diff
@cli.command()
@click.argument("old_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("new_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--details", type=click.Path(file_okay=False), metavar="DIR",
              help="Write one CSV per object listing every added/removed/changed record and field.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--only", multiple=True, metavar="OBJECT", help="Only compare these objects.")
@handle_errors
def diff(old_dir, new_dir, details, as_json, only):
    """What changed between two exports (e.g. last night's backup vs tonight's)."""
    from dataclasses import asdict

    from sf_exit.diff import diff_exports

    with console.status("Comparing exports…"):
        diffs = diff_exports(Path(old_dir), Path(new_dir), Path(details) if details else None,
                             list(only) or None)
    touched = [d for d in diffs if d.touched]
    if as_json:
        click.echo(json.dumps([asdict(d) for d in touched], indent=1))
        return
    if not touched:
        console.print("[green]No differences.[/] Every object has identical records.")
        return
    t = Table(title=f"{len(touched)} of {len(diffs)} objects changed", title_justify="left")
    for col, j in (("Object", "left"), ("Added", "right"), ("Removed", "right"),
                   ("Changed", "right"), ("Most-changed fields", "left")):
        t.add_column(col, justify=j)
    for d in sorted(touched, key=lambda d: -(d.added + d.removed + d.changed)):
        note = ", ".join(f"{k} ({v:,})" for k, v in list(d.changed_fields.items())[:3])
        if d.status != "both":
            note = f"[yellow]{d.status}[/]"
        elif d.fields_added or d.fields_removed:
            note = " ".join(filter(None, [
                ("+" + ", +".join(d.fields_added)) if d.fields_added else "",
                ("-" + ", -".join(d.fields_removed)) if d.fields_removed else "", note]))
        t.add_row(d.name, f"[green]{d.added:,}[/]" if d.added else "",
                  f"[red]{d.removed:,}[/]" if d.removed else "",
                  f"[yellow]{d.changed:,}[/]" if d.changed else "", note)
    stdout.print(t)
    if details:
        console.print(f"Per-record details in [bold]{details}[/]")


# ---------------------------------------------------------------- metadata
@cli.command("metadata")
@auth_options
@click.option("-d", "--out", "out_dir", default="./sf-export", show_default=True,
              type=click.Path(file_okay=False))
@click.option("--type", "types", multiple=True, metavar="TYPE",
              help="Only these metadata types (glob, repeatable): --type ApexClass --type 'Flow*'.")
@click.option("--exclude-type", "exclude_types", multiple=True, metavar="TYPE")
@handle_errors
def metadata_cmd(out_dir, types, exclude_types, **auth):
    """Export configuration only: Apex, Flows, objects, layouts, reports, permission sets…

    Writes a deployable Metadata API folder to <out>/metadata (use `sf project deploy start
    --metadata-dir` or `sf project convert mdapi` with it).
    """
    from sf_exit.metadata import export_metadata

    console.print(BANNER.format(v=__version__))
    sf = make_client(auth)
    _print_org(sf)
    with console.status("Listing metadata types…") as status:
        res = export_metadata(sf, Path(out_dir), types, exclude_types,
                              on_progress=lambda m: status.update(m))
    console.print(
        f"[bold green]Metadata exported:[/] {res.components:,} named components across "
        f"{res.types:,} types, {res.files:,} files → [bold]{Path(out_dir) / 'metadata'}[/]"
    )
    if res.failed_types:
        console.print(f"[yellow]{len(res.failed_types)} type(s) could not be retrieved[/]: "
                      + ", ".join(sorted(res.failed_types)) + " (see metadata/retrieve-warnings.txt)")


def main() -> None:
    cli(obj={})


if __name__ == "__main__":  # pragma: no cover
    main()

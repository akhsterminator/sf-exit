"""Ways to log in to Salesforce.

Resolution order (first one that has enough information wins):

1. ``--access-token`` + ``--instance-url``      (any existing session)
2. ``--org ALIAS``                               (reuse a Salesforce CLI login: `sf org login web`)
3. ``--jwt-key-file`` + ``--client-id`` + ``--username``   (JWT bearer flow, headless/CI)
4. ``--username`` + ``--password`` + ``--client-id``       (OAuth username-password flow)
5. ``--client-id`` + ``--client-secret`` + ``--instance-url`` (OAuth client-credentials flow)
6. nothing at all → the Salesforce CLI's *default* org, if one is set
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

import requests

from sf_exit.client import SalesforceError

PRODUCTION_LOGIN = "https://login.salesforce.com"
SANDBOX_LOGIN = "https://test.salesforce.com"


class AuthError(Exception):
    pass


@dataclass
class AuthConfig:
    access_token: Optional[str] = None
    instance_url: Optional[str] = None
    org: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    jwt_key_file: Optional[str] = None
    login_url: str = PRODUCTION_LOGIN


@dataclass
class Session:
    instance_url: str
    access_token: str
    method: str
    refresh: Optional[Callable[[], str]] = None


def _token_request(url: str, data: dict) -> dict:
    resp = requests.post(url.rstrip("/") + "/services/oauth2/token", data=data, timeout=60)
    if resp.status_code >= 400:
        try:
            body = resp.json()
            msg = f"{body.get('error')}: {body.get('error_description')}"
        except ValueError:
            msg = resp.text[:300]
        raise AuthError(f"Salesforce login failed ({resp.status_code}) {msg}")
    return resp.json()


def _sf_cli_binary() -> Optional[str]:
    return shutil.which("sf") or shutil.which("sfdx")


def from_sf_cli(org: Optional[str]) -> Session:
    binary = _sf_cli_binary()
    if not binary:
        raise AuthError(
            "The Salesforce CLI ('sf') was not found on PATH. Install it from "
            "https://developer.salesforce.com/tools/salesforcecli or pass other credentials."
        )
    cmd = [binary, "org", "display", "--json"]
    if org:
        cmd += ["--target-org", org]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthError(f"Could not run the Salesforce CLI: {exc}") from exc
    try:
        payload = json.loads(out.stdout or "{}")
    except ValueError as exc:
        raise AuthError(f"Unexpected output from '{' '.join(cmd)}': {out.stdout[:300]}") from exc
    result = payload.get("result") or {}
    if payload.get("status", 1) != 0 or not result.get("accessToken"):
        message = payload.get("message") or out.stderr.strip() or "no org found"
        raise AuthError(f"Salesforce CLI could not provide a session for {org or 'the default org'}: {message}")

    def refresh() -> str:
        return from_sf_cli(org).access_token

    return Session(result["instanceUrl"], result["accessToken"], f"sf-cli:{result.get('alias') or result.get('username') or org}", refresh)


def is_sandbox_url(url: str) -> bool:
    """Sandbox and scratch-org login hosts (same rules as the Salesforce CLI)."""
    host = (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()
    return (host == "test.salesforce.com"
            or host.endswith((".sandbox.my.salesforce.com", ".scratch.my.salesforce.com",
                              ".sandbox.lightning.force.com", ".scratch.lightning.force.com"))
            or bool(re.match(r"^[a-z0-9-]+--[a-z0-9-]+\.(?:[a-z0-9-]+\.)?my\.salesforce\.com$", host))
            or bool(re.match(r"^cs\d+\.", host)))


def jwt_audience(login_url: str) -> str:
    """The JWT 'aud' claim: never the My Domain URL, always login. or test.salesforce.com,
    unless overridden with SF_AUDIENCE_URL (e.g. for Government Cloud)."""
    override = os.environ.get("SF_AUDIENCE_URL") or os.environ.get("SFDX_AUDIENCE_URL")
    if override:
        return override.rstrip("/")
    return SANDBOX_LOGIN if is_sandbox_url(login_url) else PRODUCTION_LOGIN


def jwt_flow(cfg: AuthConfig) -> Session:
    try:
        import jwt  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise AuthError("JWT login needs an extra: pip install 'sf-exit[jwt]'") from exc
    with open(cfg.jwt_key_file or "", encoding="utf-8") as fh:
        key = fh.read()
    audience = jwt_audience(cfg.login_url)

    def fetch() -> dict:
        assertion = jwt.encode(
            {"iss": cfg.client_id, "sub": cfg.username, "aud": audience, "exp": int(time.time()) + 180},
            key,
            algorithm="RS256",
        )
        return _token_request(
            cfg.login_url,
            {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        )

    tok = fetch()
    return Session(tok["instance_url"], tok["access_token"], "jwt", lambda: fetch()["access_token"])


def password_flow(cfg: AuthConfig) -> Session:
    def fetch() -> dict:
        data = {
            "grant_type": "password",
            "client_id": cfg.client_id,
            "username": cfg.username,
            "password": cfg.password,
        }
        if cfg.client_secret:
            data["client_secret"] = cfg.client_secret
        return _token_request(cfg.login_url, data)

    tok = fetch()
    return Session(tok["instance_url"], tok["access_token"], "password", lambda: fetch()["access_token"])


def client_credentials_flow(cfg: AuthConfig) -> Session:
    def fetch() -> dict:
        return _token_request(
            cfg.instance_url or cfg.login_url,
            {
                "grant_type": "client_credentials",
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
            },
        )

    tok = fetch()
    return Session(
        tok.get("instance_url") or cfg.instance_url or "",
        tok["access_token"],
        "client-credentials",
        lambda: fetch()["access_token"],
    )


def login(cfg: AuthConfig) -> Session:
    try:
        if cfg.access_token:
            if not cfg.instance_url:
                raise AuthError("--access-token also needs --instance-url (e.g. https://acme.my.salesforce.com)")
            return Session(cfg.instance_url, cfg.access_token, "access-token")
        if cfg.org:
            return from_sf_cli(cfg.org)
        if cfg.jwt_key_file:
            if not (cfg.client_id and cfg.username):
                raise AuthError("JWT login needs --client-id and --username as well as --jwt-key-file")
            return jwt_flow(cfg)
        if cfg.username and cfg.password:
            if not cfg.client_id:
                raise AuthError("Username/password login needs --client-id (a connected app consumer key)")
            return password_flow(cfg)
        if cfg.client_id and cfg.client_secret:
            if not cfg.instance_url:
                raise AuthError("Client-credentials login needs --instance-url (your My Domain URL)")
            return client_credentials_flow(cfg)
        if _sf_cli_binary():
            return from_sf_cli(None)
    except SalesforceError as exc:  # pragma: no cover - defensive
        raise AuthError(str(exc)) from exc
    raise AuthError(
        "No Salesforce credentials given.\n"
        "  Easiest: install the Salesforce CLI, run `sf org login web --alias my-org`,\n"
        "  then `sf-exit export --org my-org`.\n"
        "  See `sf-exit export --help` for token, JWT, password and client-credentials options."
    )

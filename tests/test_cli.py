import json

from click.testing import CliRunner

from sf_exit import cli as cli_mod

from .fake_org import build_org, client_for


def _patch(monkeypatch, org):
    def fake_make_client(kw):
        for k in list(kw):
            kw.pop(k)
        return client_for(org)

    monkeypatch.setattr(cli_mod, "make_client", fake_make_client)


def test_cli_export_load_verify(monkeypatch, tmp_path):
    org = build_org()
    _patch(monkeypatch, org)
    runner = CliRunner()
    out = tmp_path / "exp"
    res = runner.invoke(cli_mod.cli, ["export", "-d", str(out), "--workers", "2"], obj={})
    assert res.exit_code == 0, res.output
    assert "Export complete" in res.output
    assert json.loads((out / "manifest.json").read_text())["totals"]["objects"] >= 10

    res = runner.invoke(cli_mod.cli, ["load", str(out), "--to", f"sqlite:///{tmp_path / 'x.db'}"], obj={})
    assert res.exit_code == 0, res.output
    assert "Loaded" in res.output

    res = runner.invoke(cli_mod.cli, ["verify", str(out), "--live"], obj={})
    assert res.exit_code == 0, res.output
    assert "offline checks passed" in res.output


def test_cli_plan_and_doctor(monkeypatch):
    org = build_org()
    _patch(monkeypatch, org)
    runner = CliRunner()
    res = runner.invoke(cli_mod.cli, ["plan"], obj={})
    assert res.exit_code == 0, res.output
    assert "Contact" in res.output and "Nothing was changed" in res.output
    res = runner.invoke(cli_mod.cli, ["doctor"], obj={})
    assert "API access" in res.output


def test_cli_without_credentials_gives_helpful_error(monkeypatch):
    monkeypatch.setattr("sf_exit.auth._sf_cli_binary", lambda: None)
    for k in ("SF_ORG", "SF_TARGET_ORG", "SF_ACCESS_TOKEN", "SF_CLIENT_ID", "SF_USERNAME"):
        monkeypatch.delenv(k, raising=False)
    res = CliRunner().invoke(cli_mod.cli, ["export"], obj={})
    assert res.exit_code == 2
    assert "sf org login web" in res.output

import argparse
import json
from pathlib import Path

import gremlins.cli as cli


def test_setup_core_does_not_touch_provider_or_clients(monkeypatch, tmp_path: Path, capsys):
    lock = tmp_path / "stack.lock.json"
    lock.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(cli, "write_stack_lock", lambda config: lock)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)

    def forbidden(*args, **kwargs):
        raise AssertionError("core setup must not touch providers, adapters, or clients")

    monkeypatch.setattr(cli, "_ensure_model", forbidden)
    monkeypatch.setattr(cli, "_write_wrapper", forbidden)
    monkeypatch.setattr(cli, "_configure_claude", forbidden)
    monkeypatch.setattr(cli, "_configure_codex", forbidden)

    rc = cli.setup(argparse.Namespace(profile="mac-local"))
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["core"]["ok"] is True
    assert payload["core"]["stack_lock"] == str(lock)


def test_doctor_core_passes_without_ollama_claude_or_codex(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: "/usr/bin/git" if name == "git" else None,
    )
    monkeypatch.setattr(cli, "_wrapper_path", lambda: tmp_path / "missing-gremlins-mcp")

    def provider_must_not_be_called(*args, **kwargs):
        raise AssertionError("doctor must not probe a missing optional provider")

    monkeypatch.setattr(cli, "health", provider_must_not_be_called)

    rc = cli.doctor(argparse.Namespace())
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["core"]["ok"] is True
    assert payload["providers"]["ollama"]["required"] is False
    assert payload["providers"]["ollama"]["available"] is False
    assert payload["clients"]["claude"]["available"] is False
    assert payload["clients"]["codex"]["available"] is False
    assert payload["interfaces"]["mcp"]["required"] is False


def test_deploy_defaults_are_core_only():
    args = cli.build_parser().parse_args(["deploy"])
    assert args.pull_model is False
    assert args.client == []


def test_explicit_setup_commands_parse():
    parser = cli.build_parser()

    core = parser.parse_args(["setup"])
    provider = parser.parse_args(["provider", "setup", "ollama", "--no-pull"])
    adapter = parser.parse_args(["adapter", "install", "mcp"])
    client = parser.parse_args(["adapter", "configure", "claude"])

    assert core.func is cli.setup
    assert provider.func is cli.provider_setup
    assert provider.pull is False
    assert adapter.func is cli.adapter_install
    assert client.func is cli.adapter_configure


def test_install_script_bootstraps_core_only():
    script = (cli.project_root() / "install.sh").read_text(encoding="utf-8")

    assert "ollama" not in script.lower()
    assert " setup --profile " in script
    assert " deploy " not in script

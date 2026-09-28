from pathlib import Path

import gremlins.cli as cli


def test_write_wrapper_preserves_virtualenv_interpreter_path(monkeypatch, tmp_path: Path):
    wrapper = tmp_path / "gremlins-mcp"
    venv_python = tmp_path / ".venv" / "bin" / "python"

    monkeypatch.setattr(cli, "_wrapper_path", lambda: wrapper)
    monkeypatch.setattr(cli.sys, "executable", str(venv_python))

    written = cli._write_wrapper("mac-local")
    content = written.read_text(encoding="utf-8")

    assert written == wrapper
    assert f"exec '{venv_python}' -m gremlins.server" in content

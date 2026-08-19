"""CLI ``mcp-hub`` (R-C5, R-S5): AC-21, AC-69."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from hub.cli import main
from tests.support import catalog_doc, facade_server, native_server, write_catalog


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    try:
        code = main(argv)
    except SystemExit as exc:  # argparse --help/ошибки аргументов
        code = int(exc.code or 0)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


# --- AC-21 -----------------------------------------------------------------


@pytest.mark.ac("AC-21")
def test_catalog_validate_valid_returns_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_catalog(
        tmp_path / "ok.yaml", catalog_doc([facade_server("gitlab"), native_server("tag")])
    )
    code, out = _run(["catalog", "validate", "--path", str(path)], capsys)
    assert code == 0
    assert "OK" in out
    assert "version=1" in out, out  # версия каталога из документа
    assert "servers=2" in out, out  # число загруженных серверов


@pytest.mark.ac("AC-21")
def test_catalog_validate_invalid_returns_1_with_field_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = native_server("tag")
    del broken["title"]
    path = write_catalog(tmp_path / "bad.yaml", catalog_doc([facade_server("gitlab"), broken]))
    code, out = _run(["catalog", "validate", "--path", str(path)], capsys)
    assert code == 1
    assert "servers[1].title" in out
    assert "OK" not in out


@pytest.mark.ac("AC-21")
def test_catalog_validate_missing_file_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run(["catalog", "validate", "--path", str(tmp_path / "nope.yaml")], capsys)
    assert code == 1
    assert "nope.yaml" in out


@pytest.mark.ac("AC-21")
def test_catalog_validate_reports_unconfigured_beta_servers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BETA_URL", raising=False)
    path = write_catalog(
        tmp_path / "beta.yaml",
        catalog_doc(
            [native_server("tag", status="beta", mcp_url="${BETA_URL}"), facade_server("gitlab")]
        ),
    )
    code, out = _run(["catalog", "validate", "--path", str(path)], capsys)
    assert code == 0
    assert "OK" in out
    assert "unconfigured" in out
    assert "tag" in out


@pytest.mark.ac("AC-21")
def test_catalog_validate_uses_hub_catalog_path_env_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_catalog(tmp_path / "env.yaml", catalog_doc([native_server("tag")]))
    monkeypatch.setenv("HUB_CATALOG_PATH", str(path))
    code, out = _run(["catalog", "validate"], capsys)
    assert code == 0
    assert "OK" in out


@pytest.mark.ac("AC-21")
def test_catalog_validate_defaults_to_local_catalog_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, _ = _run(["catalog", "validate"], capsys)
    assert code == 1  # ./catalog.yaml отсутствует
    write_catalog(tmp_path / "catalog.yaml", catalog_doc([native_server("tag")]))
    code, out = _run(["catalog", "validate"], capsys)
    assert code == 0
    assert "OK" in out


@pytest.mark.ac("AC-21")
def test_catalog_validate_needs_no_other_hub_env(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Проверка через реальный процесс: без HUB_* (кроме пути) команда работает."""
    path = write_catalog(tmp_path / "ok.yaml", catalog_doc([native_server("tag")]))
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "hub.cli", "catalog", "validate", "--path", str(path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# --- AC-69 -----------------------------------------------------------------


@pytest.mark.ac("AC-69")
def test_cli_help_lists_serve_and_catalog(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run(["--help"], capsys)
    assert code == 0
    assert "serve" in out
    assert "catalog" in out


@pytest.mark.ac("AC-69")
def test_cli_catalog_help_lists_validate_and_path(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run(["catalog", "--help"], capsys)
    assert code == 0
    assert "validate" in out
    assert "--path" in out
    code, out = _run(["catalog", "validate", "--help"], capsys)
    assert code == 0
    assert "--path" in out


@pytest.mark.ac("AC-69")
def test_cli_serve_help_has_host_and_port(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run(["serve", "--help"], capsys)
    assert code == 0
    assert "--host" in out
    assert "--port" in out


@pytest.mark.ac("AC-69")
def test_cli_serve_passes_host_and_port_to_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    calls: list[dict[str, Any]] = []

    def fake_run(app: Any, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert main(["serve", "--host", "127.0.0.1", "--port", "9010"]) == 0
    assert len(calls) == 1
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 9010
    assert "hub.app" in str(calls[0]["app"])


@pytest.mark.ac("AC-69")
def test_cli_without_command_or_unknown_command_is_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _ = _run([], capsys)
    assert code != 0
    code, _ = _run(["bogus"], capsys)
    assert code != 0


@pytest.mark.ac("AC-69")
def test_installed_entrypoint_mcp_hub_help() -> None:
    entrypoint = Path(sys.executable).parent / "mcp-hub"
    if not entrypoint.exists():
        pytest.skip("console script mcp-hub не установлен в текущем окружении (.venv/bin/mcp-hub)")
    result = subprocess.run(
        [str(entrypoint), "--help"], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0
    assert "serve" in result.stdout and "catalog" in result.stdout
    result = subprocess.run(
        [str(entrypoint), "catalog", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0
    assert "validate" in result.stdout and "--path" in result.stdout

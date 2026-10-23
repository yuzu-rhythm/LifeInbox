from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from life_inbox.cloud_run import (
    CLOUD_RUN_BIND_HOST,
    CLOUD_RUN_DATA_DIR_ENV,
    CLOUD_RUN_DATABASE_ENV,
    CLOUD_RUN_MODE_ENV,
    CLOUD_RUN_PORT_ENV,
    CLOUD_RUN_READ_ONLY_ENV,
    CLOUD_RUN_SQLITE_ACK_ENV,
    CloudRunConfigurationError,
    create_cloud_run_app,
    load_cloud_run_runtime,
    main,
)


def _cloud_environment(tmp_path: Path) -> dict[str, str]:
    return {
        CLOUD_RUN_MODE_ENV: "true",
        CLOUD_RUN_READ_ONLY_ENV: "true",
        CLOUD_RUN_PORT_ENV: "8080",
        CLOUD_RUN_DATABASE_ENV: f"sqlite:///{tmp_path / 'app.db'}",
        CLOUD_RUN_DATA_DIR_ENV: str(tmp_path),
        CLOUD_RUN_SQLITE_ACK_ENV: "true",
    }


def test_cloud_run_runtime_uses_port_and_preserves_loopback_app_setting(tmp_path: Path):
    runtime = load_cloud_run_runtime(_cloud_environment(tmp_path))

    assert runtime.port == 8080
    assert runtime.settings.web_port == 8080
    assert runtime.settings.web_host == "127.0.0.1"
    assert CLOUD_RUN_BIND_HOST == "0.0.0.0"


@pytest.mark.parametrize(
    "missing_name",
    [
        CLOUD_RUN_MODE_ENV,
        CLOUD_RUN_PORT_ENV,
        CLOUD_RUN_DATABASE_ENV,
        CLOUD_RUN_DATA_DIR_ENV,
        CLOUD_RUN_READ_ONLY_ENV,
        CLOUD_RUN_SQLITE_ACK_ENV,
    ],
)
def test_cloud_run_runtime_fails_closed_when_required_setting_is_missing(
    tmp_path: Path,
    missing_name: str,
):
    environment = _cloud_environment(tmp_path)
    environment.pop(missing_name)

    with pytest.raises(CloudRunConfigurationError) as exc_info:
        load_cloud_run_runtime(environment)

    assert exc_info.value.variable == missing_name


def test_cloud_run_runtime_rejects_non_loopback_application_host(tmp_path: Path):
    environment = _cloud_environment(tmp_path)
    environment["WEB_HOST"] = "0.0.0.0"

    with pytest.raises(CloudRunConfigurationError, match="WEB_HOST"):
        load_cloud_run_runtime(environment)


def test_cloud_run_runtime_rejects_disabled_read_only_flag(tmp_path: Path):
    environment = _cloud_environment(tmp_path)
    environment[CLOUD_RUN_READ_ONLY_ENV] = "false"

    with pytest.raises(CloudRunConfigurationError, match=CLOUD_RUN_READ_ONLY_ENV):
        load_cloud_run_runtime(environment)


def test_main_passes_cloud_run_port_and_bind_host_to_uvicorn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    runtime = load_cloud_run_runtime(_cloud_environment(tmp_path))
    calls: dict[str, object] = {}

    monkeypatch.setattr("life_inbox.cloud_run.load_cloud_run_runtime", lambda: runtime)

    def fake_create(resolved_runtime: object) -> object:
        calls["runtime"] = resolved_runtime
        return object()

    monkeypatch.setattr("life_inbox.cloud_run.create_cloud_run_app", fake_create)

    def fake_run(app: object, *, host: str, port: int) -> None:
        calls.update(app=app, host=host, port=port)

    monkeypatch.setattr("uvicorn.run", fake_run)

    assert main() == 0
    assert calls["runtime"] is runtime
    assert calls["host"] == "0.0.0.0"
    assert calls["port"] == 8080


def test_cloud_run_healthz_is_available_without_personal_payload(tmp_path: Path):
    app = create_cloud_run_app(load_cloud_run_runtime(_cloud_environment(tmp_path)))

    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_cloud_run_read_only_middleware_blocks_quick_and_api_writes(tmp_path: Path):
    app = create_cloud_run_app(load_cloud_run_runtime(_cloud_environment(tmp_path)))
    client = TestClient(app)

    blocked_requests = (
        client.post("/quick", data={"capture_type": "memo", "text": "not saved"}),
        client.put("/api/v1/health/sleep/2026-08-30", json={}),
        client.post("/api/v1/rhythm/collection/run", json={}),
        client.patch("/quick"),
        client.delete("/quick"),
    )

    for response in blocked_requests:
        assert response.status_code == 503
        assert response.json() == {
            "error": "cloud_run_read_only",
            "message": "This Cloud Run revision is a read-only snapshot.",
        }
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"


def test_cloud_run_read_only_middleware_keeps_get_and_head_routes_usable(tmp_path: Path):
    app = create_cloud_run_app(load_cloud_run_runtime(_cloud_environment(tmp_path)))
    client = TestClient(app)

    for path in ("/healthz", "/"):
        get_response = client.get(path)
        head_response = client.head(path)
        assert get_response.status_code == 200
        assert head_response.status_code == 200
        assert head_response.content == b""


def test_built_wheel_contains_web_templates_and_static_assets(tmp_path: Path):
    project_root = Path(__file__).resolve().parents[1]
    project_copy = tmp_path / "project"
    project_copy.mkdir()
    shutil.copy2(project_root / "pyproject.toml", project_copy / "pyproject.toml")
    shutil.copy2(project_root / "README.md", project_copy / "README.md")
    shutil.copytree(
        project_root / "src",
        project_copy / "src",
        ignore=shutil.ignore_patterns("*.egg-info", "__pycache__", "*.pyc"),
    )
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()

    build_interpreter = None
    for candidate in (sys.executable, shutil.which("python"), shutil.which("python3")):
        if candidate is None:
            continue
        backend_probe = subprocess.run(
            [candidate, "-c", "import setuptools.build_meta"],
            capture_output=True,
            text=True,
            check=False,
        )
        if backend_probe.returncode == 0:
            build_interpreter = candidate
            break
    if build_interpreter is None:
        pytest.skip("setuptools.build_meta is unavailable for a local wheel build")

    result = subprocess.run(
        [
            build_interpreter,
            "-m",
            "pip",
            "wheel",
            "--no-build-isolation",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
            str(project_copy),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    wheels = sorted(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        members = set(archive.namelist())

    expected_assets = {
        "life_inbox/templates/base.html",
        "life_inbox/templates/dashboard.html",
        "life_inbox/templates/partials/task_row.html",
        "life_inbox/static/style.css",
    }
    assert expected_assets <= members

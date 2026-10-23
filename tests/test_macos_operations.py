from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MACOS_DIR = ROOT / "scripts" / "macos"
MANAGE_SCRIPT = MACOS_DIR / "manage_launch_agents.sh"
STATUS_SCRIPT = MACOS_DIR / "status_launch_agents.sh"
TEMPLATE_NAMES = {
    "com.life-inbox.bot": "com.life-inbox.bot.plist.template",
    "com.life-inbox.web": "com.life-inbox.web.plist.template",
    "com.life-inbox.backup": "com.life-inbox.backup.plist.template",
}
ABSOLUTE_PLACEHOLDERS = {
    "__ABS_PROJECT_DIR__",
    "__ABS_PYTHON_BIN__",
    "__ABS_LOG_DIR__",
}
SECRET_MARKERS = (
    "DISCORD_BOT_TOKEN",
    "OPENAI_API_KEY",
    "PASSWORD",
    "Authorization",
    "Bearer ",
)


def load_template(label: str) -> tuple[str, dict[str, object]]:
    template_path = MACOS_DIR / TEMPLATE_NAMES[label]
    text = template_path.read_text(encoding="utf-8")
    document = plistlib.loads(text.encode("utf-8"))
    return text, document


def render_template(label: str, project_dir: Path) -> dict[str, object]:
    text, _ = load_template(label)
    replacements = {
        "__ABS_PROJECT_DIR__": str(project_dir),
        "__ABS_PYTHON_BIN__": str(project_dir / ".venv" / "bin" / "python"),
        "__ABS_LOG_DIR__": str(project_dir / "logs"),
    }
    for placeholder, replacement in replacements.items():
        text = text.replace(placeholder, replacement)
    assert not any(placeholder in text for placeholder in ABSOLUTE_PLACEHOLDERS)
    return plistlib.loads(text.encode("utf-8"))


def test_templates_use_only_explicit_absolute_path_placeholders() -> None:
    for label, filename in TEMPLATE_NAMES.items():
        template_path = MACOS_DIR / filename
        assert template_path.is_file()
        text, document = load_template(label)

        assert set(re.findall(r"__ABS_[A-Z0-9_]+__", text)).issubset(ABSOLUTE_PLACEHOLDERS)
        assert "~" not in text
        assert "$HOME" not in text
        assert "${HOME}" not in text
        assert not any(marker in text for marker in SECRET_MARKERS)

        arguments = document["ProgramArguments"]
        assert arguments[0] == "__ABS_PYTHON_BIN__"
        assert arguments[1:3] == ["-m", "life_inbox"]
        assert arguments[3] == label.rsplit(".", 1)[-1]
        assert document["WorkingDirectory"] == "__ABS_PROJECT_DIR__"
        assert document["StandardOutPath"].startswith("__ABS_LOG_DIR__/")
        assert document["StandardErrorPath"].startswith("__ABS_LOG_DIR__/")


@pytest.mark.parametrize("label", ["com.life-inbox.bot", "com.life-inbox.web"])
def test_bot_and_web_have_failure_only_keepalive_and_throttling(
    label: str,
) -> None:
    _, document = load_template(label)

    assert document["RunAtLoad"] is True
    assert document["KeepAlive"] == {"SuccessfulExit": False}
    assert document["ThrottleInterval"] == 30


def test_backup_is_daily_and_uses_existing_default_output_contract() -> None:
    _, document = load_template("com.life-inbox.backup")
    arguments = document["ProgramArguments"]

    assert document["RunAtLoad"] is False
    assert document["KeepAlive"] is False
    assert document["StartCalendarInterval"] == {"Hour": 3, "Minute": 15}
    assert document["Comment"].startswith("Default output is __ABS_PROJECT_DIR__/data/backups")
    assert arguments == ["__ABS_PYTHON_BIN__", "-m", "life_inbox", "backup"]
    assert "--output-dir" not in arguments


@pytest.mark.parametrize("label", list(TEMPLATE_NAMES))
def test_rendered_paths_are_absolute_and_scoped_to_project(tmp_path: Path, label: str) -> None:
    project_dir = (tmp_path / "life inbox project").resolve()
    document = render_template(label, project_dir)
    python_bin = project_dir / ".venv" / "bin" / "python"
    log_dir = project_dir / "logs"

    assert document["WorkingDirectory"] == str(project_dir)
    assert document["ProgramArguments"][0] == str(python_bin)
    for key in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
        value = document[key]
        assert isinstance(value, str)
        assert value.startswith("/")
        assert "$" not in value
        assert "~" not in value
    assert document["StandardOutPath"].startswith(f"{log_dir}/")
    assert document["StandardErrorPath"].startswith(f"{log_dir}/")
    assert all(
        not isinstance(value, str) or "__ABS_" not in value
        for value in document.values()
    )


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is unavailable")
def test_manage_default_is_non_mutating_preview(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    python_path = project_dir / ".venv" / "bin" / "python"
    (project_dir / "data" / "backups").mkdir(parents=True)
    python_path.parent.mkdir(parents=True)
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    python_path.chmod(0o755)
    launch_agents_dir = tmp_path / "user" / "Library" / "LaunchAgents"

    result = subprocess.run(
        [
            "/bin/bash",
            str(MANAGE_SCRIPT),
            "--project-dir",
            str(project_dir),
            "--launch-agents-dir",
            str(launch_agents_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert "launchctl was not called" in result.stdout
    assert not launch_agents_dir.exists()
    assert not (project_dir / "logs").exists()


def test_manage_rejects_non_exact_launch_agents_target_without_writing(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    python_path = project_dir / ".venv" / "bin" / "python"
    (project_dir / "data" / "backups").mkdir(parents=True)
    python_path.parent.mkdir(parents=True)
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    python_path.chmod(0o755)
    unsafe_target = tmp_path / "not-launch-agents"

    result = subprocess.run(
        [
            "/bin/bash",
            str(MANAGE_SCRIPT),
            "--project-dir",
            str(project_dir),
            "--launch-agents-dir",
            str(unsafe_target),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "LaunchAgents" in result.stderr
    assert not unsafe_target.exists()


def test_helpers_reject_symlinked_launch_agents_target_without_writing(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    python_path = project_dir / ".venv" / "bin" / "python"
    (project_dir / "data" / "backups").mkdir(parents=True)
    python_path.parent.mkdir(parents=True)
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    python_path.chmod(0o755)

    real_target = tmp_path / "real-target" / "Library" / "LaunchAgents"
    real_target.mkdir(parents=True)
    symlink_target = tmp_path / "user" / "Library" / "LaunchAgents"
    symlink_target.parent.mkdir(parents=True)
    symlink_target.symlink_to(real_target, target_is_directory=True)

    result = subprocess.run(
        [
            "/bin/bash",
            str(MANAGE_SCRIPT),
            "--project-dir",
            str(project_dir),
            "--launch-agents-dir",
            str(symlink_target),
            "--install",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "symlink" in result.stderr
    assert not list(real_target.iterdir())


def test_helpers_are_strict_and_do_not_embed_credentials_or_fixed_sleep() -> None:
    for script_path in (MANAGE_SCRIPT, STATUS_SCRIPT):
        text = script_path.read_text(encoding="utf-8")
        assert "set -euo pipefail" in text
        assert "sleep " not in text
        assert "rm -rf" not in text
        assert "${HOME}" not in text
        assert "$HOME" not in text
        assert not any(marker in text for marker in SECRET_MARKERS)

    manage_text = MANAGE_SCRIPT.read_text(encoding="utf-8")
    assert "INSTALL=0" in manage_text
    assert "--install" in manage_text
    assert "--load" in manage_text
    assert "--unload" in manage_text
    assert "--load requires explicit --install" in manage_text


def test_status_does_not_source_or_print_configuration_files() -> None:
    text = STATUS_SCRIPT.read_text(encoding="utf-8")

    assert "source " not in text
    assert "printenv" not in text
    assert "cat .env" not in text
    assert "cat .env.local" not in text
    assert "launchctl print" in text
    assert ">/dev/null 2>&1" in text


def test_shell_scripts_pass_bash_syntax_check() -> None:
    for script_path in (MANAGE_SCRIPT, STATUS_SCRIPT):
        result = subprocess.run(
            ["/bin/bash", "-n", str(script_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_operations_documentation_covers_safe_lifecycle() -> None:
    text = (ROOT / "docs" / "MACOS_OPERATIONS.md").read_text(encoding="utf-8")
    for phrase in (
        "Dry run / preview",
        "インストールとロード",
        "Unload",
        "Status / diagnostics",
        "Logs",
        "Backup verification",
        "Rollback",
        "Limitations and safety boundaries",
        "インストール、ロード、外部サービスへの接続は行いません",
    ):
        assert phrase in text

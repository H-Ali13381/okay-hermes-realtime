from __future__ import annotations

import os
import stat
import subprocess
import tomllib
import zipfile
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMD_DIR = REPO_ROOT / "systemd"
CONTROLLER_UNIT = SYSTEMD_DIR / "okay-hermes-realtime-controller.service"
WAKEWORD_UNIT = SYSTEMD_DIR / "okay-hermes-realtime-wakeword.service"
INSTALLER = REPO_ROOT / "scripts" / "install_user_services.sh"
CONFIG_EXAMPLE = REPO_ROOT / "config.example.env"
PYPROJECT = REPO_ROOT / "pyproject.toml"

CONTROLLER_EXEC = (
    "%h/.local/share/okay-hermes-realtime/venv/bin/"
    "okay-hermes-realtime-controller"
)
WAKEWORD_EXEC = "%h/.local/lib/okay-hermes-realtime/okay-hermes-realtime-wake-listener"
ACTIVATION_EXEC = (
    "%h/.local/share/okay-hermes-realtime/venv/bin/"
    "okay-hermes-realtime-activation"
)
WAKEWORD_MODEL = (
    "%h/.local/share/okay-hermes-realtime/models/"
    "okay-hermes-realtime-wakeword.onnx"
)
SOCKET_ENV = "ACTIVATION_SOCKET_PATH=%t/okay-hermes-realtime/activation.sock"
CAPTURE_HEALTH = "%h/.local/state/okay-hermes-realtime/capture-health"


def _read_lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def _find_execstart(lines: Iterable[str]) -> str:
    for line in lines:
        if line.startswith("ExecStart="):
            return line[len("ExecStart=") :]
    raise AssertionError("missing ExecStart")


def test_service_unit_files_exist():
    assert SYSTEMD_DIR.is_dir()
    assert CONTROLLER_UNIT.is_file()
    assert WAKEWORD_UNIT.is_file()


def test_service_units_do_not_reference_okay_hermes_voice_artifacts():
    legacy_markers = [
        "hermes-wakeword.service",
        "okay-hermes-voice",
        "~/.hermes/wakeword/config.yaml",
    ]

    for unit in [CONTROLLER_UNIT, WAKEWORD_UNIT]:
        text = unit.read_text()
        for marker in legacy_markers:
            assert marker not in text


def test_controller_unit_contract_and_socket_layout():
    lines = _read_lines(CONTROLLER_UNIT)

    assert "[Service]" in lines
    assert "RuntimeDirectory=okay-hermes-realtime" in lines
    assert "RuntimeDirectoryMode=0700" in lines
    assert "StateDirectory=okay-hermes-realtime" in lines
    assert "StateDirectoryMode=0700" in lines

    exec_cmd = _find_execstart(lines)
    assert CONTROLLER_EXEC in exec_cmd
    assert any(SOCKET_ENV in line for line in lines)


def test_wakeword_unit_contract_and_args():
    lines = _read_lines(WAKEWORD_UNIT)

    requires_line = next((line for line in lines if line.startswith("Requires=")), None)
    assert requires_line is not None
    assert "okay-hermes-realtime-controller.service" in requires_line
    assert "pipewire.service" in requires_line
    assert "wireplumber.service" in requires_line

    after_line = next((line for line in lines if line.startswith("After=")), None)
    assert after_line is not None
    for item in (
        "okay-hermes-realtime-controller.service",
        "pipewire.service",
        "wireplumber.service",
    ):
        assert item in after_line

    exec_cmd = _find_execstart(lines)
    assert WAKEWORD_EXEC in exec_cmd
    assert WAKEWORD_MODEL in exec_cmd
    assert ACTIVATION_EXEC in exec_cmd
    assert "--threshold 0.6973556280136108" in exec_cmd
    assert "--consecutive-windows 2" in exec_cmd
    assert "--inference-interval-ms 250" in exec_cmd
    assert f"--capture-health {CAPTURE_HEALTH}" in exec_cmd


def test_pyproject_has_package_entrypoints_and_build_backend():
    with open(PYPROJECT, "rb") as fh:
        data = tomllib.load(fh)

    scripts = data["project"]["scripts"]
    assert scripts["okay-hermes-realtime-controller"] == "realtime_action_spike.service:main"
    assert scripts["okay-hermes-realtime-activation"] == (
        "realtime_action_spike.activation_handler:main"
    )

    assert data["tool"]["uv"]["package"] is True
    assert data["build-system"]["build-backend"] == "hatchling.build"
    assert data["build-system"]["requires"]

    package_data = data["tool"]["hatch"]["build"]["targets"]["wheel"].get("include")
    assert package_data is not None
    assert "src/realtime_action_spike/web/**" in package_data


def test_built_wheel_contains_runtime_package_web_assets_and_entrypoints(tmp_path):
    result = subprocess.run(
        ("uv", "build", "--wheel", "--out-dir", str(tmp_path)),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr

    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert "realtime_action_spike/service.py" in names
        assert "realtime_action_spike/web/voice.js" in names
        entrypoints = archive.read(
            "openai_realtime_action_spike-0.1.0.dist-info/entry_points.txt"
        ).decode()
    assert "okay-hermes-realtime-controller" in entrypoints
    assert "okay-hermes-realtime-activation" in entrypoints


def test_config_example_has_replacement_defaults_and_no_ohv_paths():
    lines = CONFIG_EXAMPLE.read_text()

    assert "OPENAI_API_KEY=" in lines
    assert "VOICE_BROWSER_PROFILE=~/.local/share/okay-hermes-realtime/brave-profile" in lines
    assert "WAKEWORD_MODEL_PATH=~/.local/share/okay-hermes-realtime/models/" in lines
    assert "WAKEWORD_THRESHOLD=0.6973556280136108" in lines
    assert "WAKEWORD_CONSECUTIVE_WINDOWS=2" in lines
    assert "WAKEWORD_INFERENCE_INTERVAL_MS=250" in lines

    assert "~/.hermes" not in lines
    assert "okay-hermes-voice" not in lines
    assert "%h" not in lines


def test_installer_has_one_exit_trap_so_native_temp_cleanup_cannot_replace_rollback():
    trap_lines = [
        line.strip()
        for line in INSTALLER.read_text().splitlines()
        if line.strip().startswith("trap ") and line.strip().endswith(" EXIT")
    ]
    assert trap_lines == ["trap cleanup EXIT"]


def _make_fake_command(directory: Path, name: str, script: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    command = directory / name
    command.write_text(f"#!/usr/bin/env bash\n{script}\n")
    command.chmod(0o755)


def _run_installer(
    prefix: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    command_env["HOME"] = str(prefix)
    bin_dir = prefix / "fake-bin"

    _make_fake_command(
        bin_dir,
        "systemctl",
        "printf '%s\\n' \"$*\" >>\"$HOME/systemctl.log\"",
    )

    command_env["PATH"] = f"{bin_dir}:{command_env['PATH']}"

    if env:
        command_env.update(env)

    args = ("--prefix", str(prefix), *args)
    return subprocess.run(
        (str(INSTALLER), *args),
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=command_env,
    )


def test_installer_detects_collisions_without_touching_unrelated_services(
    tmp_path,
):
    prefix = tmp_path / "home"
    model = tmp_path / "model.onnx"
    listener = tmp_path / "listener"
    model.write_text("dummy model")
    listener.write_text("#!/bin/sh\n")
    listener.chmod(0o755)

    (prefix / ".config/systemd/user").mkdir(parents=True)
    (
        prefix / ".config/systemd/user/okay-hermes-realtime-controller.service"
    ).write_text("already there")

    result = _run_installer(
        prefix,
        "--model",
        str(model),
        "--listener",
        str(listener),
        "--skip-systemctl",
        "--skip-native",
        "--skip-venv",
    )

    assert result.returncode != 0
    assert "Collision" in (result.stderr + result.stdout)

    # No unrelated service command should be invoked when install aborts.
    assert not (prefix / "systemctl.log").exists()


def test_installer_install_and_uninstall_isolated_prefix(tmp_path):
    prefix = tmp_path / "prefix"
    model = tmp_path / "model.onnx"
    model.write_text("dummy model")
    listener = tmp_path / "listener"
    listener.write_text("#!/bin/sh\necho hello\n")
    listener.chmod(0o755)
    onnxruntime_root = tmp_path / "onnxruntime"
    onnxruntime_lib = onnxruntime_root / "lib/libonnxruntime.so.1"
    onnxruntime_lib.parent.mkdir(parents=True)
    onnxruntime_lib.write_text("fake shared object")

    install_result = _run_installer(
        prefix,
        "--model",
        str(model),
        "--listener",
        str(listener),
        "--onnxruntime-root",
        str(onnxruntime_root),
        "--skip-venv",
    )
    assert install_result.returncode == 0, install_result.stderr

    config_path = prefix / ".config/okay-hermes-realtime/config.env"
    controller_unit = prefix / ".config/systemd/user/okay-hermes-realtime-controller.service"
    wakeword_unit = prefix / ".config/systemd/user/okay-hermes-realtime-wakeword.service"
    listener_installed = (
        prefix
        / ".local/lib/okay-hermes-realtime/okay-hermes-realtime-wake-listener"
    )

    assert config_path.is_file()
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700
    assert controller_unit.is_file()
    assert wakeword_unit.is_file()
    assert listener_installed.is_file()
    installed_onnxruntime = (
        prefix / ".local/lib/okay-hermes-realtime/lib/libonnxruntime.so.1"
    )
    assert installed_onnxruntime.read_text() == "fake shared object"

    log = (
        prefix / "systemctl.log"
    ).read_text() if (prefix / "systemctl.log").exists() else ""
    assert "daemon-reload" in log
    assert "hermes-wakeword.service" not in log

    uninstall_result = _run_installer(
        prefix,
        "--uninstall",
        "--skip-venv",
    )
    assert uninstall_result.returncode == 0, uninstall_result.stderr

    assert not config_path.exists()
    assert not controller_unit.exists()
    assert not wakeword_unit.exists()
    assert not listener_installed.exists()
    assert not installed_onnxruntime.exists()
    assert not (prefix / ".local/share/okay-hermes-realtime").exists()
    uninstall_log = (
        prefix / "systemctl.log"
    ).read_text() if (prefix / "systemctl.log").exists() else ""
    assert "--user disable" in uninstall_log or "--user stop" in uninstall_log


def test_installer_creates_exact_replacement_venv_and_non_editable_install(tmp_path):
    prefix = tmp_path / "prefix"
    model = tmp_path / "model.onnx"
    model.write_text("dummy model")
    fake_bin = prefix / "fake-bin"
    _make_fake_command(
        fake_bin,
        "uv",
        """printf '%s\\n' "$*" >>"$HOME/uv.log"
if [[ "$1" == "venv" ]]; then
  [[ "${2:-}" != "--directory" ]] || exit 41
  mkdir -p "$2/bin"
  touch "$2/bin/python"
  chmod 0755 "$2/bin/python"
fi""",
    )

    result = _run_installer(
        prefix,
        "--model",
        str(model),
        "--skip-native",
        "--skip-systemctl",
    )
    assert result.returncode == 0, result.stderr

    venv_dir = prefix / ".local/share/okay-hermes-realtime/venv"
    assert (venv_dir / "bin/python").is_file()
    uv_log = (prefix / "uv.log").read_text()
    assert f"venv {venv_dir}" in uv_log
    assert f"--python {venv_dir}/bin/python" in uv_log
    assert " -e " not in f" {uv_log} "


def test_installer_rolls_back_new_replacement_files_when_venv_install_fails(tmp_path):
    prefix = tmp_path / "prefix"
    model = tmp_path / "model.onnx"
    model.write_text("dummy model")
    fake_bin = prefix / "fake-bin"
    _make_fake_command(
        fake_bin,
        "uv",
        """if [[ "$1" == "venv" ]]; then
  mkdir -p "$2/bin"
  touch "$2/bin/python"
  chmod 0755 "$2/bin/python"
  exit 0
fi
exit 52""",
    )

    result = _run_installer(
        prefix,
        "--model",
        str(model),
        "--skip-native",
        "--skip-systemctl",
    )
    assert result.returncode == 52

    assert not (prefix / ".config/okay-hermes-realtime/config.env").exists()
    assert not (
        prefix / ".config/systemd/user/okay-hermes-realtime-controller.service"
    ).exists()
    assert not (
        prefix / ".config/systemd/user/okay-hermes-realtime-wakeword.service"
    ).exists()
    assert not (prefix / ".local/bin/okay-hermes-realtime-controller").exists()
    assert not (prefix / ".local/bin/okay-hermes-realtime-activation").exists()
    assert not (
        prefix / ".local/share/okay-hermes-realtime/models/okay-hermes-realtime-wakeword.onnx"
    ).exists()
    assert not (prefix / ".local/share/okay-hermes-realtime/venv").exists()

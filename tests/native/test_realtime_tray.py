from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TRAY_DIR = REPO_ROOT / "native" / "realtime-tray"
CPP_PATH = TRAY_DIR / "main.cpp"
STATE_HEADER_PATH = TRAY_DIR / "tray_state.h"
CMAKE_PATH = TRAY_DIR / "CMakeLists.txt"
INSTALLER_PATH = REPO_ROOT / "scripts" / "install_realtime_tray.sh"


def tray_code() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (CPP_PATH, STATE_HEADER_PATH)
        if path.exists()
    )


def test_realtime_tray_sources_exist() -> None:
    assert CPP_PATH.exists()
    assert STATE_HEADER_PATH.exists()
    assert CMAKE_PATH.exists()
    assert INSTALLER_PATH.exists()


def test_tray_menu_matches_replacement_contract() -> None:
    source = tray_code()

    assert '"Turn ON"' in source
    assert '"Turn OFF"' in source
    assert '"Open Voice Page"' in source
    assert '"Exit"' in source
    assert '"Restart"' not in source


def test_tray_controls_only_replacement_units_and_paths() -> None:
    source = tray_code()

    assert "okay-hermes-realtime-controller.service" in source
    assert "okay-hermes-realtime-wakeword.service" in source
    assert ".local/state/okay-hermes-realtime" in source
    forbidden = (
        "hermes-wakeword.service",
        "hermes-voice-handler.service",
        "okay-hermes-voice",
        ".hermes/",
    )
    for value in forbidden:
        assert value not in source.replace("okay-hermes-realtime-wakeword.service", "")


def test_tray_is_event_driven_without_shell_or_periodic_service_polling() -> None:
    source = tray_code()

    assert "QDBusConnection" in source
    assert "QDBusPendingCallWatcher" in source
    assert "QFileSystemWatcher" in source
    assert "PulseAudioQt" in source
    assert "asyncCall(" in source
    assert "QProcess" not in source
    assert "systemctl" not in source
    assert "journalctl" not in source
    assert "pollTimer" not in source
    assert '"is-active"' not in source
    assert "popen(" not in source
    assert "std::system" not in source


def test_open_voice_page_posts_to_controller_without_opening_a_browser() -> None:
    source = tray_code()

    assert "QNetworkAccessManager" in source
    assert "QNetworkRequest" in source
    assert "QNetworkReply" in source
    assert "http://127.0.0.1:8765/internal/open" in source
    assert "post(" in source
    assert "QDesktopServices" not in source
    assert "--app=" not in source
    assert "brave" not in source.lower()


def test_tray_watches_capture_controller_and_microphone_health() -> None:
    source = tray_code()

    assert "captureHealthChanged" in source
    assert "controllerHealthChanged" in source
    assert "defaultSourceChanged" in source
    assert "sourceAdded" in source
    assert "sourceRemoved" in source
    assert "no microphone" in source.lower()
    assert "conversation active" in source.lower()


def test_systemd_signal_connections_retry_and_commands_are_async() -> None:
    source = tray_code()

    assert "systemdRetryTimer" in source
    assert "scheduleSystemdWatcherRetry" in source
    assert "QDBusConnection::sessionBus().connect" in source
    assert "QDBusPendingReply<void>" in source
    assert "runSystemdCommandsAsync" in source
    assert ".call(" not in source


def test_turn_off_stops_wakeword_then_controller_through_systemd() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    stop_block = source.split("void stopServices() {", maxsplit=1)[1].split(
        "void openVoicePage() {",
        maxsplit=1,
    )[0]

    assert 'runSystemdCommandsAsync("StopUnit", {kWakewordUnit, kControllerUnit})' in stop_block


def test_cmake_links_all_event_driven_backends() -> None:
    cmake = CMAKE_PATH.read_text(encoding="utf-8")

    assert "Qt6 REQUIRED COMPONENTS Widgets DBus Network" in cmake
    assert "KF6PulseAudioQt" in cmake
    assert "Qt6::DBus" in cmake
    assert "Qt6::Network" in cmake
    assert "KF6::PulseAudioQt" in cmake


def test_pure_state_helpers_compile_and_match_health_contract(tmp_path: Path) -> None:
    if not shutil.which("pkg-config") or not shutil.which("c++"):
        pytest.skip("native helper test requires pkg-config and c++")
    pkg = subprocess.run(
        ["pkg-config", "--cflags", "--libs", "Qt6Core"],
        text=True,
        capture_output=True,
        check=False,
    )
    if pkg.returncode != 0:
        pytest.skip("Qt6Core pkg-config metadata unavailable")

    test_cpp = tmp_path / "tray_state_test.cpp"
    test_cpp.write_text(
        textwrap.dedent(
            f"""
            #include <cassert>
            #include <QString>
            #include "{STATE_HEADER_PATH}"

            int main() {{
                using okay_hermes_realtime_tray::CaptureHealth;
                using okay_hermes_realtime_tray::ControllerHealth;
                using okay_hermes_realtime_tray::DaemonState;

                assert(okay_hermes_realtime_tray::systemdUnitObjectPath(
                    QStringLiteral("okay-hermes-realtime-controller.service")) ==
                    QStringLiteral("/org/freedesktop/systemd1/unit/okay_2dhermes_2drealtime_2dcontroller_2eservice"));
                assert(okay_hermes_realtime_tray::captureHealthFromStatusText(
                    QStringLiteral("healthy\\n")) == CaptureHealth::Healthy);
                assert(okay_hermes_realtime_tray::captureHealthFromStatusText(
                    QStringLiteral("unhealthy")) == CaptureHealth::Unhealthy);
                assert(okay_hermes_realtime_tray::controllerHealthFromStatusText(
                    QStringLiteral("ready")) == ControllerHealth::Ready);
                assert(okay_hermes_realtime_tray::controllerHealthFromStatusText(
                    QStringLiteral("conversation-active")) == ControllerHealth::ConversationActive);
                assert(okay_hermes_realtime_tray::controllerHealthFromStatusText(
                    QStringLiteral("error")) == ControllerHealth::Error);

                assert(okay_hermes_realtime_tray::stateFromInputs(
                    false, CaptureHealth::Healthy, true, true, ControllerHealth::Ready) ==
                    DaemonState::NoMicrophone);
                assert(okay_hermes_realtime_tray::stateFromInputs(
                    true, CaptureHealth::Unknown, false, false, ControllerHealth::Unknown) ==
                    DaemonState::Off);
                assert(okay_hermes_realtime_tray::stateFromInputs(
                    true, CaptureHealth::Healthy, true, true, ControllerHealth::Ready) ==
                    DaemonState::On);
                assert(okay_hermes_realtime_tray::stateFromInputs(
                    true, CaptureHealth::Healthy, false, false, ControllerHealth::Ready) ==
                    DaemonState::On);
                assert(okay_hermes_realtime_tray::stateFromInputs(
                    true, CaptureHealth::Healthy, true, true,
                    ControllerHealth::ConversationActive) == DaemonState::ConversationActive);
                assert(okay_hermes_realtime_tray::stateFromInputs(
                    true, CaptureHealth::Healthy, false, false,
                    ControllerHealth::ConversationActive) == DaemonState::ConversationActive);
                assert(okay_hermes_realtime_tray::stateFromInputs(
                    true, CaptureHealth::Unhealthy, true, true, ControllerHealth::Ready) ==
                    DaemonState::Error);
                assert(okay_hermes_realtime_tray::stateFromInputs(
                    true, CaptureHealth::Healthy, true, false, ControllerHealth::Ready) ==
                    DaemonState::NoMicrophone);
                assert(okay_hermes_realtime_tray::stateFromInputs(
                    true, CaptureHealth::Unknown, true, true, ControllerHealth::Ready) ==
                    DaemonState::Starting);
                assert(okay_hermes_realtime_tray::stateFromInputs(
                    true, CaptureHealth::Unknown, false, true, ControllerHealth::Ready) ==
                    DaemonState::Starting);
                return 0;
            }}
            """
        ),
        encoding="utf-8",
    )
    binary = tmp_path / "tray_state_test"
    command = [
        "c++",
        "-std=c++17",
        "-fPIC",
        str(test_cpp),
        "-o",
        str(binary),
        *pkg.stdout.split(),
    ]
    subprocess.run(command, text=True, capture_output=True, check=True)
    subprocess.run([str(binary)], check=True)


def test_tray_configures_and_builds_in_temp_directory(tmp_path: Path) -> None:
    if not shutil.which("cmake"):
        pytest.skip("native tray build requires cmake")

    build_dir = tmp_path / "realtime-tray-build"
    command = [
        "cmake",
        "-S",
        str(TRAY_DIR),
        "-B",
        str(build_dir),
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    if shutil.which("ninja"):
        command.extend(["-G", "Ninja"])
    subprocess.run(command, text=True, capture_output=True, check=True)
    subprocess.run(
        ["cmake", "--build", str(build_dir), "--config", "Release"],
        text=True,
        capture_output=True,
        check=True,
    )
    assert (build_dir / "okay-hermes-realtime-tray").exists()


def test_installer_uses_only_replacement_owned_paths_and_autostart(tmp_path: Path) -> None:
    installer = INSTALLER_PATH.read_text(encoding="utf-8")

    assert "cmake" in installer
    assert "okay-hermes-realtime-tray" in installer
    assert ".local/lib/okay-hermes-realtime" in installer
    assert ".config/autostart" in installer
    assert "okay-hermes-realtime-tray.desktop" in installer
    assert "Exec=" in installer
    assert "python" not in installer.lower()
    assert "hermes-wakeword-tray" not in installer
    assert "okay-hermes-voice" not in installer
    assert "systemctl" not in installer
    assert "pkill" not in installer
    assert "killall" not in installer

    install_root = tmp_path / "home"
    subprocess.run(
        [str(INSTALLER_PATH), "--prefix", str(install_root)],
        text=True,
        capture_output=True,
        check=True,
    )
    assert (install_root / ".local/lib/okay-hermes-realtime/okay-hermes-realtime-tray").is_file()
    desktop = install_root / ".config/autostart/okay-hermes-realtime-tray.desktop"
    assert desktop.is_file()
    installed_binary = install_root / ".local/lib/okay-hermes-realtime/okay-hermes-realtime-tray"
    assert str(installed_binary) in desktop.read_text()

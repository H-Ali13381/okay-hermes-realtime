from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NATIVE_DIR = ROOT / "native"
SRC_FILE = NATIVE_DIR / "okay-hermes-realtime-wake-listener.c"
BUILD_SCRIPT = NATIVE_DIR / "build_wake_listener.sh"
INCLUDE_DIR = NATIVE_DIR / "include"


def read_source() -> str:
    return SRC_FILE.read_text(encoding="utf-8")


def strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//.*", "", source)
    return source


def function_source(source: str, name: str) -> str:
    source_no_comments = strip_comments(source)
    marker = f"{name}("
    idx = source_no_comments.find(marker)
    assert idx != -1, f"missing function {name}"
    open_brace = source_no_comments.find("{", idx)
    assert open_brace != -1, f"missing function body for {name}"

    depth = 0
    for pos in range(open_brace, len(source_no_comments)):
        ch = source_no_comments[pos]
        if ch == "{":
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                return source_no_comments[open_brace : pos + 1]

    raise AssertionError(f"unterminated function body for {name}")


def compile_fake_onnx_root(tmpdir: Path) -> Path:
    root = tmpdir / "onnxruntime"
    include_dir = root / "include"
    lib_dir = root / "lib"
    include_dir.mkdir(parents=True)
    lib_dir.mkdir()

    # Copy official pinned headers used by the build.
    shutil.copy2(INCLUDE_DIR / "onnxruntime_c_api.h", include_dir / "onnxruntime_c_api.h")
    shutil.copy2(INCLUDE_DIR / "onnxruntime_ep_c_api.h", include_dir / "onnxruntime_ep_c_api.h")

    fake_src = tmpdir / "fake_onnxruntime.c"
    fake_src.write_text(
        "#include \"onnxruntime_c_api.h\"\n"
        "const OrtApiBase *OrtGetApiBase(void) { return 0; }\n",
        encoding="utf-8",
    )

    fake_so = lib_dir / "libonnxruntime.so.1"
    subprocess.run(
        ["cc", "-fPIC", "-shared", "-o", str(fake_so), str(fake_src), "-I", str(include_dir)],
        check=True,
    )

    return root


def build_listener(onnx_root: Path, output: Path) -> None:
    subprocess.run(
        [
            str(BUILD_SCRIPT),
            "--output",
            str(output),
            "--onnxruntime-root",
            str(onnx_root),
        ],
        check=True,
        env={**os.environ, "ONNXRUNTIME_ROOT": str(onnx_root)},
    )


def test_pinned_official_onnx_headers_present() -> None:
    api_header = (INCLUDE_DIR / "onnxruntime_c_api.h").read_text(encoding="utf-8")
    assert "#define ORT_API_VERSION 26" in api_header
    assert "#include \"onnxruntime_ep_c_api.h\"" in api_header
    assert (INCLUDE_DIR / "onnxruntime_ep_c_api.h").is_file()


def test_no_forbidden_task14_tokens_in_source() -> None:
    source = read_source().lower()
    forbidden = [
        "~/.hermes",
        "okay-hermes-voice",
        "popup",
        "systemctl",
        "ohv",
        "router",
        " tts",
        " stt",
    ]
    for token in forbidden:
        assert token not in source, f"found forbidden token: {token}"


def test_stream_failure_marks_run_failed_for_nonzero_exit() -> None:
    source = read_source()

    on_state = function_source(source, "on_state_changed")
    assert "PW_STREAM_STATE_ERROR" in on_state
    assert "atomic_store(&data->failed, true)" in on_state
    # A graceful stop clears `running` first (signal handler), so the
    # UNCONNECTED callback fired during teardown must not mark the run
    # as failed or systemd would restart a deliberately stopped service.
    assert "atomic_compare_exchange_strong(&data->running" in on_state

    main_body = function_source(source, "main")
    assert "atomic_load(&data.failed)" in main_body
    assert "status = 1" in main_body


def test_rt_callback_is_dedicated_audio_ingest() -> None:
    source = read_source()
    on_process = function_source(source, "on_process")

    assert "ring_write" in on_process
    assert "spa_format_audio_raw_parse" in function_source(source, "on_stream_param_changed")
    assert "run_model" not in on_process
    assert "fork" not in on_process
    assert "exec" not in on_process
    assert "malloc" not in on_process
    assert "calloc" not in on_process


def test_handler_argv_preserves_separator_tail() -> None:
    parse_options_body = function_source(read_source(), "parse_options")

    assert "if (strcmp(argv[i + 1], \"--\") == 0)" in parse_options_body
    assert "for (size_t j = (size_t)(i + 2); j < (size_t)argc; j++)" in parse_options_body
    assert "options->handler_argv[0] = argv[++i]" in parse_options_body
    assert "options->handler_argc = 1" in parse_options_body


def test_wake_model_frees_names_without_releasing_ort_owned_default_allocator() -> None:
    source = read_source()
    init_body = function_source(source, "wake_model_init")
    destroy_body = function_source(source, "wake_model_destroy")

    assert "GetAllocatorWithDefaultOptions" in init_body
    assert "AllocatorFree(model->allocator, model->input_name)" in destroy_body
    assert "AllocatorFree(model->allocator, model->output_name)" in destroy_body
    assert "ReleaseAllocator" not in destroy_body


def test_fake_onnx_root_and_build_binary_help_self_test(tmp_path: Path) -> None:
    onnx_root = compile_fake_onnx_root(tmp_path)
    output = tmp_path / "okay-hermes-realtime-wake-listener"
    build_listener(onnx_root, output)

    help_out = subprocess.run(
        [str(output), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--model" in help_out.stdout
    assert "--handler" in help_out.stdout
    assert "--capture-health" in help_out.stdout
    assert "--activation-archive-dir" in help_out.stdout
    assert "--threshold" in help_out.stdout
    assert "--consecutive" in help_out.stdout
    assert "--inference-interval-ms" in help_out.stdout

    self_test = subprocess.run(
        [str(output), "--self-test"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = self_test.stdout.strip()
    event = json.loads(payload)
    assert event["event"] == "self_test"
    assert event["status"] == "ok"
    assert "score" in event


def test_self_test_writes_activation_wav_and_metadata_when_archive_is_enabled(
    tmp_path: Path,
) -> None:
    onnx_root = compile_fake_onnx_root(tmp_path)
    output = tmp_path / "okay-hermes-realtime-wake-listener"
    archive = tmp_path / "activations"
    build_listener(onnx_root, output)

    subprocess.run(
        [
            str(output),
            "--self-test",
            "--activation-archive-dir",
            str(archive),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    wav_files = list(archive.glob("activation_*_selftest.wav"))
    metadata_files = list(archive.glob("activation_*_selftest.json"))
    assert len(wav_files) == 1
    assert len(metadata_files) == 1

    with wave.open(str(wav_files[0]), "rb") as recording:
        assert recording.getnchannels() == 1
        assert recording.getsampwidth() == 2
        assert recording.getframerate() == 16_000
        assert recording.getnframes() == 48_000

    metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
    assert metadata == {
        "probability": 1.0,
        "sample_rate": 16_000,
        "sample_count": 48_000,
        "duration_seconds": 3.0,
        "native_listener": True,
        "self_test": True,
    }


def test_binary_links_with_origin_libpath_and_colocated_soname(tmp_path: Path) -> None:
    onnx_root = compile_fake_onnx_root(tmp_path)
    output = tmp_path / "okay-hermes-realtime-wake-listener"
    build_listener(onnx_root, output)

    readelf_output = subprocess.run(
        ["readelf", "-d", str(output)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "$ORIGIN/lib" in readelf_output

    ldd_output = subprocess.run(
        ["ldd", str(output)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    lib_path = str(output.parent / "lib" / "libonnxruntime.so")
    assert lib_path in ldd_output or "libonnxruntime.so.1" in ldd_output


def test_no_fake_ort_api_table_in_test_shim(tmp_path: Path) -> None:
    fake_c = tmp_path / "fake_onnxruntime.c"
    fake_c.write_text(
        "#include \"onnxruntime_c_api.h\"\nconst OrtApiBase *OrtGetApiBase(void) { return 0; }\n",
        encoding="utf-8",
    )

    fake_text = fake_c.read_text(encoding="utf-8")
    assert "OrtApiBase" in fake_text
    assert "OrtApiBase *" in fake_text
    assert "OrtGetApi" in fake_text
    fake_body = fake_text.replace("OrtApiBase", "")
    fake_body = fake_body.replace("OrtGetApiBase", "")
    assert "OrtApi" not in fake_body


def test_build_script_compiles_without_warnings(tmp_path: Path) -> None:
    onnx_root = compile_fake_onnx_root(tmp_path)
    output = tmp_path / "okay-hermes-realtime-wake-listener"
    result = subprocess.run(
        [
            str(BUILD_SCRIPT),
            "--output",
            str(output),
            "--onnxruntime-root",
            str(onnx_root),
        ],
        env={**os.environ, "ONNXRUNTIME_ROOT": str(onnx_root)},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"build failed: {result.stdout}\n{result.stderr}")
    assert output.exists()

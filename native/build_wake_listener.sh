#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_file="$repo_dir/native/okay-hermes-realtime-wake-listener.c"
output="$repo_dir/native/okay-hermes-realtime-wake-listener"
onnx_root="${ONNXRUNTIME_ROOT:-}"

usage() {
  cat <<'EOF'
Usage: build_wake_listener.sh --output PATH --onnxruntime-root DIR

Build the native realtime wake listener binary.

Options:
  --output PATH             Output binary path.
  --onnxruntime-root DIR    ONNX Runtime root path containing include/ and lib/ directories.
  --help                    Show this help text.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      output="$2"
      shift 2
      ;;
    --onnxruntime-root)
      onnx_root="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$onnx_root" ]]; then
  echo "ONNXRUNTIME_ROOT is required (or pass --onnxruntime-root)." >&2
  exit 1
fi

if [[ ! -d "$onnx_root" ]]; then
  echo "ONNX Runtime root not found: $onnx_root" >&2
  exit 1
fi

if [[ ! -f "$onnx_root/include/onnxruntime_c_api.h" ]]; then
  echo "onnxruntime_c_api.h not found under: $onnx_root/include" >&2
  exit 1
fi

find_ort_lib() {
  local candidates=(
    "$onnx_root/lib/libonnxruntime.so.1"
    "$onnx_root/lib/libonnxruntime.so.1.26.0"
    "$onnx_root/lib/libonnxruntime.so"
    "$onnx_root/capi/libonnxruntime.so.1"
    "$onnx_root/capi/libonnxruntime.so.1.26.0"
    "$onnx_root/capi/libonnxruntime.so"
  )

  for candidate in "${candidates[@]}"; do
    if [[ -f "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done

  echo ""
  return 1
}

ort_lib="$(find_ort_lib)"
if [[ -z "$ort_lib" ]]; then
  echo "No libonnxruntime shared object found in $onnx_root/lib or $onnx_root/capi" >&2
  exit 1
fi

out_dir="$(dirname "$output")"
out_lib_dir="$out_dir/lib"
mkdir -p "$out_lib_dir"

ort_lib_name="$(basename "$ort_lib")"
copy_lib="$out_lib_dir/$ort_lib_name"
cp "$ort_lib" "$copy_lib"

soname=""
if command -v readelf >/dev/null 2>&1; then
  soname=$(readelf -d "$copy_lib" | awk -F'[][]' '/SONAME/ {print $3; exit}')
fi
if [[ -z "$soname" ]]; then
  soname="libonnxruntime.so.1"
fi

if [[ "$soname" != "$ort_lib_name" ]]; then
  ln -sfn "$ort_lib_name" "$out_lib_dir/$soname"
fi
if [[ "libonnxruntime.so" != "$ort_lib_name" ]]; then
  ln -sfn "$ort_lib_name" "$out_lib_dir/libonnxruntime.so"
fi

if ! pkg=$(pkg-config --cflags --libs libpipewire-0.3); then
  echo "pkg-config could not locate libpipewire-0.3" >&2
  exit 1
fi

if ! command -v cc >/dev/null 2>&1; then
  echo "C compiler not found" >&2
  exit 1
fi

tmp_output="$out_dir/.okay-hermes-realtime-wake-listener.$$.tmp"
trap 'rm -f "$tmp_output"' EXIT

cc -std=c11 -O2 -Wall -Wextra -Wpedantic \
  -I"$repo_dir/native/include" \
  -I"$onnx_root/include" \
  "$source_file" \
  $pkg \
  "$copy_lib" \
  -Wl,-rpath,'$ORIGIN/lib' \
  -lm -pthread \
  -o "$tmp_output"

chmod 0755 "$tmp_output"
mv -f "$tmp_output" "$output"
trap - EXIT

echo "$output"
#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install_user_services.sh [options]

Install or uninstall isolated replacement user services, listeners, and launchers.

Options:
  --prefix PATH              Install root (defaults to $HOME)
  --model PATH               Source ONNX model path
  --listener PATH            Listener executable to install (or skipped with --skip-native)
  --onnxruntime-root PATH    ONNX Runtime root for rebuilding listener
  --force                    Overwrite existing target files
  --enable                   Enable and start services after install
  --skip-venv                Skip creating/replacing the replacement venv
  --skip-native              Skip rebuilding/collecting native listener artifacts
  --skip-systemctl           Skip calling systemctl (test mode)
  --uninstall                Remove installed files and disable units
  --rollback                 Roll back any tracked installation state
  --help                     Show this help text
EOF
}

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
prefix="$HOME"
model_path=""
listener_path=""
onnxruntime_root="${ONNXRUNTIME_ROOT:-}"
force=0
enable=0
skip_venv=0
skip_native=0
skip_systemctl=0
uninstall=0
rollback_only=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      if [[ $# -lt 2 ]]; then
        echo "missing value for --prefix" >&2
        exit 2
      fi
      prefix="$2"
      shift 2
      ;;
    --model)
      if [[ $# -lt 2 ]]; then
        echo "missing value for --model" >&2
        exit 2
      fi
      model_path="$2"
      shift 2
      ;;
    --listener)
      if [[ $# -lt 2 ]]; then
        echo "missing value for --listener" >&2
        exit 2
      fi
      listener_path="$2"
      shift 2
      ;;
    --onnxruntime-root)
      if [[ $# -lt 2 ]]; then
        echo "missing value for --onnxruntime-root" >&2
        exit 2
      fi
      onnxruntime_root="$2"
      shift 2
      ;;
    --force)
      force=1
      shift
      ;;
    --enable)
      enable=1
      shift
      ;;
    --skip-venv)
      skip_venv=1
      shift
      ;;
    --skip-native)
      skip_native=1
      shift
      ;;
    --skip-systemctl)
      skip_systemctl=1
      shift
      ;;
    --uninstall)
      uninstall=1
      shift
      ;;
    --rollback)
      rollback_only=1
      shift
      ;;
    --help|-h)
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

config_dir="$prefix/.config/okay-hermes-realtime"
systemd_dir="$prefix/.config/systemd/user"
bin_dir="$prefix/.local/bin"
share_dir="$prefix/.local/share/okay-hermes-realtime"
lib_dir="$prefix/.local/lib/okay-hermes-realtime"
venv_dir="$share_dir/venv"
state_dir="$prefix/.local/state/okay-hermes-realtime"

config_file="$config_dir/config.env"
controller_unit="$systemd_dir/okay-hermes-realtime-controller.service"
wakeword_unit="$systemd_dir/okay-hermes-realtime-wakeword.service"
controller_launcher="$bin_dir/okay-hermes-realtime-controller"
activation_launcher="$bin_dir/okay-hermes-realtime-activation"
listener_binary="$lib_dir/okay-hermes-realtime-wake-listener"
listener_lib_dir="$lib_dir/lib"
model_target="$share_dir/models/okay-hermes-realtime-wakeword.onnx"
install_log="$share_dir/.install.log"

requirements=(
  "$config_file"
  "$controller_unit"
  "$wakeword_unit"
  "$controller_launcher"
  "$activation_launcher"
  "$listener_binary"
  "$model_target"
)

installed_files=()
temp_dirs=()
installing=0
rollback_from_log=0

mark_installed() {
  local path="$1"
  installed_files+=("$path")
}

rollback_installed() {
  if (( rollback_from_log == 1 )); then
    if [[ -f "$install_log" ]]; then
      while IFS= read -r line; do
        [[ -n "$line" ]] && rm -rf -- "$line"
      done < "$install_log"
      rm -f "$install_log"
    fi
    return
  fi

  for path in "${installed_files[@]}"; do
    rm -rf -- "$path"
  done
}

cleanup() {
  local code=$?
  local temp_dir
  for temp_dir in "${temp_dirs[@]}"; do
    rm -rf -- "$temp_dir"
  done
  if (( installing == 1 )); then
    rollback_installed
  fi
  exit "$code"
}
trap cleanup EXIT

require_file_absent() {
  local target="$1"
  if [[ -e "$target" && "$force" -ne 1 ]]; then
    echo "Collision: target path exists: $target" >&2
    echo "Use --force to replace existing replacement-owned files." >&2
    exit 2
  fi
}

copy_with_mode() {
  local src="$1"
  local dst="$2"
  local mode="$3"
  install -m "$mode" "$src" "$dst"
  mark_installed "$dst"
}

install_launchers() {
  mkdir -p "$bin_dir"
  cat >"$controller_launcher" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$share_dir/venv/bin/okay-hermes-realtime-controller" "\$@"
EOF
  chmod 0755 "$controller_launcher"
  mark_installed "$controller_launcher"

  cat >"$activation_launcher" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$share_dir/venv/bin/okay-hermes-realtime-activation" "\$@"
EOF
  chmod 0755 "$activation_launcher"
  mark_installed "$activation_launcher"
}

install_units() {
  mkdir -p "$systemd_dir"
  cp "$repo_dir/systemd/okay-hermes-realtime-controller.service" "$controller_unit"
  cp "$repo_dir/systemd/okay-hermes-realtime-wakeword.service" "$wakeword_unit"
  mark_installed "$controller_unit"
  mark_installed "$wakeword_unit"

  chmod 0644 "$controller_unit" "$wakeword_unit"
}

install_config() {
  chmod 0700 "$config_dir"
  install -m 0600 "$repo_dir/config.example.env" "$config_file"
  mark_installed "$config_file"
}

install_model() {
  if [[ -z "$model_path" ]]; then
    echo "--model is required" >&2
    exit 2
  fi
  if [[ ! -f "$model_path" ]]; then
    echo "model file not found: $model_path" >&2
    exit 2
  fi

  mkdir -p "$(dirname "$model_target")"
  cp "$model_path" "$model_target"
  mark_installed "$model_target"
}

install_venv() {
  mkdir -p "$share_dir"
  if command -v uv >/dev/null 2>&1; then
    uv venv "$venv_dir"
    mark_installed "$venv_dir"
    uv pip install --python "$venv_dir/bin/python" "$repo_dir"
  else
    if ! command -v python3 >/dev/null 2>&1; then
      echo "python3 is required for venv creation" >&2
      exit 2
    fi
    python3 -m venv "$venv_dir"
    mark_installed "$venv_dir"
    "$venv_dir/bin/python" -m pip install --upgrade pip >/dev/null
    "$venv_dir/bin/pip" install --no-input "$repo_dir"
  fi
}

install_native_listener() {
  if [[ "$skip_native" -eq 1 ]]; then
    return 0
  fi

  mkdir -p "$lib_dir"

  if [[ -n "$listener_path" ]]; then
    if [[ -z "$onnxruntime_root" ]]; then
      echo "--onnxruntime-root is required with --listener" >&2
      exit 2
    fi
    cp "$listener_path" "$listener_binary"
    mark_installed "$listener_binary"
  else
    if [[ -z "$onnxruntime_root" ]]; then
      echo "--listener or --onnxruntime-root is required for listener install" >&2
      exit 2
    fi

    tmp_dir="$(mktemp -d)"
    temp_dirs+=("$tmp_dir")
    "$repo_dir/native/build_wake_listener.sh" --output "$tmp_dir/okay-hermes-realtime-wake-listener" --onnxruntime-root "$onnxruntime_root"
    cp "$tmp_dir/okay-hermes-realtime-wake-listener" "$listener_binary"
    mark_installed "$listener_binary"
  fi

  local source_lib_dir="$onnxruntime_root/lib"
  if [[ ! -d "$source_lib_dir" ]]; then
    echo "ONNX Runtime library directory not found: $source_lib_dir" >&2
    exit 2
  fi
  mkdir -p "$listener_lib_dir"
  local libs=("$source_lib_dir"/libonnxruntime.so*)
  if [[ ! -e "${libs[0]}" ]]; then
    echo "ONNX Runtime shared library not found under: $source_lib_dir" >&2
    exit 2
  fi
  local lib
  for lib in "${libs[@]}"; do
    cp -a "$lib" "$listener_lib_dir/"
    mark_installed "$listener_lib_dir/$(basename "$lib")"
  done
}

run_systemctl() {
  if (( skip_systemctl == 1 )); then
    return 0
  fi
  systemctl --user "$@"
}

run_uninstall_systemctl() {
  run_systemctl daemon-reload
  if [[ "$uninstall" -eq 1 ]]; then
    run_systemctl disable --now okay-hermes-realtime-controller.service okay-hermes-realtime-wakeword.service || true
    run_systemctl stop okay-hermes-realtime-controller.service okay-hermes-realtime-wakeword.service || true
  fi
}

run_install_systemctl() {
  run_systemctl daemon-reload
  if (( enable == 1 )); then
    run_systemctl enable --now okay-hermes-realtime-controller.service okay-hermes-realtime-wakeword.service
  fi
}

rollback_state() {
  rollback_from_log=1
  if [[ -f "$install_log" ]]; then
    rollback_installed
    echo "rollback completed"
  else
    echo "no rollback state found at $install_log" >&2
    exit 2
  fi
}

record_install_state() {
  : > "$install_log"
  local path
  for path in "${installed_files[@]}"; do
    printf '%s\n' "$path" >> "$install_log"
  done
}

restore_install_trap() {
  rollback_from_log=0
  installed_files=( )
}

if (( rollback_only == 1 )); then
  rollback_state
  exit 0
fi

if (( uninstall == 1 )); then
  installing=1

  if [[ -f "$config_file" ]]; then
    rm -f "$config_file"
  fi
  if [[ -f "$controller_unit" ]]; then
    rm -f "$controller_unit"
  fi
  if [[ -f "$wakeword_unit" ]]; then
    rm -f "$wakeword_unit"
  fi
  if [[ -f "$controller_launcher" ]]; then
    rm -f "$controller_launcher"
  fi
  if [[ -f "$activation_launcher" ]]; then
    rm -f "$activation_launcher"
  fi
  if [[ -f "$listener_binary" ]]; then
    rm -f "$listener_binary"
  fi
  if [[ -d "$lib_dir" ]]; then
    rm -rf "$listener_lib_dir" "$lib_dir"/*.so* || true
    rmdir --ignore-fail-on-non-empty "$lib_dir" || true
  fi
  if [[ -f "$venv_dir/pyvenv.cfg" ]]; then
    rm -rf "$venv_dir"
  fi
  if [[ -d "$share_dir/models" ]]; then
    rm -rf "$share_dir/models"
  fi
  rm -f "$install_log"
  if [[ -d "$share_dir" ]]; then
    rmdir --ignore-fail-on-non-empty "$share_dir" || true
  fi
  run_uninstall_systemctl

  installing=0
  exit 0
fi

for target in "${requirements[@]}"; do
  require_file_absent "$target"
  parent_dir="$(dirname "$target")"
  mkdir -p "$parent_dir"
  if [[ "$parent_dir" == "$lib_dir" ]]; then
    # keep listener directory for later
    mkdir -p "$lib_dir"
  fi
done

installing=1

install_config
install_units
install_launchers
install_model
install_native_listener

if [[ "$skip_venv" -eq 0 ]]; then
  install_venv
fi

run_install_systemctl
record_install_state
installing=0
exit 0

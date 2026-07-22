#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
prefix="$HOME"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      [[ $# -ge 2 ]] || { printf '%s\n' 'missing value for --prefix' >&2; exit 2; }
      prefix="$2"
      shift 2
      ;;
    --help|-h)
      printf '%s\n' 'usage: install_realtime_tray.sh [--prefix HOME]'
      exit 0
      ;;
    *)
      printf '%s\n' 'unknown option' >&2
      exit 2
      ;;
  esac
done

build_dir="$(mktemp -d)"
trap 'rm -rf "$build_dir"' EXIT
cmake -S "$repo_dir/native/realtime-tray" -B "$build_dir" -DCMAKE_BUILD_TYPE=Release -G Ninja
cmake --build "$build_dir"

install_dir="$prefix/.local/lib/okay-hermes-realtime"
autostart_dir="$prefix/.config/autostart"
binary="$install_dir/okay-hermes-realtime-tray"
mkdir -p "$install_dir" "$autostart_dir"
install -m 0755 "$build_dir/okay-hermes-realtime-tray" "$binary"

cat >"$autostart_dir/okay-hermes-realtime-tray.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Okay Hermes Realtime Tray
Exec=$binary
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
chmod 0600 "$autostart_dir/okay-hermes-realtime-tray.desktop"

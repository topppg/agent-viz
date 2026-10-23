#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ "${1:-}" != --build ]]; then
  if [[ "$(git -C "$repo_root" branch --show-current)" != main ]]; then
    echo 'Run this installer from a checkout on main.' >&2
    exit 1
  fi
  if [[ -n "$(git -C "$repo_root" status --porcelain)" ]]; then
    echo 'Commit or set aside local changes before updating main.' >&2
    exit 1
  fi
  git -C "$repo_root" pull --ff-only origin main
  exec bash "$repo_root/src/app/install.sh" --build
fi

bash "$repo_root/src/app/build.sh"

applications="$HOME/Applications"
target="$applications/Spiritboard.app"
mkdir -p "$applications"
if [[ -L "$target" ]]; then
  echo "Refusing to replace symlink: $target" >&2
  exit 1
fi

stage="$(mktemp -d "$applications/.spiritboard-install.XXXXXX")"
new_app="$stage/Spiritboard.app"
backup_app="$stage/previous.app"
restore_previous() {
  if [[ -e "$backup_app" && ! -e "$target" ]]; then
    mv "$backup_app" "$target"
  fi
  rm -R "$stage"
}
trap restore_previous EXIT
ditto "$repo_root/build/mac-app/Spiritboard.app" "$new_app"

if pgrep -U "$(id -u)" -x Spiritboard >/dev/null; then
  osascript -e 'tell application id "io.github.topppg.spiritboard" to quit'
  for _ in {1..10}; do
    if ! pgrep -U "$(id -u)" -x Spiritboard >/dev/null; then
      break
    fi
    sleep 1
  done
  if pgrep -U "$(id -u)" -x Spiritboard >/dev/null; then
    echo 'Spiritboard is still running. Close it and try again.' >&2
    exit 1
  fi
fi

if [[ -e "$target" ]]; then
  mv "$target" "$backup_app"
fi
mv "$new_app" "$target"
trap - EXIT
rm -R "$stage"
open "$target"
echo "Installed $target"

#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export PYTHONUTF8=1
export PYTHONDONTWRITEBYTECODE=1
python_bin=python3
if [[ -x .venv/bin/python ]]; then python_bin=.venv/bin/python; fi
"$python_bin" -B -m tools.build_linux_bridges
headless=false
for argument in "$@"; do
  if [[ "$argument" == "--headless" ]]; then headless=true; fi
done
# WSLg can expose a read-only X11 socket mount. Give headless sessions a private
# writable mount without changing the desktop's mount or network namespace.
if $headless && [[ -d /tmp/.X11-unix ]] && { [[ ! -w /tmp/.X11-unix ]] || [[ "$(stat -c %a /tmp/.X11-unix)" != "1777" ]]; }; then
  if ! command -v unshare >/dev/null || ! command -v mount >/dev/null; then
    printf '%s\n' '无桌面模式需要可写的 /tmp/.X11-unix，或者安装 unshare 和 mount。' >&2
    exit 1
  fi
  exec unshare -Urm sh -c 'mount -t tmpfs -o mode=1777 tmpfs /tmp/.X11-unix && exec "$@"' helper-linux "$python_bin" -B web.py "$@"
fi
exec "$python_bin" -B web.py "$@"

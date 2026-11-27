#!/usr/bin/env bash
# D455 Studio: a web page for record_d455.py (live view, recording, settings, playback).
#   bash studio.sh              start it and open http://127.0.0.1:8455/ in the browser
#   bash studio.sh --simulate   try it without a camera
#   bash studio.sh --doctor     check this computer and the camera connection, explain problems
# macOS asks for the login password at each start (the camera needs administrator rights).
# To never type it again, install the background service once: bash install_service.sh
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PORT=8455
previous=""
for arg in "$@"; do
  if [ "$previous" = "--port" ]; then PORT="$arg"; fi
  case "$arg" in --port=*) PORT="${arg#--port=}" ;; esac
  previous="$arg"
done
URL="http://127.0.0.1:${PORT}/"

open_page() {
  if [ "$(uname -s)" = "Darwin" ]; then open "$URL"; else xdg-open "$URL" >/dev/null 2>&1 || true; fi
}

for arg in "$@"; do
  if [ "$arg" = "--doctor" ]; then  # the self check: no camera, no server, no password
    bash run.sh --version >/dev/null
    exec .venv/bin/python d455_studio.py "$@"
  fi
done

# Already running, or installed as the on-demand service (the request starts it): just open the page.
if curl -fsS --max-time 20 "${URL}api/ping" >/dev/null 2>&1; then
  echo "D455 Studio 已经在运行（或者已装成后台服务），直接打开 $URL"
  open_page
  exit 0
fi

bash run.sh --version  # first run: creates .venv and installs the packages

# Open the page as this user once the server answers (the server itself may run as root).
(
  for _ in $(seq 1 240); do
    sleep 0.5
    if curl -fsS --max-time 1 "${URL}api/ping" >/dev/null 2>&1; then open_page; exit 0; fi
  done
) &

# On macOS d455_studio.py asks for the password itself (sudo) before it touches the camera.
exec .venv/bin/python d455_studio.py --no-browser "$@"

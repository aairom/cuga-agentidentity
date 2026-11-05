#!/usr/bin/env bash
# Start the Streamlit showcase in detached mode and print the URL.
source "$(dirname "$0")/_common.sh"
[ -x .venv/bin/streamlit ] || { echo "Run scripts/setup.sh first." >&2; exit 1; }
mkdir -p "$RUN_DIR"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Already running (pid $(cat "$PID_FILE")): http://localhost:$PORT"; exit 0
fi
if lsof -i ":$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use. Set AI_UI_PORT to another port." >&2; exit 1
fi
# Start in a new session (setsid is missing on macOS, so use Python) so the server survives this shell.
"$ROOT/.venv/bin/python" - "$PID_FILE" "$LOG_FILE" "$PORT" <<'PY'
import subprocess, sys
pid_file, log_file, port = sys.argv[1:4]
with open(log_file, "ab") as log:
    p = subprocess.Popen([".venv/bin/streamlit", "run", "app.py", "--server.port", port, "--server.headless", "true",
                          "--browser.gatherUsageStats", "false"], stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
open(pid_file, "w").write(str(p.pid))
PY
for _ in $(seq 1 60); do
  if curl -fs "http://localhost:$PORT/_stcore/health" >/dev/null 2>&1; then
    echo "Showcase is running (pid $(cat "$PID_FILE"))."
    echo "Open: http://localhost:$PORT"
    echo "Logs: $LOG_FILE"; exit 0
  fi
  kill -0 "$(cat "$PID_FILE")" 2>/dev/null || { echo "Failed to start. See $LOG_FILE" >&2; rm -f "$PID_FILE"; exit 1; }
  sleep 1
done
echo "Started but health check timed out. See $LOG_FILE" >&2; exit 1

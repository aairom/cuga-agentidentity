#!/usr/bin/env bash
# Gracefully stop the showcase (SIGTERM, then SIGKILL after 10 s).
source "$(dirname "$0")/_common.sh"
if [ ! -f "$PID_FILE" ]; then echo "Not running."; exit 0; fi
PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill -TERM "$PID"
  for _ in $(seq 1 10); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  kill -0 "$PID" 2>/dev/null && { echo "Forcing stop"; kill -KILL "$PID"; }
fi
rm -f "$PID_FILE"
echo "Stopped."

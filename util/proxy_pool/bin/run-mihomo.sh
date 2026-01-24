#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT_DIR/bin/mihomo"
CFG_DEFAULT="$ROOT_DIR/mihomo.yaml"
STATE_DIR="$ROOT_DIR/state"
PID_FILE="$STATE_DIR/mihomo.pid"
LOG_FILE="$STATE_DIR/mihomo.log"

cmd="${1:-}"
cfg="${2:-$CFG_DEFAULT}"

mkdir -p "$STATE_DIR"

status() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "running pid=$(cat "$PID_FILE")"
    return 0
  fi
  echo "not running"
  return 1
}

start() {
  if status >/dev/null 2>&1; then
    status
    exit 0
  fi
  if [ ! -x "$BIN" ]; then
    echo "mihomo not found/executable: $BIN" >&2
    echo "hint: run: $ROOT_DIR/bin/install-mihomo.sh" >&2
    exit 2
  fi
  if [ ! -f "$cfg" ]; then
    echo "config not found: $cfg" >&2
    exit 2
  fi

  # Detach robustly so the process keeps running after the shell exits.
  # `setsid` is preferred when available (WSL/CI can be picky about nohup).
  if command -v setsid >/dev/null 2>&1; then
    setsid "$BIN" -d "$ROOT_DIR" -f "$cfg" >>"$LOG_FILE" 2>&1 < /dev/null &
  else
    nohup "$BIN" -d "$ROOT_DIR" -f "$cfg" >>"$LOG_FILE" 2>&1 < /dev/null &
  fi
  echo $! > "$PID_FILE"
  sleep 0.2
  status
  echo "log: $LOG_FILE"
}

stop() {
  if ! status >/dev/null 2>&1; then
    status
    exit 0
  fi
  pid="$(cat "$PID_FILE")"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 50); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "stopped"
      return 0
    fi
    sleep 0.1
  done
  echo "force kill pid=$pid"
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
}

case "$cmd" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  *) echo "usage: $(basename "$0") start|stop|restart|status [config_path]" >&2; exit 2 ;;
esac

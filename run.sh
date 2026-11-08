#!/usr/bin/env bash
# Uso: ./run.sh [puerto]   (default 8765)
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8765}"

if lsof -ti tcp:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Ya hay algo escuchando en el puerto $PORT; lo reinicio."
  kill $(lsof -ti tcp:"$PORT" -sTCP:LISTEN)
  sleep 1
fi

# abre el navegador cuando el server responde (el índice de Disco tarda unos segundos)
( for _ in $(seq 1 120); do
    curl -s -o /dev/null "http://localhost:$PORT/" && { open "http://localhost:$PORT"; exit; }
    sleep 1
  done ) &

exec python3 app.py "$PORT"

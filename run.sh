#!/usr/bin/env bash
# Запуск live-таймингов: останавливает уже запущенный сервер на этом порту
# и стартует заново. Аргументы — как у `python -m live`:
#   ./run.sh                     → --speed 4 openf1 (последняя сессия)
#   ./run.sh --speed 8 openf1 2026 Monza Race
cd "$(dirname "$0")"
PORT=8765
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [[ ${args[i]} == --port ]] && PORT=${args[i+1]}
done
[[ ${#args[@]} -eq 0 ]] && args=(--speed 4 openf1)

if pids=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null) && [[ -n $pids ]]; then
  echo "останавливаю прежний сервер на порту $PORT (pid $pids)"
  kill $pids
  for _ in {1..20}; do lsof -ti tcp:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.2; done
fi
exec venv/bin/python -m live "${args[@]}"

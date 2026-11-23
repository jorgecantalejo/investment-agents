#!/usr/bin/env bash
# Ejecuta el informe desde cron. Evita ejecuciones solapadas con flock.
# Uso: scripts/run_report.sh [opciones de src.main]
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs
exec /usr/bin/flock -n "$PROJECT_DIR/logs/.run.lock" "$PROJECT_DIR/.venv/bin/python" -m src.main "$@" >> "$PROJECT_DIR/logs/cron.log" 2>&1

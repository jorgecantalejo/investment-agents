#!/usr/bin/env bash
# Ejecuta /analisis con Claude Code en modo no interactivo (suscripción, sin API) desde cron.
# Evita ejecuciones solapadas con flock. Solo permite las herramientas que necesita la skill.
# Uso: scripts/run_claude_analysis.sh [perfiles] [opciones de /analisis]   (por defecto: semanal)
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs
ARGS="${*:-semanal}"
LOG="$PROJECT_DIR/logs/claude-cron.log"
{
  echo "=== $(date '+%F %T') /analisis $ARGS ==="
  rc=0
  /usr/bin/flock -n "$PROJECT_DIR/logs/.run.lock" /usr/local/bin/claude -p "/analisis $ARGS" \
    --permission-mode dontAsk \
    --allowedTools "Bash(.venv/bin/python -m src.main *)" "Bash(date *)" "Bash(ls *)" \
      "Read" "Agent" "Write(/$PROJECT_DIR/data/handoff/**)" "Edit(/$PROJECT_DIR/data/handoff/**)" < /dev/null || rc=$?
  echo "=== fin $(date '+%F %T') (código $rc) ==="
  # Publicación desactivada hasta tener la web nueva (sin importes): los informes actuales muestran euros.
  # if [ "$rc" -eq 0 ]; then
  #   "$PROJECT_DIR/scripts/publish_site.sh" || echo "Fallo al publicar en GitHub Pages"
  # fi
} >> "$LOG" 2>&1

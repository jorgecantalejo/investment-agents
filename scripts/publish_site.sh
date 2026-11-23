#!/usr/bin/env bash
# Regenera la web (docs/) y la publica en GitHub Pages: commit + push de docs/ únicamente.
# Sin LLM ni búsquedas: usa el último análisis semanal guardado y recalcula la estrategia y los factores del oro.
# Pensado para cron a diario tras el cierre de Xetra, y tras cada análisis semanal.
# Uso: scripts/publish_site.sh
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
"$PROJECT_DIR/.venv/bin/python" -m src.main --site
git add docs
if git diff --cached --quiet -- docs; then
  echo "Sin cambios en docs/: nada que publicar"
  exit 0
fi
git commit -q -m "Actualizar web $(date '+%F %H:%M')" -- docs
git push -q origin HEAD
echo "Web publicada"

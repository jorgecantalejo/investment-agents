#!/usr/bin/env bash
# Regenera docs/ a partir de reports/ y lo publica en GitHub Pages (commit + push de docs/ únicamente).
# Uso: scripts/publish_site.sh
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
"$PROJECT_DIR/.venv/bin/python" scripts/build_site.py
git add docs
if git diff --cached --quiet -- docs; then
  echo "Sin cambios en docs/: nada que publicar"
  exit 0
fi
git commit -m "Publicar informes $(date +%F)" -- docs
git push origin HEAD

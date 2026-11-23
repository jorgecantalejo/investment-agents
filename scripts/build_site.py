"""Copia los informes HTML de reports/ a docs/ y genera docs/index.html para GitHub Pages.

Uso: python scripts/build_site.py
"""

from __future__ import annotations

import html
import re
import shutil
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_DIR / "reports"
SITE_DIR = PROJECT_DIR / "docs"

NAME_RE = re.compile(r"^(?P<kind>informe|estrategia)_(?P<date>\d{4}-\d{2}-\d{2})(?:_(?P<label>.+))?\.html$")
KIND_LABELS = {"informe": "Informe", "estrategia": "Estrategia"}

PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Informes de inversión</title>
<style>
  :root {{ color-scheme: dark; --bg: #0a0e14; --panel: #10161f; --border: #222b3a; --text: #e6e9ef;
    --muted: #8e98aa; --accent: #3987e5; --cyan: #33c3d6;
    --sans: Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    --mono: "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, monospace; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text); font: 15px/1.55 var(--sans); }}
  main {{ max-width: 760px; margin: 0 auto; padding: 32px 16px 48px; }}
  h1 {{ margin: 0 0 4px; font-size: 1.4rem; font-weight: 600; }}
  .muted {{ color: var(--muted); font-size: .85rem; }}
  h2 {{ margin: 28px 0 10px; font: 500 .75rem var(--mono); color: var(--cyan); text-transform: uppercase; letter-spacing: .08em; }}
  ul {{ list-style: none; margin: 0; padding: 0; }}
  li a {{ display: flex; justify-content: space-between; gap: 12px; padding: 12px 14px; margin-bottom: 8px;
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px; color: var(--text); text-decoration: none; }}
  li a:hover {{ border-color: var(--accent); }}
  .tag {{ color: var(--muted); font: .8rem var(--mono); white-space: nowrap; }}
  footer {{ margin-top: 32px; }}
</style>
</head>
<body>
<main>
<h1>Informes de inversión</h1>
<p class="muted">Actualizado el {updated}. No es asesoramiento financiero.</p>
{sections}
<footer class="muted">Generado automáticamente a partir de reports/.</footer>
</main>
</body>
</html>
"""


def build() -> int:
    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / ".nojekyll").touch()
    by_date: dict[str, list[tuple[str, str, str]]] = {}
    for path in sorted(REPORTS_DIR.glob("*.html")):
        match = NAME_RE.match(path.name)
        if not match:
            continue
        shutil.copy2(path, SITE_DIR / path.name)
        label = (match["label"] or "completo").replace("_", " ").replace("-", " + ")
        by_date.setdefault(match["date"], []).append((path.name, KIND_LABELS[match["kind"]], label))

    sections = []
    for date in sorted(by_date, reverse=True):
        items = "\n".join(
            f'<li><a href="{html.escape(name)}"><span>{html.escape(label.capitalize())}</span>'
            f'<span class="tag">{kind}</span></a></li>'
            for name, kind, label in by_date[date]
        )
        sections.append(f"<h2>{date}</h2>\n<ul>\n{items}\n</ul>")

    page = PAGE.format(
        updated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        sections="\n".join(sections) or '<p class="muted">Todavía no hay informes.</p>',
    )
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")
    return sum(len(v) for v in by_date.values())


if __name__ == "__main__":
    print(f"{build()} informes copiados a {SITE_DIR}")

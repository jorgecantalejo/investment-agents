"""Gráficos SVG en línea para el informe (sin JavaScript ni librerías externas).

La geometría se calcula aquí para que la plantilla solo los coloque. Cada gráfico lleva
<title> para el tooltip nativo y el valor siempre aparece también como texto.
"""

from __future__ import annotations

import math

from markupsafe import Markup, escape

# Escala divergente miedo <-> codicia: rojo / gris neutro / azul (apta para daltonismo; el texto da la etiqueta).
FEAR_GREED_BANDS: list[tuple[float, float, str, str]] = [
    (0, 25, "#d03b3b", "Miedo extremo"),
    (25, 45, "#e9806e", "Miedo"),
    (45, 55, "#5b5f6b", "Neutral"),
    (55, 75, "#6fa3ee", "Codicia"),
    (75, 100, "#3987e5", "Codicia extrema"),
]


def fear_greed_label(value: float | None) -> str:
    if value is None:
        return "insufficient_data"
    for low, high, _, label in FEAR_GREED_BANDS:
        if value <= high:
            return label
    return FEAR_GREED_BANDS[-1][3]


def _polar(cx: float, cy: float, r: float, fraction: float) -> tuple[float, float]:
    """Punto del semicírculo superior: fraction 0 = izquierda, 1 = derecha."""
    angle = math.pi * (1 - fraction)
    return cx + r * math.cos(angle), cy - r * math.sin(angle)


def _arc(cx: float, cy: float, r: float, start: float, end: float) -> str:
    x1, y1 = _polar(cx, cy, r, start)
    x2, y2 = _polar(cx, cy, r, end)
    return f"M{x1:.2f},{y1:.2f} A{r},{r} 0 0 1 {x2:.2f},{y2:.2f}"


def gauge(value: float | None, title: str) -> Markup:
    """Semicírculo 0-100 por bandas con aguja. Sin dato: pista gris y sin aguja."""
    cx, cy, r, width = 60, 60, 48, 10
    gap = 0.012  # separación de 2px entre bandas
    parts = [f'<svg class="gauge" viewBox="0 0 120 72" role="img" aria-label="{escape(title)}"><title>{escape(title)}: '
             f'{"sin dato" if value is None else f"{value:.0f} · {fear_greed_label(value)}"}</title>']
    for low, high, color, _ in FEAR_GREED_BANDS:
        opacity = "1" if value is not None else "0.25"
        parts.append(
            f'<path d="{_arc(cx, cy, r, low / 100 + gap, high / 100 - gap)}" stroke="{color}" stroke-width="{width}" '
            f'fill="none" stroke-linecap="butt" opacity="{opacity}"/>'
        )
    if value is not None:
        frac = max(0.0, min(1.0, value / 100))
        nx, ny = _polar(cx, cy, r - 16, frac)
        parts.append(f'<line x1="{cx}" y1="{cy}" x2="{nx:.2f}" y2="{ny:.2f}" class="gauge-needle" stroke-width="2.5" stroke-linecap="round"/>')
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="4" class="gauge-hub"/>')
    parts.append("</svg>")
    return Markup("".join(parts))


def sparkline(values: list[float], title: str, width: int = 160, height: int = 40) -> Markup:
    """Línea de 12 meses con área suave y punto final. Color según el signo del periodo."""
    if len(values) < 2:
        return Markup('<div class="spark-empty">sin histórico</div>')
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    pad = 3
    step = (width - 2 * pad) / (len(values) - 1)
    points = [(pad + i * step, pad + (height - 2 * pad) * (1 - (v - low) / span)) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area = f"{pad},{height - pad} {line} {points[-1][0]:.1f},{height - pad}"
    trend = "up" if values[-1] >= values[0] else "down"
    change = (values[-1] / values[0] - 1) * 100 if values[0] else 0.0
    end_x, end_y = points[-1]
    return Markup(
        f'<svg class="spark spark-{trend}" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
        f'aria-label="{escape(title)}"><title>{escape(title)}: {values[0]:,.2f} → {values[-1]:,.2f} ({change:+.1f}%)</title>'
        f'<polygon points="{area}" class="spark-area"/>'
        f'<polyline points="{line}" class="spark-line" fill="none" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>'
        f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="3" class="spark-dot"/></svg>'
    )


def score_ring(score: int | None) -> Markup:
    """Anillo 0-10 para la puntuación de momento de inversión."""
    r, c = 20, 2 * math.pi * 20
    if score is None:
        return Markup('<svg class="ring" viewBox="0 0 48 48"><circle cx="24" cy="24" r="20" class="ring-track"/>'
                      '<text x="24" y="28" text-anchor="middle" class="ring-text">—</text></svg>')
    level = "high" if score >= 7 else "mid" if score >= 4 else "low"
    dash = c * max(0, min(10, score)) / 10
    return Markup(
        f'<svg class="ring ring-{level}" viewBox="0 0 48 48" role="img" aria-label="Puntuación {score} de 10">'
        f'<title>Puntuación {score}/10</title><circle cx="24" cy="24" r="{r}" class="ring-track"/>'
        f'<circle cx="24" cy="24" r="{r}" class="ring-fill" stroke-dasharray="{dash:.1f} {c:.1f}" transform="rotate(-90 24 24)"/>'
        f'<text x="24" y="29" text-anchor="middle" class="ring-text">{score}</text></svg>'
    )


def _scale(lo: float, hi: float, a: float, b: float):
    span = (hi - lo) or 1.0
    return lambda v: b - (v - lo) / span * (b - a)


def _nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    raw = (hi - lo) / count or 1.0
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step) * step
    return [round(first + i * step, 6) for i in range(count + 2) if first + i * step <= hi]


def _path(xs: list[float], values: list[float | None], y) -> str:
    """Polilínea que se corta donde falta el dato."""
    parts, pen = [], False
    for x, v in zip(xs, values):
        if v is None:
            pen = False
            continue
        parts.append(f"{'L' if pen else 'M'}{x:.1f},{y(v):.1f}")
        pen = True
    return " ".join(parts)


def trend_chart(chart, trades, stop: float | None, stop_label: str, title: str) -> Markup:
    """Precio con SMA del filtro, canal de ruptura (máximo/mínimo previos), entradas ▲, salidas ▼ y stop activo."""
    n = len(chart.dates)
    if n < 2:
        return Markup('<div class="spark-empty">sin histórico</div>')
    width, height, left, right, top, bottom = 900, 320, 8, 64, 12, 26
    values = [v for series in (chart.close, chart.sma, chart.upper, chart.lower) for v in series if v is not None]
    if stop is not None:
        values.append(stop)
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.04 or 1.0
    lo, hi = lo - pad, hi + pad
    y = _scale(lo, hi, top, height - bottom)
    step = (width - left - right) / (n - 1)
    xs = [left + i * step for i in range(n)]
    plot_right = width - right

    out = [f'<svg class="tchart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}"><title>{escape(title)}</title>']
    for t in _nice_ticks(lo, hi):
        out.append(f'<line x1="{left}" x2="{plot_right}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="tc-grid"/>'
                   f'<text x="{plot_right + 6}" y="{y(t) + 4:.1f}" class="tc-axis">{t:,.2f}</text>')
    month = None
    for i, d in enumerate(chart.dates):
        if d[:7] != month:
            month = d[:7]
            if i > 0 and i < n - 12:
                out.append(f'<line x1="{xs[i]:.1f}" x2="{xs[i]:.1f}" y1="{height - bottom}" y2="{height - bottom + 4}" class="tc-tick"/>'
                           f'<text x="{xs[i]:.1f}" y="{height - 8}" text-anchor="middle" class="tc-axis">{d[5:7]}/{d[2:4]}</text>')

    # Canal de ruptura como banda neutra
    band = [(x, u, l) for x, u, l in zip(xs, chart.upper, chart.lower) if u is not None and l is not None]
    if band:
        poly = " ".join(f"{x:.1f},{y(u):.1f}" for x, u, _ in band) + " " + " ".join(f"{x:.1f},{y(l):.1f}" for x, _, l in reversed(band))
        out.append(f'<polygon points="{poly}" class="tc-band"/>')
        out.append(f'<path d="{_path(xs, chart.upper, y)}" class="tc-edge"/><path d="{_path(xs, chart.lower, y)}" class="tc-edge"/>')
    out.append(f'<path d="{_path(xs, chart.sma, y)}" class="tc-sma"/>')
    out.append(f'<path d="{_path(xs, chart.close, y)}" class="tc-close"/>')

    if stop is not None:
        out.append(f'<line x1="{left}" x2="{plot_right}" y1="{y(stop):.1f}" y2="{y(stop):.1f}" class="tc-stop"/>'
                   f'<text x="{left + 4}" y="{y(stop) - 5:.1f}" class="tc-label">{escape(stop_label)} {stop:,.2f}</text>')

    index = {d: i for i, d in enumerate(chart.dates)}
    for t in trades:
        if t.entry_date in index:
            i = index[t.entry_date]
            px, py = xs[i], y(t.entry_price) + 12
            out.append(f'<path d="M{px:.1f},{py - 6:.1f} L{px + 6:.1f},{py + 5:.1f} L{px - 6:.1f},{py + 5:.1f} Z" class="tc-in">'
                       f'<title>Entrada {t.entry_date} a {t.entry_price:,.2f} · stop {t.initial_stop:,.2f}</title></path>')
        if t.exit_date and t.exit_date in index and t.exit_price is not None:
            i = index[t.exit_date]
            px, py = xs[i], y(t.exit_price) - 12
            out.append(f'<path d="M{px:.1f},{py + 6:.1f} L{px + 6:.1f},{py - 5:.1f} L{px - 6:.1f},{py - 5:.1f} Z" class="tc-out">'
                       f'<title>Salida {t.exit_date} a {t.exit_price:,.2f} ({"stop" if t.exit_reason == "stop" else "mínimo"}) · {t.r_multiple:+.2f} R</title></path>')

    # Punto final de cada línea (la leyenda HTML da la identidad y el valor)
    for value, cls in ((chart.sma[-1], "tc-dot-sma"), (chart.close[-1], "tc-dot")):
        if value is not None:
            out.append(f'<circle cx="{xs[-1]:.1f}" cy="{y(value):.1f}" r="4" class="{cls}"/>')

    # Capa de hover: una columna transparente por sesión con su tooltip nativo
    for i, d in enumerate(chart.dates):
        bits = [f"{d}", f"Cierre {chart.close[i]:,.2f}"]
        for lbl, series in (("SMA", chart.sma), ("Máx. previo", chart.upper), ("Mín. previo", chart.lower)):
            if series[i] is not None:
                bits.append(f"{lbl} {series[i]:,.2f}")
        out.append(f'<rect x="{xs[i] - step / 2:.1f}" y="{top}" width="{max(step, 1):.2f}" height="{height - top - bottom}" class="tc-hit">'
                   f'<title>{" · ".join(bits)}</title></rect>')
    out.append("</svg>")
    return Markup("".join(out))


def r_bars(trades, title: str) -> Markup:
    """Resultado de cada operación cerrada en múltiplos de R (riesgo inicial), barras desde cero."""
    closed = [t for t in trades if t.r_multiple is not None]
    if not closed:
        return Markup('<div class="spark-empty">sin operaciones cerradas</div>')
    width, height, left, right, top, bottom = 560, 150, 4, 40, 8, 8
    values = [t.r_multiple for t in closed]
    lo, hi = min(0.0, min(values)), max(0.0, max(values))
    y = _scale(lo, hi, top, height - bottom)
    slot = (width - left - right) / len(closed)
    bar = max(2.0, min(28.0, slot - 2))
    out = [f'<svg class="tchart rchart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}"><title>{escape(title)}</title>']
    for t in _nice_ticks(lo, hi, 3):
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="tc-grid"/>'
                   f'<text x="{width - right + 6}" y="{y(t) + 4:.1f}" class="tc-axis">{t:+g} R</text>')
    for i, t in enumerate(closed):
        r = t.r_multiple
        x = left + i * slot + (slot - bar) / 2
        y0, y1 = sorted((y(0), y(r)))
        h = max(y1 - y0, 1.5)
        cls = "tc-win" if r > 0 else "tc-loss"
        out.append(f'<rect x="{x:.1f}" y="{y0:.1f}" width="{bar:.1f}" height="{h:.1f}" rx="2" class="{cls}">'
                   f'<title>{t.entry_date} → {t.exit_date}: {r:+.2f} R ({t.return_pct:+.2f}%)</title></rect>')
    out.append(f'<line x1="{left}" x2="{width - right}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="tc-zero"/></svg>')
    return Markup("".join(out))


def regime_chart(chart, switches, title: str) -> Markup:
    """Cierre semanal con SMA rápida y lenta, fondo según la exposición decidida (0/50/100%) y marcas en cada traspaso."""
    n = len(chart.dates)
    if n < 2:
        return Markup('<div class="spark-empty">sin histórico</div>')
    width, height, left, right, top, bottom = 900, 320, 8, 64, 12, 26
    values = [v for series in (chart.close, chart.sma_fast, chart.sma_slow) for v in series if v is not None]
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.04 or 1.0
    lo, hi = lo - pad, hi + pad
    y = _scale(lo, hi, top, height - bottom)
    step = (width - left - right) / (n - 1)
    xs = [left + i * step for i in range(n)]
    plot_right = width - right

    out = [f'<svg class="tchart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}"><title>{escape(title)}</title>']
    # Fondo: una franja por semana con la exposición vigente tras esa decisión
    for i, e in enumerate(chart.exposure):
        if e:
            out.append(f'<rect x="{xs[i] - step / 2:.1f}" y="{top}" width="{step + 0.5:.2f}" height="{height - top - bottom}" class="rc-exp{e}"/>')
    for t in _nice_ticks(lo, hi):
        out.append(f'<line x1="{left}" x2="{plot_right}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="tc-grid"/>'
                   f'<text x="{plot_right + 6}" y="{y(t) + 4:.1f}" class="tc-axis">{t:,.0f}</text>')
    quarter = None
    for i, d in enumerate(chart.dates):
        q = (d[:4], (int(d[5:7]) - 1) // 3)
        if q != quarter:
            quarter = q
            if 0 < i < n - 6:
                out.append(f'<line x1="{xs[i]:.1f}" x2="{xs[i]:.1f}" y1="{height - bottom}" y2="{height - bottom + 4}" class="tc-tick"/>'
                           f'<text x="{xs[i]:.1f}" y="{height - 8}" text-anchor="middle" class="tc-axis">{d[5:7]}/{d[2:4]}</text>')
    out.append(f'<path d="{_path(xs, chart.sma_slow, y)}" class="tc-sma"/>')
    out.append(f'<path d="{_path(xs, chart.sma_fast, y)}" class="rc-fast"/>')
    out.append(f'<path d="{_path(xs, chart.close, y)}" class="tc-close"/>')

    index = {d: i for i, d in enumerate(chart.dates)}
    for s in switches:
        if s.date not in index:
            continue
        i = index[s.date]
        px = xs[i]
        tip = f"{s.date}: {s.from_pct}% → {s.to_pct}% · {'; '.join(s.reasons)}"
        if s.to_pct > s.from_pct:
            py = y(chart.close[i]) + 12
            out.append(f'<path d="M{px:.1f},{py - 6:.1f} L{px + 6:.1f},{py + 5:.1f} L{px - 6:.1f},{py + 5:.1f} Z" class="tc-in"><title>{escape(tip)}</title></path>')
        else:
            py = y(chart.close[i]) - 12
            out.append(f'<path d="M{px:.1f},{py + 6:.1f} L{px + 6:.1f},{py - 5:.1f} L{px - 6:.1f},{py - 5:.1f} Z" class="tc-out"><title>{escape(tip)}</title></path>')

    for value, cls in ((chart.sma_slow[-1], "tc-dot-sma"), (chart.close[-1], "tc-dot")):
        if value is not None:
            out.append(f'<circle cx="{xs[-1]:.1f}" cy="{y(value):.1f}" r="4" class="{cls}"/>')
    for i, d in enumerate(chart.dates):
        bits = [f"Semana {d}", f"Cierre {chart.close[i]:,.2f}"]
        for lbl, series in (("SMA rápida", chart.sma_fast), ("SMA lenta", chart.sma_slow)):
            if series[i] is not None:
                bits.append(f"{lbl} {series[i]:,.2f}")
        if chart.dd25[i] is not None:
            bits.append(f"Días de distribución {chart.dd25[i]}")
        if chart.exposure[i] is not None:
            bits.append(f"Exposición {chart.exposure[i]}%")
        out.append(f'<rect x="{xs[i] - step / 2:.1f}" y="{top}" width="{max(step, 1):.2f}" height="{height - top - bottom}" class="tc-hit">'
                   f'<title>{" · ".join(bits)}</title></rect>')
    out.append("</svg>")
    return Markup("".join(out))

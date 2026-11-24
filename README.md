# investment-agents

Sistema personal de investigación para un inversor pasivo europeo. Genera periódicamente un **informe HTML**
que valora **si es buen momento para invertir** en cada activo configurado, con horizonte de largo plazo: qué activos
tienen mejor momento relativo, dónde conviene esperar, con qué tamaño de entrada y bajo qué condiciones. El informe
es genérico: no depende de la situación ni de los planes de aportación de nadie.

La cabecera muestra los indicadores globales calculados en Python: índice de miedo y codicia de acciones (CNN) y de
cripto (alternative.me), VIX, bono a 10 años de EE. UU., índice dólar (DXY) y EUR/USD, junto con el régimen macro y
el riesgo para la renta variable. Debajo, los datos macro oficiales de FRED (tipos de la Fed y del BCE, curva 10a-2a,
tipo real, inflación esperada, IPC de EE. UU., IPCA de la zona euro, paro, diferencial high yield y condiciones
financieras NFCI) y el CAPE del S&P 500 con su percentil histórico. Después vienen las tarjetas resumen de cada activo (con minigráfico de 12 meses,
enlazadas a su ficha), los próximos eventos, la tabla comparativa y las fichas detalladas.

> No es un trader automático ni asesoramiento financiero. No ejecuta órdenes: el resultado es un informe para revisión humana.

## Cómo funciona

```
config.yaml ─► market (yfinance + caché) ─► quant (métricas en Python)
                                             │
             research (Tavily, presupuesto, caché, fuentes S1..Sn)
                                             │
             agentes LLM: macro ─► activo ×N ─► supervisor   (salidas Pydantic estrictas)
                                             │
             synthesis: guardrails deterministas, importes, contradicciones, datos faltantes
                                             │
             reports/informe_AAAA-MM-DD.html  +  data/runs/run_<id>.json  +  data/runs/history.sqlite
```

Decisiones de diseño:

- **El LLM es intercambiable**: Claude (por defecto, SDK de Anthropic) u OpenAI (Agents SDK), según `llm.provider`.
- **Los cálculos los hace Python**, nunca el LLM: rentabilidades 1/3/6/12m (en divisa local y en EUR), SMA 50/200,
  drawdown desde el máximo de 12 meses, volatilidad anualizada, comparativa vs benchmark y tendencia cuantitativa.
- **Datos oficiales antes que noticias**: las series macro se leen de FRED (CSV público, sin clave) y el CAPE del
  S&P 500 de la tabla mensual de multpl.com (datos de Shiller desde 1871). Python calcula variaciones interanuales,
  valores de hace 1 y 12 meses y percentiles del CAPE (toda la historia y 30 años). Caché de 24 h (`data.macro_data`,
  `data.macro_cache_ttl_hours`); el CAPE solo se asigna a activos con `cape_reference: "sp500"`. Si el analista llama
  «barato» a un activo con CAPE en percentil ≥ 80 (o «caro» en ≤ 20), se registra una contradicción.
- **Búsquedas en webs preferidas**: `research.preferred_domains` limita cada búsqueda a fuentes de referencia por
  ámbito (macro: BCE, FMI, Fed, Trading Economics…; renta variable: Morningstar, Research Affiliates, justETF…;
  cripto: Glassnode, Coin Metrics; materias primas: World Gold Council…). Un activo puede fijar las suyas con
  `preferred_domains`. Si una búsqueda da menos de `min_preferred_results` resultados, se repite sin restricción
  **después** de todas las demás y solo si queda presupuesto.
- **La búsqueda web la hace Python**, no los agentes. Cada resultado se registra como fuente (`S1`, `S2`…) con URL,
  fecha de publicación y fecha de consulta. Los agentes citan por id, y Python descarta ids inventados y convierte en
  «interpretación» cualquier «hecho» sin fuente válida. Así el gasto de créditos está acotado y todo es trazable.
- **El LLM propone y Python decide los límites**. Guardrails en `src/synthesis/guardrails.py`:
  puntuación mínima, convicción baja, datos parciales o pobres, sin macro, sentimiento eufórico, «no comprar solo
  porque haya subido» (subida >30% en 12m sin valuación favorable), «no comprar solo porque esté barato» (se exigen
  al menos 2 señales favorables), lenguaje prohibido (apalancamiento, ventas, cortos, certeza, precios objetivo),
  acción más conservadora entre analista y supervisor, capital de referencia y nº máximo de activos con entrada.
  Cada ajuste aparece en la sección «Guardrails aplicados» del informe.
- **Degradación controlada**: sin Tavily → análisis sin noticias y con convicción reducida; sin OpenAI → informe solo
  cuantitativo y ninguna entrada; ticker inválido → el activo queda excluido; proveedor caído → se usa caché caducada
  (marcada); fallo de plantilla → informe HTML de emergencia. Ningún fallo parcial impide generar el informe.
- Una métrica no disponible se muestra siempre como `insufficient_data`.

## Instalación en Fedora (desde cero)

```bash
sudo dnf install -y python3 python3-pip git cronie
sudo systemctl enable --now crond            # solo si vas a programarlo

cd ~/investment-agents
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt               # requirements-dev.txt para incluir pytest

cp .env.example .env                          # y rellena tus claves reales
chmod 600 .env
python -m src.main --validate-config
```

Claves necesarias (en `.env`, nunca en el código):

| Variable | Para qué | Si falta |
|---|---|---|
| `ANTHROPIC_API_KEY` | Agentes con Claude (`llm.provider: "anthropic"`, por defecto) | Informe solo cuantitativo, sin entradas |
| `OPENAI_API_KEY` | Agentes con OpenAI (`llm.provider: "openai"`) | Ídem |
| `TAVILY_API_KEY` | Noticias, contexto macro, eventos | Análisis sin fuentes web y con convicción reducida |
| `LLM_MODEL` (opcional) | Fijar el modelo | Se usa `llm.model` o el del proveedor (`claude-opus-5-5`) |

Las claves de API se crean y pagan aparte de las suscripciones de chat: una suscripción a Claude Pro o ChatGPT Plus
**no** sirve como clave de API. Para Claude, crea la clave en <https://console.anthropic.com> y añade saldo
(prepago por uso). Tavily tiene un plan gratuito de 1.000 créditos al mes, suficiente para unos 4-5 informes semanales.

### Proveedor LLM

```yaml
llm:
  provider: "anthropic"   # o "openai"
  model: null             # claude-opus-5-5 por defecto; p. ej. "claude-sonnet-5-5" o "claude-haiku-5-5" para abaratar
  effort: "medium"        # low | medium | high | xhigh | max
```

Con Claude cada agente es una única llamada a la Messages API con salida estructurada (`messages.parse` + el esquema
Pydantic) y fallback automático en el servidor si un clasificador de seguridad rechaza la petición por error.

## Uso

```bash
python -m src.main                                  # análisis completo -> reports/informe_AAAA-MM-DD.html
python -m src.main --asset msci_world               # un activo
python -m src.main --assets msci_world,bitcoin,gold # varios activos
python -m src.main --profiles semanal,refugio       # un informe por perfil (ver «Perfiles»)
```

Los informes parciales llevan los activos en el nombre (`informe_2026-11-16_msci_world-gold.html`) para no
sobrescribir el informe completo del día.

Ejecutar solo partes concretas:

| Opción | Efecto |
|---|---|
| `--mode quant` | Solo datos de mercado y cálculos. Sin LLM ni web: coste cero |
| `--mode no-web` | Datos + agentes LLM, sin Tavily (útil si solo tienes clave de Claude) |
| `--skip-macro` | Sin búsquedas ni agente macro |
| `--skip-supervisor` | Síntesis determinista en lugar del supervisor LLM |
| `--render data/runs/run_<id>.json` | Regenera el HTML de una ejecución guardada, sin llamadas a APIs |
| `--handoff DIR` | El análisis lo hace un LLM externo a través de ficheros (ver abajo) |
| `--handoff-status DIR` | Estado de una carpeta handoff en JSON (pendientes, inválidas, informes generados) |
| `--profiles a,b` / `--list-profiles` | Informes por perfil / listar perfiles |
| `--strategy gold_trend` (o `all`) | Solo las estrategias de tendencia: informe propio, sin LLM ni web (ver abajo) |
| `--site [DIR]` | Web pública en `docs/` (o DIR): último análisis + estrategias (oro, S&P 500) y factores del oro recalculados, sin importes |
| `--no-cache` | Ignora la caché local |
| `--history [N]` | Muestra las últimas N decisiones guardadas |
| `--list-assets` / `--validate-config` | Utilidades de configuración |
| `--log-level DEBUG` | Más detalle en consola (el fichero de log siempre es DEBUG) |

Códigos de salida: `0` OK · `2` configuración inválida · `3` informe de emergencia · `4` sin ningún dato de mercado · `5` handoff pendiente.

## Modo handoff (LLM externo por ficheros, sin API)

Permite que el análisis lo haga un LLM externo, por ejemplo Claude Code con tu suscripción, sin clave de API:

```bash
python -m src.main --assets msci_world,gold --handoff data/handoff/semana47
```

1. La primera ejecución descarga datos, busca en Tavily, guarda el estado en `state.json` y escribe
   `requests/macro.md` (instrucciones, datos, fuentes y esquema JSON). Sale con código `5` (pendiente).
2. El analista escribe `responses/macro.json`.
3. Se repite **el mismo comando**: Python valida la respuesta contra el esquema Pydantic (si no cumple, deja el error
   en `requests/<agente>.error.txt`), aplica los guardrails y pide la siguiente etapa: activos y luego supervisor.
4. Cuando están todas las respuestas, se genera el informe y la ejecución se guarda como cualquier otra.

Los datos y las fuentes se congelan en el primer paso, así que no cambian entre pasos. Usa una carpeta nueva para cada informe.

`--handoff-status DIR` devuelve en JSON el estado de cada petición (`pending`, `invalid` con el error de validación,
`answered`), la lista `waiting_for` y, al terminar, `result` con las rutas de los informes y las decisiones
(también en `DIR/result.json`).

### Desde Claude Code: `/analisis`

La skill del proyecto (`.claude/skills/analisis/SKILL.md`) automatiza todo el ciclo:

```
/analisis                         # perfil «semanal»
/analisis semanal,refugio         # varios informes en una sola orden
/analisis refugio --mode no-web   # sin Tavily
```

Crea `data/handoff/<fecha>_<perfiles>/`, ejecuta el pipeline, lanza en paralelo un subagente por petición pendiente
(macro → activos → un supervisor por perfil), comprueba cada respuesta con `--handoff-status`, repite hasta generar
los informes y muestra el resumen.

## Estrategia experimental: S&P 500, salir solo en bajista con salida de emergencia

Exposición 0/100% entre un fondo indexado al S&P 500 y un monetario en EUR, decidida solo con el cierre semanal de SPY:
se sale si el cierre está bajo la SMA de 40 semanas y la media baja (aplazando una semana si la semana fue de pánico), o
de inmediato si el VIX cierra la semana por encima de 35 con el cierre bajo la media; se vuelve a entrar con un cierre
más de un 1% sobre la media. Volumen, CMF, OBV y el cruce SMA10/40 son solo contexto. Configuración en
`regime_strategies.sp500_regime`; reglas, checklist del viernes, protocolo ante un desplome, resultados y limitaciones en
[`estrategias/sp500_regimen.md`](estrategias/sp500_regimen.md); indicador de TradingView en `estrategias/sp500_regimen.pine`.
Se calcula al generar la web (`--site`).

## Estrategia experimental: oro, seguimiento de tendencia

Reglas deterministas en Python sobre velas diarias del iShares Physical Gold (`PPFB.DE`, Xetra, EUR, ISIN IE00B4ND3602),
configuradas en `strategies.gold_trend` de `config.yaml`:

| Paso | Regla |
|---|---|
| Filtro | Cierre > SMA200 |
| Entrada | Cierre > máximo de las 20 sesiones previas → compra en la apertura siguiente |
| Stop inicial | Entrada − 2×ATR20 (Wilder) |
| Salida | Cierre ≤ stop inicial o cierre < mínimo de las 10 sesiones previas → venta en la apertura siguiente |
| Tamaño | Participaciones = `risk_pct` (1%) de `capital_eur` ÷ (riesgo por participación + comisión y diferencial de ida y vuelta), sin superar el capital |

El estado (`Sin posición`, `Entrada pendiente`, `Mantener`, `Salida pendiente`) se obtiene **reconstruyendo la posición
teórica** con las reglas sobre todo el histórico, no a partir de tu cartera real. El informe muestra los cuatro chequeos, una
explicación en texto, un gráfico de precio con la SMA, el canal de ruptura, las entradas y salidas y el stop activo, el
riesgo y el tamaño máximo, el resultado de cada operación pasada en múltiplos de R, y la auditoría (regla, datos usados
con su huella SHA-256 y fecha de generación). Antes del cierre de Xetra (`session_close`) se descarta la vela del día.

```bash
python -m src.main --strategy gold_trend   # reports/estrategia_AAAA-MM-DD_gold_trend.html + data/runs/strategy_<id>.json
```

Como lleva `asset_id: "gold"`, la misma sección aparece también en los informes que incluyen el oro (perfiles `semanal` y
`refugio`, también con `/analisis`). Es independiente de la puntuación y de los guardrails del análisis a largo plazo.

## Perfiles

Un perfil es un informe con nombre sobre un subconjunto de activos (`profiles` en `config.yaml`):

```yaml
profiles:
  semanal:        {title: "Visión completa", assets: all}
  refugio:        {title: "Refugio y alternativos", assets: [gold, bitcoin, commodities]}
```

Con varios perfiles, los datos de mercado, las búsquedas y los análisis macro y de cada activo se hacen **una sola
vez** sobre la unión de activos. El supervisor, los guardrails y el reparto del capital de referencia se aplican
**por perfil**: cada informe es independiente (`informe_AAAA-MM-DD_<perfil>.html`, `run_<id>-<perfil>.json`). En
handoff, los supervisores se llaman `supervisor_<perfil>`.

## Configuración (`config.yaml`)

`config.example.yaml` es una copia de referencia. Para **añadir un activo** solo hace falta otro bloque en `assets`:

```yaml
  - id: "emerging"                 # minúsculas, números y _
    name: "Mercados emergentes"
    ticker: "EIMI.L"
    asset_class: "equity"          # equity | bond | commodity | crypto | real_estate | private_markets | other
    currency: "USD"                # divisa de cotización (para convertir a EUR)
    benchmark_ticker: "IWDA.AS"    # opcional; por defecto defaults.benchmark_ticker
    valuation_proxy_ticker: "EEM"  # opcional: ETF del que leer PER / dividend yield
    search_terms: "emerging markets equities"
    enabled: true
```

Las reglas personales están en `rules` (presupuesto puntual, máximo por activo, puntuación mínima, fracción por
nivel `small/normal/staged`, tramos de la compra escalonada…). La configuración se valida de forma estricta: un campo
mal escrito da error en lugar de ignorarse.

Acciones posibles: `no_extra` (esperar), `small_extra` (entrada pequeña), `normal_extra` (entrada normal) y
`staged_extra` (entrada escalonada). Cómo se calculan los importes: `small_extra` = 33%, `normal_extra` = 66% y `staged_extra` = 100% de
`max_single_buy_eur` (en 3 tramos cada 14 días), redondeados a 10 EUR, como máximo en 2 activos y sin superar
`tactical_budget_eur` (capital de referencia) en total. El informe muestra la asignación también como % de ese capital.

## Auditoría

- `data/runs/run_<id>.json`: registro completo (configuración usada, datos, fuentes, salidas de los agentes,
  guardrails, decisiones, versiones de librerías). Permite regenerar el informe con `--render`.
- `data/runs/history.sqlite`: índice consultable (`runs`, `decisions`), p. ej.
  `sqlite3 data/runs/history.sqlite "select created_at, asset_id, final_action, amount_eur from decisions"`.
- `logs/run_<id>.log`: log de la ejecución con el `run_id` en cada línea.
- `data/cache/`: caché JSON de precios (12h), fundamentales (24h) y búsquedas (12h).

## Web pública (GitHub Pages)

```bash
python -m src.main --site          # genera docs/index.html, docs/sp500.html y docs/oro.html
scripts/publish_site.sh            # genera y publica (commit + push solo de docs/)
```

Sin histórico: cada generación sobrescribe las mismas URLs y borra de `docs/` cualquier otro HTML.

- **Portada** (`index.html`): régimen macro, indicadores, datos de FRED, resumen de la semana, tarjetas de los activos con
  la marca de cambio respecto al análisis completo anterior (p. ej. «4→6», «esperar→entrada pequeña»), eventos,
  comparativa y fuentes. Sale del último análisis completo guardado en `data/runs/` (con macro y analistas).
- **S&P 500** (`sp500.html`): 1) análisis semanal; 2) estrategia de régimen semanal (salir solo en bajista, con emergencia; 0/100%);
  3) checklist del viernes; 4) histórico de traspasos y comparativa; 5) reglas. Ver `estrategias/sp500_regimen.md`.
- **Oro** (`oro.html`): 1) datos e indicadores del análisis semanal; 2) estrategia de tendencia; 3) factores macro.
  La estrategia y los factores se **recalculan cada vez** que se genera la web, así que se pueden publicar a diario sin LLM.
- **Sin importes**: la web es pública y no muestra capital, presupuestos, importes ni participaciones. La estrategia
  muestra niveles de precio y el riesgo en % (por ejemplo, distancia al stop y tamaño máximo como % del capital).

### Factores macro del oro (`src/quant/gold_factors.py`)

Estudio informativo: **no cambia las señales de la estrategia**. Con el oro en EUR (`GC=F` ÷ EUR/USD, equivalente a
`PPFB.DE` pero con historia desde 2003) y cada factor (tipo real y tipo de la Fed, inflación esperada, IPC de EE. UU. e IPCA,
DXY, EUR/USD, VIX, diferencial high yield, petróleo y plata):

| Columna | Cálculo |
|---|---|
| Últimos 3m | Variación del factor en 3 meses (pb en tipos e inflación, pts en VIX, % en precios) |
| Si sube / si baja | Rentabilidad media del oro a 3 meses en los meses en que el factor venía subiendo o bajando, % de subidas y nº de meses |
| Solidez (t) | Diferencia entre ambos regímenes ÷ error estándar, con muestra efectiva = meses ÷ 3 (ventanas solapadas) |
| Corr. 52s | Correlación de variaciones semanales del último año (no en datos mensuales) |
| Lectura actual | «Favorece» o «perjudica» solo si la diferencia es ≥ 1 pp, hay ≥ 24 meses por régimen y \|t\| ≥ 1,5; si no, «sin efecto claro» |

Las series mensuales (IPC) se desplazan un mes por el retraso de publicación. Cada factor falla por separado. Junto a
cada uno se muestra la relación que suele citarse y se avisa cuando los datos del periodo dicen lo contrario.

## Programar con cron

```bash
crontab -e
# Lunes a las 08:00: análisis semanal con Claude Code (suscripción, modo handoff) y publicación de la web
0 8 * * 1 /home/<usuario>/investment-agents/scripts/run_claude_analysis.sh semanal
# De lunes a viernes a las 18:30: estrategia y factores del oro recalculados y web publicada (solo Python)
30 18 * * 1-5 /home/<usuario>/investment-agents/scripts/publish_site.sh >> /home/<usuario>/investment-agents/logs/site.log 2>&1
```

`run_claude_analysis.sh` lanza `/analisis` con `claude -p` y solo permite las herramientas que necesita; evita
ejecuciones solapadas (`flock`) y escribe en `logs/claude-cron.log`. Con API en lugar de suscripción:
`scripts/run_report.sh --profiles semanal` (salida en `logs/cron.log`).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Cubren rentabilidades, drawdown, medias móviles, volatilidad, validación YAML, generación del HTML (incluido el
informe de emergencia), guardrails, asignación de presupuesto y un pipeline completo con proveedores simulados (sin red).

## Estructura

```
src/
  main.py                CLI
  pipeline.py            orquestación por etapas
  settings.py            rutas y variables de entorno
  models.py              modelos de dominio compartidos (RunRecord, AssetSnapshot, Source…)
  config/                esquema Pydantic y carga del YAML
  core/                  errores, reintentos, caché, logging
  market/                datos de mercado (providers/ + servicio con caché y FX)
  quant/                 métricas puras, snapshot, estrategia de tendencia (trend.py, trend_snapshot.py)
                         y régimen semanal del S&P 500 (regime.py, regime_snapshot.py)
  research/              búsqueda web (providers/ + servicio con presupuesto y fuentes)
  analysis/              esquemas de salida, prompts y runners LLM (Anthropic y OpenAI Agents SDK)
  synthesis/             guardrails, importes, contradicciones, datos faltantes
  reporting/             contexto de plantilla y render HTML
  storage/               persistencia JSON + SQLite
templates/               report.html (informe), strategy.html (estrategias), _trend.html y _styles.html (compartidos)
tests/                   pytest
```

### Añadir fuentes de datos

- **Precios / valuación**: implementa `PriceProvider` o `FundamentalsProvider` (`src/market/providers/base.py`) y
  pásalo a `MarketDataService` en `src/pipeline.py`. Así se podría añadir CAPE o percentiles históricos de valuación,
  que hoy aparecen como `insufficient_data` porque yfinance no los ofrece.
- **Series macro**: añade una fila a `_FRED_INDICATORS` en `src/quant/indicators.py` (id de la serie de FRED y
  `level` o `yoy`); aparece sola en el informe y en la entrada de los agentes.
- **Búsqueda**: implementa `SearchProvider` (`src/research/providers/base.py`).

## Limitaciones conocidas

- yfinance es una fuente no oficial; puede cambiar o fallar. Los índices (`^GSPC`, `^NDX`) no tienen valuación, por eso
  se leen de un ETF equivalente (`valuation_proxy_ticker`).
- EV/EBITDA, P/S y los percentiles históricos (salvo el CAPE del S&P 500) no suelen estar disponibles con las fuentes actuales.
- El CAPE se lee de multpl.com (fuente no oficial); si cambia su formato, aparece como `insufficient_data`. Solo existe
  para el S&P 500: no se aplica al MSCI World ni al Nasdaq para no mezclar índices distintos.
- Las series de FRED mensuales (IPC, IPCA, paro) llevan unas semanas de retraso; la fecha de cada dato aparece en el informe.
- Los próximos eventos salen de fuentes web: confirma las fechas en los calendarios oficiales.
- El índice de miedo y codicia de CNN se lee de un endpoint de datos no oficial de su web; si cambia, ese medidor
  aparece como `insufficient_data` y el resto del informe no se ve afectado.

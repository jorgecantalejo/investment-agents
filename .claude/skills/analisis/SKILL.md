---
name: analisis
description: Genera uno o varios informes de inversión (perfiles de config.yaml) haciendo de analista en modo handoff, con un subagente por petición en paralelo. Usar cuando el usuario pida lanzar/generar el análisis, el informe semanal o informes por perfil.
argument-hint: "[perfiles separados por comas, p. ej. semanal,refugio] [--mode no-web] [--skip-macro] [--skip-supervisor] [--no-cache]"
allowed-tools: Bash(.venv/bin/python -m src.main *), Bash(date *), Bash(ls *), Read, Write, Agent
---

# /analisis — informes de inversión con Claude Code como analista

Orquesta el modo handoff de `src.main`: Python descarga datos, calcula métricas, busca fuentes y aplica los
guardrails; tú (y tus subagentes) solo escribís las respuestas JSON de los agentes LLM. No uses ninguna API de LLM.

Argumentos recibidos: `$ARGUMENTS`

## 1. Preparar

- Perfiles: la primera palabra de los argumentos que no empiece por `--` (separados por comas). Si no hay,
  usa `semanal`. Comprueba que existen con `.venv/bin/python -m src.main --list-profiles`; si alguno no existe,
  díselo al usuario con la lista disponible y para.
- Resto de argumentos (`--mode no-web`, `--skip-macro`, `--skip-supervisor`, `--no-cache`): pásalos tal cual.
- Carpeta: `data/handoff/<AAAA-MM-DD>_<perfiles unidos por ->` (fecha de `date +%F`). Si ya existe, mira
  `--handoff-status`: si `complete` es true, añade sufijo `_2`, `_3`…; si no, reanúdala (mismo comando).

Comando base (siempre idéntico en todos los pasos):

    .venv/bin/python -m src.main --profiles <perfiles> --handoff <DIR> [otros argumentos]

## 2. Bucle (máximo 8 vueltas)

1. Ejecuta el comando base (timeout amplio: la primera vez descarga datos y busca en la web).
   - Código `0` o `3`: informe(s) generado(s) → paso 3.
   - Código `5`: hay peticiones pendientes → sigue.
   - Código `2` u otro: muestra el error al usuario y para.
2. `.venv/bin/python -m src.main --handoff-status <DIR>` → lista `waiting_for`.
3. Lanza **en un solo mensaje** un subagente (`general-purpose`) por cada etiqueta de `waiting_for`, con el
   prompt de abajo. Las etapas van en orden (macro → activos → supervisores), así que en cada vuelta solo
   aparecen las de una etapa; los activos y los supervisores de varios perfiles van en paralelo.
4. Cuando terminen, vuelve a `--handoff-status`. Si alguna sigue `invalid` o `pending`, relanza solo esas
   (incluye el `error` en el prompt). Después vuelve al punto 1.

Prompt para cada subagente (sustituye `<DIR>` y `<label>`):

> Eres el agente «<label>» de un sistema de análisis de inversión. Lee la petición completa en
> `<DIR>/requests/<label>.md` y sigue exactamente sus instrucciones y reglas obligatorias: usa solo los datos
> y las fuentes (ids S1, S2…) que aparecen en ella, no inventes cifras, ids ni URLs, marca como
> `interpretation` lo que no tenga fuente y escribe los textos en español. Escribe **solo JSON válido** que
> cumpla el esquema del final de la petición en `<DIR>/responses/<label>.json` (sin bloque markdown).
> Después ejecuta `.venv/bin/python -m src.main --handoff-status <DIR>` y comprueba que `agents.<label>.state`
> es `answered`; si es `invalid`, corrige según `error` y repite. No modifiques ningún otro fichero.
> Responde solo con una línea: la acción propuesta y la puntuación (o el régimen, si eres macro).

No escribas tú las respuestas salvo que un subagente falle dos veces con la misma etiqueta.

## 3. Resultado

Lee `result` de `--handoff-status <DIR>` y presenta al usuario, por perfil:

- ruta del informe HTML,
- tabla breve: activo, puntuación, acción, importe,
- avisos relevantes (código `3` = informe de emergencia; menciona `fallback` si es true).

Termina con la carpeta handoff usada. No repitas el contenido del informe: el detalle está en el HTML.

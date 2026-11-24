# S&P 500 · Régimen semanal: SMA40 con banda de tolerancia

Estrategia experimental. La señal se calcula sobre el cierre semanal de **SPY**. Las órdenes se ejecutan como
**traspasos** entre un **fondo indexado al S&P 500** y un **fondo monetario en euros**. La exposición solo puede ser
**0 % o 100 %** del capital asignado.

- Implementación de referencia: `src/quant/regime.py` (reglas) y `src/quant/regime_snapshot.py` (estado, checklist y backtest).
- Parámetros: `regime_strategies.sp500_regime` en `config.yaml` (los valores de este documento son los de por defecto).
- Web: página `sp500.html`, que se recalcula a diario y toma la decisión con el cierre del viernes.
- TradingView: [`sp500_regimen.pine`](sp500_regimen.pine).

> Aviso: documento informativo y educativo. No es asesoramiento financiero.

---

## 1. Resumen ejecutivo

1. **Una sola media decide: la SMA de 40 semanas.** Es la misma idea que en el oro: una regla estructural y sencilla
   que se revisa una vez por semana.
2. **Banda de tolerancia para no traspasar por ruido.** Se sale solo si el cierre del viernes queda **más de un 1,5 %
   por debajo** de la SMA40 y se vuelve a entrar solo si cierra **más de un 1 % por encima**. Entre las dos líneas no
   se hace nada.
3. **Solo cuentan los cierres semanales.** Un desplome aislado en viernes (≥ 4 %) se evalúa con el cierre del jueves.
4. **El volumen no da órdenes.** Los días de distribución, el CMF, el OBV, el VIX y el cruce SMA10/SMA40 se muestran
   como contexto. Con traspasos de fondos que tardan días, el volumen diario es demasiado nervioso para operar.

### Por qué se simplificó (versión anterior: 0/50/100 % con flujo institucional)

La primera versión graduaba la exposición al 50 % con avisos de volumen y reentraba con Follow-Through Days. En
1993-2026 rendía **peor que la SMA40 sola**: +7,4 % anual con −27,8 % de caída máxima y unos 6,5 traspasos al año. Las
reentradas por FTD fallaron varias veces en 2008 y en 2020 se perdió el rebote hasta julio. Añadirle más indicadores
habría sido sobreoptimizar; la mejora vino de quitar.

---

## 2. Definiciones

| Símbolo | Definición |
|---|---|
| `C` | Cierre semanal de SPY: cierre de la última sesión de la semana (viernes o, si es festivo, el jueves) |
| `SMA40` | Media simple de los últimos 40 cierres semanales |
| Línea de salida | `SMA40 × (1 − 1,5 %)` |
| Línea de entrada | `SMA40 × (1 + 1 %)` |
| Choque de un día | La última sesión de la semana cae **≥ 4 %**: ese viernes se evalúa con `C` y `SMA40` de la sesión anterior |

Contexto (no da órdenes):

| Dato | Definición |
|---|---|
| `SMA10` | Media de 10 cierres semanales; el cruce con la SMA40 confirma el régimen, pero llega tarde |
| Día de distribución | Sesión con variación ≤ −0,20 % y volumen mayor que el de la sesión anterior; se cuentan en 25 sesiones (≤ 5 sano, ≥ 7 vigilar) |
| `CMF20` | Chaikin Money Flow de 20 sesiones |
| Divergencia OBV | `C` más alto que hace 10 semanas con el OBV más bajo |
| `VIX` | Cierre del VIX; > 25 es tensión |

---

## 3. Reglas exactas

Se evalúan solo al cierre semanal y el traspaso se ordena el siguiente día hábil.

| Exposición actual | Condición del viernes | Acción |
|---|---|---|
| 100 % | `C` < línea de salida | **Salir:** traspaso total al monetario (→ 0 %) |
| 0 % | `C` > línea de entrada | **Entrar:** traspaso total al fondo S&P 500 (→ 100 %) |
| Cualquiera | `C` entre las dos líneas | Nada |

Al arrancar (primera semana con SMA40) se está al 100 % si `C > SMA40` y al 0 % si no.

### Por qué un desplome aislado de un día no saca del mercado

- **No hay regla diaria.** Ningún dato diario dispara una orden.
- **La banda absorbe los pinchazos.** Perforar la media un 0,5 % un viernes y recuperarla el siguiente no genera traspasos.
- **Un desplome en viernes se neutraliza.** Si la última sesión cae ≥ 4 %, ese viernes se decide con el cierre del
  jueves. El desplome cuenta la semana siguiente, cuando ya se ha visto si hubo rebote.

---

## 4. Protocolo de excepciones: el viernes cae un 8-10 % y el lunes rebota

| Momento | Qué hacer |
|---|---|
| **Viernes, al cierre (−8/−10 %)** | **No ordenar nada.** Es un choque de un día (≥ 4 %), así que la decisión se toma con el cierre del **jueves**. Si el jueves ya estaba bajo la línea de salida, se sale; si no, se mantiene. |
| **Fin de semana** | Nada. No se ordenan traspasos por la noticia ni por los futuros. |
| **Lunes, rebota** | Nada. Entre semana no se decide. La web muestra una «lectura provisional», que solo es informativa. |
| **Viernes siguiente** | Evaluación normal, con el desplome ya incluido en la media. Si el cierre está bajo la línea de salida, se sale. |
| **Si el choque es de lunes a jueves** | No hay neutralización: el viernes ya muestra si hubo rebote y se aplica la regla. |

Si el traspaso ya está en curso cuando llega el choque, no se cancela ni se revierte.

---

## 5. Checklist semanal (5 minutos cada viernes, tras el cierre de EE. UU.)

Todo está en la web (`sp500.html` → «Checklist del viernes») y en la tabla del indicador de TradingView.

1. ¿Ha caído ≥ 4 % la última sesión? Entonces usa el cierre del jueves.
2. Anota `C` y la SMA40.
3. **Si estás al 100 %:** ¿`C` < línea de salida (SMA40 − 1,5 %)? → ordena el traspaso total al monetario.
4. **Si estás al 0 %:** ¿`C` > línea de entrada (SMA40 + 1 %)? → ordena el traspaso total al fondo.
5. En cualquier otro caso, no hagas nada. El contexto de volumen y VIX se mira, pero no cambia la decisión.

---

## 6. Indicador para TradingView (Pine Script v5)

Fichero: [`sp500_regimen.pine`](sp500_regimen.pine).

**Instalación**

1. Abre el gráfico **semanal (1W)** de `AMEX:SPY`.
2. Pine Editor → pega el script → «Añadir al gráfico».

**Qué dibuja**

- SMA40s (naranja), línea de salida (puntos naranjas) y línea de entrada (puntos verdes).
- SMA10s como contexto (lila).
- Fondo del gráfico según la exposición: verde = 100 %, rojo = 0 %.
- Etiquetas en cada cambio de exposición.
- «D» / «A» en semanas con ≥ 2 días de distribución / acumulación, y aspa naranja en las semanas de choque.
- Tabla con la exposición, la distancia a la media y el nivel que cambiaría la decisión.

**Alertas** (frecuencia «Una vez por cierre de barra»): «Entrar al 100 %» y «Salir al 0 %».

**Diferencias conocidas con Python:** TradingView usa precios sin ajustar por dividendos, así que las medias difieren
unas décimas, y la exposición inicial depende del inicio del histórico cargado. La referencia es la web.

---

## 7. Resultados históricos y limitaciones (SPY 1993-2026)

Supuestos: rentabilidad de SPY con dividendos, en USD; traspaso efectivo 2 sesiones después de la decisión; monetario
al 0 %; sin comisiones del fondo ni efecto divisa.

| | Rent. anual | Volatilidad | Máx. caída | Peor año | Exposición media | Traspasos/año |
|---|---:|---:|---:|---:|---:|---:|
| **SMA40 con banda −1,5 % / +1 %** | +9,8 % | 12,6 % | −19,9 % | −12,6 % | 78 % | 1,7 |
| SMA40 sin banda | +9,2 % | 12,5 % | −22,9 % | −12,0 % | 77 % | 3,4 |
| Cruce SMA10/SMA40 | +10,0 % | — | −33,7 % | −10,3 % | 77 % | 0,9 |
| Versión anterior (0/50/100 % con flujo) | +7,4 % | 11,0 % | −27,8 % | −12,6 % | 67 % | 6,5 |
| Comprar y mantener | +10,6 % | 18,7 % | −55,2 % | −36,8 % | 100 % | 0 |

Otras variantes probadas:

| Variante | Rent. anual | Máx. caída | Traspasos/año |
|---|---:|---:|---:|
| Dos cierres seguidos bajo la SMA40 | +9,4 % | −22,2 % | 2,3 |
| Banda −1 % / +1 % | +9,7 % | −19,9 % | 2,0 |
| Banda −1,5 % / +1,5 % | +9,8 % | −19,0 % | 1,5 |
| Banda −2 % / +1 % | +9,5 % | −23,9 % | 1,5 |

Caídas del S&P 500 de más del 15 %:

| Caída (pico → mínimo) | Comprar y mantener | Con banda | SMA40 sin banda |
|---|---:|---:|---:|
| 1998-07 → 1998-08 | −19,0 % | −19,0 % | −19,0 % |
| 2000-03 → 2002-10 | −47,5 % | −16,8 % | −18,6 % |
| 2007-10 → 2009-03 | −55,2 % | −10,7 % | −10,3 % |
| 2018-09 → 2018-12 | −19,3 % | −9,6 % | −13,1 % |
| 2020-02 → 2020-03 | −33,7 % | −11,3 % | −11,3 % |
| 2022-01 → 2022-10 | −24,5 % | −14,8 % | −11,3 % |

Lo que dicen los datos:

- **La banda mejora a la media sola en todo:** más rentabilidad, menos caída y la mitad de traspasos.
- **No es un ajuste fino:** todas las bandas entre −1 % y −1,5 % de salida con +1 % / +1,5 % de entrada dan entre
  +9,5 % y +9,8 % anual con caídas de −19 % a −20 %, y la mejora se mantiene en las dos mitades del histórico
  (1993-2009 y 2010-2026). Con −2 % de salida empeora la caída máxima.
- **Sigue costando rentabilidad frente a comprar y mantener** (unos 0,8 puntos al año), a cambio de reducir la caída
  máxima de −55 % a −20 %.
- **No protege de caídas muy rápidas:** en 1998 la caída fue completa antes del primer cierre semanal bajo la banda.
- **Sigue habiendo serruchos:** p. ej. salida el 27-oct-2023 y reentrada el 3-nov-2023. Son pocos, pero existen.
- **En 2020** salió el 28-feb y volvió a entrar el 29-may (la versión anterior no volvió hasta julio).
- **Sesgo de ajuste:** la banda se eligió mirando este histórico. Las bandas vecinas dan resultados parecidos, pero el
  resultado es algo optimista.

La página web recalcula estas tablas cada día con todo el histórico disponible.

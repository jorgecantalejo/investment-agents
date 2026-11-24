# S&P 500 · Régimen semanal: salir solo en bajista, con salida de emergencia

Estrategia experimental. La señal se calcula sobre el cierre semanal de **SPY** y del **VIX**. Las órdenes se ejecutan
como **traspasos** entre un **fondo indexado al S&P 500** y un **fondo monetario en euros**. La exposición solo puede
ser **0 % o 100 %** del capital asignado.

- Implementación de referencia: `src/quant/regime.py` (reglas) y `src/quant/regime_snapshot.py` (estado, checklist y backtest).
- Parámetros: `regime_strategies.sp500_regime` en `config.yaml` (los valores de este documento son los de por defecto).
- Web: página `sp500.html`, que se recalcula a diario y toma la decisión con el cierre del viernes.
- TradingView: [`sp500_regimen.pine`](sp500_regimen.pine).

> Aviso: documento informativo y educativo. No es asesoramiento financiero.

---

## 1. Resumen ejecutivo

1. **Estar dentro casi siempre; salir solo cuando el mercado es bajista de verdad.** Bajista significa que el cierre
   semanal está bajo la media de 40 semanas **y** esa media ya está bajando. Una caída dentro de una tendencia alcista
   no saca del mercado.
2. **No vender en el suelo.** Si la semana de la señal bajista ha sido de pánico (caída ≥ 3,5 % o RSI diario < 30), la
   salida se aplaza al viernes siguiente.
3. **Salida de emergencia para desplomes rápidos.** Si el VIX cierra la semana por encima de 35 con el cierre bajo la
   media, se sale sin esperar. Es un seguro para caídas como la de marzo de 2020, cuando la media seguía subiendo.
4. **Volver a entrar con un cierre semanal más de un 1 % sobre la media.**
5. **Solo cuentan los cierres semanales.** El volumen, el CMF, el OBV y el cruce SMA10/SMA40 son solo contexto.

### Cómo se llegó aquí

| Versión | Rent. anual | Máx. caída | Traspasos/año | Por qué se descartó |
|---|---:|---:|---:|---|
| 0/50/100 % con flujo institucional y FTD | +7,4 % | −28 % | 6,5 | Peor que la SMA40 sola |
| SMA40 con banda −1,5 % / +1 % | +9,8 % | −20 % | 1,8 | Neta de impuestos, rinde menos que comprar y mantener |
| Solo bajista, sin emergencia | +11,7 % | −34 % | 1,0 | Se comió entera la caída de 2020 |
| **Solo bajista + emergencia VIX > 35** | **+11,3 %** | **−21 %** | **1,1** | — |

---

## 2. Definiciones

| Símbolo | Definición |
|---|---|
| `C` | Cierre semanal de SPY: cierre de la última sesión de la semana (viernes o, si es festivo, el jueves) |
| `SMA40` | Media simple de los últimos 40 cierres semanales |
| Media bajando | `SMA40` de esta semana < `SMA40` de hace 4 semanas |
| `VIX` | Cierre del VIX en la misma sesión |
| Semana estirada | Caída semanal (`C` frente al cierre semanal anterior) ≥ 3,5 %, o RSI(14) diario de Wilder < 30 al cierre |
| Línea de entrada | `SMA40 × 1,01` |

---

## 3. Reglas exactas (en este orden, cada viernes)

| # | Si estás… | Condición | Acción |
|---|---|---|---|
| 1 | al 100 % | `VIX > 35` **y** `C < SMA40` | **Emergencia:** traspaso total al monetario, sin esperar |
| 2 | al 100 %, con una salida aplazada | `C < SMA40` y la media bajando | Traspaso total al monetario. Si no se cumple, la salida aplazada se anula |
| 3 | al 100 % | `C < SMA40` y la media bajando | Si la semana está estirada: **aplazar** al viernes siguiente. Si no: traspaso total al monetario |
| 4 | al 0 % | `C` > línea de entrada | Traspaso total al fondo |

Si no se cumple nada, no se hace nada. El aplazamiento se usa una sola vez: el viernes siguiente solo cuenta si sigue
siendo bajista. Las aportaciones se mantienen al margen: la regla parte de un capital y no aporta más.

---

## 4. Protocolo de excepciones: el viernes cae un 8-10 % y el lunes rebota

| Situación | Qué hacer |
|---|---|
| **VIX > 35 y cierre bajo la SMA40** | Salir (emergencia). Es la regla 1: en un pánico de verdad es preferible no esperar. Puede ser una falsa alarma (1998, 2010, 2011 y 2025 se recuperaron), y es el precio del seguro. |
| **VIX ≤ 35, cierre bajo la SMA40 con la media bajando** | La semana está estirada, así que no se vende: se aplaza al viernes siguiente. |
| **Cierre bajo la SMA40 con la media subiendo** | Nada. Es una corrección dentro de un mercado alcista. |
| **Lunes, rebota** | Nada. Entre semana no se decide. La web muestra una «lectura provisional», que solo es informativa. |

Si el traspaso ya está en curso cuando llega el rebote, no se cancela ni se revierte.

---

## 5. Checklist semanal (5 minutos cada viernes, tras el cierre de EE. UU.)

Todo está en la web (`sp500.html` → «Checklist del viernes») y en la tabla del indicador de TradingView.

1. **Si estás al 100 %:**
   - ¿VIX > 35 y SPY bajo la SMA40? → **sal**.
   - ¿Tenías una salida aplazada? Si SPY sigue bajo la SMA40 y la media baja → **sal**; si no, sigues dentro.
   - ¿SPY bajo la SMA40 y la media más baja que hace 4 semanas? Si la semana cayó ≥ 3,5 % o el RSI < 30 → **aplaza**;
     si no → **sal**.
2. **Si estás al 0 %:** ¿SPY cierra por encima de la SMA40 + 1 %? → **entra**.
3. En cualquier otro caso, no hagas nada.

---

## 6. Indicador para TradingView (Pine Script v5)

Fichero: [`sp500_regimen.pine`](sp500_regimen.pine). Gráfico **semanal (1W)** de `AMEX:SPY` → Pine Editor → pegar →
«Añadir al gráfico».

- SMA40s (naranja; roja cuando baja), línea de entrada (puntos verdes) y SMA10s como contexto.
- Fondo verde = 100 %, rojo = 0 %; rombo naranja = salida aplazada; etiquetas con el motivo de cada cambio.
- Tabla con el checklist del último viernes.
- Alertas «Entrar al 100 %» y «Salir al 0 %» (frecuencia «Una vez por cierre de barra»).

Diferencias con Python: precios sin ajustar por dividendos y RSI de TradingView. La referencia es la web.

---

## 7. Resultados históricos y limitaciones (SPY 1993-2026)

Supuestos: rentabilidad de SPY con dividendos, en USD; traspaso efectivo 2 sesiones después de la decisión (martes);
sin comisiones del fondo ni efecto divisa.

**1.000 € invertidos el 3-dic-1993**, monetario al T-Bill a 3 meses − 0,15 %, impuesto del ahorro al reembolsar todo
al final:

| | Neto final | Máx. caída | Caída en 2020 | Traspasos |
|---|---:|---:|---:|---:|
| **Solo bajista + emergencia VIX > 35** | **29.975 €** | −20 % | −12 % | 37 |
| Solo bajista, sin emergencia | 32.154 € | −34 % | −34 % | 32 |
| SMA40 con banda −1,5 / +1 | 19.637 € | −20 % | −11 % | 59 |
| Comprar y mantener | 22.151 € | −55 % | −34 % | 0 |

Caídas del S&P 500 de más del 15 % (monetario al 0 %):

| Caída (pico → mínimo) | Comprar y mantener | Esta estrategia |
|---|---:|---:|
| 1998-07 → 1998-08 | −19,0 % | −19,0 % |
| 2000-03 → 2002-10 | −47,5 % | −15,4 % |
| 2007-10 → 2009-03 | −55,2 % | −12,7 % |
| 2018-09 → 2018-12 | −19,3 % | −8,6 % |
| 2020-02 → 2020-03 | −33,7 % | −11,3 % |
| 2022-01 → 2022-10 | −24,5 % | −12,6 % |

Lo que dicen los datos:

- **La salida por mercado bajista es la que gana dinero.** Esquivó la mayor parte de 2000-02 y 2008 y volvió a
  entrar a tiempo.
- **La emergencia es un seguro, no una mejora.** Cuesta unos 2.200 € (un 7 % del final) frente a no tenerla y salta
  pocas veces (6 en 33 años). Solo en 2020 fue decisiva; en 1998, 2010, 2011 y abril de 2025 fue falsa alarma. Con un
  umbral de 40 el resultado es parecido.
- **Por qué no se puede prever un desplome como el de 2020:** el viernes anterior el índice estaba un 10 % sobre su
  media, con el VIX en 17. Ninguna regla semanal lo anticipa; lo que se consigue es reaccionar tras la primera semana
  de pánico.
- **Las caídas muy rápidas sin pánico de VIX no se evitan:** en 1998 la caída fue completa antes de cualquier señal.
- **Sesgo de ajuste:** las reglas se eligieron mirando este histórico. Los umbrales vecinos (pendiente de 1 a 13
  semanas, VIX 35-40, caída semanal 3-5 %, RSI 25-35) dan resultados parecidos, pero el resultado es optimista.
- **Vender un ETF en lugar de traspasar fondos empeora el resultado** en unos 2,5 puntos al año por los impuestos de
  cada venta; ejecutar antes no compensa.

La página web recalcula las tablas de rentabilidad y de caídas cada día con todo el histórico disponible, con el
monetario al 0 %.

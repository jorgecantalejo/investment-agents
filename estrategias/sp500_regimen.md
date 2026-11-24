# S&P 500 · Estrategia de régimen semanal y flujo institucional

Estrategia experimental. Las señales se calculan sobre **SPY** (precio y volumen diarios) y el **VIX**. Las órdenes se
ejecutan como **traspasos** entre un **fondo indexado al S&P 500** y un **fondo monetario en euros**. La exposición
solo puede ser **0 %, 50 % o 100 %** del capital asignado.

- Implementación de referencia: `src/quant/regime.py` (reglas) y `src/quant/regime_snapshot.py` (estado, checklist y backtest).
- Parámetros: `regime_strategies.sp500_regime` en `config.yaml` (los valores de este documento son los de por defecto).
- Web: página `sp500.html`, que se recalcula a diario y toma la decisión con el cierre del viernes.
- TradingView: [`sp500_regimen.pine`](sp500_regimen.pine).

> Aviso: documento informativo y educativo. No es asesoramiento financiero.

---

## 1. Resumen ejecutivo

1. **Solo cuentan los cierres semanales.** Se decide con el cierre de la última sesión de la semana (normalmente el
   viernes) y se ordena el traspaso después. Un día aislado no cambia nada: para salir del todo hacen falta **dos
   cierres semanales seguidos** por debajo de la media de 40 semanas.
2. **El precio manda: el régimen lo marcan las medias semanales.** El mercado es alcista cuando el cierre semanal está
   por encima de la SMA de 40 semanas y la SMA de 10 está por encima de la de 40. Fuera de ese régimen nunca se está
   al 100 %.
3. **El volumen gradúa el tamaño.** Los días de distribución, el Chaikin Money Flow, la divergencia OBV y el VIX
   deciden si se está al 100 % (flujo alineado) o al 50 % (divergencias o tensión). También sirven para confirmar una
   salida.
4. **Se vuelve a entrar por fases.** Tras un mercado bajista, un **Follow-Through Day** permite entrar al 50 %
   (protegido por el mínimo del rebote). El 100 % solo llega cuando tendencia y flujo vuelven a estar alineados.

---

## 2. Definiciones (todas numéricas)

| Símbolo | Definición |
|---|---|
| `C` | Cierre semanal de SPY: cierre de la última sesión de la semana (viernes o, si es festivo, el jueves) |
| `SMA10`, `SMA40` | Medias simples de los últimos 10 y 40 cierres semanales |
| Día de distribución (DD) | Sesión con variación ≤ **−0,20 %** y volumen **mayor** que el de la sesión anterior |
| `DD25` | Número de DD en las últimas **25 sesiones**, contando la del viernes |
| `CMF20` | Chaikin Money Flow de 20 sesiones: Σ(MFM·Vol) / ΣVol, con MFM = ((C−L) − (H−C)) / (H−L) |
| Divergencia OBV | `C` > `C` de hace 10 semanas **y** OBV < OBV de hace 10 semanas (el precio sube y el volumen neto sale) |
| `VIX` | Cierre del VIX en la sesión de evaluación |
| Intento de rebote | Solo se sigue con exposición 0 %. Empieza con el mínimo de la sesión de la salida; el **día 1** es la primera sesión que cierra por encima de la anterior; cualquier sesión cuyo mínimo pierda el mínimo de referencia **reinicia** el intento |
| Follow-Through Day (FTD) | Sesión del **día 4 al 7** del intento con subida **≥ +1,25 %** y volumen **mayor** que el de la sesión anterior. Si el día 7 pasa sin FTD, el intento caduca hasta que se marque un mínimo nuevo |
| Choque de un día | La última sesión de la semana cae **≥ 4 %** |

**Avisos de flujo y macro.** Hay cuatro posibles:

- **A1:** `DD25 ≥ 7`.
- **A2:** `CMF20 < 0` este viernes **y** el anterior.
- **A3:** hay divergencia OBV.
- **A4:** `VIX > 25` este viernes **y** el anterior.

**Por qué el umbral es 7 y no 5.** Con el volumen de SPY y la definición de −0,2 %, la **mediana** de días de
distribución en 25 sesiones (1993-2026) es 5. Un 65 % de las semanas tiene 5 o más. Con un umbral de 5 la estrategia
viviría casi siempre al 50 %. Por eso 7 cuenta como aviso y hasta 5 se considera flujo sano. En ese tramo, 7 está en
torno al percentil 75 de la serie.

---

## 3. Reglas exactas de entrada (traspaso monetario → S&P 500)

Se evalúan solo al cierre semanal y se ordenan el siguiente día hábil.

### Al 100 % (ALINEADO): deben cumplirse **todas** a la vez

1. `C > SMA40`
2. `SMA10 > SMA40`
3. `DD25 ≤ 5`
4. `CMF20 > 0`
5. Sin divergencia OBV.
6. `VIX ≤ 25`

Se aplica desde cualquier estado (0 % o 50 %).

### Al 50 % desde 0 %: basta **una** de estas dos

- **Reentrada por FTD (50 % «de rebote»):** ha habido un FTD desde el viernes anterior **y** `C` > mínimo del intento
  de rebote.
  - Ese 50 % se deshace (vuelta a 0 %) si un cierre semanal queda por debajo del mínimo del intento.
  - Pasa a ser un 50 % normal cuando `C > SMA40`.
- **Reentrada por tendencia:** `C > SMA40` y `SMA10 > SMA40`, aunque el flujo aún no esté alineado.

---

## 4. Reglas exactas de salida y protección (traspaso S&P 500 → monetario)

### Del 100 % al 50 % (REDUCIR): basta **una**

- `C < SMA40` (primer cierre semanal bajo la media).
- **Dos o más** avisos A1-A4 a la vez.

### Al 0 % (SALIDA) desde 100 % o 50 %: deben cumplirse **todas**

1. `C < SMA40` este viernes.
2. `C < SMA40` también el viernes anterior: es el **segundo cierre semanal seguido** bajo la media.
3. Al menos una confirmación de flujo o de tendencia: `DD25 ≥ 7`, `CMF20 < 0` o `SMA10 < SMA40`.

Si los dos cierres bajo la media no vienen confirmados por el flujo, la exposición se queda en el 50 %.

### Por qué un desplome aislado de un día no saca del mercado

- **No hay regla diaria.** Ningún dato diario dispara una orden por sí solo: los días de distribución, el CMF y el OBV
  solo se miran el viernes.
- **El 0 % exige estructura.** Hacen falta dos cierres semanales bajo la SMA40, es decir, al menos 6 sesiones. Además,
  el flujo tiene que confirmar la ruptura.
- **Los avisos que un solo día puede provocar piden persistencia.** El CMF y el VIX deben estar en zona de aviso dos
  viernes seguidos. Para el 100 → 50 % hacen falta dos avisos a la vez.
- **Un desplome en viernes se neutraliza.** Si la última sesión de la semana cae ≥ 4 %, ese viernes se evalúa con los
  datos del jueves (cierre, medias, DD25, CMF, OBV y VIX). El desplome se tendrá en cuenta el viernes siguiente, cuando
  ya se haya visto si hubo rebote.

---

## 5. Protocolo de gestión de excepciones: el viernes cae un 8-10 % y el lunes rebota

| Momento | Qué hacer |
|---|---|
| **Viernes, al cierre (−8/−10 %)** | **No ordenar nada.** Es un choque de un día (≥ 4 %), así que la decisión de esta semana se toma con el cierre del **jueves**. Si con el jueves no había señal, se mantiene la exposición. Si el jueves ya cumplía una regla (p. ej. el segundo cierre bajo la SMA40 confirmado), se ejecuta esa regla y no otra. |
| **Fin de semana** | Nada. No se ordenan traspasos por la noticia ni por el precio de los futuros. |
| **Lunes, rebota** | Nada. Es un sistema semanal: entre semana no se decide. La web mostrará una «lectura provisional», que solo es informativa. |
| **Viernes siguiente** | Evaluación normal, con el desplome ya incluido en DD25, CMF, OBV y medias. Si el rebote recupera la SMA40 no pasa nada. Si el cierre sigue bajo la SMA40, es el **primer** cierre bajo la media y como máximo se baja al 50 %. El 0 % requeriría otro cierre semanal bajo la media, confirmado por el flujo. |
| **Si el choque es de lunes a jueves** | No hay neutralización: el viernes ya muestra si hubo rebote. Aun así, ese viernes solo puede bajar al 50 % (primer cierre bajo la SMA40). El CMF y el VIX necesitan dos viernes seguidos para contar como aviso. |
| **Si ya estás al 0 % y llega el desplome** | El desplome reinicia el intento de rebote (nuevo mínimo). El FTD se buscará entre los días 4 y 7 contados desde el primer día alcista. |

Si el traspaso ya está en curso cuando llega el choque, no se cancela ni se revierte: la regla siguiente se aplica el
viernes que toque.

---

## 6. Checklist semanal (10 minutos cada viernes, tras el cierre de EE. UU.)

Todo está en la web (`sp500.html` → «Checklist del viernes») y en la tabla del indicador de TradingView.

1. **Tendencia.** Anota el cierre semanal de SPY, la SMA10s y la SMA40s.
   - ¿`C > SMA40`? ¿`SMA10 > SMA40`?
   - ¿Cuántos cierres semanales seguidos lleva bajo la SMA40 (0, 1, 2…)?
   - ¿Ha caído ≥ 4 % la última sesión? Entonces usa los datos del jueves.
2. **Días de distribución en 25 sesiones.** Lectura: ≤ 5 sano · 6 neutro · ≥ 7 aviso A1.
3. **CMF20.** ¿Es > 0? Si es < 0, ¿también lo era el viernes anterior? (aviso A2)
4. **OBV a 10 semanas.** ¿El precio está más alto que hace 10 semanas con el OBV más bajo? (aviso A3)
5. **VIX al cierre.** ¿Es > 25? ¿También lo era el viernes anterior? (aviso A4)
6. *(Solo si estás al 0 %)* **Intento de rebote.** ¿En qué día va? ¿Ha habido FTD esta semana? ¿Se respeta el mínimo
   del intento?

**Decisión:** aplica las reglas de los apartados 3 y 4 a tu exposición actual.

- Si la exposición resultante es distinta, ordena el traspaso el lunes:
  - 100 → 50 %: traspasa la mitad del fondo S&P 500 al monetario.
  - 50 → 0 %: traspasa el resto.
  - 0 → 50 % o 50 → 100 %: traspasa del monetario al fondo.
- Si es la misma, no hagas nada.

---

## 7. Indicador para TradingView (Pine Script v5)

Fichero: [`sp500_regimen.pine`](sp500_regimen.pine).

**Instalación**

1. Abre el gráfico **semanal (1W)** de `AMEX:SPY`.
2. Pine Editor → pega el script → «Añadir al gráfico».

**Qué dibuja**

- SMA10s (lila, discontinua) y SMA40s (naranja).
- Fondo del gráfico según la exposición: verde = 100 %, naranja = 50 %, rojo = 0 %.
- Etiquetas en cada cambio de exposición, con el motivo.
- «D» sobre las semanas con ≥ 2 días de distribución y «A» bajo las semanas con ≥ 2 días de acumulación.
- Aspa naranja en las semanas de choque.
- Tabla con el checklist del último viernes.

**Alertas**

Crea una alerta sobre el indicador, frecuencia «Una vez por cierre de barra». Hay tres:

- «Subir exposición».
- «Reducir al 50 %».
- «Salir al 0 %».

**Diferencias conocidas con la implementación en Python**

- TradingView usa precios sin ajustar por dividendos, así que las medias difieren unas décimas.
- La exposición inicial depende del inicio del histórico cargado.

La referencia es la web.

---

## 8. Resultados históricos y limitaciones (SPY 1993-2026)

Supuestos:

- Rentabilidad de SPY con dividendos, en USD.
- Traspaso efectivo 2 sesiones después de la decisión.
- Monetario al 0 %.
- Sin comisiones del fondo ni efecto divisa.

| | Rent. anual | Volatilidad | Máx. caída | Peor año | Exposición media |
|---|---:|---:|---:|---:|---:|
| Esta estrategia | +7,4 % | 11,0 % | −27,8 % | −12,6 % | 67 % |
| Solo SMA40 semanal (0/100 %) | +9,2 % | 12,5 % | −22,9 % | −12,0 % | 77 % |
| Comprar y mantener | +10,6 % | 18,7 % | −55,2 % | −36,8 % | 100 % |

Lo que dicen los datos:

- **Reduce mucho las caídas.** En 2000-02, 2007-09 y 2022 la caída de la estrategia fue aproximadamente la mitad o
  menos que la del índice.
- **Cuesta rentabilidad**, unos 3 puntos al año frente a comprar y mantener.
- **La capa de volumen no mejora a la media de 40 semanas sola.** En este histórico, la regla simple «100 % sobre la
  SMA40, 0 % debajo» rinde más con una caída máxima parecida.
  - La causa principal son las reentradas por FTD fallidas de 2008 (varios FTD seguidos de nuevos mínimos).
  - También pesa que la estrategia pasa más tiempo al 50 %.
- **En 2020 (caída en V) evitó lo peor del desplome, pero se perdió el rebote.**
  - Bajó al 50 % el 28-feb y salió al 0 % el 6-mar.
  - No hubo un FTD válido en la ventana de los días 4-7, así que no volvió a entrar (al 50 %) hasta el 10-jul.
- **Frecuencia de traspasos:** unos 6 al año. Cada traspaso entre fondos tarda varios días.
- **Sesgo de ajuste:** los umbrales (7/5 días de distribución, 2 avisos, VIX 25) se fijaron mirando este mismo
  histórico, así que el resultado es optimista.

La página web recalcula esta tabla y la de caídas cada día, con todo el histórico disponible.

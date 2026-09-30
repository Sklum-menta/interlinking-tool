"""Lógica de negocio: cruce de datasets, scoring y generación de la
propuesta de interlinking.

Este módulo es intencionadamente independiente de Streamlit para poder
testearlo con pytest de forma aislada (ver tests/test_scoring.py y
tests/test_exclusion.py).
"""
from __future__ import annotations

import re

import pandas as pd

from core.config import AffinityScores, LimitesPropuesta, OportunidadSEO, ScoringWeights
from core.data_loader import InputDatasets, normalize_url

# ---------------------------------------------------------------------------
# 1) Tabla maestra: una fila por URL indexable con todos sus atributos
# ---------------------------------------------------------------------------


def _build_relevancia_lookup(relevancia_categoria: pd.DataFrame | None) -> dict[tuple[str, str], float]:
    """Construye un diccionario {(categoria_principal, categoria_secundaria): relevancia}.

    Una fila con `categoria_secundaria` vacía se interpreta como
    "aplica a toda la categoría principal, sea cual sea la
    subcategoría" (igual que en la plantilla de Sheets: hay una tabla
    de relevancia por categoría principal y otra, aparte, por
    subcategoría).
    """
    lookup: dict[tuple[str, str], float] = {}
    if relevancia_categoria is None or relevancia_categoria.empty:
        return lookup
    for _, row in relevancia_categoria.iterrows():
        principal = str(row.get("categoria_principal", "") or "").strip().lower()
        secundaria = str(row.get("categoria_secundaria", "") or "").strip().lower()
        if not principal:
            continue
        try:
            valor = float(row.get("relevancia"))
        except (TypeError, ValueError):
            continue
        if pd.isna(valor):
            continue
        lookup[(principal, secundaria)] = valor
    return lookup


def _lookup_relevancia(
    principal: str | None, secundaria: str | None, lookup: dict[tuple[str, str], float]
) -> float:
    """Valor por defecto 0.5 (neutro) si no hay ajuste manual para esa
    categoría/subcategoría. Nunca marca `pendiente_confirmar`: es un
    ajuste opcional de negocio, no un dato obligatorio de entrada.
    """
    p = str(principal or "").strip().lower()
    s = str(secundaria or "").strip().lower()
    if (p, s) in lookup:
        return lookup[(p, s)]
    if (p, "") in lookup:
        return lookup[(p, "")]
    return 0.5


def build_master_table(
    datasets: InputDatasets,
    relevancia_categoria: pd.DataFrame | None = None,
    prioridad_negocio: pd.DataFrame | None = None,
    grupos_aislados: list[str] | None = None,
    search_console: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Cruza crawl + volumen + taxonomía por URL y añade el nº de
    enlaces entrantes y salientes actuales (a partir del dataset de
    enlaces), la salud técnica (si el crawl la trae) y el rendimiento en
    Search Console (opcional), más los dos ajustes MANUALES de negocio
    opcionales:

    - `relevancia_categoria`: tabla con columnas `categoria_principal`,
      `categoria_secundaria` (opcional) y `relevancia` (0-1) — el
      equipo la ajusta a mano desde la interfaz para dar más o menos
      importancia a ciertas categorías (p.ej. mensualmente).
    - `prioridad_negocio`: tabla con columnas `url` y
      `prioridad_negocio` — para penalizar o beneficiar URLs concretas
      por motivos de negocio puntuales.
    - `search_console`: tabla con columnas `url`, `clics_28d`,
      `impresiones_28d`, `posicion_media` (ver
      `core.data_loader.load_search_console`). Opcional: si no se pasa
      (o viene vacía), las URLs quedan sin estos datos y los pesos que
      dependen de ellos (`posicion_oportunidad`, `impresiones_busqueda`)
      no tienen ningún efecto, por mucho que se suban por encima de 0
      desde la interfaz — el mismo comportamiento "neutro por defecto"
      que ya tienen `relevancia_categoria` y `prioridad_negocio`.

    La base es el crawl (las categorías indexables). Si una URL del
    crawl no aparece en volumen o en taxonomía, se marca con
    `falta_volumen` / `falta_taxonomia` en lugar de asumir un valor por
    defecto (p.ej. volumen=0), tal y como pide el punto 5 del encargo.
    Los dos ajustes manuales, al ser opcionales por diseño, sí tienen un
    valor neutro por defecto (0.5 / 0.0) cuando no se han rellenado.
    """
    master = datasets.crawl.merge(datasets.volumen, on="url", how="left")
    master = master.merge(datasets.taxonomia, on="url", how="left")

    master["falta_volumen"] = master["volumen"].isna()
    master["falta_taxonomia"] = master["categoria_principal"].isna() | (
        master["categoria_principal"].astype(str).str.strip() == ""
    )

    if "status_code" not in master.columns:
        master["status_code"] = float("nan")
    if "indexable" not in master.columns:
        master["indexable"] = None

    if not datasets.enlaces.empty:
        entrantes = (
            datasets.enlaces.groupby("destination_url")
            .size()
            .rename("enlaces_entrantes_actuales")
        )
        master = master.merge(
            entrantes, left_on="url", right_index=True, how="left"
        )
        salientes = (
            datasets.enlaces.groupby("source_url")
            .size()
            .rename("enlaces_salientes_actuales")
        )
        master = master.merge(
            salientes, left_on="url", right_index=True, how="left"
        )
    else:
        master["enlaces_entrantes_actuales"] = 0
        master["enlaces_salientes_actuales"] = 0
    master["enlaces_entrantes_actuales"] = master["enlaces_entrantes_actuales"].fillna(0)
    master["enlaces_salientes_actuales"] = master["enlaces_salientes_actuales"].fillna(0)

    if search_console is not None and not search_console.empty:
        master = master.merge(
            search_console[["url", "clics_28d", "impresiones_28d", "posicion_media"]],
            on="url",
            how="left",
        )
    else:
        master["clics_28d"] = float("nan")
        master["impresiones_28d"] = float("nan")
        master["posicion_media"] = float("nan")

    relevancia_lookup = _build_relevancia_lookup(relevancia_categoria)
    master["relevancia_categoria"] = master.apply(
        lambda r: _lookup_relevancia(r["categoria_principal"], r["categoria_secundaria"], relevancia_lookup),
        axis=1,
    )

    if prioridad_negocio is not None and not prioridad_negocio.empty:
        prio = prioridad_negocio.copy()
        prio["url"] = prio["url"].map(normalize_url)
        prio = prio.groupby("url")["prioridad_negocio"].mean().rename("prioridad_negocio")
        master = master.merge(prio, on="url", how="left")
    else:
        master["prioridad_negocio"] = 0.0
    master["prioridad_negocio"] = master["prioridad_negocio"].fillna(0.0)

    patrones_grupo = list(grupos_aislados) if grupos_aislados is not None else list(DEFAULT_GRUPOS_AISLADOS)
    master["grupo_aislado"] = master.apply(
        lambda r: _detectar_grupo_aislado(r["categoria_principal"], r["categoria_secundaria"], patrones_grupo),
        axis=1,
    )

    return master.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 1.1) Grupos aislados (p.ej. Black Friday, Rebajas): categorías que SOLO
# pueden enlazarse entre sí mismas, nunca con el resto del catálogo ni
# entre grupos distintos. Es una restricción dura de negocio, no un peso
# de scoring: si no coinciden, el par ni siquiera se genera como candidato.
# ---------------------------------------------------------------------------

DEFAULT_GRUPOS_AISLADOS: tuple[str, ...] = ("black friday", "rebajas", "special price")


def _detectar_grupo_aislado(
    categoria_principal: str | None,
    categoria_secundaria: str | None,
    patrones: list[str],
) -> str:
    """Devuelve el patrón (en minúsculas) que coincide con la categoría
    principal o secundaria de la URL, o "" si no pertenece a ningún
    grupo aislado (categoría "normal", sin restricción).

    Coincidencia por subcadena e insensible a mayúsculas sobre
    `categoria_principal` + `categoria_secundaria` juntos, para que dé
    igual que "Black Friday"/"Rebajas" esté como principal, como
    secundaria, o repartido entre las dos (p.ej. Categoria_Principal=
    "Precios especiales", Categoria_Secundaria="Black Friday").
    """
    texto = f"{categoria_principal or ''} {categoria_secundaria or ''}".strip().lower()
    for patron in patrones:
        p = str(patron).strip().lower()
        if p and p in texto:
            return p
    return ""


def _normalize_min_max(series: pd.Series, *, invert: bool = False) -> pd.Series:
    """Normaliza a [0, 1]. Los NaN se preservan (no se imputan). Si
    todos los valores válidos son iguales, se devuelve 0.5 para todos
    ellos (no hay señal para diferenciar).
    """
    values = series.astype(float)
    if invert:
        values = -values
    valid = values.dropna()
    if valid.empty:
        return pd.Series([float("nan")] * len(series), index=series.index)
    vmin, vmax = valid.min(), valid.max()
    if vmax == vmin:
        return values.where(values.isna(), 0.5)
    return (values - vmin) / (vmax - vmin)


def comparar_evolucion_search_console(actual: pd.DataFrame, anterior: pd.DataFrame) -> pd.DataFrame:
    """Compara dos exports de Search Console (mismas columnas que
    devuelve `core.data_loader.load_search_console`: `url`, `clics_28d`,
    `impresiones_28d`, `posicion_media`) y devuelve, por cada URL
    presente en ambos, la variación de cada métrica entre `anterior` y
    `actual`. Pensado para ver mes a mes si las categorías que
    recibieron enlaces nuevos van mejorando.

    `delta_posicion` positivo = mejora (ha subido puestos, la posición
    media ha bajado numéricamente); negativo = ha empeorado.
    """
    a = actual.rename(
        columns={
            "clics_28d": "clics_actual",
            "impresiones_28d": "impresiones_actual",
            "posicion_media": "posicion_actual",
        }
    )
    b = anterior.rename(
        columns={
            "clics_28d": "clics_anterior",
            "impresiones_28d": "impresiones_anterior",
            "posicion_media": "posicion_anterior",
        }
    )
    out = a.merge(b, on="url", how="inner")
    out["delta_clics"] = out["clics_actual"] - out["clics_anterior"]
    out["delta_impresiones"] = out["impresiones_actual"] - out["impresiones_anterior"]
    out["delta_posicion"] = out["posicion_anterior"] - out["posicion_actual"]
    out["delta_clics_pct"] = (
        out["delta_clics"] / out["clics_anterior"].replace(0, pd.NA)
    ) * 100
    return out.sort_values("delta_clics", ascending=False).reset_index(drop=True)


def oportunidad_posicion_score(posicion: float | None, oportunidad: OportunidadSEO) -> float:
    """Puntúa de 0 a 1 cuánta "oportunidad" representa la posición media
    de una URL en Search Console, según el rango configurado en
    `oportunidad` (por defecto, posiciones 4-20 = zona de oportunidad):

    - Dentro del rango [`posicion_min`, `posicion_max`]: 1.0 (máxima
      prioridad — está "a las puertas" de mejorar bastante con un
      empujón de enlaces internos).
    - Mejor que `posicion_min` (posición más baja, ya en muy buen
      puesto): decae linealmente hacia 0 a medida que se acerca a la
      posición 0 — sigue aportando algo (mantener el puesto también
      importa) pero con menos prioridad que las que están en la zona de
      oportunidad.
    - Peor que `posicion_max`: decae linealmente a lo largo de
      `ventana_decaimiento` posiciones hasta llegar a 0 (posiciones muy
      alejadas de la primera página no se consideran una oportunidad a
      corto plazo).

    Devuelve NaN si no hay dato de posición (no se subió Search Console,
    o esa URL no aparece en el export).
    """
    if posicion is None or (isinstance(posicion, float) and pd.isna(posicion)):
        return float("nan")
    if oportunidad.posicion_min <= posicion <= oportunidad.posicion_max:
        return 1.0
    if posicion < oportunidad.posicion_min:
        if oportunidad.posicion_min <= 0:
            return 1.0
        return max(0.0, posicion / oportunidad.posicion_min)
    ventana = oportunidad.ventana_decaimiento or 1.0
    return max(0.0, 1.0 - (posicion - oportunidad.posicion_max) / ventana)


def _sin_nan(valor: float) -> float:
    """0.0 si el valor es NaN (dato opcional no disponible para esa
    fila/dataset), el valor tal cual en otro caso. Así un peso > 0 sobre
    una señal opcional sin datos no rompe el score de toda la fila (lo
    trata como si esa señal no aportara nada), en vez de propagar NaN.
    """
    return 0.0 if (valor is None or (isinstance(valor, float) and pd.isna(valor))) else valor


def affinity_score(
    principal_origen: str | None,
    secundaria_origen: str | None,
    principal_destino: str | None,
    secundaria_destino: str | None,
    affinity: AffinityScores,
) -> float | None:
    """Devuelve la puntuación de afinidad de categoría entre origen y
    destino, o None si no se puede calcular por falta de
    categorización en alguno de los dos lados.
    """
    for value in (principal_origen, principal_destino):
        if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
            return None

    if str(principal_origen).strip().lower() == str(principal_destino).strip().lower():
        sec_o = str(secundaria_origen or "").strip().lower()
        sec_d = str(secundaria_destino or "").strip().lower()
        if sec_o and sec_d and sec_o == sec_d:
            return affinity.misma_principal_y_secundaria
        return affinity.misma_principal
    return affinity.distinta


# ---------------------------------------------------------------------------
# 2) Exclusión de enlaces ya existentes y auto-enlaces
# ---------------------------------------------------------------------------


def exclude_existing_links(
    candidates: pd.DataFrame, enlaces: pd.DataFrame
) -> pd.DataFrame:
    """Elimina de `candidates` (que debe tener columnas
    `origen`/`destino`) cualquier par para el que ya exista un enlace
    origen -> destino en `enlaces` (columnas `source_url` /
    `destination_url`), en cualquier zona. También elimina los pares
    origen == destino.

    No se considera que un enlace destino -> origen (en sentido
    contrario) bloquee la propuesta origen -> destino: son enlaces
    distintos.

    Implementado con `MultiIndex.isin` (vectorizado) en vez de
    `.apply(..., axis=1)` fila a fila: con catálogos grandes, comparar
    par a par en Python puro es uno de los puntos que más tiempo/memoria
    consumía en el cruce completo (ver `generate_link_proposals`), y
    aquí se puede evitar sin cambiar el resultado.
    """
    candidates = candidates[candidates["origen"] != candidates["destino"]]

    if enlaces.empty or candidates.empty:
        return candidates.reset_index(drop=True)

    existing_index = pd.MultiIndex.from_arrays(
        [enlaces["source_url"], enlaces["destination_url"]]
    )
    candidates_index = pd.MultiIndex.from_arrays(
        [candidates["origen"], candidates["destino"]]
    )
    mask_existing = candidates_index.isin(existing_index)
    return candidates[~mask_existing].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 3) Generación de la propuesta completa
# ---------------------------------------------------------------------------

RESULT_COLUMNS = [
    "categoria_origen",
    "categoria_destino",
    "score",
    "keyword_destino",
    "volumen_destino",
    "texto_ancla_sugerido",
    "categoria_principal_origen",
    "categoria_secundaria_origen",
    "categoria_principal_destino",
    "categoria_secundaria_destino",
    "enlaces_entrantes_actuales_destino",
    "enlaces_salientes_actuales_origen",
    "num_productos_destino",
    "relevancia_categoria_destino",
    "prioridad_negocio_destino",
    "posicion_media_destino",
    "impresiones_28d_destino",
    "clics_28d_destino",
    "pendiente_confirmar",
    "motivo_pendiente",
    "seleccionada",
]


# Nº de filas (pares origen-destino) que como máximo se procesan de golpe
# en cada bloque de `generate_link_proposals`. El tamaño de bloque, en Nº
# de categorías ORIGEN, se recalcula según cuántas categorías destino
# tenga el catálogo (`_BATCH_SIZE // nº de destinos`), para que el pico de
# memoria dependa de esta constante y NO crezca con el tamaño del
# catálogo: un catálogo con más URLs simplemente se parte en más bloques,
# no en bloques más grandes. Probado con datos sintéticos: con
# _BATCH_SIZE=250.000 el pico de memoria se mantiene por debajo de ~1 GB
# tanto con 3.000 como con 6.000 URLs. No cambia el resultado, solo
# cuánta memoria hace falta a la vez.
_BATCH_SIZE = 250_000


def _tamano_bloque_origenes(n_destinos: int) -> int:
    return max(1, _BATCH_SIZE // max(n_destinos, 1))


def _calcular_scores_bloque(
    pairs: pd.DataFrame, weights: ScoringWeights, affinity: AffinityScores
) -> pd.DataFrame:
    """Calcula afinidad, motivo de "pendiente" y score para un bloque de
    pares origen-destino ya filtrado (grupos aislados, salud técnica y
    enlaces existentes ya excluidos). Antes esto se hacía fila a fila con
    un bucle de Python (`itertuples` + `affinity_score`); aquí se hace
    vectorizado con pandas/numpy sobre todo el bloque a la vez, que es
    muchísimo más rápido con catálogos grandes y es lo que hace viable
    procesar por bloques en vez de en una sola pasada gigante.

    El resultado (columnas y valores) es idéntico al que producía el
    bucle fila a fila original.
    """
    principal_o = pairs["categoria_principal_origen"].fillna("").astype(str).str.strip()
    principal_d = pairs["categoria_principal_destino"].fillna("").astype(str).str.strip()
    secundaria_o = pairs["categoria_secundaria_origen"].fillna("").astype(str).str.strip().str.lower()
    secundaria_d = pairs["categoria_secundaria_destino"].fillna("").astype(str).str.strip().str.lower()

    sin_categoria = (principal_o == "") | (principal_d == "")
    misma_principal = principal_o.str.lower() == principal_d.str.lower()
    misma_secundaria = misma_principal & (secundaria_o != "") & (secundaria_d != "") & (secundaria_o == secundaria_d)

    afinidad = pd.Series(float("nan"), index=pairs.index)
    afinidad = afinidad.mask(~sin_categoria & misma_secundaria, affinity.misma_principal_y_secundaria)
    afinidad = afinidad.mask(~sin_categoria & misma_principal & ~misma_secundaria, affinity.misma_principal)
    afinidad = afinidad.mask(~sin_categoria & ~misma_principal, affinity.distinta)
    # sin_categoria se queda a NaN (afinidad "no calculable" = affinity_score
    # devolviendo None en la versión anterior fila a fila).

    falta_volumen_destino = pairs["falta_volumen_destino"]
    falta_taxonomia_destino = pairs["falta_taxonomia_destino"]
    falta_taxonomia_origen = pairs["falta_taxonomia_origen"]

    pendiente = falta_volumen_destino | falta_taxonomia_destino | falta_taxonomia_origen | afinidad.isna()

    # El motivo textual solo depende de qué combinación de las 3 señales
    # de "falta X" está activa (8 combinaciones posibles) — se calcula una
    # vez por combinación, no fila a fila, y se asigna con `.map`.
    combo = (
        falta_volumen_destino.astype(int)
        + falta_taxonomia_destino.astype(int) * 2
        + falta_taxonomia_origen.astype(int) * 4
    )
    motivo_por_combo = {}
    for c in range(8):
        partes_c = []
        if c & 1:
            partes_c.append("categoría destino sin volumen de búsqueda")
        if c & 2:
            partes_c.append("categoría destino sin categorización")
        if c & 4:
            partes_c.append("categoría origen sin categorización")
        motivo_por_combo[c] = "; ".join(partes_c)
    motivo = combo.map(motivo_por_combo)
    # Caso residual: afinidad no calculable sin que ninguna de las 3
    # señales lo explique (no debería darse en la práctica, ya que
    # `sin_categoria` implica alguna de las `falta_taxonomia_*`, pero se
    # cubre igual que en la versión anterior, por seguridad).
    motivo = motivo.mask((motivo == "") & pendiente, "categorización incompleta")
    motivo = motivo.mask(~pendiente, "")

    score = (
        weights.volumen_busqueda * pairs["norm_volumen_destino"]
        + weights.pocos_productos * pairs["norm_pocos_productos_destino"]
        + weights.pocos_enlaces_entrantes * pairs["norm_pocos_enlaces_destino"]
        + weights.afinidad_categoria * afinidad.fillna(0.0)
        + weights.relevancia_categoria * pairs["relevancia_categoria_destino"]
        + weights.prioridad_negocio * pairs["prioridad_negocio_destino"]
        + weights.autoridad_origen * pairs["norm_autoridad_origen"].fillna(0.0)
        + weights.presupuesto_enlaces_origen * pairs["norm_presupuesto_enlaces_origen"].fillna(0.0)
        + weights.posicion_oportunidad * pairs["posicion_oportunidad_destino"].fillna(0.0)
        + weights.impresiones_busqueda * pairs["norm_impresiones_destino"].fillna(0.0)
    )
    score = score.mask(pendiente, float("nan"))

    pairs = pairs.copy()
    pairs["afinidad"] = afinidad
    pairs["score"] = score
    pairs["motivo_pendiente"] = motivo
    pairs["pendiente_confirmar"] = pendiente
    pairs["keyword_destino"] = pairs["keyword_destino"].fillna("")
    pairs["texto_ancla_sugerido"] = pairs["keyword_destino"]
    return pairs


def _recortar_bloque_a_lo_relevante(
    pairs: pd.DataFrame, limites: LimitesPropuesta
) -> pd.DataFrame:
    """De todos los pares candidatos ya puntuados de un bloque, se
    queda solo con lo que de verdad hace falta conservar:

    - Los pendientes de confirmar (para que el equipo los revise).
    - Los que quedan SELECCIONADOS (el top `max_enlaces_nuevos_por_origen`
      por score, para cada categoría origen del bloque).

    El resto -candidatos válidos pero que no entraron en el top-N de su
    origen- se descarta aquí mismo. Es la parte que de verdad evita que
    la propuesta final ocupe O(N²): con un catálogo de miles de URLs,
    la inmensa mayoría de los pares candidatos son justamente estos (un
    origen tiene como candidatos a casi todo el catálogo, pero como
    mucho le hacen falta 5). Cada categoría origen vive entera dentro de
    un único bloque (el reparto en bloques es por origen, nunca la
    parte), así que este recorte por bloque da el mismo resultado que
    hacerlo una vez al final sobre la tabla completa.
    """
    pendiente = pairs["pendiente_confirmar"]

    validas = pairs[~pendiente].copy()
    validas = validas[validas["score"] >= limites.score_minimo]
    validas = validas.sort_values(["origen", "score"], ascending=[True, False])
    validas["_orden"] = validas.groupby("origen").cumcount()
    seleccionadas_idx = validas[validas["_orden"] < limites.max_enlaces_nuevos_por_origen].index

    pairs = pairs.copy()
    pairs["seleccionada"] = False
    pairs.loc[seleccionadas_idx, "seleccionada"] = True

    return pairs[pairs["pendiente_confirmar"] | pairs["seleccionada"]]


def generate_link_proposals(
    datasets: InputDatasets,
    weights: ScoringWeights | None = None,
    affinity: AffinityScores | None = None,
    limites: LimitesPropuesta | None = None,
    relevancia_categoria: pd.DataFrame | None = None,
    prioridad_negocio: pd.DataFrame | None = None,
    grupos_aislados: list[str] | None = None,
    search_console: pd.DataFrame | None = None,
    oportunidad: OportunidadSEO | None = None,
) -> pd.DataFrame:
    """Genera la propuesta de interlinking completa.

    Devuelve UNA tabla con todos los pares (origen, destino) candidatos
    válidos (sin auto-enlaces, sin enlaces ya existentes y respetando
    los grupos aislados como Black Friday/Rebajas), cada uno con:
      - su score (o NaN si está pendiente de confirmar),
      - el motivo si está pendiente de confirmar,
      - si ha sido seleccionada dentro del límite de enlaces nuevos por
        categoría origen (`seleccionada=True`) o no.

    Las filas "pendiente_confirmar" NUNCA se seleccionan automáticamente
    (no se puede confiar en un score calculado sobre datos incompletos),
    pero se conservan en la tabla para que el equipo las revise y
    complete los datos que faltan.

    `grupos_aislados` es una lista de patrones (por defecto
    `DEFAULT_GRUPOS_AISLADOS` = Black Friday, Rebajas y Special Price):
    una categoría
    que coincide con uno de estos patrones (en su categoría principal o
    secundaria) SOLO puede enlazar, y ser enlazada, por otras categorías
    del MISMO patrón. Nunca se mezclan entre grupos distintos, ni con el
    resto del catálogo. Es una restricción dura: los pares que la
    incumplen ni siquiera se generan como candidatos.
    """
    weights = (weights or ScoringWeights()).normalizados()
    affinity = affinity or AffinityScores()
    limites = limites or LimitesPropuesta()
    oportunidad = oportunidad or OportunidadSEO()

    master = build_master_table(
        datasets, relevancia_categoria, prioridad_negocio, grupos_aislados, search_console
    )
    if master.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    master["norm_volumen"] = _normalize_min_max(master["volumen"])
    master["norm_pocos_productos"] = _normalize_min_max(master["num_productos"], invert=True)
    master["norm_pocos_enlaces"] = _normalize_min_max(
        master["enlaces_entrantes_actuales"], invert=True
    )
    # Autoridad de origen: mismo dato (enlaces entrantes actuales) que
    # "pocos enlaces entrantes", pero SIN invertir — aquí más enlaces
    # entrantes propios significa más autoridad que transmitir como
    # origen, no una carencia que cubrir como destino.
    master["norm_autoridad"] = _normalize_min_max(master["enlaces_entrantes_actuales"])
    # Presupuesto de enlaces salientes: cuantos menos tenga ya la
    # categoría (como origen), más "hueco" le queda para que un enlace
    # nuevo siga aportando valor real.
    master["norm_presupuesto_enlaces"] = _normalize_min_max(
        master["enlaces_salientes_actuales"], invert=True
    )
    master["norm_impresiones"] = _normalize_min_max(master["impresiones_28d"])
    master["posicion_oportunidad"] = master["posicion_media"].map(
        lambda p: oportunidad_posicion_score(p, oportunidad)
    )

    # Salud técnica del destino (opcional): si el crawl trae status_code
    # y/o indexabilidad, se excluyen de raíz como destino las URLs con
    # status distinto de 200 o no indexables — restricción dura, igual
    # que los grupos aislados, no una penalización de score. Si no se
    # aportó ese dato (NaN/None), no se excluye nada.
    saludable = pd.Series(True, index=master.index)
    saludable &= ~(master["status_code"].notna() & (master["status_code"] != 200))
    # OJO: comparar con "==" y no con "is" — cuando la columna "indexable"
    # no tiene ningún None (todas las filas traen el dato), pandas la
    # convierte a dtype bool puro y cada valor pasa a ser un
    # numpy.bool_, para el que `v is False` es SIEMPRE False (no son el
    # mismo objeto que el `False` de Python) aunque el valor sea
    # numéricamente falso — con "is" la exclusión no excluiría nada en
    # ese caso. "==" funciona igual de bien tanto si la columna es
    # dtype bool puro como si es dtype object con None mezclado (None ==
    # False se evalúa a False sin lanzar excepción).
    saludable &= ~(master["indexable"] == False)  # noqa: E712
    master["destino_saludable"] = saludable

    urls = master["url"].tolist()
    if len(urls) < 2:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    origen_df = master.add_suffix("_origen").rename(columns={"url_origen": "origen"})
    destino_df = master.add_suffix("_destino").rename(columns={"url_destino": "destino"})

    # El cruce origen x destino es, por definición, un producto cartesiano
    # (cada categoría contra todas las demás como posible destino). Con un
    # catálogo grande esto puede ser muchos millones de pares si se
    # construye de una sola vez (N² filas en memoria a la vez), que es lo
    # que hacía que la app se quedara sin memoria con el catálogo real de
    # Sklum. Para evitarlo, se procesa por bloques de categorías ORIGEN
    # (cada bloque se cruza contra TODAS las categorías destino, pero solo
    # `_BATCH_SIZE` orígenes a la vez): el resultado final es exactamente
    # el mismo (mismas filas, mismo orden tras el sort de más abajo), solo
    # cambia cuánta memoria hace falta en cada momento.
    bloques_resultado: list[pd.DataFrame] = []
    n_origenes = len(origen_df)
    tamano_bloque = _tamano_bloque_origenes(len(destino_df))
    for inicio in range(0, n_origenes, tamano_bloque):
        bloque_origen = origen_df.iloc[inicio : inicio + tamano_bloque]
        pairs = (
            bloque_origen.assign(_key=1)
            .merge(destino_df.assign(_key=1), on="_key")
            .drop(columns="_key")
        )

        # Restricción dura de grupos aislados (Black Friday, Rebajas...):
        # solo se permite el par si origen y destino están en el mismo
        # grupo (o ambos son categorías "normales", grupo_aislado == "").
        pairs = pairs[pairs["grupo_aislado_origen"] == pairs["grupo_aislado_destino"]]
        if pairs.empty:
            continue

        # Restricción dura de salud técnica: nunca se propone como destino
        # una URL caída, redirigida o no indexable (si ese dato está
        # disponible).
        pairs = pairs[pairs["destino_saludable_destino"]]
        if pairs.empty:
            continue

        candidate_pairs = pairs[["origen", "destino"]].copy()
        kept = exclude_existing_links(candidate_pairs, datasets.enlaces)
        pairs = pairs.merge(kept, on=["origen", "destino"], how="inner")
        if pairs.empty:
            continue

        pairs = _calcular_scores_bloque(pairs, weights, affinity)
        pairs = _recortar_bloque_a_lo_relevante(pairs, limites)
        if pairs.empty:
            continue
        bloques_resultado.append(pairs)

    if not bloques_resultado:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    resultado = pd.concat(bloques_resultado, ignore_index=True)

    resultado = resultado.rename(
        columns={
            "origen": "categoria_origen",
            "destino": "categoria_destino",
        }
    )

    resultado["seleccionada"] = False
    validas = resultado[~resultado["pendiente_confirmar"]].copy()
    validas = validas[validas["score"] >= limites.score_minimo]
    validas = validas.sort_values(
        ["categoria_origen", "score"], ascending=[True, False]
    )
    validas["_orden"] = validas.groupby("categoria_origen").cumcount()
    seleccionadas_idx = validas[validas["_orden"] < limites.max_enlaces_nuevos_por_origen].index
    resultado.loc[seleccionadas_idx, "seleccionada"] = True

    resultado = resultado.rename(
        columns={
            "volumen_destino": "volumen_destino",
            "enlaces_entrantes_actuales_destino": "enlaces_entrantes_actuales_destino",
            "num_productos_destino": "num_productos_destino",
            "categoria_principal_destino": "categoria_principal_destino",
            "categoria_secundaria_destino": "categoria_secundaria_destino",
        }
    )

    resultado = resultado.sort_values(
        ["categoria_origen", "score"], ascending=[True, False], na_position="last"
    )

    return resultado[RESULT_COLUMNS].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5) Formato "ancho" (id + enlaces en columnas) para integraciones externas
# ---------------------------------------------------------------------------

_ID_SLUG_RE = re.compile(r"(\d+)-")


def extraer_id_de_url(url: str | None) -> str:
    """Extrae el ID numérico del slug de una URL de Sklum, p.ej.
    'https://www.sklum.com/es/524-comprar-mobiliario' -> '524'. Si la
    URL no sigue ese patrón (o viene vacía), devuelve "" en vez de
    lanzar un error, para que una URL atípica no rompa la exportación
    entera.
    """
    if not url or not isinstance(url, str):
        return ""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    match = _ID_SLUG_RE.match(slug)
    return match.group(1) if match else ""


def build_formato_ancho(resultado: pd.DataFrame) -> pd.DataFrame:
    """Convierte la propuesta (una fila por par origen-destino) al
    formato ancho que ya usaba el equipo con el flujo anterior de
    Sheets/Apps Script: una fila por URL origen, con su "id" (extraído
    de la URL) y, a continuación, pares linked_id_N / linked_url_N con
    cada enlace SELECCIONADO (respeta el máximo y el score mínimo ya
    aplicados en `generate_link_proposals`), ordenados de mayor a menor
    score. El número de pares de columnas se ajusta automáticamente al
    mayor nº de enlaces seleccionados que tenga cualquier origen (no
    viene fijo a 5): si el límite configurado es distinto, cambia solo.
    """
    columnas_vacias = ["id", "url", "categoria_principal", "categoria_secundaria", "n_enlaces"]
    if resultado is None or resultado.empty or "seleccionada" not in resultado.columns:
        return pd.DataFrame(columns=columnas_vacias)

    seleccion = resultado[resultado["seleccionada"]].copy()
    if seleccion.empty:
        return pd.DataFrame(columns=columnas_vacias)

    seleccion = seleccion.sort_values(
        ["categoria_origen", "score"], ascending=[True, False], na_position="last"
    )
    # OJO: pandas no permite que un campo de itertuples empiece por "_"
    # (lo renombra a algo posicional tipo "_1"), así que aquí la columna
    # de orden va sin guion bajo — a diferencia de la "_orden" que usa
    # generate_link_proposals más arriba, que se consume con .loc/boolean
    # indexing, no con itertuples.
    seleccion["orden"] = seleccion.groupby("categoria_origen").cumcount() + 1
    max_enlaces = int(seleccion["orden"].max())

    filas = []
    for origen, grupo in seleccion.groupby("categoria_origen", sort=False):
        primera = grupo.iloc[0]
        fila = {
            "id": extraer_id_de_url(origen),
            "url": origen,
            "categoria_principal": primera.get("categoria_principal_origen", ""),
            "categoria_secundaria": primera.get("categoria_secundaria_origen", ""),
            "n_enlaces": int(len(grupo)),
        }
        for row in grupo.itertuples(index=False):
            n = int(row.orden)
            fila[f"linked_id_{n}"] = extraer_id_de_url(row.categoria_destino)
            fila[f"linked_url_{n}"] = row.categoria_destino
            fila[f"linked_category_{n}"] = getattr(row, "categoria_principal_destino", "")
            fila[f"linked_subcategory_{n}"] = getattr(row, "categoria_secundaria_destino", "")
            score_n = getattr(row, "score", float("nan"))
            fila[f"linked_score_{n}"] = round(score_n, 3) if pd.notna(score_n) else ""
            fila[f"justificacion_{n}"] = _justificacion_enlace(row)
        filas.append(fila)

    columnas = list(columnas_vacias)
    for n in range(1, max_enlaces + 1):
        columnas += [
            f"linked_id_{n}",
            f"linked_url_{n}",
            f"linked_category_{n}",
            f"linked_subcategory_{n}",
            f"linked_score_{n}",
            f"justificacion_{n}",
        ]

    return pd.DataFrame(filas).reindex(columns=columnas)


def _valor_valido(valor) -> bool:
    """True si `valor` es un dato real (ni None ni NaN). Atajo para no
    repetir el chequeo típico de pandas en cada regla de
    `_justificacion_enlace`.
    """
    return valor is not None and not (isinstance(valor, float) and pd.isna(valor))


def _justificacion_enlace(row) -> str:
    """Explicación breve, en lenguaje llano (no en jerga de scoring), de
    por qué se propone este enlace en concreto: qué señales concretas
    (volumen de búsqueda, pocos enlaces entrantes, categoría prioritaria,
    prioridad de negocio, oportunidad de posicionamiento, misma
    categoría...) pesaron a favor de ese destino.

    Recibe una fila (namedtuple de `itertuples`) de la propuesta ya
    calculada por `generate_link_proposals`: usa `getattr(..., None)`
    para cada señal porque esta función también debe funcionar si se le
    pasa una tabla con menos columnas (p.ej. en tests, o en una
    integración externa que no traiga todo `RESULT_COLUMNS`) — en ese
    caso simplemente omite las razones que no puede comprobar, en vez de
    fallar.
    """
    razones: list[str] = []

    volumen = getattr(row, "volumen_destino", None)
    if _valor_valido(volumen) and volumen > 0:
        razones.append(f"la categoría destino tiene volumen de búsqueda ({int(volumen)}/mes)")

    entrantes = getattr(row, "enlaces_entrantes_actuales_destino", None)
    if _valor_valido(entrantes) and entrantes <= 2:
        razones.append("todavía tiene pocos enlaces internos apuntándole")

    relevancia = getattr(row, "relevancia_categoria_destino", None)
    if _valor_valido(relevancia) and relevancia > 0.5:
        razones.append("está marcada como categoría prioritaria")

    prioridad = getattr(row, "prioridad_negocio_destino", None)
    if _valor_valido(prioridad) and prioridad > 0:
        razones.append("tiene prioridad de negocio asignada")

    posicion = getattr(row, "posicion_media_destino", None)
    if _valor_valido(posicion) and 4 <= posicion <= 20:
        razones.append(f"está en posición media {posicion:.0f} en Google, en zona de oportunidad")

    principal_o = getattr(row, "categoria_principal_origen", None)
    principal_d = getattr(row, "categoria_principal_destino", None)
    if (
        _valor_valido(principal_o)
        and _valor_valido(principal_d)
        and str(principal_o).strip()
        and str(principal_o).strip().lower() == str(principal_d).strip().lower()
    ):
        razones.append("es de la misma categoría que el origen")

    if not razones:
        score = getattr(row, "score", None)
        if _valor_valido(score):
            razones.append(f"mejor encaje disponible según el score combinado ({score:.2f})")
        else:
            razones.append("mejor encaje disponible según el score combinado")

    return "; ".join(razones)

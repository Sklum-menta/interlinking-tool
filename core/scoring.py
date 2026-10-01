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
    # Igual que `falta_volumen`/`falta_taxonomia`: si el nº de productos no
    # se ha podido interpretar (columna vacía, o texto en un formato que
    # `_parse_num_productos` no reconoce), NO se asume un valor por
    # defecto — la URL se marca pendiente de confirmar en vez de dejar
    # que el score salga silenciosamente en NaN (ver `_calcular_scores_bloque`).
    master["falta_num_productos"] = master["num_productos"].isna()

    if "status_code" not in master.columns:
        master["status_code"] = float("nan")
    if "indexable" not in master.columns:
        master["indexable"] = None
    if "profundidad" not in master.columns:
        master["profundidad"] = float("nan")

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

    patrones_grupo = _combinar_con_grupos_obligatorios(grupos_aislados)
    master["grupo_aislado"] = master.apply(
        lambda r: _detectar_grupo_aislado(
            r["categoria_principal"], r["categoria_secundaria"], patrones_grupo, r["url"]
        ),
        axis=1,
    )

    master["id"] = master["url"].map(extraer_id_de_url)
    if "h1" not in master.columns:
        master["h1"] = ""
    master["h1"] = master["h1"].fillna("").astype(str).str.strip()
    sin_h1 = master["h1"] == ""
    master.loc[sin_h1, "h1"] = master.loc[sin_h1, "url"].map(_derivar_titulo_desde_url)

    return master.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 1.1) Grupos aislados (p.ej. Black Friday, Rebajas): categorías que SOLO
# pueden enlazarse entre sí mismas, nunca con el resto del catálogo ni
# entre grupos distintos. Es una restricción dura de negocio, no un peso
# de scoring: si no coinciden, el par ni siquiera se genera como candidato.
# ---------------------------------------------------------------------------

DEFAULT_GRUPOS_AISLADOS: tuple[str, ...] = ("black friday", "rebajas", "special price", "navidad")


def _combinar_con_grupos_obligatorios(grupos_aislados: list[str] | None) -> list[str]:
    """Combina los patrones que pase el usuario (UI o llamada directa) con los
    4 grupos OBLIGATORIOS de `DEFAULT_GRUPOS_AISLADOS`.

    Regla de negocio confirmada explícitamente por el cliente: Black Friday,
    Rebajas, Special Price y Navidad NUNCA pueden enlazarse entre sí ni con
    el resto del catálogo — cada uno solo enlaza dentro de su propio grupo.
    Esto no es una preferencia configurable: da igual que `grupos_aislados`
    llegue vacío, con solo alguno de los 4, o incluso como `[]` explícito
    (por ejemplo si alguien borra el cuadro de texto de la interfaz por
    error) — los 4 patrones por defecto se aplican SIEMPRE. `grupos_aislados`
    solo sirve para AÑADIR grupos aislados extra por encima de esos 4, nunca
    para quitarlos.
    """
    combinados = list(DEFAULT_GRUPOS_AISLADOS)
    if grupos_aislados:
        existentes = {p.strip().lower() for p in combinados}
        for patron in grupos_aislados:
            p = str(patron).strip()
            if p and p.lower() not in existentes:
                combinados.append(p)
                existentes.add(p.lower())
    return combinados


def _detectar_grupo_aislado(
    categoria_principal: str | None,
    categoria_secundaria: str | None,
    patrones: list[str],
    url: str | None = None,
) -> str:
    """Devuelve el patrón (en minúsculas) que coincide con esta URL, o ""
    si no pertenece a ningún grupo aislado (categoría "normal", sin
    restricción).

    El SLUG de la URL se mira PRIMERO y manda sobre la categorización
    manual cuando ambos dan una respuesta distinta: el slug es un hecho
    técnico (p.ej. "...-black-friday") que no se puede escribir mal en
    una celda de Excel, mientras que `categoria_secundaria` es un campo
    rellenado a mano y, revisando el catálogo real, se han encontrado
    varias categorías de Black Friday / Rebajas etiquetadas por error
    como "Special Price" (y alguna de Navidad sin etiquetar en absoluto).
    Si el slug no da ninguna coincidencia, se recurre a
    `categoria_principal` + `categoria_secundaria` como hasta ahora (para
    los grupos aislados que el equipo añada a mano y que no sigan ningún
    patrón de URL concreto).

    Coincidencia por subcadena e insensible a mayúsculas. Cuando una URL
    coincide con MÁS DE UN patrón a la vez (p.ej. una categoría puntual
    "especial-price-navidad" o "navidad-black-friday", que existen en el
    catálogo), se resuelve de forma determinista por el orden de
    `patrones` (los 4 obligatorios van en el orden Black Friday > Rebajas
    > Special Price > Navidad) — no hay una respuesta "correcta" única
    para esos casos límite, así que al menos es siempre la misma.
    """
    texto_url = str(url or "").lower()
    for patron in patrones:
        p = str(patron).strip().lower()
        p_url = p.replace(" ", "-")
        if p and (p_url in texto_url or p in texto_url):
            return p

    texto_manual = f"{categoria_principal or ''} {categoria_secundaria or ''}".strip().lower()
    for patron in patrones:
        p = str(patron).strip().lower()
        if p and p in texto_manual:
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
    "id_origen",
    "h1_origen",
    "categoria_destino",
    "id_destino",
    "h1_destino",
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
    "profundidad_origen",
    "num_productos_destino",
    "relevancia_categoria_destino",
    "prioridad_negocio_destino",
    "posicion_media_destino",
    "impresiones_28d_destino",
    "clics_28d_destino",
    "pendiente_confirmar",
    "motivo_pendiente",
    "seleccionada",
    "motivo_num_enlaces_origen",
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
    falta_num_productos_destino = pairs["falta_num_productos_destino"]

    pendiente = (
        falta_volumen_destino
        | falta_taxonomia_destino
        | falta_taxonomia_origen
        | falta_num_productos_destino
        | afinidad.isna()
    )

    # El motivo textual solo depende de qué combinación de las 4 señales
    # de "falta X" está activa (16 combinaciones posibles) — se calcula
    # una vez por combinación, no fila a fila, y se asigna con `.map`.
    combo = (
        falta_volumen_destino.astype(int)
        + falta_taxonomia_destino.astype(int) * 2
        + falta_taxonomia_origen.astype(int) * 4
        + falta_num_productos_destino.astype(int) * 8
    )
    motivo_por_combo = {}
    for c in range(16):
        partes_c = []
        if c & 1:
            partes_c.append("categoría destino sin volumen de búsqueda")
        if c & 2:
            partes_c.append("categoría destino sin categorización")
        if c & 4:
            partes_c.append("categoría origen sin categorización")
        if c & 8:
            partes_c.append("categoría destino sin nº de productos")
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
        + weights.muchos_productos * pairs["norm_muchos_productos_destino"]
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


def _margen_candidatos_por_origen(limites: LimitesPropuesta) -> int:
    """Cuántos candidatos por origen se conservan de cada bloque ANTES de
    la selección final con presupuesto de destino (ver
    `_recortar_bloque_a_lo_relevante` y `_seleccionar_con_presupuesto_destino`).

    Tiene que ser mayor que el cupo máximo posible por origen (el
    excepcional, no el normal: ver `LimitesPropuesta.max_enlaces_nuevos_por_origen_excepcional`):
    si el destino mejor puntuado de un origen ya ha agotado su cupo de
    enlaces nuevos (`max_enlaces_nuevos_por_destino`) porque otros
    orígenes lo eligieron antes, hace falta tener a mano el siguiente
    mejor candidato de ESE origen para poder sustituirlo — si solo
    guardásemos el top "a secas" (como antes de repartir por destino),
    ese origen se quedaría con menos enlaces de los que le tocan en vez
    de pasar al siguiente candidato válido.
    """
    return max(limites.max_enlaces_nuevos_por_origen_excepcional * 10, 50)


# Umbrales mínimos (absolutos, no solo relativos) para que los criterios
# de ampliación excepcional de abajo no se activen "para todo el
# catálogo a la vez" cuando los datos de enlaces internos son escasos o
# vienen casi vacíos — ver `_elegibilidad_ampliacion_origen`.
_MEDIANA_SALIENTES_MINIMA_PARA_ACTIVAR = 3
_PERCENTIL_AUTORIDAD_MINIMO_PARA_ACTIVAR = 5
_PERCENTIL_AUTORIDAD_ORIGEN = 0.9
_MAX_SALIENTES_PARA_AMPLIAR = 2

# Rendimiento real en Search Console (opcional, requiere subir el export
# de GSC): una categoría con muchos clics en los últimos 28 días ya ha
# demostrado tener autoridad/relevancia de verdad para el usuario final,
# no solo "sobre el papel" vía enlaces internos — decisión de negocio del
# 1 oct ("si tienen mucho rendimiento en GSC es que tienen autoridad").
# Misma salvaguarda que las demás: si casi nadie tiene datos de GSC o el
# tráfico es residual en todo el catálogo, no se activa.
_PERCENTIL_GSC_ORIGEN = 0.9
_CLICS_MINIMO_PARA_ACTIVAR = 20

# Profundidad de rastreo (opcional, ver `core.data_loader.PROFUNDIDAD_CANDIDATES`):
# una categoría muy cerca de la home (percentil 10 más bajo de profundidad
# del catálogo) se considera también "mucha autoridad interna" estructural,
# igual que tener muchos enlaces entrantes -es, de hecho, la señal de
# autoridad interna más estándar en SEO, y no depende de que el dataset de
# enlaces esté completo-. Salvaguarda equivalente a la de enlaces entrantes:
# si el catálogo entero es "plano" (todo a 1-2 clics de la home, típico de
# webs pequeñas o mal rastreadas) el criterio no se activa, porque entonces
# "estar cerca de la home" no sería nada excepcional.
_PERCENTIL_PROFUNDIDAD_ORIGEN = 0.10
_DIFERENCIA_MINIMA_MEDIANA_PARA_ACTIVAR_PROFUNDIDAD = 1


def _elegibilidad_ampliacion_origen(master: pd.DataFrame) -> dict[str, str]:
    """Devuelve {url_origen: motivo} SOLO para las categorías origen que
    cumplen una condición claramente EXCEPCIONAL (decisión de negocio:
    "que no sea una norma, solo casos puntuales y explicados") para
    poder recibir más de los `max_enlaces_nuevos_por_origen` enlaces
    nuevos normales, hasta el tope
    `max_enlaces_nuevos_por_origen_excepcional`:

    - Casi no tiene enlaces salientes propios todavía
      (`enlaces_salientes_actuales` <= 2): un origen así tiene mucho
      "hueco" real para enlazar sin saturar la página, así que
      limitarlo al cupo normal dejaría valor sin aprovechar.
    - Está en el 10% de categorías con más autoridad interna (más
      enlaces entrantes ya recibidos): un hub así puede permitirse
      repartir más enlaces sin diluir su propia relevancia.
    - (Si se ha subido Search Console) Está en el 10% de categorías con
      más clics reales en los últimos 28 días: tráfico real demostrado,
      no solo enlaces internos — misma idea de "autoridad", con prueba
      de rendimiento de verdad.
    - (Si el rastreo trae el dato de profundidad) Está en el 10% de
      categorías más cerca de la home: misma idea que el punto anterior,
      pero mirando la posición estructural en vez de los enlaces ya
      contados — útil también cuando el dataset de enlaces es incompleto.

    Cada condición lleva además un umbral mínimo ABSOLUTO (no solo un
    percentil relativo): si el dataset de enlaces viene casi vacío (p.ej.
    un crawl sin el export de enlaces internos), todas las categorías
    tendrían "pocos enlaces salientes" o "pocos entrantes" a la vez, y
    sin este umbral mínimo la excepción se activaría para el catálogo
    entero — justo lo contrario de "solo casos puntuales". Lo mismo para
    profundidad: si todo el catálogo está a la misma distancia (o casi)
    de la home, no se activa.
    """
    motivos: dict[str, str] = {}
    if master.empty:
        return motivos

    salientes = master["enlaces_salientes_actuales"]
    entrantes = master["enlaces_entrantes_actuales"]

    activar_pocos_salientes = salientes.median() >= _MEDIANA_SALIENTES_MINIMA_PARA_ACTIVAR
    umbral_autoridad = entrantes.quantile(_PERCENTIL_AUTORIDAD_ORIGEN)
    activar_autoridad = umbral_autoridad >= _PERCENTIL_AUTORIDAD_MINIMO_PARA_ACTIVAR

    clics = master["clics_28d"] if "clics_28d" in master.columns else pd.Series(dtype=float)
    activar_gsc = False
    umbral_clics = None
    if clics.notna().any():
        umbral_clics = clics.quantile(_PERCENTIL_GSC_ORIGEN)
        activar_gsc = pd.notna(umbral_clics) and umbral_clics >= _CLICS_MINIMO_PARA_ACTIVAR

    profundidad = master["profundidad"] if "profundidad" in master.columns else pd.Series(dtype=float)
    activar_profundidad = False
    umbral_profundidad = None
    if profundidad.notna().any():
        umbral_profundidad = profundidad.quantile(_PERCENTIL_PROFUNDIDAD_ORIGEN)
        mediana_profundidad = profundidad.median()
        activar_profundidad = (
            pd.notna(umbral_profundidad)
            and pd.notna(mediana_profundidad)
            and (mediana_profundidad - umbral_profundidad)
            >= _DIFERENCIA_MINIMA_MEDIANA_PARA_ACTIVAR_PROFUNDIDAD
        )

    for row in master.itertuples(index=False):
        if activar_pocos_salientes and pd.notna(row.enlaces_salientes_actuales) and row.enlaces_salientes_actuales <= _MAX_SALIENTES_PARA_AMPLIAR:
            motivos[row.url] = (
                f"casi no tiene enlaces salientes propios todavía "
                f"({int(row.enlaces_salientes_actuales)}, muy por debajo de la media del catálogo): "
                "le sobra presupuesto de enlazado para asumir más enlaces nuevos sin saturar la página"
            )
        elif activar_autoridad and pd.notna(row.enlaces_entrantes_actuales) and row.enlaces_entrantes_actuales >= umbral_autoridad:
            motivos[row.url] = (
                f"está entre el 10% de categorías con más autoridad interna del catálogo "
                f"({int(row.enlaces_entrantes_actuales)} enlaces entrantes propios): "
                "puede repartir más enlaces sin diluir su propia relevancia"
            )
        elif (
            activar_gsc
            and pd.notna(getattr(row, "clics_28d", None))
            and row.clics_28d >= umbral_clics
        ):
            motivos[row.url] = (
                f"tiene mucho rendimiento real en Search Console "
                f"({int(row.clics_28d)} clics en los últimos 28 días, entre el 10% con más "
                "tráfico del catálogo): autoridad demostrada de verdad, puede repartir más "
                "enlaces sin diluir su propia relevancia"
            )
        elif (
            activar_profundidad
            and pd.notna(getattr(row, "profundidad", None))
            and row.profundidad <= umbral_profundidad
        ):
            motivos[row.url] = (
                f"está entre el 10% de categorías más cerca de la home en la arquitectura "
                f"de la web ({int(row.profundidad)} clic(s) de distancia): puede repartir "
                "más enlaces sin diluir su propia relevancia estructural"
            )
    return motivos


def _recortar_bloque_a_lo_relevante(
    pairs: pd.DataFrame, limites: LimitesPropuesta
) -> pd.DataFrame:
    """De todos los pares candidatos ya puntuados de un bloque, se
    queda solo con lo que de verdad hace falta conservar:

    - Los pendientes de confirmar (para que el equipo los revise).
    - Los mejores candidatos por score de cada categoría origen del
      bloque, con margen de sobra (ver `_margen_candidatos_por_origen`)
      para que la selección final pueda repartir el presupuesto de
      enlaces nuevos por destino sin quedarse sin candidatos de reserva.

    El resto -candidatos válidos que ni de lejos entran en el margen de su
    origen- se descarta aquí mismo. Es la parte que de verdad evita que
    la propuesta final ocupe O(N²): con un catálogo de miles de URLs, la
    inmensa mayoría de los pares candidatos son justamente estos (un
    origen tiene como candidatos a casi todo el catálogo, pero como mucho
    le hace falta un margen de unas pocas decenas). Cada categoría origen
    vive entera dentro de un único bloque (el reparto en bloques es por
    origen, nunca al revés), así que este recorte por bloque no pierde
    ningún candidato que pudiera hacer falta en la selección final global.

    OJO: la columna `seleccionada` que se rellena aquí es solo una marca
    provisional para decidir qué conservar en memoria — la selección de
    verdad (con presupuesto de destino) se recalcula desde cero al final
    de `generate_link_proposals`, una vez juntados todos los bloques.
    """
    pendiente = pairs["pendiente_confirmar"]
    margen = _margen_candidatos_por_origen(limites)

    validas = pairs[~pendiente].copy()
    validas = validas[validas["score"] >= limites.score_minimo]
    validas = validas.sort_values(["origen", "score"], ascending=[True, False])
    validas["_orden"] = validas.groupby("origen").cumcount()
    seleccionadas_idx = validas[validas["_orden"] < margen].index

    pairs = pairs.copy()
    pairs["seleccionada"] = False
    pairs.loc[seleccionadas_idx, "seleccionada"] = True

    return pairs[pairs["pendiente_confirmar"] | pairs["seleccionada"]]


def _seleccionar_con_presupuesto_destino(
    resultado: pd.DataFrame,
    limites: LimitesPropuesta,
    ampliacion_origen: dict[str, str] | None = None,
) -> tuple[pd.Series, dict[str, int]]:
    """Selección final de enlaces nuevos, con dos cupos a la vez (además
    de una tercera pasada de rescate de mínimos, ver
    `_rescatar_minimo_por_congestion`). Devuelve `(seleccionada,
    donantes_rescate)`: la serie booleana de siempre, más un recuento de
    qué orígenes han "donado" un enlace durante el rescate (para que
    `_motivos_num_enlaces` pueda explicarlo si acaban por debajo de su
    cupo normal por esta razón).

    - `max_enlaces_nuevos_por_origen`: cuántos enlaces salientes nuevos
      como mucho por categoría origen, en el caso normal; hasta
      `max_enlaces_nuevos_por_origen_excepcional` para los orígenes de
      `ampliacion_origen` (ver `_elegibilidad_ampliacion_origen`).
    - `max_enlaces_nuevos_por_destino`: cuántos enlaces entrantes NUEVOS
      como mucho puede acumular una misma categoría destino en esta
      propuesta (ver `LimitesPropuesta`).

    Reparto POR RONDAS (decisión de negocio: demasiadas categorías se
    quedaban con menos enlaces de los que les tocaban, no por falta real
    de candidatos sino por el ORDEN en que se procesaban los pares). En
    la ronda 1 cada origen compite únicamente por su MEJOR candidato; en
    la ronda 2, todos los orígenes que aún tengan hueco compiten por su
    2º mejor candidato; y así sucesivamente hasta el cupo de cada
    origen. Dentro de cada ronda, si varios orígenes compiten por el
    mismo destino casi lleno, gana el par con mejor score (empate
    determinista, no por orden alfabético).

    Esto es deliberadamente distinto de ordenar TODOS los pares por
    score de forma global: con el orden global, un puñado de orígenes
    cuyos candidatos con mejor score global agotaban antes el cupo de
    los destinos más populares dejaban a muchos otros orígenes con menos
    de su cupo normal de enlaces, aunque SÍ tuvieran candidatos válidos
    de sobra -simplemente no les había tocado turno a tiempo-. Por
    rondas, ningún origen se queda atrás en la cola por culpa de
    candidatos de OTROS orígenes que ni siquiera son su mejor opción:
    cada uno agota primero sus mejores opciones antes de que nadie entre
    en las peores.

    Sin el cupo por destino, unas pocas categorías "ganadoras a priori"
    (mucho volumen, muchos productos, pocos enlaces entrantes de
    partida...) se llevaban la inmensa mayoría de los enlaces nuevos
    -algunas repetidas más de 70 veces, como orígenes distintas- mientras
    cientos de categorías del catálogo se quedaban sin ningún enlace
    nuevo: un enlazado poco repartido y de baja calidad, justo lo que
    reportó el usuario al comparar con el script anterior (que sí
    limitaba cuántos enlaces entrantes nuevos podía recibir cada
    categoría mediante su columna "En. Obj.").
    """
    ampliacion_origen = ampliacion_origen or {}

    validas = resultado[~resultado["pendiente_confirmar"]].copy()
    validas = validas[validas["score"] >= limites.score_minimo]
    if validas.empty:
        return pd.Series(False, index=resultado.index), {}

    validas = validas.sort_values(
        ["categoria_origen", "score"], ascending=[True, False]
    )
    validas["_rango_origen"] = validas.groupby("categoria_origen").cumcount() + 1

    max_origen_normal = limites.max_enlaces_nuevos_por_origen
    max_origen_excepcional = limites.max_enlaces_nuevos_por_origen_excepcional
    max_destino = limites.max_enlaces_nuevos_por_destino
    # OJO: el límite de rondas es cuántos candidatos por origen hay
    # disponibles como mucho (el margen de `_recortar_bloque_a_lo_relevante`),
    # NO el cupo de enlaces del origen — un origen tiene que poder seguir
    # probando candidatos más abajo de su lista (rango 6, 7, 8...) si sus
    # mejores opciones chocan una y otra vez con destinos ya llenos,
    # exactamente igual que antes de repartir por rondas. Limitar aquí las
    # rondas al cupo (5 o 10) dejaría a un origen sin ninguna posibilidad
    # de completar su cupo en cuanto sus primeras opciones fallasen, por
    # muchos candidatos válidos que le quedasen más abajo en la lista.
    max_rango_global = int(validas["_rango_origen"].max())

    origen_count: dict[str, int] = {}
    destino_count: dict[str, int] = {}
    seleccionadas_idx: list = []

    for rango in range(1, max_rango_global + 1):
        candidatos_rango = validas[validas["_rango_origen"] == rango]
        if candidatos_rango.empty:
            continue
        # Dentro de la misma ronda, mejor score primero: si dos orígenes
        # compiten por el mismo destino casi lleno en esta ronda, gana
        # el par de mejor encaje.
        candidatos_rango = candidatos_rango.sort_values("score", ascending=False)
        for idx, origen, destino in zip(
            candidatos_rango.index,
            candidatos_rango["categoria_origen"],
            candidatos_rango["categoria_destino"],
        ):
            cap_origen = (
                max_origen_excepcional if origen in ampliacion_origen else max_origen_normal
            )
            if origen_count.get(origen, 0) >= cap_origen:
                continue
            if destino_count.get(destino, 0) >= max_destino:
                continue
            seleccionadas_idx.append(idx)
            origen_count[origen] = origen_count.get(origen, 0) + 1
            destino_count[destino] = destino_count.get(destino, 0) + 1

    seleccionadas_idx, donantes_rescate = _rescatar_minimo_por_congestion(
        validas, origen_count, destino_count, seleccionadas_idx, max_destino, max_origen_normal
    )

    seleccionada = pd.Series(False, index=resultado.index)
    seleccionada.loc[seleccionadas_idx] = True
    return seleccionada, donantes_rescate


# Mínimo aceptable para un origen que se queda por debajo del cupo normal
# (decisión de negocio del 1 oct: "lo normal es que salgan 5 y solo en
# casos excepcionales que salgan menos, pero no quiero varias categorías
# con 1 solo enlace"). Si un origen tiene de sobra más candidatos válidos
# de los que finalmente consiguió -es decir, perdió todas las rondas
# frente a otros orígenes mejor puntuados, no por falta real de
# destinos- se le garantiza llegar al menos a este mínimo, "robando" el
# hueco al ocupante MÁS prescindible de un destino lleno (nunca se
# empuja a nadie por debajo de este mismo mínimo para rescatar a otro).
_MINIMO_SI_HAY_CANDIDATOS_DE_SOBRA = 3


def _rescatar_minimo_por_congestion(
    validas: pd.DataFrame,
    origen_count: dict[str, int],
    destino_count: dict[str, int],
    seleccionadas_idx: list,
    max_destino: int,
    max_origen_normal: int,
) -> tuple[list, dict[str, int]]:
    """Segunda pasada tras el reparto por rondas: ningún origen con
    candidatos de sobra se queda con menos de
    `_MINIMO_SI_HAY_CANDIDATOS_DE_SOBRA` enlaces solo por mala suerte de
    congestión. Para cada origen "necesitado" se recorren sus candidatos
    no elegidos (en orden de score): si el destino todavía tiene hueco
    libre, se añade directamente (no debería pasar normalmente -si había
    hueco, la ronda principal ya lo habría cogido-, pero un rescate
    anterior puede haber liberado un hueco mientras tanto); si está
    lleno, se desaloja al ocupante MENOS imprescindible de ESE destino
    -el de peor score cuyo origen pueda permitirse perder un enlace sin
    él mismo caer por debajo del mínimo, priorizando desalojar a quien
    más margen tenga-. Devuelve la lista de índices seleccionados
    actualizada y un recuento de cuántas veces ha "donado" un enlace cada
    origen (para que `_motivos_num_enlaces` pueda explicarlo si ese
    origen acaba, por este motivo, por debajo de su cupo normal).

    El mínimo efectivo nunca supera `max_origen_normal`: si el cupo
    normal configurado es menor que `_MINIMO_SI_HAY_CANDIDATOS_DE_SOBRA`
    (poco habitual, pero technically posible), el rescate no debe forzar
    MÁS enlaces de los que el propio cupo normal permite.
    """
    minimo = min(_MINIMO_SI_HAY_CANDIDATOS_DE_SOBRA, max_origen_normal)
    n_candidatos_total = validas.groupby("categoria_origen").size()
    necesitados = sorted(
        origen
        for origen, cnt in origen_count.items()
        if cnt < minimo
        and n_candidatos_total.get(origen, 0) > cnt
    )
    if not necesitados:
        return seleccionadas_idx, {}

    validas_por_origen = {
        origen: grupo.sort_values("score", ascending=False)
        for origen, grupo in validas.groupby("categoria_origen")
    }

    seleccionadas_set = set(seleccionadas_idx)
    destino_count = dict(destino_count)
    ocupantes_por_destino: dict[str, list] = {}
    for idx in seleccionadas_idx:
        destino = validas.at[idx, "categoria_destino"]
        ocupantes_por_destino.setdefault(destino, []).append(idx)

    donantes_rescate: dict[str, int] = {}

    for origen in necesitados:
        candidatos = validas_por_origen.get(origen)
        if candidatos is None:
            continue
        for idx, destino in zip(candidatos.index, candidatos["categoria_destino"]):
            if origen_count.get(origen, 0) >= minimo:
                break
            if idx in seleccionadas_set:
                continue
            if destino_count.get(destino, 0) < max_destino:
                # Hueco libre de verdad (p.ej. liberado por un rescate
                # anterior en esta misma pasada): se añade sin desalojar
                # a nadie.
                seleccionadas_set.add(idx)
                ocupantes_por_destino.setdefault(destino, []).append(idx)
                destino_count[destino] = destino_count.get(destino, 0) + 1
                origen_count[origen] = origen_count.get(origen, 0) + 1
                continue
            ocupantes = ocupantes_por_destino.get(destino, [])
            elegibles = [
                o_idx
                for o_idx in ocupantes
                if origen_count.get(validas.at[o_idx, "categoria_origen"], 0) - 1
                >= minimo
            ]
            if not elegibles:
                # Este destino concreto no se puede liberar sin crear otra
                # víctima por debajo del mínimo: se prueba el siguiente
                # candidato de este mismo origen necesitado.
                continue
            # Desalojar primero a quien más margen tiene (mayor conteo
            # actual) y, entre esos, el enlace de peor score de ese destino.
            elegibles.sort(
                key=lambda o_idx: (
                    -origen_count[validas.at[o_idx, "categoria_origen"]],
                    validas.at[o_idx, "score"],
                )
            )
            desalojado_idx = elegibles[0]
            origen_desalojado = validas.at[desalojado_idx, "categoria_origen"]

            seleccionadas_set.discard(desalojado_idx)
            ocupantes_por_destino[destino].remove(desalojado_idx)
            origen_count[origen_desalojado] = origen_count.get(origen_desalojado, 0) - 1
            donantes_rescate[origen_desalojado] = donantes_rescate.get(origen_desalojado, 0) + 1

            seleccionadas_set.add(idx)
            ocupantes_por_destino.setdefault(destino, []).append(idx)
            origen_count[origen] = origen_count.get(origen, 0) + 1

    return list(seleccionadas_set), donantes_rescate


def _motivos_num_enlaces(
    resultado: pd.DataFrame,
    limites: LimitesPropuesta,
    ampliacion_origen: dict[str, str],
    donantes_rescate: dict[str, int] | None = None,
) -> dict[str, str]:
    """Explica, por categoría origen, por qué tiene MÁS o MENOS enlaces
    nuevos de los `max_enlaces_nuevos_por_origen` "normales" — nunca en
    silencio (decisión de negocio: toda desviación del cupo normal, en
    cualquiera de los dos sentidos, se explica en la propuesta final, ver
    `build_formato_ancho`). Si un origen se queda exactamente en el cupo
    normal, no hace falta ninguna explicación (cadena vacía).

    `donantes_rescate` (ver `_rescatar_minimo_por_congestion`) identifica
    a los orígenes que han "donado" uno de sus enlaces para rescatar a
    otro origen muy congestionado; si ESO es lo que explica que un origen
    se quede por debajo del cupo normal (y no la congestión genérica de
    siempre), se explica así específicamente, con honestidad.
    """
    max_normal = limites.max_enlaces_nuevos_por_origen
    donantes_rescate = donantes_rescate or {}

    validas = resultado[
        (~resultado["pendiente_confirmar"]) & (resultado["score"] >= limites.score_minimo)
    ]
    n_candidatos = validas.groupby("categoria_origen").size()

    seleccionadas = resultado[resultado["seleccionada"]]
    n_seleccionados = seleccionadas.groupby("categoria_origen").size()

    # OJO: se recorren TODOS los orígenes de la propuesta (no solo los que
    # tienen algún enlace seleccionado). Un origen que se queda sin NINGÚN
    # enlace nuevo (0) sigue estando por debajo del cupo normal y por tanto
    # también necesita su motivo explicado — antes de este fix quedaba en
    # blanco porque no aparecía en `n_seleccionados` (al no tener ninguna
    # fila con `seleccionada=True`, `groupby` nunca genera esa clave).
    motivos: dict[str, str] = {}
    for origen in resultado["categoria_origen"].unique():
        n_sel = int(n_seleccionados.get(origen, 0))
        if n_sel > max_normal:
            motivos[origen] = ampliacion_origen.get(
                origen, "cupo ampliado de forma excepcional"
            )
        elif n_sel < max_normal:
            n_cand = int(n_candidatos.get(origen, 0))
            if n_cand < max_normal:
                motivos[origen] = (
                    f"solo hay {n_cand} categoría(s) destino candidata(s) que cumplen los "
                    "requisitos mínimos para este origen (tras aplicar grupos aislados, "
                    "salud técnica y enlaces ya existentes)"
                )
            elif origen in donantes_rescate:
                veces = donantes_rescate[origen]
                minimo_efectivo = min(_MINIMO_SI_HAY_CANDIDATOS_DE_SOBRA, max_normal)
                motivos[origen] = (
                    f"ha cedido {veces} de sus enlaces nuevos a otra(s) categoría(s) que, sin "
                    "este ajuste, se habrían quedado con muy pocos (se garantiza un mínimo de "
                    f"{minimo_efectivo} enlaces a cualquier categoría con candidatos de sobra, "
                    "para que el reparto sea más justo)"
                )
            else:
                motivos[origen] = (
                    "los destinos candidatos con mejor encaje ya habían agotado su cupo de "
                    "enlaces entrantes nuevos con otras categorías de origen mejor puntuadas "
                    "para ese mismo destino"
                )
    return motivos


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
    contador: dict | None = None,
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

    `grupos_aislados` es una lista de patrones ADICIONALES a los 4
    obligatorios de `DEFAULT_GRUPOS_AISLADOS` (Black Friday, Rebajas,
    Special Price y Navidad), que se aplican SIEMPRE pase lo que pase en
    este parámetro (ver `_combinar_con_grupos_obligatorios`): una categoría
    que coincide con uno de estos patrones (en su categoría principal o
    secundaria) SOLO puede enlazar, y ser enlazada, por otras categorías
    del MISMO patrón. Nunca se mezclan entre grupos distintos, ni con el
    resto del catálogo. Es una restricción dura de negocio, no configurable
    a la baja: los pares que la incumplen ni siquiera se generan como
    candidatos.

    `contador`, si se pasa un dict (aunque sea vacío), se rellena con el
    nº de pares que sobreviven en cada etapa del filtrado (pensado para
    diagnosticar por qué una propuesta ha salido vacía sin tener que
    adivinar en qué paso se ha quedado en 0 — ver `diagnosticar_datasets`
    más abajo). No afecta al resultado devuelto ni al comportamiento si
    se deja en `None` (el valor por defecto).
    """
    weights = (weights or ScoringWeights()).normalizados()
    affinity = affinity or AffinityScores()
    limites = limites or LimitesPropuesta()
    oportunidad = oportunidad or OportunidadSEO()

    if contador is not None:
        contador.update(
            {
                "score_minimo_usado": limites.score_minimo,
                "max_enlaces_nuevos_por_origen_usado": limites.max_enlaces_nuevos_por_origen,
                "max_enlaces_nuevos_por_destino_usado": limites.max_enlaces_nuevos_por_destino,
                "pares_antes_de_filtros": 0,
                "pares_tras_grupo_aislado": 0,
                "pares_tras_salud_destino": 0,
                "pares_tras_excluir_enlaces_existentes": 0,
                "pares_pendientes_confirmar": 0,
                "pares_validos_con_score": 0,
                "score_valido_minimo": None,
                "score_valido_maximo": None,
                "pares_seleccionados": 0,
            }
        )

    master = build_master_table(
        datasets, relevancia_categoria, prioridad_negocio, grupos_aislados, search_console
    )
    if master.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    ampliacion_origen = _elegibilidad_ampliacion_origen(master)

    master["norm_volumen"] = _normalize_min_max(master["volumen"])
    # Decisión de negocio del 30 sept: cuantos MÁS productos tenga la
    # categoría destino, más prioridad — antes era al revés (invert=True,
    # favorecía a las categorías con pocos productos). Interesa reforzar
    # con enlaces internos a las categorías con más catálogo, no compensar
    # a las pequeñas.
    master["norm_muchos_productos"] = _normalize_min_max(master["num_productos"])
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
        if contador is not None:
            contador["pares_antes_de_filtros"] += len(pairs)

        # Restricción dura de grupos aislados (Black Friday, Rebajas...):
        # solo se permite el par si origen y destino están en el mismo
        # grupo (o ambos son categorías "normales", grupo_aislado == "").
        pairs = pairs[pairs["grupo_aislado_origen"] == pairs["grupo_aislado_destino"]]
        if contador is not None:
            contador["pares_tras_grupo_aislado"] += len(pairs)
        if pairs.empty:
            continue

        # Restricción dura de salud técnica: nunca se propone como destino
        # una URL caída, redirigida o no indexable (si ese dato está
        # disponible).
        pairs = pairs[pairs["destino_saludable_destino"]]
        if contador is not None:
            contador["pares_tras_salud_destino"] += len(pairs)
        if pairs.empty:
            continue

        candidate_pairs = pairs[["origen", "destino"]].copy()
        kept = exclude_existing_links(candidate_pairs, datasets.enlaces)
        pairs = pairs.merge(kept, on=["origen", "destino"], how="inner")
        if contador is not None:
            contador["pares_tras_excluir_enlaces_existentes"] += len(pairs)
        if pairs.empty:
            continue

        pairs = _calcular_scores_bloque(pairs, weights, affinity)
        if contador is not None:
            pendientes = pairs["pendiente_confirmar"]
            validos = pairs.loc[~pendientes, "score"]
            contador["pares_pendientes_confirmar"] += int(pendientes.sum())
            contador["pares_validos_con_score"] += int((~pendientes).sum())
            if not validos.empty:
                bloque_min = float(validos.min())
                bloque_max = float(validos.max())
                actual_min = contador["score_valido_minimo"]
                actual_max = contador["score_valido_maximo"]
                contador["score_valido_minimo"] = (
                    bloque_min if actual_min is None else min(actual_min, bloque_min)
                )
                contador["score_valido_maximo"] = (
                    bloque_max if actual_max is None else max(actual_max, bloque_max)
                )
        pairs = _recortar_bloque_a_lo_relevante(pairs, limites)
        # OJO: aquí NO se cuenta todavía "pares_seleccionados" -- la marca
        # `seleccionada` de este punto es solo provisional (ver docstring
        # de `_recortar_bloque_a_lo_relevante`); el recuento de verdad se
        # hace más abajo, una vez calculada la selección final con
        # presupuesto de destino sobre la tabla completa.
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

    resultado["seleccionada"], donantes_rescate = _seleccionar_con_presupuesto_destino(
        resultado, limites, ampliacion_origen
    )
    if contador is not None:
        contador["pares_seleccionados"] = int(resultado["seleccionada"].sum())

    resultado["motivo_num_enlaces_origen"] = resultado["categoria_origen"].map(
        _motivos_num_enlaces(resultado, limites, ampliacion_origen, donantes_rescate)
    ).fillna("")

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
# 4.1) Diagnóstico: por qué la propuesta ha salido vacía (o casi vacía)
# ---------------------------------------------------------------------------
#
# `generate_link_proposals` siempre puede devolver 0 filas si los datos de
# entrada no encajan entre sí (aunque la carga de cada fichero por
# separado no haya dado ningún error), y en ese caso el motivo real puede
# estar en cualquiera de varios sitios: URLs que no cruzan entre datasets,
# una columna de salud técnica (Status_Code/Indexable) mal detectada que
# excluye TODAS las URLs como destino, o una taxonomía que no se ha
# reconocido bien. Esta función recalcula (de forma barata, sin el cruce
# N² completo) los números clave de cada paso para poder señalar la causa
# más probable sin tener que examinar el fichero original a mano.


def diagnosticar_datasets(
    datasets: InputDatasets,
    relevancia_categoria: pd.DataFrame | None = None,
    prioridad_negocio: pd.DataFrame | None = None,
    grupos_aislados: list[str] | None = None,
    search_console: pd.DataFrame | None = None,
) -> dict:
    """Devuelve un dict con estadísticas de diagnóstico sobre los 4
    datasets de entrada, pensado para mostrarse en la interfaz cuando
    `generate_link_proposals` devuelve una propuesta vacía. Nunca lanza
    una excepción por sí misma.
    """
    diagnostico: dict = {
        "n_crawl": int(len(datasets.crawl)),
        "n_volumen": int(len(datasets.volumen)),
        "n_taxonomia": int(len(datasets.taxonomia)),
        "n_enlaces": int(len(datasets.enlaces)),
    }

    urls_crawl = set(datasets.crawl["url"]) if not datasets.crawl.empty else set()
    urls_volumen = set(datasets.volumen["url"]) if not datasets.volumen.empty else set()
    urls_taxonomia = set(datasets.taxonomia["url"]) if not datasets.taxonomia.empty else set()

    diagnostico["urls_crawl_con_volumen"] = len(urls_crawl & urls_volumen)
    diagnostico["urls_crawl_con_taxonomia"] = len(urls_crawl & urls_taxonomia)
    diagnostico["ejemplo_urls_crawl"] = sorted(urls_crawl)[:5]
    diagnostico["ejemplo_urls_volumen"] = sorted(urls_volumen)[:5]
    diagnostico["ejemplo_urls_taxonomia"] = sorted(urls_taxonomia)[:5]
    diagnostico["ejemplo_urls_crawl_sin_taxonomia"] = sorted(urls_crawl - urls_taxonomia)[:5]

    if len(urls_crawl) < 2:
        diagnostico["motivo_probable"] = (
            "El dataset de crawl tiene menos de 2 URLs válidas tras la limpieza "
            "(o ninguna). Revisa que la columna de URL del fichero no venga "
            "vacía y que se haya reconocido bien (mira 'ejemplo_urls_crawl')."
        )
        return diagnostico

    master = build_master_table(
        datasets, relevancia_categoria, prioridad_negocio, grupos_aislados, search_console
    )
    diagnostico["n_master"] = int(len(master))

    if "status_code" not in master.columns:
        master["status_code"] = float("nan")
    if "indexable" not in master.columns:
        master["indexable"] = None

    saludable = pd.Series(True, index=master.index)
    saludable &= ~(master["status_code"].notna() & (master["status_code"] != 200))
    saludable &= ~(master["indexable"] == False)  # noqa: E712
    diagnostico["n_destino_saludable"] = int(saludable.sum())
    diagnostico["n_destino_no_saludable"] = int((~saludable).sum())
    if master["status_code"].notna().any():
        diagnostico["distribucion_status_code"] = {
            str(k): int(v) for k, v in master["status_code"].value_counts(dropna=False).items()
        }
    if master["indexable"].notna().any():
        diagnostico["distribucion_indexable"] = {
            str(k): int(v) for k, v in master["indexable"].value_counts(dropna=False).items()
        }
    if "profundidad" in master.columns and master["profundidad"].notna().any():
        diagnostico["profundidad_disponible"] = True
        diagnostico["profundidad_mediana_catalogo"] = float(master["profundidad"].median())
    else:
        diagnostico["profundidad_disponible"] = False

    # `master` ya trae "grupo_aislado" calculado por `build_master_table`
    # (con la misma lógica, prioridad de URL incluida) — se reutiliza tal
    # cual en vez de recalcularlo aquí por segunda vez con una copia
    # desactualizada de la lógica.
    grupo_aislado = master["grupo_aislado"]
    diagnostico["distribucion_grupo_aislado"] = {
        (k if k else "(normal, sin grupo)"): int(v)
        for k, v in grupo_aislado.value_counts(dropna=False).items()
    }

    n_categorias = int(
        master["categoria_principal"].astype(str).str.strip().replace("", pd.NA).nunique(dropna=True)
    )
    diagnostico["n_categorias_principales_distintas"] = n_categorias

    if diagnostico["n_destino_saludable"] < 2:
        diagnostico["motivo_probable"] = (
            "Prácticamente ninguna URL queda marcada como 'destino saludable' "
            "(según las columnas Status_Code/Indexable): revisa esas dos "
            "columnas en el fichero, es posible que se esté leyendo una "
            "columna distinta a la esperada (mira 'distribucion_status_code' "
            "y 'distribucion_indexable')."
        )
    elif n_categorias <= 1:
        diagnostico["motivo_probable"] = (
            "Todas las URLs comparten la misma categoría principal (o no se "
            "les ha asignado ninguna): revisa la columna de "
            "Categoria_Principal del fichero."
        )
    elif diagnostico["urls_crawl_con_taxonomia"] == 0:
        diagnostico["motivo_probable"] = (
            "Ninguna URL del crawl tiene taxonomía asociada: aunque estén en "
            "el mismo fichero, puede que la columna de URL usada para "
            "detectar el crawl no sea la misma que la usada para la "
            "taxonomía (revisa 'ejemplo_urls_crawl' vs "
            "'ejemplo_urls_taxonomia')."
        )
    else:
        diagnostico["motivo_probable"] = (
            "Los filtros básicos (salud técnica, categorías) no descartan "
            "nada por sí solos: si aun así la propuesta sale vacía, el motivo "
            "más probable es que todos los pares candidato ya tuvieran un "
            "enlace existente entre sí (revisa el dataset de enlaces, "
            "'n_enlaces' arriba) o que el score mínimo configurado sea "
            "demasiado alto."
        )

    return diagnostico


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


_PREFIJOS_TITULO_A_QUITAR = ("comprar-",)


def _derivar_titulo_desde_url(url: str | None) -> str:
    """Aproxima un título legible a partir del slug de la URL para las
    filas sin H1 real (columna opcional, ver `H1_CANDIDATES` en
    `core.data_loader`): quita el ID numérico del principio y el prefijo
    "comprar-" si lo hay, y cambia los guiones por espacios.

    OJO: esto NO es el H1 real de la página, es solo una aproximación
    para poder revisar la propuesta de un vistazo mientras el export del
    rastreo no incluya esa columna — en cuanto el crawl la traiga, se usa
    el H1 real y esta función deja de aplicarse a esas filas.
    """
    if not url or not isinstance(url, str):
        return ""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    match = _ID_SLUG_RE.match(slug)
    if match:
        slug = slug[match.end():]
    for prefijo in _PREFIJOS_TITULO_A_QUITAR:
        if slug.startswith(prefijo):
            slug = slug[len(prefijo):]
            break
    texto = slug.replace("-", " ").replace("_", " ").strip()
    return texto[:1].upper() + texto[1:] if texto else ""


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
    columnas_vacias = [
        "id",
        "url",
        "h1",
        "categoria_principal",
        "categoria_secundaria",
        "n_enlaces",
        "motivo_num_enlaces",
    ]
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
            "id": primera.get("id_origen", "") or extraer_id_de_url(origen),
            "url": origen,
            "h1": primera.get("h1_origen", ""),
            "categoria_principal": primera.get("categoria_principal_origen", ""),
            "categoria_secundaria": primera.get("categoria_secundaria_origen", ""),
            "n_enlaces": int(len(grupo)),
            "motivo_num_enlaces": primera.get("motivo_num_enlaces_origen", "") or "",
        }
        for row in grupo.itertuples(index=False):
            n = int(row.orden)
            fila[f"linked_id_{n}"] = getattr(row, "id_destino", "") or extraer_id_de_url(row.categoria_destino)
            fila[f"linked_url_{n}"] = row.categoria_destino
            fila[f"linked_h1_{n}"] = getattr(row, "h1_destino", "")
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
            f"linked_h1_{n}",
            f"linked_category_{n}",
            f"linked_subcategory_{n}",
            f"linked_score_{n}",
            f"justificacion_{n}",
        ]

    return pd.DataFrame(filas).reindex(columns=columnas)


# ---------------------------------------------------------------------------
# 5.1) Formato "para IT" (SQL listo para ejecutar, mismo formato que el
# documento que el equipo ya pasaba a IT en marzo 2025)
# ---------------------------------------------------------------------------

# Lista de `id_shop` (multi-tienda de PrestaShop: distintos idiomas/países
# de Sklum) que llevaba el documento de marzo 2025. Es un dato de NEGOCIO
# (qué tiendas existen hoy), no una constante técnica, así que se deja
# como valor por defecto configurable en vez de fijarlo sin más -- si el
# equipo añade o retira alguna tienda, basta con pasar `shops` distinto.
IT_SHOPS_DEFAULT = "11,15,16,17,19,20,23,26,382,383"

FORMATO_IT_COLUMNS = [
    "ID CAT MAIN",
    "URL",
    "Identificadores de las categorías, lista 2",
    "UPDATE",
]


def build_formato_it(resultado: pd.DataFrame, shops: str = IT_SHOPS_DEFAULT) -> pd.DataFrame:
    """Convierte la propuesta al formato exacto que el equipo ya pasaba a
    IT en marzo 2025 (hoja "Info a IT"): una fila por URL origen con su
    ID de categoría, la lista de IDs destino separados por coma (mismo
    orden de score que `build_formato_ancho`) y la sentencia SQL ya lista
    para ejecutar sobre `led_category_shop`.

    Diferencia deliberada con el documento de marzo 2025: aquella lista
    tenía SIEMPRE exactamente 5 IDs por fila; aquí tiene tantos IDs como
    enlaces se hayan seleccionado de verdad para esa categoría (de 3 a
    `max_enlaces_nuevos_por_origen_excepcional`) -- refleja la propuesta
    real en vez de recortarla por compatibilidad con el formato antiguo.
    Si el campo `id_list_two` de PrestaShop/la plantilla del front
    necesitara un nº fijo de huecos, hay que confirmarlo con IT antes de
    ejecutar el SQL.

    No incluye enlaces manuales a páginas CMS (tipo "cms:1069" en el
    documento de marzo 2025): esta herramienta solo conoce categorías de
    producto del rastreo, no páginas de contenido.

    `shops` es la lista de `id_shop` separados por coma que va en el
    WHERE de cada UPDATE -- por defecto, la misma que ya usaba el equipo
    en marzo 2025 (`IT_SHOPS_DEFAULT`).
    """
    if resultado is None or resultado.empty or "seleccionada" not in resultado.columns:
        return pd.DataFrame(columns=FORMATO_IT_COLUMNS)

    ancho = build_formato_ancho(resultado)
    if ancho.empty:
        return pd.DataFrame(columns=FORMATO_IT_COLUMNS)

    id_cols = sorted(
        (c for c in ancho.columns if c.startswith("linked_id_")),
        key=lambda c: int(c.rsplit("_", 1)[-1]),
    )

    filas = []
    for _, row in ancho.iterrows():
        ids = [str(row[c]) for c in id_cols if _valor_valido(row[c]) and str(row[c]).strip() != ""]
        id_main = str(row.get("id", "") or "").strip()
        if not ids or not id_main:
            continue
        lista2 = ",".join(ids)
        url = str(row.get("url", "") or "")
        filas.append(
            {
                "ID CAT MAIN": int(id_main) if id_main.isdigit() else id_main,
                "URL": f"https://www.{url}" if url else "",
                "Identificadores de las categorías, lista 2": lista2,
                "UPDATE": (
                    f"UPDATE led_category_shop SET `id_list_two`='{lista2}' WHERE  "
                    f"`id_category`={id_main} AND `id_shop`in ({shops});"
                ),
            }
        )

    return pd.DataFrame(filas, columns=FORMATO_IT_COLUMNS)


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

"""Lógica de negocio: cruce de datasets, scoring y generación de la
propuesta de interlinking.

Este módulo es intencionadamente independiente de Streamlit para poder
testearlo con pytest de forma aislada (ver tests/test_scoring.py y
tests/test_exclusion.py).
"""
from __future__ import annotations

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
    """
    candidates = candidates[candidates["origen"] != candidates["destino"]]

    if enlaces.empty:
        return candidates.reset_index(drop=True)

    existing_pairs = set(
        zip(enlaces["source_url"], enlaces["destination_url"])
    )
    mask_existing = candidates.apply(
        lambda row: (row["origen"], row["destino"]) in existing_pairs, axis=1
    )
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

    pairs = origen_df.assign(_key=1).merge(destino_df.assign(_key=1), on="_key").drop(columns="_key")

    # Restricción dura de grupos aislados (Black Friday, Rebajas...): solo
    # se permite el par si origen y destino están en el mismo grupo (o
    # ambos son categorías "normales", grupo_aislado == "").
    pairs = pairs[pairs["grupo_aislado_origen"] == pairs["grupo_aislado_destino"]]
    if pairs.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    # Restricción dura de salud técnica: nunca se propone como destino una
    # URL caída, redirigida o no indexable (si ese dato está disponible).
    pairs = pairs[pairs["destino_saludable_destino"]]
    if pairs.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    candidate_pairs = pairs[["origen", "destino"]].copy()
    kept = exclude_existing_links(candidate_pairs, datasets.enlaces)
    pairs = pairs.merge(kept, on=["origen", "destino"], how="inner")

    if pairs.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    afinidad_valores = []
    motivos = []
    scores = []
    for row in pairs.itertuples(index=False):
        motivo_partes: list[str] = []

        falta_volumen_destino = getattr(row, "falta_volumen_destino")
        falta_taxonomia_destino = getattr(row, "falta_taxonomia_destino")
        falta_taxonomia_origen = getattr(row, "falta_taxonomia_origen")

        if falta_volumen_destino:
            motivo_partes.append("categoría destino sin volumen de búsqueda")
        if falta_taxonomia_destino:
            motivo_partes.append("categoría destino sin categorización")
        if falta_taxonomia_origen:
            motivo_partes.append("categoría origen sin categorización")

        afinidad = affinity_score(
            getattr(row, "categoria_principal_origen"),
            getattr(row, "categoria_secundaria_origen"),
            getattr(row, "categoria_principal_destino"),
            getattr(row, "categoria_secundaria_destino"),
            affinity,
        )
        afinidad_valores.append(afinidad)

        if motivo_partes or afinidad is None:
            motivos.append("; ".join(motivo_partes) or "categorización incompleta")
            scores.append(float("nan"))
        else:
            score = (
                weights.volumen_busqueda * getattr(row, "norm_volumen_destino")
                + weights.pocos_productos * getattr(row, "norm_pocos_productos_destino")
                + weights.pocos_enlaces_entrantes * getattr(row, "norm_pocos_enlaces_destino")
                + weights.afinidad_categoria * afinidad
                + weights.relevancia_categoria * getattr(row, "relevancia_categoria_destino")
                + weights.prioridad_negocio * getattr(row, "prioridad_negocio_destino")
                + weights.autoridad_origen * _sin_nan(getattr(row, "norm_autoridad_origen"))
                + weights.presupuesto_enlaces_origen
                * _sin_nan(getattr(row, "norm_presupuesto_enlaces_origen"))
                + weights.posicion_oportunidad * _sin_nan(getattr(row, "posicion_oportunidad_destino"))
                + weights.impresiones_busqueda * _sin_nan(getattr(row, "norm_impresiones_destino"))
            )
            motivos.append("")
            scores.append(score)

    pairs["afinidad"] = afinidad_valores
    pairs["score"] = scores
    pairs["motivo_pendiente"] = motivos
    pairs["pendiente_confirmar"] = pairs["motivo_pendiente"] != ""

    pairs["keyword_destino"] = pairs["keyword_destino"].fillna("")
    pairs["texto_ancla_sugerido"] = pairs["keyword_destino"]

    resultado = pairs.rename(
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

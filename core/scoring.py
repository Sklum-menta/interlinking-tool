"""Lógica de negocio: cruce de datasets, scoring y generación de la
propuesta de interlinking.

Este módulo es intencionadamente independiente de Streamlit para poder
testearlo con pytest de forma aislada (ver tests/test_scoring.py y
tests/test_exclusion.py).
"""
from __future__ import annotations

import pandas as pd

from core.config import AffinityScores, LimitesPropuesta, ScoringWeights
from core.data_loader import InputDatasets

# ---------------------------------------------------------------------------
# 1) Tabla maestra: una fila por URL indexable con todos sus atributos
# ---------------------------------------------------------------------------


def build_master_table(datasets: InputDatasets) -> pd.DataFrame:
    """Cruza crawl + volumen + taxonomía por URL y añade el nº de
    enlaces entrantes actuales (a partir del dataset de enlaces).

    La base es el crawl (las categorías indexables). Si una URL del
    crawl no aparece en volumen o en taxonomía, se marca con
    `falta_volumen` / `falta_taxonomia` en lugar de asumir un valor por
    defecto (p.ej. volumen=0), tal y como pide el punto 5 del encargo.
    """
    master = datasets.crawl.merge(datasets.volumen, on="url", how="left")
    master = master.merge(datasets.taxonomia, on="url", how="left")

    master["falta_volumen"] = master["volumen"].isna()
    master["falta_taxonomia"] = master["categoria_principal"].isna() | (
        master["categoria_principal"].astype(str).str.strip() == ""
    )

    if not datasets.enlaces.empty:
        entrantes = (
            datasets.enlaces.groupby("destination_url")
            .size()
            .rename("enlaces_entrantes_actuales")
        )
        master = master.merge(
            entrantes, left_on="url", right_index=True, how="left"
        )
    else:
        master["enlaces_entrantes_actuales"] = 0
    master["enlaces_entrantes_actuales"] = master["enlaces_entrantes_actuales"].fillna(0)

    return master.reset_index(drop=True)


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
    "num_productos_destino",
    "pendiente_confirmar",
    "motivo_pendiente",
    "seleccionada",
]


def generate_link_proposals(
    datasets: InputDatasets,
    weights: ScoringWeights | None = None,
    affinity: AffinityScores | None = None,
    limites: LimitesPropuesta | None = None,
) -> pd.DataFrame:
    """Genera la propuesta de interlinking completa.

    Devuelve UNA tabla con todos los pares (origen, destino) candidatos
    válidos (sin auto-enlaces ni enlaces ya existentes), cada uno con:
      - su score (o NaN si está pendiente de confirmar),
      - el motivo si está pendiente de confirmar,
      - si ha sido seleccionada dentro del límite de enlaces nuevos por
        categoría origen (`seleccionada=True`) o no.

    Las filas "pendiente_confirmar" NUNCA se seleccionan automáticamente
    (no se puede confiar en un score calculado sobre datos incompletos),
    pero se conservan en la tabla para que el equipo las revise y
    complete los datos que faltan.
    """
    weights = (weights or ScoringWeights()).normalizados()
    affinity = affinity or AffinityScores()
    limites = limites or LimitesPropuesta()

    master = build_master_table(datasets)
    if master.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    master["norm_volumen"] = _normalize_min_max(master["volumen"])
    master["norm_pocos_productos"] = _normalize_min_max(master["num_productos"], invert=True)
    master["norm_pocos_enlaces"] = _normalize_min_max(
        master["enlaces_entrantes_actuales"], invert=True
    )

    urls = master["url"].tolist()
    if len(urls) < 2:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    origen_df = master.add_suffix("_origen").rename(columns={"url_origen": "origen"})
    destino_df = master.add_suffix("_destino").rename(columns={"url_destino": "destino"})

    pairs = origen_df.assign(_key=1).merge(destino_df.assign(_key=1), on="_key").drop(columns="_key")

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

"""Interfaz Streamlit de la herramienta de interlinking SEO.

`streamlit run app.py`
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from auth import require_login
from core.config import AffinityScores, AppConfig, LimitesPropuesta, ScoringWeights
from core.data_loader import (
    DataLoadError,
    InputDatasets,
    load_crawl_productos,
    load_enlaces,
    load_taxonomia,
    load_volumen,
)
from core.scoring import generate_link_proposals

st.set_page_config(
    page_title="Interlinking SEO — Sklum",
    page_icon="🔗",
    layout="wide",
)

config = AppConfig.from_env()
user_email = require_login(config)

st.title("🔗 Herramienta de Interlinking SEO")
st.caption(f"Sesión: {user_email}")


# ---------------------------------------------------------------------------
# Carga de datasets: subida manual o carpeta compartida configurada
# ---------------------------------------------------------------------------

def _find_in_shared_dir(directory: str, patterns: list[str]) -> str | None:
    base = Path(directory)
    if not base.exists():
        return None
    candidates = []
    for pattern in patterns:
        candidates.extend(base.glob(pattern))
    if not candidates:
        return None
    return str(max(candidates, key=lambda p: p.stat().st_mtime))


st.header("1. Datasets de entrada")

usar_carpeta = False
if config.shared_data_dir:
    usar_carpeta = st.toggle(
        f"Leer automáticamente el export más reciente de '{config.shared_data_dir}'",
        value=True,
    )

col1, col2, col3 = st.columns(3)

crawl_file = None
enlaces_file = None
volumen_file = None
taxonomia_file = None

if usar_carpeta and config.shared_data_dir:
    crawl_file = _find_in_shared_dir(config.shared_data_dir, ["*crawl*", "*producto*"])
    enlaces_file = _find_in_shared_dir(config.shared_data_dir, ["*enlace*", "*outlink*", "*link*"])
    volumen_file = _find_in_shared_dir(config.shared_data_dir, ["*volumen*", "*volume*", "*keyword*"])
    taxonomia_file = _find_in_shared_dir(config.shared_data_dir, ["*taxonom*", "*categor*"])
    st.write(
        {
            "crawl": crawl_file,
            "enlaces": enlaces_file,
            "volumen": volumen_file,
            "taxonomia": taxonomia_file,
        }
    )

with col1:
    st.subheader("Crawl (nº productos)")
    if not crawl_file:
        crawl_file = st.file_uploader("URL + nº de productos", type=["csv", "xlsx"], key="crawl")
with col2:
    st.subheader("Enlaces existentes")
    if not enlaces_file:
        enlaces_file = st.file_uploader(
            "Export de enlaces (All Outlinks o por zona)", type=["csv", "xlsx"], key="enlaces"
        )
with col3:
    st.subheader("Volumen de búsqueda")
    if not volumen_file:
        volumen_file = st.file_uploader("URL + Keyword + Volumen", type=["csv", "xlsx"], key="volumen")

st.subheader("Taxonomía (categoría principal / secundaria)")
if not taxonomia_file:
    taxonomia_file = st.file_uploader(
        "URL + Categoria_Principal + Categoria_Secundaria", type=["csv", "xlsx"], key="taxonomia"
    )

st.caption(
    "¿No tienes datos a mano? Usa el dataset de ejemplo en `sample_data/` "
    "para probar la herramienta (ver README)."
)


# ---------------------------------------------------------------------------
# Panel de configuración de pesos y límites
# ---------------------------------------------------------------------------

st.header("2. Configuración del scoring")

with st.expander("Pesos del scoring y límites", expanded=True):
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        w_volumen = st.slider("Peso: volumen de búsqueda", 0.0, 1.0, 0.40, 0.05)
    with c2:
        w_productos = st.slider("Peso: pocos productos", 0.0, 1.0, 0.20, 0.05)
    with c3:
        w_enlaces = st.slider("Peso: pocos enlaces entrantes", 0.0, 1.0, 0.20, 0.05)
    with c4:
        w_afinidad = st.slider("Peso: afinidad de categoría", 0.0, 1.0, 0.20, 0.05)

    st.caption(
        "Los 4 pesos se normalizan automáticamente para que sumen 100%, "
        "así que puedes moverlos libremente."
    )

    c5, c6, c7 = st.columns(3)
    with c5:
        aff_misma = st.slider("Afinidad: misma principal y secundaria", 0.0, 1.0, 1.0, 0.05)
    with c6:
        aff_principal = st.slider("Afinidad: solo misma principal", 0.0, 1.0, 0.6, 0.05)
    with c7:
        aff_distinta = st.slider("Afinidad: categorías distintas", 0.0, 1.0, 0.15, 0.05)

    c8, c9 = st.columns(2)
    with c8:
        max_enlaces = st.number_input(
            "Máx. enlaces nuevos sugeridos por categoría origen", min_value=1, max_value=50, value=5
        )
    with c9:
        score_minimo = st.slider("Score mínimo para proponer un enlace", 0.0, 1.0, 0.0, 0.05)

weights = ScoringWeights(
    volumen_busqueda=w_volumen,
    pocos_productos=w_productos,
    pocos_enlaces_entrantes=w_enlaces,
    afinidad_categoria=w_afinidad,
)
affinity = AffinityScores(
    misma_principal_y_secundaria=aff_misma,
    misma_principal=aff_principal,
    distinta=aff_distinta,
)
limites = LimitesPropuesta(
    max_enlaces_nuevos_por_origen=int(max_enlaces),
    score_minimo=score_minimo,
)


# ---------------------------------------------------------------------------
# Generación de la propuesta
# ---------------------------------------------------------------------------

st.header("3. Generar propuesta")

if "propuesta" not in st.session_state:
    st.session_state["propuesta"] = None

generar = st.button("🚀 Generar propuesta de interlinking", type="primary")

if generar:
    faltan = [
        nombre
        for nombre, f in [
            ("crawl", crawl_file),
            ("enlaces", enlaces_file),
            ("volumen", volumen_file),
            ("taxonomía", taxonomia_file),
        ]
        if f is None
    ]
    if faltan:
        st.error(f"Faltan datasets: {', '.join(faltan)}.")
    else:
        try:
            datasets = InputDatasets(
                crawl=load_crawl_productos(crawl_file),
                enlaces=load_enlaces(enlaces_file),
                volumen=load_volumen(volumen_file),
                taxonomia=load_taxonomia(taxonomia_file),
            )
            with st.spinner("Calculando scores y generando propuesta..."):
                resultado = generate_link_proposals(datasets, weights, affinity, limites)
            st.session_state["propuesta"] = resultado
            st.success(f"Propuesta generada: {len(resultado)} filas.")
        except DataLoadError as exc:
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001
            st.exception(exc)


# ---------------------------------------------------------------------------
# Resultados
# ---------------------------------------------------------------------------

resultado: pd.DataFrame | None = st.session_state.get("propuesta")

if resultado is not None and not resultado.empty:
    st.header("4. Resultado")

    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        categorias_principales = sorted(
            resultado["categoria_principal_destino"].dropna().astype(str).unique().tolist()
        )
        filtro_categoria = st.multiselect("Filtrar por categoría principal (destino)", categorias_principales)
    with fc2:
        filtro_score_min = st.slider("Score mínimo a mostrar", 0.0, 1.0, 0.0, 0.05)
    with fc3:
        solo_seleccionadas = st.checkbox("Mostrar solo las propuestas seleccionadas", value=True)

    vista = resultado.copy()
    if filtro_categoria:
        vista = vista[vista["categoria_principal_destino"].astype(str).isin(filtro_categoria)]
    if solo_seleccionadas:
        vista = vista[vista["seleccionada"] | vista["pendiente_confirmar"]]
    vista = vista[(vista["score"].fillna(1.0) >= filtro_score_min) | vista["pendiente_confirmar"]]

    def _badge(row):
        return "⚠️ Pendiente de confirmar" if row["pendiente_confirmar"] else "✅"

    vista_mostrar = vista.copy()
    vista_mostrar.insert(0, "estado", vista_mostrar.apply(_badge, axis=1))

    st.dataframe(
        vista_mostrar,
        use_container_width=True,
        column_config={
            "score": st.column_config.NumberColumn(format="%.3f"),
        },
    )

    n_pendientes = int(resultado["pendiente_confirmar"].sum())
    n_seleccionadas = int(resultado["seleccionada"].sum())
    st.caption(
        f"{n_seleccionadas} propuestas seleccionadas · {n_pendientes} filas pendientes de "
        "confirmar por falta de volumen o categorización."
    )

    st.subheader("Descargar propuesta")
    dcol1, dcol2 = st.columns(2)
    with dcol1:
        st.download_button(
            "⬇️ Descargar CSV",
            data=resultado.to_csv(index=False).encode("utf-8"),
            file_name="propuesta_interlinking.csv",
            mime="text/csv",
        )
    with dcol2:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            resultado.to_excel(writer, index=False, sheet_name="Propuesta")
        st.download_button(
            "⬇️ Descargar Excel",
            data=buffer.getvalue(),
            file_name="propuesta_interlinking.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
elif resultado is not None:
    st.info("La propuesta generada está vacía: revisa que los datasets tengan URLs en común.")

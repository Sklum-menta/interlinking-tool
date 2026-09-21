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
from core.config import AffinityScores, AppConfig, LimitesPropuesta, OportunidadSEO, ScoringWeights
from core.data_loader import (
    DataLoadError,
    InputDatasets,
    load_crawl_productos,
    load_enlaces,
    load_plantilla_unificada,
    load_prioridad_negocio,
    load_relevancia_manual,
    load_search_console,
    load_taxonomia,
    load_volumen,
)
from core.scoring import comparar_evolucion_search_console, generate_link_proposals

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

modo_datos = st.radio(
    "¿Cómo quieres subir los datos?",
    ["Plantilla única (recomendado)", "Ficheros separados (avanzado)"],
    horizontal=True,
    help=(
        "Plantilla única: UN solo Excel/CSV con una fila por URL y todas las "
        "columnas (nº de productos, categoría, keyword + volumen, enlaces "
        "existentes numerados por zona). Es la forma más rápida de copiar y "
        "pegar los datos del rastreo. Ficheros separados: el formato anterior, "
        "con 4 exports distintos (crawl, enlaces, volumen, taxonomía)."
    ),
)

crawl_file = None
enlaces_file = None
volumen_file = None
taxonomia_file = None
plantilla_file = None

if modo_datos == "Plantilla única (recomendado)":
    st.caption(
        "Descarga la plantilla vacía, pega ahí los datos de vuestro rastreo "
        "(una fila por URL) y súbela aquí. Ver el formato exacto en el README, "
        "sección 2.6."
    )
    _plantilla_path = Path(__file__).parent / "sample_data" / "plantilla_unificada.xlsx"
    if _plantilla_path.exists():
        st.download_button(
            "⬇️ Descargar plantilla vacía (Excel)",
            data=_plantilla_path.read_bytes(),
            file_name="plantilla_interlinking.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    plantilla_file = st.file_uploader(
        "Plantilla unificada (URL, nº productos, categoría, keyword + volumen, enlaces)",
        type=["csv", "xlsx"],
        key="plantilla_unificada",
    )
else:
    usar_carpeta = False
    if config.shared_data_dir:
        usar_carpeta = st.toggle(
            f"Leer automáticamente el export más reciente de '{config.shared_data_dir}'",
            value=True,
        )

    col1, col2, col3 = st.columns(3)

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

with st.expander("📈 Search Console (opcional)", expanded=False):
    st.caption(
        "Sube el export de clics + impresiones (últimos 28 días) y posición media "
        "para activar las señales de 'oportunidad SEO' del scoring (sección 2). Si "
        "además subes el export del mes anterior, verás en los resultados cómo ha "
        "evolucionado cada URL desde la última vez."
    )
    sc_actual_file = st.file_uploader(
        "Search Console — mes actual (URL, Clics_28d, Impresiones_28d, Posicion_Media)",
        type=["csv", "xlsx"],
        key="sc_actual",
    )
    sc_anterior_file = st.file_uploader(
        "Search Console — mes anterior (opcional, para ver la evolución)",
        type=["csv", "xlsx"],
        key="sc_anterior",
    )


# ---------------------------------------------------------------------------
# Panel de configuración de pesos y límites
# ---------------------------------------------------------------------------

st.header("2. Configuración del scoring")

st.caption(
    "Cada 'peso' es simplemente la importancia relativa (de 0 a 1) que le das a "
    "cada criterio a la hora de decidir qué categoría destino es más prioritaria "
    "para recibir un enlace nuevo. No hace falta que sumen 1: se reparten "
    "automáticamente en proporción (p.ej. 40/20/20/20 y 4/2/2/2 dan exactamente "
    "el mismo resultado). Pon más alto lo que más te importe y más bajo (o a 0) "
    "lo que no quieras que influya."
)

with st.expander("Pesos del scoring y límites", expanded=True):
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        w_volumen = st.slider(
            "Peso: volumen de búsqueda",
            0.0,
            1.0,
            0.40,
            0.05,
            help="Más alto → prioriza enlazar categorías cuya keyword principal tiene más búsquedas mensuales.",
        )
    with c2:
        w_productos = st.slider(
            "Peso: pocos productos",
            0.0,
            1.0,
            0.20,
            0.05,
            help="Más alto → prioriza categorías con pocos productos (les cuesta más posicionar por sí solas).",
        )
    with c3:
        w_enlaces = st.slider(
            "Peso: pocos enlaces entrantes",
            0.0,
            1.0,
            0.20,
            0.05,
            help="Más alto → prioriza categorías que hoy reciben pocos enlaces internos (reparte mejor el 'link juice').",
        )
    with c4:
        w_afinidad = st.slider(
            "Peso: afinidad de categoría",
            0.0,
            1.0,
            0.20,
            0.05,
            help="Más alto → prioriza enlazar entre categorías relacionadas (misma categoría principal/secundaria) frente a categorías sin relación.",
        )

    st.caption(
        "Los 4 pesos de arriba se normalizan automáticamente para que sumen 100%, "
        "así que puedes moverlos libremente sin hacer cuentas."
    )

    st.markdown(
        "**Afinidad de categoría**: cuánta relación exige entre origen y destino "
        "para considerarlos 'afines' (0 = ninguna relación, 1 = máxima)."
    )
    c5, c6, c7 = st.columns(3)
    with c5:
        aff_misma = st.slider(
            "Misma principal y secundaria",
            0.0,
            1.0,
            1.0,
            0.05,
            help="Ej: 'Muebles > Salón' con 'Muebles > Salón'. Caso de máxima afinidad.",
        )
    with c6:
        aff_principal = st.slider(
            "Solo misma principal",
            0.0,
            1.0,
            0.6,
            0.05,
            help="Ej: 'Muebles > Salón' con 'Muebles > Dormitorio'. Relación parcial.",
        )
    with c7:
        aff_distinta = st.slider(
            "Categorías distintas",
            0.0,
            1.0,
            0.15,
            0.05,
            help="Ej: 'Muebles' con 'Iluminación'. Sin relación directa.",
        )

    c8, c9 = st.columns(2)
    with c8:
        max_enlaces = st.number_input(
            "Máx. enlaces nuevos sugeridos por categoría origen",
            min_value=1,
            max_value=50,
            value=5,
            help="De todas las candidatas, cuántos enlaces nuevos (como máximo) se proponen desde cada categoría origen — los de mayor score.",
        )
    with c9:
        score_minimo = st.slider(
            "Score mínimo para proponer un enlace",
            0.0,
            1.0,
            0.0,
            0.05,
            help="Descarta candidatas por debajo de este score, aunque entrarían dentro del máximo de arriba. 0 = no descarta ninguna por score.",
        )

    st.markdown(
        "**Página origen** (0.0 = no afectan; miran a la categoría que enlaza, no a la que recibe el enlace)"
    )
    c12, c13 = st.columns(2)
    with c12:
        w_autoridad_origen = st.slider(
            "Peso: autoridad del origen",
            0.0,
            1.0,
            0.0,
            0.05,
            help="Más alto → prioriza enlazar desde categorías que ya reciben muchos enlaces internos propios (transmiten más valor al enlazar).",
        )
    with c13:
        w_presupuesto_origen = st.slider(
            "Peso: presupuesto de enlaces del origen",
            0.0,
            1.0,
            0.0,
            0.05,
            help="Más alto → prioriza enlazar desde categorías que hoy tienen pocos enlaces salientes en total (cada enlace nuevo diluye menos valor).",
        )

    st.markdown(
        "**Search Console — oportunidad SEO** (0.0 = no afectan; requieren subir el "
        "dataset de Search Console más arriba; si no se sube, estos pesos no tienen "
        "ningún efecto aunque estén por encima de 0)"
    )
    c14, c15 = st.columns(2)
    with c14:
        w_posicion_oportunidad = st.slider(
            "Peso: posición en zona de oportunidad",
            0.0,
            1.0,
            0.0,
            0.05,
            help="Más alto → prioriza categorías destino cuya posición media en Google está dentro del rango configurado abajo (candidatas a subir a primera página).",
        )
    with c15:
        w_impresiones = st.slider(
            "Peso: impresiones en Google",
            0.0,
            1.0,
            0.0,
            0.05,
            help="Más alto → prioriza categorías destino con más impresiones en Google (más visibilidad potencial, aunque hoy generen pocos clics).",
        )
    c16, c17 = st.columns(2)
    with c16:
        posicion_min = st.number_input(
            "Posición mínima de la 'zona de oportunidad'",
            min_value=1.0,
            max_value=100.0,
            value=4.0,
            step=1.0,
            help="Por debajo de esta posición (ya muy bien posicionada) se considera que hay menos margen de mejora.",
        )
    with c17:
        posicion_max = st.number_input(
            "Posición máxima de la 'zona de oportunidad'",
            min_value=1.0,
            max_value=200.0,
            value=20.0,
            step=1.0,
            help="Por encima de esta posición se considera que está demasiado lejos de la primera página para ser una prioridad a corto plazo.",
        )

    st.markdown("**Ajustes manuales de negocio** (0.0 = no afectan; súbelos solo si rellenas las tablas de abajo)")
    c10, c11 = st.columns(2)
    with c10:
        w_relevancia = st.slider("Peso: relevancia manual de categoría", 0.0, 1.0, 0.0, 0.05)
    with c11:
        w_prioridad = st.slider("Peso: prioridad de negocio manual (URL)", 0.0, 1.0, 0.0, 0.05)

    st.markdown("**Categorías aisladas** (nunca se enlazan con el resto del catálogo, solo entre sí mismas)")
    grupos_aislados_texto = st.text_area(
        "Un patrón por línea (se busca como texto, sin distinguir mayúsculas, "
        "en Categoria_Principal + Categoria_Secundaria de cada URL)",
        value="Black Friday\nRebajas\nSpecial Price",
        help=(
            "Ej. si una URL tiene Categoria_Secundaria='Black Friday', solo podrá "
            "enlazar (y ser enlazada por) otras URLs cuya categoría también "
            "contenga 'Black Friday'. Nunca con el resto del catálogo ni con "
            "otro grupo aislado (p.ej. Rebajas o Special Price)."
        ),
    )
    grupos_aislados = [linea.strip() for linea in grupos_aislados_texto.splitlines() if linea.strip()]

weights = ScoringWeights(
    volumen_busqueda=w_volumen,
    pocos_productos=w_productos,
    pocos_enlaces_entrantes=w_enlaces,
    afinidad_categoria=w_afinidad,
    relevancia_categoria=w_relevancia,
    prioridad_negocio=w_prioridad,
    autoridad_origen=w_autoridad_origen,
    presupuesto_enlaces_origen=w_presupuesto_origen,
    posicion_oportunidad=w_posicion_oportunidad,
    impresiones_busqueda=w_impresiones,
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
oportunidad = OportunidadSEO(posicion_min=posicion_min, posicion_max=posicion_max)


# ---------------------------------------------------------------------------
# Ajustes manuales de negocio (opcional): relevancia por categoría y
# prioridad de negocio por URL. Editables directamente aquí, para poder
# retocar la importancia cada vez que se genere la propuesta (p.ej. cada
# mes) sin tener que tocar ficheros ni código.
# ---------------------------------------------------------------------------

st.header("3. Ajustes manuales de negocio (opcional)")
st.caption(
    "Edita aquí, cada vez que generes la propuesta, la importancia de "
    "ciertas categorías o de URLs concretas por motivos de negocio. Si lo "
    "dejas vacío no afecta al resultado (y si subes los pesos de arriba "
    "sin rellenar nada, tampoco: el valor por defecto es neutro)."
)

categorias_disponibles = None
_taxonomia_source = plantilla_file if modo_datos == "Plantilla única (recomendado)" else taxonomia_file
if _taxonomia_source is not None:
    try:
        _taxonomia_preview = load_taxonomia(_taxonomia_source)
        categorias_disponibles = (
            _taxonomia_preview[["categoria_principal", "categoria_secundaria"]]
            .drop_duplicates()
            .sort_values(["categoria_principal", "categoria_secundaria"])
            .reset_index(drop=True)
        )
    except Exception:
        categorias_disponibles = None
    finally:
        if hasattr(_taxonomia_source, "seek"):
            _taxonomia_source.seek(0)

col_rel, col_prio = st.columns(2)

with col_rel:
    with st.expander("📊 Relevancia manual por categoría / subcategoría", expanded=False):
        st.caption(
            "Valor 0–1: cuanto más alto, más prioridad para RECIBIR enlaces "
            "tiene esa categoría. Deja 'categoria_secundaria' vacía para que "
            "aplique a toda la categoría principal."
        )
        cargado_relevancia = st.file_uploader(
            "Cargar tabla guardada de un mes anterior", type=["csv", "xlsx"], key="relevancia_upload"
        )
        if cargado_relevancia is not None:
            try:
                st.session_state["relevancia_tabla"] = load_relevancia_manual(cargado_relevancia)
            except DataLoadError as exc:
                st.error(str(exc))
        if "relevancia_tabla" not in st.session_state:
            if categorias_disponibles is not None:
                st.session_state["relevancia_tabla"] = categorias_disponibles.assign(relevancia=0.5)
            else:
                st.session_state["relevancia_tabla"] = pd.DataFrame(
                    columns=["categoria_principal", "categoria_secundaria", "relevancia"]
                )

        if st.button(
            "↻ Rellenar categorías desde la taxonomía cargada",
            disabled=categorias_disponibles is None,
            key="btn_rellenar_relevancia",
        ):
            st.session_state["relevancia_tabla"] = categorias_disponibles.assign(relevancia=0.5)

        relevancia_editada = st.data_editor(
            st.session_state["relevancia_tabla"],
            num_rows="dynamic",
            width="stretch",
            key="relevancia_editor",
            column_config={
                "relevancia": st.column_config.NumberColumn(min_value=0.0, max_value=1.0, step=0.05),
            },
        )
        st.session_state["relevancia_tabla"] = relevancia_editada
        st.download_button(
            "⬇️ Descargar esta tabla (para el mes que viene)",
            data=relevancia_editada.to_csv(index=False).encode("utf-8"),
            file_name="relevancia_manual_categorias.csv",
            mime="text/csv",
            key="download_relevancia",
        )

with col_prio:
    with st.expander("🎯 Prioridad de negocio manual por URL", expanded=False):
        st.caption(
            "Añade URLs concretas que quieras penalizar o beneficiar por "
            "motivos de negocio puntuales (p.ej. de -1 a 1; 0 = neutro)."
        )
        cargado_prioridad = st.file_uploader(
            "Cargar tabla guardada de un mes anterior", type=["csv", "xlsx"], key="prioridad_upload"
        )
        if cargado_prioridad is not None:
            try:
                st.session_state["prioridad_tabla"] = load_prioridad_negocio(cargado_prioridad)
            except DataLoadError as exc:
                st.error(str(exc))
        if "prioridad_tabla" not in st.session_state:
            st.session_state["prioridad_tabla"] = pd.DataFrame(columns=["url", "prioridad_negocio"])

        prioridad_editada = st.data_editor(
            st.session_state["prioridad_tabla"],
            num_rows="dynamic",
            width="stretch",
            key="prioridad_editor",
        )
        st.session_state["prioridad_tabla"] = prioridad_editada
        st.download_button(
            "⬇️ Descargar esta tabla (para el mes que viene)",
            data=prioridad_editada.to_csv(index=False).encode("utf-8"),
            file_name="prioridad_negocio_manual.csv",
            mime="text/csv",
            key="download_prioridad",
        )


# ---------------------------------------------------------------------------
# Generación de la propuesta
# ---------------------------------------------------------------------------

st.header("4. Generar propuesta")

if "propuesta" not in st.session_state:
    st.session_state["propuesta"] = None

generar = st.button("🚀 Generar propuesta de interlinking", type="primary")

if generar:
    datasets: InputDatasets | None = None

    if modo_datos == "Plantilla única (recomendado)":
        if plantilla_file is None:
            st.error("Sube la plantilla unificada.")
        else:
            try:
                datasets = load_plantilla_unificada(plantilla_file)
            except DataLoadError as exc:
                st.error(str(exc))
    else:
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
            except DataLoadError as exc:
                st.error(str(exc))

    if datasets is not None:
        try:
            relevancia_manual = st.session_state.get("relevancia_tabla")
            prioridad_manual = st.session_state.get("prioridad_tabla")

            search_console_actual = None
            if sc_actual_file is not None:
                search_console_actual = load_search_console(sc_actual_file)

            with st.spinner("Calculando scores y generando propuesta..."):
                resultado = generate_link_proposals(
                    datasets,
                    weights,
                    affinity,
                    limites,
                    relevancia_categoria=relevancia_manual,
                    prioridad_negocio=prioridad_manual,
                    grupos_aislados=grupos_aislados,
                    search_console=search_console_actual,
                    oportunidad=oportunidad,
                )
            st.session_state["propuesta"] = resultado
            st.success(f"Propuesta generada: {len(resultado)} filas.")

            st.session_state["evolucion_sc"] = None
            if search_console_actual is not None and sc_anterior_file is not None:
                try:
                    search_console_anterior = load_search_console(sc_anterior_file)
                    st.session_state["evolucion_sc"] = comparar_evolucion_search_console(
                        search_console_actual, search_console_anterior
                    )
                except DataLoadError as exc:
                    st.warning(f"No se ha podido comparar con el mes anterior: {exc}")
        except DataLoadError as exc:
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001
            st.exception(exc)


# ---------------------------------------------------------------------------
# Resultados
# ---------------------------------------------------------------------------

resultado: pd.DataFrame | None = st.session_state.get("propuesta")

if resultado is not None and not resultado.empty:
    st.header("5. Resultado")

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
        width="stretch",
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

    evolucion: pd.DataFrame | None = st.session_state.get("evolucion_sc")
    if evolucion is not None and not evolucion.empty:
        st.subheader("📈 Evolución en Search Console vs. mes anterior")
        st.caption(
            "Comparación URL a URL entre el Search Console actual y el del mes "
            "anterior. `delta_posicion` positivo = ha mejorado (subido puestos). "
            "Útil para ver si las categorías que recibieron enlaces nuevos el mes "
            "pasado están funcionando mejor."
        )
        evolucion_vista = evolucion.sort_values("delta_clics", ascending=False)
        st.dataframe(
            evolucion_vista,
            width="stretch",
            column_config={
                "delta_clics_pct": st.column_config.NumberColumn(format="%.1f%%"),
                "delta_posicion": st.column_config.NumberColumn(format="%.1f"),
            },
        )
        st.download_button(
            "⬇️ Descargar evolución (CSV)",
            data=evolucion_vista.to_csv(index=False).encode("utf-8"),
            file_name="evolucion_search_console.csv",
            mime="text/csv",
            key="download_evolucion",
        )
elif resultado is not None:
    st.info("La propuesta generada está vacía: revisa que los datasets tengan URLs en común.")

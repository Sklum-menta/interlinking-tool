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
from core.scoring import (
    build_formato_ancho,
    comparar_evolucion_search_console,
    diagnosticar_datasets,
    generate_link_proposals,
)

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
            0.30,
            0.05,
            help="Más alto → prioriza enlazar categorías cuya keyword principal tiene más búsquedas mensuales.",
        )
    with c2:
        w_productos = st.slider(
            "Peso: muchos productos",
            0.0,
            1.0,
            0.10,
            0.05,
            help="Más alto → prioriza categorías con MÁS productos (más catálogo, más recorrido de venta). Decisión de negocio del 30 sept: antes era al revés (se priorizaban las de pocos productos); ahora interesa reforzar con enlaces internos a las categorías con más catálogo.",
        )
    with c3:
        w_enlaces = st.slider(
            "Peso: pocos enlaces entrantes",
            0.0,
            1.0,
            0.20,
            0.05,
            help="Más alto → prioriza categorías que hoy reciben pocos enlaces internos y necesitan un empujón (reparte mejor el 'link juice').",
        )
    with c4:
        w_afinidad = st.slider(
            "Peso: afinidad de categoría",
            0.0,
            1.0,
            0.15,
            0.05,
            help="Más alto → prioriza enlazar entre categorías relacionadas (misma categoría principal/secundaria) frente a categorías sin relación.",
        )

    st.caption(
        "Todos los pesos de esta sección (incluidos los de 'Página origen' y "
        "'Search Console' más abajo) se normalizan automáticamente en conjunto "
        "para que sumen 100%, así que puedes moverlos libremente sin hacer cuentas."
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

    max_enlaces_destino = st.number_input(
        "Máx. enlaces nuevos que puede RECIBIR una misma categoría destino",
        min_value=1,
        max_value=50,
        value=8,
        help=(
            "Evita que unas pocas categorías 'ganadoras' (mucho volumen, muchos "
            "productos, pocos enlaces entrantes de partida...) se lleven la "
            "mayoría de los enlaces nuevos mientras el resto del catálogo se "
            "queda sin ninguno. Con este límite, cuando una categoría destino "
            "ya ha recibido este nº de enlaces nuevos en la propuesta, deja de "
            "proponerse como destino y el siguiente mejor candidato de cada "
            "origen ocupa su lugar — así los enlaces nuevos se reparten por "
            "más categorías en vez de repetirse siempre en las mismas."
        ),
    )

    st.markdown(
        "**Página origen** (miran a la categoría que enlaza, no a la que recibe el enlace). "
        "'Autoridad del origen' viene activada por defecto — decisión de negocio del 30 sept: "
        "que sean las categorías ya bien posicionadas/con más autoridad interna las que enlacen "
        "hacia las que necesitan el empujón, y no al revés. 'Presupuesto de enlaces del origen' "
        "sigue en 0.0 (opcional) hasta que se active explícitamente."
    )
    c12, c13 = st.columns(2)
    with c12:
        w_autoridad_origen = st.slider(
            "Peso: autoridad del origen",
            0.0,
            1.0,
            0.15,
            0.05,
            help="Más alto → prioriza enlazar desde categorías que ya reciben muchos enlaces internos propios (transmiten más valor al enlazar hacia categorías con oportunidad).",
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
        "**Search Console — oportunidad SEO** (requieren subir el dataset de Search "
        "Console más arriba; si no se sube, estos pesos no tienen ningún efecto aunque "
        "estén por encima de 0). 'Posición en zona de oportunidad' viene activada por "
        "defecto — decisión de negocio del 30 sept: enlazar hacia las categorías con "
        "volumen alto de búsquedas que todavía no acaban de posicionar, para darles el "
        "empujón que necesitan."
    )
    c14, c15 = st.columns(2)
    with c14:
        w_posicion_oportunidad = st.slider(
            "Peso: posición en zona de oportunidad",
            0.0,
            1.0,
            0.10,
            0.05,
            help="Más alto → prioriza categorías destino cuya posición media en Google está dentro del rango configurado abajo (candidatas a subir a primera página con un empujón de enlaces internos).",
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

    st.markdown(
        "**Categorías aisladas adicionales** — Black Friday, Rebajas, Special Price "
        "y Navidad NUNCA se enlazan con el resto del catálogo ni entre sí (solo dentro "
        "de su propio grupo); esta regla de negocio va siempre activa y no se puede "
        "desactivar desde aquí. Este cuadro es solo para añadir OTROS grupos aislados "
        "extra, si hiciera falta."
    )
    grupos_aislados_texto = st.text_area(
        "Un patrón adicional por línea (opcional; se busca como texto, sin distinguir "
        "mayúsculas, en Categoria_Principal + Categoria_Secundaria de cada URL). "
        "Black Friday/Rebajas/Special Price/Navidad no hace falta escribirlos: ya están "
        "siempre aislados.",
        value="",
        help=(
            "Ej. si quieres aislar también, por ejemplo, 'Outlet', escribe 'Outlet' aquí "
            "y esas URLs solo se enlazarán entre sí. Los 4 grupos obligatorios "
            "(Black Friday, Rebajas, Special Price, Navidad) siguen aislados aunque "
            "dejes este cuadro vacío."
        ),
    )
    grupos_aislados = [linea.strip() for linea in grupos_aislados_texto.splitlines() if linea.strip()]

weights = ScoringWeights(
    volumen_busqueda=w_volumen,
    muchos_productos=w_productos,
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
    max_enlaces_nuevos_por_destino=int(max_enlaces_destino),
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
                # El widget de abajo (key="relevancia_editor") guarda en
                # session_state su propio diff de ediciones (filas
                # editadas/añadidas/borradas) y lo reaplica POR POSICIÓN
                # sobre lo que le pasemos como tabla base en el próximo
                # render. Si aquí cambiamos la tabla base (nueva subida)
                # sin borrar ese diff viejo, Streamlit mezcla ediciones de
                # la tabla anterior con las filas de la tabla nueva —
                # combinaciones categoria_principal/categoria_secundaria
                # que no existen, y filas que desaparecen. Hay que
                # limpiar el estado del widget para que arranque de cero.
                st.session_state.pop("relevancia_editor", None)
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
            # Mismo motivo que arriba: esta tabla también reemplaza la
            # base por completo, así que hay que descartar el diff viejo
            # del editor para que no se reaplique sobre filas que ya no
            # se corresponden con las mismas categorías.
            st.session_state.pop("relevancia_editor", None)

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
            data=relevancia_editada.to_csv(index=False).encode("utf-8-sig"),
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
                # Mismo motivo que en la tabla de relevancia: al reemplazar
                # la tabla base hay que descartar el diff de ediciones que
                # el widget guarda bajo su propia key, o Streamlit lo
                # reaplica por posición sobre filas que ya no son las mismas.
                st.session_state.pop("prioridad_editor", None)
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
            data=prioridad_editada.to_csv(index=False).encode("utf-8-sig"),
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

            contador_generacion: dict = {}
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
                    contador=contador_generacion,
                )
            st.session_state["propuesta"] = resultado
            st.success(f"Propuesta generada: {len(resultado)} filas.")

            st.session_state["ultimo_diagnostico"] = None
            st.session_state["ultimo_embudo"] = None
            if resultado.empty:
                st.session_state["ultimo_embudo"] = contador_generacion
                try:
                    st.session_state["ultimo_diagnostico"] = diagnosticar_datasets(
                        datasets,
                        relevancia_categoria=relevancia_manual,
                        prioridad_negocio=prioridad_manual,
                        grupos_aislados=grupos_aislados,
                        search_console=search_console_actual,
                    )
                except Exception:  # noqa: BLE001 - el diagnóstico nunca debe tapar el resultado real
                    st.session_state["ultimo_diagnostico"] = None

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

    st.caption(
        "Esta tabla ya es la propuesta final: solo los enlaces seleccionados "
        "(top de cada categoría origen) más las filas pendientes de revisar "
        "por falta de datos. Los candidatos válidos que no entraron en el "
        "límite por origen no se guardan (con catálogos grandes serían "
        "millones de filas), así que no hace falta filtrarlos aquí."
    )

    fc1, fc2 = st.columns(2)
    with fc1:
        categorias_principales = sorted(
            resultado["categoria_principal_destino"].dropna().astype(str).unique().tolist()
        )
        filtro_categoria = st.multiselect("Filtrar por categoría principal (destino)", categorias_principales)
    with fc2:
        filtro_score_min = st.slider("Score mínimo a mostrar", 0.0, 1.0, 0.0, 0.05)

    vista = resultado.copy()
    if filtro_categoria:
        vista = vista[vista["categoria_principal_destino"].astype(str).isin(filtro_categoria)]
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
    st.caption(
        "👉 Para compartir con el equipo o importar en Sheets, usa la de la "
        "derecha (**formato id + enlaces**): una fila por categoría origen, "
        "con categoría/subcategoría/score y una justificación de cada "
        "enlace propuesto. Las otras dos son la tabla técnica completa "
        "(un candidato por fila), útiles para depurar el scoring."
    )
    dcol1, dcol2, dcol3 = st.columns(3)
    with dcol1:
        st.download_button(
            "⬇️ Descargar CSV (técnico)",
            data=resultado.to_csv(index=False).encode("utf-8-sig"),
            file_name="propuesta_interlinking.csv",
            mime="text/csv",
            help="Una fila por par origen-destino seleccionado o pendiente de revisar, con todas las señales de scoring.",
        )
    with dcol2:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            resultado.to_excel(writer, index=False, sheet_name="Propuesta")
        st.download_button(
            "⬇️ Descargar Excel (técnico)",
            data=buffer.getvalue(),
            file_name="propuesta_interlinking.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    with dcol3:
        formato_ancho = build_formato_ancho(resultado)
        st.download_button(
            "⬇️ Descargar propuesta final (recomendado)",
            data=formato_ancho.to_csv(index=False).encode("utf-8-sig"),
            file_name="propuesta_interlinking_formato_ancho.csv",
            mime="text/csv",
            disabled=formato_ancho.empty,
            help=(
                "Una fila por URL origen, con su id, categoría/subcategoría "
                "y los enlaces ya seleccionados: linked_id_1/linked_url_1/"
                "linked_category_1/linked_subcategory_1/linked_score_1/"
                "justificacion_1, linked_id_2/... (mismo formato que el "
                "flujo anterior de Sheets, con la justificación añadida)."
            ),
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
            data=evolucion_vista.to_csv(index=False).encode("utf-8-sig"),
            file_name="evolucion_search_console.csv",
            mime="text/csv",
            key="download_evolucion",
        )
elif resultado is not None:
    st.info("La propuesta generada está vacía: revisa que los datasets tengan URLs en común.")

    diagnostico = st.session_state.get("ultimo_diagnostico")
    if diagnostico:
        st.subheader("🔍 Por qué ha salido vacía")
        motivo = diagnostico.get("motivo_probable")
        if motivo:
            st.warning(f"**Causa más probable:** {motivo}")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("URLs en el crawl", diagnostico.get("n_crawl", 0))
        c2.metric("... con volumen", diagnostico.get("urls_crawl_con_volumen", 0))
        c3.metric("... con taxonomía", diagnostico.get("urls_crawl_con_taxonomia", 0))
        c4.metric("Enlaces existentes leídos", diagnostico.get("n_enlaces", 0))

        if "n_destino_saludable" in diagnostico:
            c5, c6 = st.columns(2)
            c5.metric("Destinos 'saludables'", diagnostico.get("n_destino_saludable", 0))
            c6.metric("Categorías principales distintas", diagnostico.get("n_categorias_principales_distintas", 0))

        with st.expander("Ver detalle completo del diagnóstico"):
            st.json(diagnostico)

    embudo = st.session_state.get("ultimo_embudo")
    if embudo:
        st.subheader("🔻 En qué paso se han perdido los enlaces")
        st.caption(
            "Cada número es cuántos pares origen→destino sobrevivían justo "
            "después de aplicar ese filtro. En cuanto un número baja a 0, ese "
            "es el filtro que ha vaciado la propuesta."
        )

        pasos = [
            ("Pares candidatos (antes de filtrar)", "pares_antes_de_filtros"),
            ("... tras 'categorías aisladas'", "pares_tras_grupo_aislado"),
            ("... tras excluir destinos no saludables", "pares_tras_salud_destino"),
            ("... tras excluir enlaces ya existentes", "pares_tras_excluir_enlaces_existentes"),
        ]
        cols = st.columns(len(pasos))
        for col, (etiqueta, clave) in zip(cols, pasos):
            col.metric(etiqueta, embudo.get(clave, 0))

        c1, c2, c3 = st.columns(3)
        c1.metric("Pares con score calculado", embudo.get("pares_validos_con_score", 0))
        c2.metric("Pendientes de confirmar", embudo.get("pares_pendientes_confirmar", 0))
        c3.metric("Seleccionados (score ≥ mínimo)", embudo.get("pares_seleccionados", 0))

        score_max = embudo.get("score_valido_maximo")
        score_minimo_usado = embudo.get("score_minimo_usado")

        if embudo.get("pares_antes_de_filtros", 0) == 0:
            st.error(
                "No se ha generado ni un solo par candidato: el catálogo tiene "
                "menos de 2 URLs útiles tras la limpieza."
            )
        elif embudo.get("pares_tras_grupo_aislado", 0) == 0:
            st.error(
                "**Aquí está el bloqueo:** el filtro de 'categorías aisladas' "
                "(sección 2, Black Friday/Rebajas/Special Price/Navidad o los patrones "
                "que tengas configurados ahí) ha descartado TODOS los pares. "
                "Revisa esa lista de patrones: seguramente coincide con texto "
                "que aparece en todas (o casi todas) las categorías del "
                "catálogo real, aislando cada una por su cuenta."
            )
        elif embudo.get("pares_tras_salud_destino", 0) == 0:
            st.error(
                "**Aquí está el bloqueo:** todas las URLs han quedado marcadas "
                "como destino 'no saludable' (columnas Status_Code/Indexable "
                "del fichero). Es muy probable que se esté leyendo una columna "
                "equivocada como si fuera 'Indexable' o 'Status_Code' — revisa "
                "esas dos columnas en tu fichero."
            )
        elif embudo.get("pares_tras_excluir_enlaces_existentes", 0) == 0:
            st.error(
                "**Aquí está el bloqueo:** todos los pares candidatos ya tenían "
                "un enlace existente entre sí, según el dataset de enlaces "
                "leído del fichero. Revisa cómo se están interpretando las "
                "columnas de enlaces existentes (bolitas/breadcrumb/texto)."
            )
        elif (
            embudo.get("pares_validos_con_score", 0) > 0
            and embudo.get("pares_seleccionados", 0) == 0
            and score_max is not None
            and score_minimo_usado is not None
            and score_max < score_minimo_usado
        ):
            st.error(
                f"**Aquí está el bloqueo:** el 'Score mínimo para proponer un "
                f"enlace' está configurado en **{score_minimo_usado:.2f}**, pero "
                f"el score más alto que se ha conseguido calcular con estos "
                f"datos es **{score_max:.2f}**. Baja ese slider en la sección 2 "
                f"(configuración del scoring) — con 0.0 nunca descarta nada por "
                f"score."
            )
        elif embudo.get("pares_seleccionados", 0) == 0 and embudo.get("pares_pendientes_confirmar", 0) == 0:
            st.warning(
                "Ninguna fila ha quedado seleccionada, aunque hay pares con "
                "score calculado. Prueba a bajar el 'Score mínimo para "
                "proponer un enlace' en la sección 2."
            )

        with st.expander("Ver todos los números del embudo"):
            st.json(embudo)

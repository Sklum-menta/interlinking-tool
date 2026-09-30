import pandas as pd
import pytest

from core.config import AffinityScores, LimitesPropuesta, OportunidadSEO, ScoringWeights
from core.data_loader import InputDatasets
from core.scoring import (
    affinity_score,
    build_formato_ancho,
    build_master_table,
    comparar_evolucion_search_console,
    diagnosticar_datasets,
    extraer_id_de_url,
    generate_link_proposals,
    oportunidad_posicion_score,
)


def _make_datasets() -> InputDatasets:
    crawl = pd.DataFrame(
        {
            "url": ["a", "b", "c", "d"],
            "num_productos": [100, 5, 50, 3],
        }
    )
    volumen = pd.DataFrame(
        {
            "url": ["a", "b", "c"],  # "d" no tiene volumen -> pendiente
            "keyword": ["kw_a", "kw_b", "kw_c"],
            "volumen": [1000, 8000, 200],
        }
    )
    taxonomia = pd.DataFrame(
        {
            "url": ["a", "b", "c", "d"],
            "categoria_principal": ["Muebles", "Muebles", "Iluminacion", "Muebles"],
            "categoria_secundaria": ["Salon", "Salon", "Techo", "Dormitorio"],
        }
    )
    enlaces = pd.DataFrame(
        {
            "source_url": ["a"],
            "destination_url": ["c"],
            "anchor_text": ["algo"],
            "zona": ["Content"],
        }
    )
    return InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)


def test_affinity_score_prioriza_misma_categoria():
    affinity = AffinityScores(misma_principal_y_secundaria=1.0, misma_principal=0.6, distinta=0.15)

    assert affinity_score("Muebles", "Salon", "Muebles", "Salon", affinity) == 1.0
    assert affinity_score("Muebles", "Salon", "Muebles", "Dormitorio", affinity) == 0.6
    assert affinity_score("Muebles", "Salon", "Iluminacion", "Techo", affinity) == 0.15


def test_affinity_score_devuelve_none_si_falta_categoria():
    affinity = AffinityScores()
    assert affinity_score(None, None, "Muebles", "Salon", affinity) is None
    assert affinity_score("", "", "Muebles", "Salon", affinity) is None


def test_build_master_table_marca_datos_faltantes():
    datasets = _make_datasets()
    master = build_master_table(datasets)

    fila_d = master.set_index("url").loc["d"]
    assert fila_d["falta_volumen"] is True or fila_d["falta_volumen"] == True  # noqa: E712
    assert fila_d["falta_taxonomia"] == False  # noqa: E712

    # "c" recibe un enlace entrante desde "a"
    fila_c = master.set_index("url").loc["c"]
    assert fila_c["enlaces_entrantes_actuales"] == 1


def test_generate_link_proposals_marca_pendientes_por_falta_de_volumen():
    datasets = _make_datasets()
    resultado = generate_link_proposals(datasets)

    hacia_d = resultado[resultado["categoria_destino"] == "d"]
    assert not hacia_d.empty
    assert hacia_d["pendiente_confirmar"].all()
    assert hacia_d["score"].isna().all()
    # Una fila pendiente nunca se marca como seleccionada automáticamente.
    assert not hacia_d["seleccionada"].any()


def test_generate_link_proposals_prioriza_mayor_volumen_y_afinidad():
    datasets = _make_datasets()
    weights = ScoringWeights(
        volumen_busqueda=1.0, muchos_productos=0.0, pocos_enlaces_entrantes=0.0, afinidad_categoria=0.0
    )
    resultado = generate_link_proposals(datasets, weights=weights)

    desde_c = resultado[
        (resultado["categoria_origen"] == "c") & (~resultado["pendiente_confirmar"])
    ].sort_values("score", ascending=False)

    # Con peso 100% en volumen, "b" (volumen 8000) debe ir antes que "a" (volumen 1000)
    orden_destinos = desde_c["categoria_destino"].tolist()
    assert orden_destinos.index("b") < orden_destinos.index("a")


def test_muchos_productos_prioriza_categorias_destino_con_mas_productos():
    """Decisión de negocio del 30 sept: cuantos MÁS productos tenga la
    categoría destino, más prioridad (antes de esa fecha era al revés:
    se priorizaban las categorías con pocos productos). Con peso 100%
    en este criterio, "a" (100 productos) debe ir antes que "b" (5
    productos), aunque "b" tenga más volumen de búsqueda.
    """
    datasets = _make_datasets()
    weights = ScoringWeights(
        volumen_busqueda=0.0, muchos_productos=1.0, pocos_enlaces_entrantes=0.0, afinidad_categoria=0.0
    )
    resultado = generate_link_proposals(datasets, weights=weights)

    desde_c = resultado[
        (resultado["categoria_origen"] == "c") & (~resultado["pendiente_confirmar"])
    ].sort_values("score", ascending=False)

    orden_destinos = desde_c["categoria_destino"].tolist()
    assert orden_destinos.index("a") < orden_destinos.index("b")


def test_pesos_por_defecto_activan_autoridad_origen_y_posicion_oportunidad():
    """Decisión de negocio del 30 sept: por defecto (sin tocar nada en la
    interfaz) ya se prioriza enlazar DESDE categorías con más autoridad
    interna HACIA categorías con volumen alto que están en zona de
    oportunidad de posición. Antes de esa fecha ambos pesos eran 0 por
    defecto (solo se activaban a mano). Este test es un candado para que
    nadie los vuelva a poner a 0 sin querer en un cambio futuro.
    """
    pesos = ScoringWeights()
    assert pesos.autoridad_origen > 0
    assert pesos.posicion_oportunidad > 0
    assert pesos.muchos_productos > 0
    # Siguen sumando 1 de partida (no es obligatorio, `normalizados()` lo
    # arregla igualmente, pero así los sliders de la interfaz arrancan
    # ya en 100% sin que el usuario tenga que hacer cuentas).
    total = (
        pesos.volumen_busqueda
        + pesos.muchos_productos
        + pesos.pocos_enlaces_entrantes
        + pesos.afinidad_categoria
        + pesos.relevancia_categoria
        + pesos.prioridad_negocio
        + pesos.autoridad_origen
        + pesos.presupuesto_enlaces_origen
        + pesos.posicion_oportunidad
        + pesos.impresiones_busqueda
    )
    assert total == pytest.approx(1.0)


def test_generate_link_proposals_respeta_limite_por_origen():
    datasets = _make_datasets()
    limites = LimitesPropuesta(max_enlaces_nuevos_por_origen=1, score_minimo=0.0)
    resultado = generate_link_proposals(datasets, limites=limites)

    for origen, grupo in resultado.groupby("categoria_origen"):
        assert grupo["seleccionada"].sum() <= 1


def _make_datasets_muchos_origenes_un_destino_popular() -> InputDatasets:
    """8 categorías origen (o1..o8) que, sin límite de destino, elegirían
    TODAS a "popular" como mejor candidata frente a "alternativa": tiene
    10 veces más volumen de búsqueda (5000 vs 500), lo que domina el
    score con los pesos por defecto aunque tenga menos productos que
    "alternativa" (el nº de productos pesa mucho menos que el volumen).
    Los orígenes o1..o8 no tienen volumen propio, así que nunca son
    elegibles como destino entre ellos (quedan "pendiente_confirmar" si
    se probasen como destino) y la única competencia real es entre
    "popular" y "alternativa".
    """
    origenes = [f"o{i}" for i in range(1, 9)]
    urls = origenes + ["popular", "alternativa"]
    crawl = pd.DataFrame({"url": urls, "num_productos": [50] * len(origenes) + [5, 10]})
    volumen = pd.DataFrame(
        {
            "url": ["popular", "alternativa"],
            "keyword": ["kw_popular", "kw_alternativa"],
            "volumen": [5000, 500],
        }
    )
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": ["Muebles"] * len(urls),
            "categoria_secundaria": ["Salon"] * len(urls),
        }
    )
    enlaces = pd.DataFrame(columns=["source_url", "destination_url", "anchor_text", "zona"])
    return InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)


def test_presupuesto_por_destino_reparte_enlaces_en_vez_de_concentrarlos():
    """Regla de calidad confirmada por el usuario el 30 sept (comparando
    con el script anterior, que limitaba enlaces entrantes nuevos por
    categoría con su columna "En. Obj."): ninguna categoría destino debe
    poder acumular enlaces nuevos sin límite solo por tener mejor pinta a
    priori. Con `max_enlaces_nuevos_por_destino=3`, "popular" (la mejor
    candidata para las 8 categorías origen) no puede recibir más de 3
    enlaces nuevos, aunque las 8 la hubiesen elegido sin ese tope.
    """
    datasets = _make_datasets_muchos_origenes_un_destino_popular()
    limites = LimitesPropuesta(
        max_enlaces_nuevos_por_origen=1, max_enlaces_nuevos_por_destino=3, score_minimo=0.0
    )
    resultado = generate_link_proposals(datasets, limites=limites)
    seleccionadas = resultado[resultado["seleccionada"]]

    conteo_por_destino = seleccionadas["categoria_destino"].value_counts()
    assert conteo_por_destino.get("popular", 0) <= 3
    assert (conteo_por_destino <= 3).all()
    # El resto de orígenes que no cupieron en "popular" deben repartirse
    # hacia "alternativa" en vez de perderse todos silenciosamente.
    assert "alternativa" in conteo_por_destino.index


def test_presupuesto_por_destino_no_afecta_si_hay_hueco_de_sobra():
    """Con un presupuesto por destino holgado (por encima del nº de
    orígenes candidatos), el comportamiento es el mismo de siempre: la
    categoría con mejor score se lleva todos los enlaces que le
    correspondan sin que el nuevo límite le quite ninguno.
    """
    datasets = _make_datasets_muchos_origenes_un_destino_popular()
    limites = LimitesPropuesta(
        max_enlaces_nuevos_por_origen=1, max_enlaces_nuevos_por_destino=100, score_minimo=0.0
    )
    resultado = generate_link_proposals(datasets, limites=limites)
    origenes_o = [f"o{i}" for i in range(1, 9)]
    seleccionadas_desde_o = resultado[
        resultado["seleccionada"] & resultado["categoria_origen"].isin(origenes_o)
    ]

    # Las 8 categorías o1..o8 eligen todas "popular" sin que el nuevo
    # límite (holgado aquí) les quite ninguna.
    assert (seleccionadas_desde_o["categoria_destino"] == "popular").sum() == 8


def test_relevancia_manual_sin_datos_no_cambia_el_orden():
    """Si no se rellena la tabla de relevancia manual, su valor por
    defecto (0.5 para todas las categorías) no debe alterar el orden
    de la propuesta, aunque su peso sea > 0.
    """
    datasets = _make_datasets()
    weights = ScoringWeights(
        volumen_busqueda=1.0,
        muchos_productos=0.0,
        pocos_enlaces_entrantes=0.0,
        afinidad_categoria=0.0,
        relevancia_categoria=0.5,
        prioridad_negocio=0.0,
    )
    resultado = generate_link_proposals(datasets, weights=weights)

    desde_c = resultado[
        (resultado["categoria_origen"] == "c") & (~resultado["pendiente_confirmar"])
    ].sort_values("score", ascending=False)
    orden_destinos = desde_c["categoria_destino"].tolist()
    assert orden_destinos.index("b") < orden_destinos.index("a")


def test_prioridad_negocio_manual_por_url_reordena_la_propuesta():
    """Con peso 100% en prioridad de negocio manual, la URL a la que se
    le haya asignado más prioridad debe ir primero, aunque tenga menos
    volumen de búsqueda.
    """
    datasets = _make_datasets()
    # "a" tiene menos volumen que "b" (1000 vs 8000), pero le damos a "a"
    # una prioridad de negocio manual mucho mayor mediante el override por URL.
    prioridad = pd.DataFrame({"url": ["a", "b"], "prioridad_negocio": [1.0, 0.0]})

    weights = ScoringWeights(
        volumen_busqueda=0.0,
        muchos_productos=0.0,
        pocos_enlaces_entrantes=0.0,
        afinidad_categoria=0.0,
        relevancia_categoria=0.0,
        prioridad_negocio=1.0,
    )
    resultado = generate_link_proposals(datasets, weights=weights, prioridad_negocio=prioridad)

    desde_c = resultado[
        (resultado["categoria_origen"] == "c") & (~resultado["pendiente_confirmar"])
    ].sort_values("score", ascending=False)
    orden_destinos = desde_c["categoria_destino"].tolist()
    assert orden_destinos.index("a") < orden_destinos.index("b")


def _make_datasets_con_grupos_aislados() -> InputDatasets:
    # bf1/bf2: Black Friday. reb1/reb2: Rebajas. sp1/sp2: Special Price.
    # nav1/nav2: Navidad. n1/n2: categorías normales.
    urls = ["bf1", "bf2", "reb1", "reb2", "sp1", "sp2", "nav1", "nav2", "n1", "n2"]
    crawl = pd.DataFrame({"url": urls, "num_productos": [10] * len(urls)})
    volumen = pd.DataFrame(
        {
            "url": urls,
            "keyword": [f"kw_{u}" for u in urls],
            "volumen": [100] * len(urls),
        }
    )
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": [
                "Precios especiales",
                "Precios especiales",
                "Precios especiales",
                "Precios especiales",
                "Sofas",
                "Sillas",
                "Decoración",
                "Decoración",
                "Muebles",
                "Muebles",
            ],
            "categoria_secundaria": [
                "Black Friday",
                "Black Friday",
                "Rebajas",
                "Rebajas",
                "Special Price",
                "Special Price",
                "Navidad",
                "Navidad",
                "Salon",
                "Salon",
            ],
        }
    )
    enlaces = pd.DataFrame(columns=["source_url", "destination_url", "anchor_text", "zona"])
    return InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)


def test_grupos_aislados_black_friday_solo_enlaza_con_black_friday():
    datasets = _make_datasets_con_grupos_aislados()
    resultado = generate_link_proposals(datasets)

    desde_bf1 = resultado[resultado["categoria_origen"] == "bf1"]
    destinos = set(desde_bf1["categoria_destino"])
    assert destinos == {"bf2"}


def test_grupos_aislados_rebajas_solo_enlaza_con_rebajas():
    datasets = _make_datasets_con_grupos_aislados()
    resultado = generate_link_proposals(datasets)

    desde_reb1 = resultado[resultado["categoria_origen"] == "reb1"]
    destinos = set(desde_reb1["categoria_destino"])
    assert destinos == {"reb2"}


def test_grupos_aislados_black_friday_y_rebajas_no_se_mezclan():
    datasets = _make_datasets_con_grupos_aislados()
    resultado = generate_link_proposals(datasets)

    pares = set(zip(resultado["categoria_origen"], resultado["categoria_destino"]))
    assert ("bf1", "reb1") not in pares
    assert ("reb1", "bf1") not in pares


def test_grupos_aislados_no_afecta_a_categorias_normales():
    datasets = _make_datasets_con_grupos_aislados()
    resultado = generate_link_proposals(datasets)

    desde_n1 = resultado[resultado["categoria_origen"] == "n1"]
    destinos = set(desde_n1["categoria_destino"])
    # n1 no debe poder enlazar a bf1/bf2/reb1/reb2/sp1/sp2, solo a n2.
    assert destinos == {"n2"}


def test_grupos_aislados_special_price_solo_enlaza_con_special_price():
    """Special Price (descubierta en la taxonomía real de Sklum) se aísla
    igual que Black Friday y Rebajas, por decisión explícita del usuario.
    """
    datasets = _make_datasets_con_grupos_aislados()
    resultado = generate_link_proposals(datasets)

    desde_sp1 = resultado[resultado["categoria_origen"] == "sp1"]
    destinos = set(desde_sp1["categoria_destino"])
    assert destinos == {"sp2"}


def test_grupos_aislados_special_price_no_se_mezcla_con_black_friday_ni_rebajas():
    datasets = _make_datasets_con_grupos_aislados()
    resultado = generate_link_proposals(datasets)

    pares = set(zip(resultado["categoria_origen"], resultado["categoria_destino"]))
    assert ("sp1", "bf1") not in pares
    assert ("bf1", "sp1") not in pares


def test_grupos_aislados_navidad_solo_enlaza_con_navidad():
    """Navidad (confirmada por el usuario el 30 sept, encontrada también
    en la taxonomía real de Sklum con 13 URLs) se aísla igual que Black
    Friday, Rebajas y Special Price: solo se enlaza consigo misma."""
    datasets = _make_datasets_con_grupos_aislados()
    resultado = generate_link_proposals(datasets)

    desde_nav1 = resultado[resultado["categoria_origen"] == "nav1"]
    destinos = set(desde_nav1["categoria_destino"])
    assert destinos == {"nav2"}


def test_grupos_aislados_navidad_no_se_mezcla_con_otros_grupos_ni_normales():
    datasets = _make_datasets_con_grupos_aislados()
    resultado = generate_link_proposals(datasets)

    pares = set(zip(resultado["categoria_origen"], resultado["categoria_destino"]))
    assert ("nav1", "bf1") not in pares
    assert ("nav1", "sp1") not in pares
    assert ("nav1", "n1") not in pares
    assert ("n1", "nav1") not in pares
    assert ("sp1", "reb1") not in pares
    assert ("reb1", "sp1") not in pares


def test_grupos_aislados_obligatorios_no_se_pueden_desactivar_pasando_lista_vacia():
    """Regla de negocio confirmada por el usuario el 30 sept: Black Friday,
    Rebajas, Special Price y Navidad NUNCA se mezclan entre sí, y esto no
    puede depender de que alguien borre o deje vacío el cuadro de
    'categorías aisladas' de la interfaz (o llame a la función pasando
    `grupos_aislados=[]` directamente). Aunque se pase una lista vacía, los
    4 grupos obligatorios se siguen aplicando igual que si no se pasara
    nada (`grupos_aislados=None`).
    """
    datasets = _make_datasets_con_grupos_aislados()
    resultado = generate_link_proposals(datasets, grupos_aislados=[])

    pares = set(zip(resultado["categoria_origen"], resultado["categoria_destino"]))
    # Ningún cruce entre grupos aislados distintos, ni con categorías normales.
    assert ("sp1", "bf1") not in pares
    assert ("bf1", "sp1") not in pares
    assert ("sp1", "reb1") not in pares
    assert ("bf1", "reb1") not in pares
    assert ("nav1", "bf1") not in pares
    assert ("nav1", "n1") not in pares
    # Y cada grupo se sigue enlazando consigo mismo con normalidad.
    assert ("bf1", "bf2") in pares or ("bf2", "bf1") in pares
    assert ("sp1", "sp2") in pares or ("sp2", "sp1") in pares


def test_grupos_aislados_parametro_solo_anade_grupos_extra_nunca_quita_los_obligatorios():
    """Pasar `grupos_aislados` con patrones custom (p.ej. desde el cuadro de
    texto de 'categorías aisladas adicionales' en la interfaz) añade esos
    grupos por encima de los 4 obligatorios, pero nunca los sustituye ni
    los desactiva.
    """
    datasets = _make_datasets_con_grupos_aislados()
    resultado = generate_link_proposals(datasets, grupos_aislados=["salon"])

    pares = set(zip(resultado["categoria_origen"], resultado["categoria_destino"]))
    # El grupo extra ("Salon") se aísla...
    assert ("n1", "n2") in pares or ("n2", "n1") in pares  # n1/n2 son ambas "Salon"
    # ...y los 4 obligatorios se mantienen intactos.
    assert ("sp1", "bf1") not in pares
    assert ("bf1", "sp1") not in pares


# ---------------------------------------------------------------------------
# Señales nuevas: autoridad de origen, presupuesto de enlaces salientes,
# salud técnica del destino y Search Console (oportunidad SEO).
# ---------------------------------------------------------------------------


def _make_datasets_senales_origen() -> InputDatasets:
    """o1 tiene mucha autoridad (recibe enlaces de x1/x2/x3) pero también
    ya tiene mucho presupuesto de enlaces salientes gastado (enlaza a
    x1/x2/x3). o2 no tiene ninguna de las dos cosas. d1 es un destino
    neutro al que ambos pueden enlazar.
    """
    urls = ["o1", "o2", "d1", "x1", "x2", "x3"]
    crawl = pd.DataFrame({"url": urls, "num_productos": [10] * len(urls)})
    volumen = pd.DataFrame(
        {"url": urls, "keyword": [f"kw_{u}" for u in urls], "volumen": [100] * len(urls)}
    )
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": ["Muebles"] * len(urls),
            "categoria_secundaria": [""] * len(urls),
        }
    )
    enlaces = pd.DataFrame(
        {
            "source_url": ["x1", "x2", "x3", "o1", "o1", "o1"],
            "destination_url": ["o1", "o1", "o1", "x1", "x2", "x3"],
            "anchor_text": [""] * 6,
            "zona": ["Content"] * 6,
        }
    )
    return InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)


def test_autoridad_origen_prioriza_categorias_origen_con_mas_enlaces_entrantes():
    datasets = _make_datasets_senales_origen()
    weights = ScoringWeights(
        volumen_busqueda=0.0,
        muchos_productos=0.0,
        pocos_enlaces_entrantes=0.0,
        afinidad_categoria=0.0,
        autoridad_origen=1.0,
    )
    resultado = generate_link_proposals(datasets, weights=weights)

    hacia_d1 = resultado[
        (resultado["categoria_destino"] == "d1") & (~resultado["pendiente_confirmar"])
    ].set_index("categoria_origen")
    # o1 recibe 3 enlaces entrantes (de x1/x2/x3), o2 no recibe ninguno.
    assert hacia_d1.loc["o1", "score"] > hacia_d1.loc["o2", "score"]


def test_presupuesto_enlaces_origen_prioriza_categorias_con_menos_salientes():
    datasets = _make_datasets_senales_origen()
    weights = ScoringWeights(
        volumen_busqueda=0.0,
        muchos_productos=0.0,
        pocos_enlaces_entrantes=0.0,
        afinidad_categoria=0.0,
        presupuesto_enlaces_origen=1.0,
    )
    resultado = generate_link_proposals(datasets, weights=weights)

    hacia_d1 = resultado[
        (resultado["categoria_destino"] == "d1") & (~resultado["pendiente_confirmar"])
    ].set_index("categoria_origen")
    # o1 ya tiene 3 enlaces salientes (a x1/x2/x3), o2 no tiene ninguno:
    # o2 debe tener más "presupuesto" disponible y por tanto más score.
    assert hacia_d1.loc["o2", "score"] > hacia_d1.loc["o1", "score"]


def _make_datasets_salud_tecnica() -> InputDatasets:
    urls = ["origen", "d_ok", "d_roto", "d_noindex"]
    crawl = pd.DataFrame(
        {
            "url": urls,
            "num_productos": [10, 10, 10, 10],
            "status_code": [200, 200, 404, 200],
            "indexable": [True, True, True, False],
        }
    )
    volumen = pd.DataFrame(
        {"url": urls, "keyword": [f"kw_{u}" for u in urls], "volumen": [100] * len(urls)}
    )
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": ["Muebles"] * len(urls),
            "categoria_secundaria": [""] * len(urls),
        }
    )
    enlaces = pd.DataFrame(columns=["source_url", "destination_url", "anchor_text", "zona"])
    return InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)


def test_salud_tecnica_excluye_destinos_caidos_o_no_indexables():
    datasets = _make_datasets_salud_tecnica()
    resultado = generate_link_proposals(datasets)

    destinos = set(resultado["categoria_destino"])
    assert "d_roto" not in destinos
    assert "d_noindex" not in destinos
    assert "d_ok" in destinos


def test_oportunidad_posicion_score_pico_en_el_rango_configurado():
    oportunidad = OportunidadSEO(posicion_min=4.0, posicion_max=20.0, ventana_decaimiento=30.0)

    assert oportunidad_posicion_score(10, oportunidad) == 1.0
    assert oportunidad_posicion_score(4, oportunidad) == 1.0
    assert oportunidad_posicion_score(20, oportunidad) == 1.0
    # Posición 1 (ya muy bien posicionada): decae hacia 0 pero no es 0.
    assert 0.0 < oportunidad_posicion_score(1, oportunidad) < 1.0
    # Posición muy alejada: decae a 0 dentro de la ventana configurada.
    assert oportunidad_posicion_score(50, oportunidad) == 0.0
    # Sin dato de posición: NaN, no un número inventado.
    assert pd.isna(oportunidad_posicion_score(None, oportunidad))
    assert pd.isna(oportunidad_posicion_score(float("nan"), oportunidad))


def test_search_console_prioriza_posicion_en_zona_de_oportunidad():
    datasets = _make_datasets()  # a, b, c (con volumen), d (pendiente)
    search_console = pd.DataFrame(
        {
            "url": ["a", "b"],
            "clics_28d": [5, 50],
            "impresiones_28d": [500, 500],
            # "a" está en la zona de oportunidad por defecto (4-20);
            # "b" ya está en posición 1, fuera de la zona (decae).
            "posicion_media": [10, 1],
        }
    )
    weights = ScoringWeights(
        volumen_busqueda=0.0,
        muchos_productos=0.0,
        pocos_enlaces_entrantes=0.0,
        afinidad_categoria=0.0,
        posicion_oportunidad=1.0,
    )
    resultado = generate_link_proposals(datasets, weights=weights, search_console=search_console)

    desde_c = resultado[
        (resultado["categoria_origen"] == "c") & (~resultado["pendiente_confirmar"])
    ].sort_values("score", ascending=False)
    orden_destinos = desde_c["categoria_destino"].tolist()
    assert orden_destinos.index("a") < orden_destinos.index("b")


def test_pesos_search_console_sin_dataset_no_rompen_ni_afectan():
    """Si no se sube Search Console, los pesos de oportunidad SEO deben
    quedar sin efecto (igual que relevancia/prioridad manual sin
    rellenar), no lanzar una excepción ni marcar nada como pendiente.
    """
    datasets = _make_datasets()
    weights = ScoringWeights(posicion_oportunidad=1.0, impresiones_busqueda=1.0)
    resultado = generate_link_proposals(datasets, weights=weights)

    validas = resultado[resultado["categoria_destino"] != "d"]
    assert validas["pendiente_confirmar"].sum() == 0
    assert validas["score"].notna().all()


def test_comparar_evolucion_search_console_calcula_deltas():
    actual = pd.DataFrame(
        {
            "url": ["a", "b"],
            "clics_28d": [20, 5],
            "impresiones_28d": [1000, 2000],
            "posicion_media": [6, 15],
        }
    )
    anterior = pd.DataFrame(
        {
            "url": ["a", "b"],
            "clics_28d": [10, 5],
            "impresiones_28d": [800, 2000],
            "posicion_media": [9, 15],
        }
    )
    out = comparar_evolucion_search_console(actual, anterior).set_index("url")

    assert out.loc["a", "delta_clics"] == 10
    assert out.loc["a", "delta_impresiones"] == 200
    # Posición mejoró de 9 a 6 → delta positivo de 3 puestos.
    assert out.loc["a", "delta_posicion"] == 3
    assert out.loc["b", "delta_clics"] == 0
    assert out.loc["b", "delta_posicion"] == 0


def test_extraer_id_de_url_toma_el_id_numerico_del_slug():
    """Las URLs de categoría de Sklum siempre llevan el ID numérico al
    principio del último segmento de la ruta (p.ej. '524-comprar-...').
    Si no sigue ese patrón, no debe romper — simplemente no hay id.
    """
    assert extraer_id_de_url("https://www.sklum.com/es/524-comprar-mobiliario") == "524"
    assert (
        extraer_id_de_url("https://www.sklum.com/es/30227-comprar-muebles-de-tv-blancos/")
        == "30227"
    )
    assert extraer_id_de_url("https://www.sklum.com/es/sin-id-numerico") == ""
    assert extraer_id_de_url("") == ""
    assert extraer_id_de_url(None) == ""


def test_build_formato_ancho_una_fila_por_origen_con_enlaces_en_columnas():
    """Formato heredado del flujo anterior en Sheets: una fila por URL
    origen con su id, y los enlaces YA SELECCIONADOS (no todos los
    candidatos) como pares linked_id_N/linked_url_N (+ categoría,
    subcategoría, score y justificación de cada enlace), ordenados de
    mayor a menor score. El nº de bloques de columnas se ajusta al mayor
    nº de enlaces seleccionados que tenga cualquier origen, no viene fijo
    a 5.
    """
    url_524 = "https://www.sklum.com/es/524-comprar-mobiliario"
    url_30227 = "https://www.sklum.com/es/30227-comprar-muebles-de-tv-blancos"
    url_18374 = "https://www.sklum.com/es/18374-comprar-muebles-de-tv-nordicos"
    url_526 = "https://www.sklum.com/es/526-comprar-accesorios-lamparas"
    url_4695 = "https://www.sklum.com/es/4695-comprar-lamparas-rusticas"
    url_no_seleccionada = "https://www.sklum.com/es/999-no-seleccionada"

    resultado = pd.DataFrame(
        {
            "categoria_origen": [url_524, url_524, url_526, url_524],
            "categoria_destino": [url_30227, url_18374, url_4695, url_no_seleccionada],
            "categoria_principal_origen": ["Muebles", "Muebles", "Iluminación", "Muebles"],
            "categoria_secundaria_origen": ["", "", "", ""],
            "categoria_principal_destino": ["Muebles", "Salón", "Iluminación", "Muebles"],
            "categoria_secundaria_destino": ["", "", "", ""],
            "volumen_destino": [500, 200, 300, 100],
            "enlaces_entrantes_actuales_destino": [1, 5, 0, 2],
            "score": [0.9, 0.8, 0.7, 0.95],
            "seleccionada": [True, True, True, False],
        }
    )

    ancho = build_formato_ancho(resultado)

    assert list(ancho.columns) == [
        "id",
        "url",
        "categoria_principal",
        "categoria_secundaria",
        "n_enlaces",
        "linked_id_1",
        "linked_url_1",
        "linked_category_1",
        "linked_subcategory_1",
        "linked_score_1",
        "justificacion_1",
        "linked_id_2",
        "linked_url_2",
        "linked_category_2",
        "linked_subcategory_2",
        "linked_score_2",
        "justificacion_2",
    ]

    fila_524 = ancho[ancho["url"] == url_524].iloc[0]
    assert fila_524["id"] == "524"
    assert fila_524["categoria_principal"] == "Muebles"
    assert fila_524["n_enlaces"] == 2
    assert fila_524["linked_id_1"] == "30227"
    assert fila_524["linked_url_1"] == url_30227
    assert fila_524["linked_category_1"] == "Muebles"
    assert fila_524["linked_score_1"] == 0.9
    # Misma categoría (Muebles) + tiene volumen -> ambas razones deben
    # aparecer en la justificación, en lenguaje llano.
    assert "misma categoría" in fila_524["justificacion_1"]
    assert "volumen de búsqueda" in fila_524["justificacion_1"]
    assert fila_524["linked_id_2"] == "18374"
    assert fila_524["linked_url_2"] == url_18374

    fila_526 = ancho[ancho["url"] == url_526].iloc[0]
    assert fila_526["id"] == "526"
    assert fila_526["n_enlaces"] == 1
    assert fila_526["linked_id_1"] == "4695"
    # Solo tiene 1 enlace seleccionado -> la 2ª columna queda vacía (NaN).
    assert pd.isna(fila_526["linked_id_2"])
    # La propuesta descartada (seleccionada=False) no debe aparecer en
    # ninguna columna, ni siquiera de otro origen.
    assert url_no_seleccionada not in ancho.filter(like="linked_url").values


def test_justificacion_cae_a_un_mensaje_generico_si_no_hay_ninguna_senal_disponible():
    """Si `resultado` no trae ninguna de las columnas informativas (p.ej.
    una integración externa que solo pase categoria_origen/destino +
    score), la justificación no debe fallar: debe caer a un mensaje
    genérico basado en el score.
    """
    resultado = pd.DataFrame(
        {
            "categoria_origen": ["https://www.sklum.com/es/1-a"],
            "categoria_destino": ["https://www.sklum.com/es/2-b"],
            "score": [0.42],
            "seleccionada": [True],
        }
    )
    ancho = build_formato_ancho(resultado)
    assert "0.42" in ancho.iloc[0]["justificacion_1"]


def test_build_formato_ancho_sin_seleccionadas_devuelve_tabla_vacia():
    resultado = pd.DataFrame(
        {
            "categoria_origen": ["https://www.sklum.com/es/1-a"],
            "categoria_destino": ["https://www.sklum.com/es/2-b"],
            "score": [0.5],
            "seleccionada": [False],
        }
    )
    ancho = build_formato_ancho(resultado)
    assert ancho.empty
    assert list(ancho.columns) == [
        "id",
        "url",
        "categoria_principal",
        "categoria_secundaria",
        "n_enlaces",
    ]


def test_generate_link_proposals_no_cambia_al_procesar_por_bloques_pequenos():
    """`generate_link_proposals` cruza el catálogo por bloques de
    categorías origen (`core.scoring._BATCH_SIZE`, un nº de filas
    objetivo por bloque) para no construir todo el producto cartesiano en
    memoria de golpe con catálogos grandes. El resultado no debe depender
    del tamaño de bloque: aquí se fuerza un tamaño de bloque minúsculo (1
    categoría origen por bloque, más bloques que URLs) y se compara con
    el resultado "normal" para un dataset con más de un origen.
    """
    import core.scoring as scoring_module

    datasets = _make_datasets()  # a, b, c (con volumen), d (pendiente)

    original = generate_link_proposals(datasets)

    valor_original = scoring_module._BATCH_SIZE
    try:
        scoring_module._BATCH_SIZE = 1  # fuerza 1 categoría origen por bloque
        con_bloques_de_1 = generate_link_proposals(datasets)
    finally:
        scoring_module._BATCH_SIZE = valor_original

    pd.testing.assert_frame_equal(
        original.reset_index(drop=True), con_bloques_de_1.reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# diagnosticar_datasets: diagnóstico automático de por qué una propuesta
# ha salido vacía (o casi vacía)
# ---------------------------------------------------------------------------


def test_diagnosticar_datasets_caso_sano_no_da_motivo_de_bloqueo_total():
    """Con un dataset normal (el mismo que usan el resto de tests, donde
    SÍ se generan filas) el diagnóstico no debe señalar ni la salud
    técnica ni la taxonomía como causa de bloqueo total: como mucho el
    motivo genérico de "no se descarta nada por los filtros básicos".
    """
    datasets = _make_datasets()
    diagnostico = diagnosticar_datasets(datasets)

    assert diagnostico["n_crawl"] == 4
    assert diagnostico["urls_crawl_con_volumen"] == 3
    assert diagnostico["urls_crawl_con_taxonomia"] == 4
    assert diagnostico["n_destino_saludable"] == 4
    assert "columna equivocada" not in diagnostico["motivo_probable"]
    assert "taxonomía asociada" not in diagnostico["motivo_probable"]


def test_diagnosticar_datasets_detecta_columna_de_salud_mal_detectada():
    """Si (por un mapeo de columnas equivocado, p.ej. una columna real
    llamada "No_Indexable" detectada como si fuera "Indexable") todas las
    URLs quedan marcadas como no indexables, el diagnóstico debe
    señalarlo como la causa más probable de que la propuesta salga
    vacía, en vez de limitarse al mensaje genérico.
    """
    datasets = _make_datasets()
    datasets.crawl = datasets.crawl.copy()
    datasets.crawl["indexable"] = False  # todas las URLs "no indexables"

    diagnostico = diagnosticar_datasets(datasets)

    assert diagnostico["n_destino_saludable"] == 0
    assert "columna equivocada" in diagnostico["motivo_probable"] or "columna distinta" in diagnostico["motivo_probable"]


def test_diagnosticar_datasets_detecta_taxonomia_sin_solape_con_crawl():
    """Si el crawl y la taxonomía no comparten ninguna URL (p.ej. porque
    se ha usado una columna de URL distinta para cada uno dentro del
    mismo fichero), el diagnóstico debe señalarlo explícitamente en vez
    de quedarse en el motivo genérico de categorías compartidas.
    """
    datasets = _make_datasets()
    datasets.taxonomia = pd.DataFrame(
        {
            "url": ["x", "y", "z", "w"],
            "categoria_principal": ["Muebles", "Muebles", "Iluminacion", "Muebles"],
            "categoria_secundaria": ["Salon", "Salon", "Techo", "Dormitorio"],
        }
    )

    diagnostico = diagnosticar_datasets(datasets)

    assert diagnostico["urls_crawl_con_taxonomia"] == 0
    assert diagnostico["n_categorias_principales_distintas"] <= 1
    assert "Categoria_Principal" in diagnostico["motivo_probable"]


def test_diagnosticar_datasets_con_menos_de_dos_urls_de_crawl():
    datasets = _make_datasets()
    datasets.crawl = datasets.crawl.iloc[:1].copy()

    diagnostico = diagnosticar_datasets(datasets)

    assert "menos de 2 URLs" in diagnostico["motivo_probable"]
    assert "n_master" not in diagnostico


# ---------------------------------------------------------------------------
# generate_link_proposals(..., contador=...): diagnóstico del embudo de
# filtrado, pensado para detectar en qué paso una propuesta se queda en
# 0 filas (grupos aislados, salud del destino, enlaces ya existentes o un
# score_minimo demasiado alto).
# ---------------------------------------------------------------------------


def test_contador_no_cambia_el_resultado_ni_falla_si_es_none():
    datasets = _make_datasets()
    sin_contador = generate_link_proposals(datasets)
    con_contador = generate_link_proposals(datasets, contador={})
    pd.testing.assert_frame_equal(
        sin_contador.reset_index(drop=True), con_contador.reset_index(drop=True)
    )


def test_contador_detecta_score_minimo_demasiado_alto():
    """Si el score mínimo configurado es más alto que cualquier score
    real alcanzable, la propuesta sale vacía (0 seleccionadas) aunque
    haya pares candidatos válidos de sobra — el contador debe dejar esto
    clarísimo: pares_validos_con_score > 0, pero score_valido_maximo por
    debajo del score_minimo usado, y pares_seleccionados == 0.
    """
    datasets = _make_datasets()
    limites = LimitesPropuesta(max_enlaces_nuevos_por_origen=5, score_minimo=0.999)

    contador: dict = {}
    resultado = generate_link_proposals(datasets, limites=limites, contador=contador)

    assert contador["pares_validos_con_score"] > 0
    assert contador["score_valido_maximo"] is not None
    assert contador["score_valido_maximo"] < 0.999
    assert contador["pares_seleccionados"] == 0
    # Las filas "pendiente_confirmar" (datos incompletos) se conservan
    # siempre, pero ninguna fila válida queda marcada como seleccionada.
    assert not resultado.empty
    assert not resultado["seleccionada"].any()


def test_falta_num_productos_marca_pendiente_en_vez_de_score_nan_silencioso():
    """Si a una URL destino le falta el nº de productos (columna vacía o
    texto irreconocible tras `_parse_num_productos`), la fila debe
    marcarse `pendiente_confirmar` con un motivo explícito, en vez de
    quedar como "válida" con un `score` en NaN que desaparece en
    silencio del resultado final (esto es justo lo que provocaba que el
    catálogo real de Sklum, con la columna Nº_Productos en un formato de
    texto no reconocido, generase una propuesta con 0 filas: el score
    salía en NaN para el 100% de los pares y ninguno superaba nunca el
    score mínimo, pero tampoco se marcaba pendiente).
    """
    datasets = _make_datasets()
    datasets.crawl = datasets.crawl.copy()
    # "c" pierde su nº de productos.
    datasets.crawl.loc[datasets.crawl["url"] == "c", "num_productos"] = float("nan")

    resultado = generate_link_proposals(datasets)

    hacia_c = resultado[resultado["categoria_destino"] == "c"]
    assert not hacia_c.empty
    assert hacia_c["pendiente_confirmar"].all()
    assert hacia_c["score"].isna().all()
    assert "nº de productos" in hacia_c["motivo_pendiente"].iloc[0]


def test_contador_detecta_bloqueo_por_grupos_aislados():
    """Si TODAS las categorías quedan aisladas en grupos distintos entre
    sí (p.ej. un patrón de aislamiento tan amplio que separa el catálogo
    en singletons), el embudo debe mostrar que los pares se pierden ya en
    el primer filtro (grupo_aislado), antes incluso de llegar a salud
    técnica o a enlaces existentes.
    """
    datasets = _make_datasets()  # categorías: a, b, c, d
    contador: dict = {}
    # Un patrón por URL (todas normalizadas a minúsculas) aísla cada
    # categoría en su propio grupo de 1: ningún par sobrevive al filtro.
    resultado = generate_link_proposals(
        datasets, grupos_aislados=["salon", "techo", "dormitorio"], contador=contador
    )

    assert contador["pares_antes_de_filtros"] > 0
    assert contador["pares_tras_grupo_aislado"] < contador["pares_antes_de_filtros"]
    assert resultado is not None

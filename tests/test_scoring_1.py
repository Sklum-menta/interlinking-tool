import pandas as pd
import pytest

from core.config import AffinityScores, LimitesPropuesta, OportunidadSEO, ScoringWeights
from core.data_loader import InputDatasets
from core.scoring import (
    _derivar_titulo_desde_url,
    _elegibilidad_ampliacion_origen,
    _MINIMO_DESTINO_SI_HAY_CANDIDATOS,
    _MINIMO_SI_HAY_CANDIDATOS_DE_SOBRA,
    _recortar_bloque_a_lo_relevante,
    _rescatar_destinos_en_cero_absoluto,
    _rescatar_minimo_destino_por_congestion,
    _rescatar_minimo_por_congestion,
    affinity_score,
    build_formato_ancho,
    build_formato_it,
    build_master_table,
    comparar_evolucion_search_console,
    diagnosticar_datasets,
    extraer_id_de_url,
    generate_link_proposals,
    IT_SHOPS_DEFAULT,
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
    # "d" no tiene ningún enlace entrante existente ni ningún candidato
    # VÁLIDO (todos están pendientes de confirmar por falta de volumen),
    # así que la última red de seguridad (`_rescatar_destinos_en_cero_absoluto`,
    # decisión de negocio: "tiene que tener 2 enlaces entrantes todas las
    # categorías como mínimo", sin excepciones) rescata hasta el mínimo
    # (2) de entre sus propios candidatos pendientes -mejor un enlace
    # razonable por afinidad de categoría pendiente de confirmar un dato,
    # que dejar la categoría completamente huérfana-. Las filas rescatadas
    # SIGUEN marcadas `pendiente_confirmar=True` (el equipo las verá
    # señaladas para revisar el dato que falta).
    assert int(hacia_d["seleccionada"].sum()) == 2


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
    orígenes candidatos), el TECHO no le quita ningún enlace a "popular":
    de los 10 enlaces nuevos que salen en total (1 por cada una de las 10
    categorías del catálogo, cupo de origen=1), "alternativa" solo puede
    ser elegida de forma NATURAL por "popular" (su único candidato no
    pendiente), así que sin el suelo de destino se quedaría en 1 -por
    debajo del mínimo de 2 ("todas las categorías tienen que tener")-. El
    rescate de mínimo por destino le cede entonces exactamente 1 origen
    más (el de peor score entre o1..o8, ya que "popular" tiene de sobra
    para perderlo): no es el techo (holgado aquí, 100) quien reparte de
    menos, es el SUELO de "alternativa" quien redirige 1 de los 8.
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

    # 7 de las 8 categorías o1..o8 van a "popular" sin que el TECHO
    # (holgado aquí) les quite ninguna; la 8ª se redirige a "alternativa"
    # para cumplir su mínimo de destino (2) -el propio "popular" ya le
    # aporta el otro, como único candidato no pendiente que tiene-.
    conteo = seleccionadas_desde_o["categoria_destino"].value_counts()
    assert conteo.get("popular", 0) == 7
    assert conteo.get("alternativa", 0) == 1


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


def test_grupo_aislado_se_detecta_por_la_url_aunque_falte_en_la_categorizacion_manual():
    """Bug real encontrado en el catálogo de Sklum (feedback del usuario,
    1 oct): una categoría de Navidad cuya `categoria_secundaria` venía mal
    etiquetada como "Textil hogar" (hueco de la categorización manual, no
    del código) se enlazaba con categorías normales de Textil hogar como
    si no perteneciera a ningún grupo aislado. Ahora el slug de la URL
    (p.ej. '.../comprar-decoracion-de-navidad-verde') también cuenta como
    señal, así que esta categoría se detecta como "navidad" aunque la
    categorización manual no la mencione en absoluto.
    """
    urls = ["https://www.sklum.com/es/1-comprar-decoracion-de-navidad-verde", "nav1", "nav2", "textil1"]
    crawl = pd.DataFrame({"url": urls, "num_productos": [10] * len(urls)})
    volumen = pd.DataFrame({"url": urls, "keyword": [f"kw_{u}" for u in urls], "volumen": [100] * len(urls)})
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": ["Decoración", "Decoración", "Decoración", "Decoración"],
            # OJO: la primera fila NO dice "Navidad" en ningún sitio de la
            # categorización manual -- es justo el hueco que causaba el bug.
            "categoria_secundaria": ["Textil hogar", "Navidad", "Navidad", "Textil hogar"],
        }
    )
    enlaces = pd.DataFrame(columns=["source_url", "destination_url", "anchor_text", "zona"])
    datasets = InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)

    resultado = generate_link_proposals(datasets)
    desde_navidad_verde = resultado[resultado["categoria_origen"] == urls[0]]
    destinos = set(desde_navidad_verde["categoria_destino"])
    assert destinos == {"nav1", "nav2"}
    assert "textil1" not in destinos


def test_grupo_aislado_la_url_manda_sobre_una_categorizacion_manual_erronea():
    """Bug real encontrado en el catálogo de Sklum (feedback del usuario,
    1 oct): varias categorías de Black Friday y Rebajas venían etiquetadas
    por error en `categoria_secundaria` como "Special Price", y por eso la
    propuesta las enlazaba con Special Price de verdad -- justo la mezcla
    que el usuario reportó. El slug de la URL (inequívoco, no se puede
    escribir mal en una celda) ahora tiene prioridad sobre ese campo
    manual cuando no coinciden.
    """
    urls = [
        "https://www.sklum.com/es/1-comprar-ofertas-sillas-black-friday",  # mal etiquetada
        "bf_real",
        "sp_real1",
        "sp_real2",
    ]
    crawl = pd.DataFrame({"url": urls, "num_productos": [10] * len(urls)})
    volumen = pd.DataFrame({"url": urls, "keyword": [f"kw_{u}" for u in urls], "volumen": [100] * len(urls)})
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": ["Sillas", "Muebles", "Sofas", "Sillas"],
            # La fila mal etiquetada dice "Special Price" en vez de
            # "Black Friday", aunque su URL es inequívocamente Black Friday.
            "categoria_secundaria": ["Special Price", "Black Friday", "Special Price", "Special Price"],
        }
    )
    enlaces = pd.DataFrame(columns=["source_url", "destination_url", "anchor_text", "zona"])
    datasets = InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)

    resultado = generate_link_proposals(datasets)
    desde_mal_etiquetada = resultado[resultado["categoria_origen"] == urls[0]]
    destinos = set(desde_mal_etiquetada["categoria_destino"])
    # Debe enlazar con Black Friday de verdad (por la URL), NUNCA con
    # Special Price (aunque así lo diga, por error, la categorización
    # manual).
    assert destinos == {"bf_real"}
    assert "sp_real1" not in destinos
    assert "sp_real2" not in destinos


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
        "h1",
        "categoria_principal",
        "categoria_secundaria",
        "n_enlaces",
        "motivo_num_enlaces",
        "linked_id_1",
        "linked_url_1",
        "linked_h1_1",
        "linked_category_1",
        "linked_subcategory_1",
        "linked_score_1",
        "justificacion_1",
        "linked_id_2",
        "linked_url_2",
        "linked_h1_2",
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
        "h1",
        "categoria_principal",
        "categoria_secundaria",
        "n_enlaces",
        "motivo_num_enlaces",
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


# ---------------------------------------------------------------------------
# Reparto por rondas (decisión de negocio: demasiadas categorías se
# quedaban con menos enlaces de los que les tocaban, no por falta real de
# candidatos sino por el ORDEN en que se procesaban los pares) y
# ampliación EXCEPCIONAL del cupo por origen (nunca como norma general,
# siempre con un motivo explicado).
# ---------------------------------------------------------------------------


def _make_datasets_muchos_origenes_compiten_por_los_mismos_destinos() -> InputDatasets:
    """9 categorías origen (o1..o9) que, usando solo el peso de volumen de
    búsqueda (señal que depende únicamente del destino, igual para
    cualquier origen), coinciden TODAS en el mismo orden de preferencia:
    primero los 5 destinos "populares" (pop1..pop5, con más volumen),
    luego los 10 "filler" (con volumen decreciente). Con un cupo de 8
    enlaces entrantes nuevos por destino, cada uno de los 5 populares solo
    puede servir a 8 de las 9 categorías origen — SIEMPRE se queda UNA
    fuera en cada uno de esos 5 destinos. Si el reparto no permite a esa
    categoría seguir probando más abajo en su lista (más allá de su
    propio cupo de 5 "intentos"), se quedaría con menos enlaces de los
    que le tocan aunque haya filler de sobra para completarlos.
    """
    populares = [f"pop{i}" for i in range(1, 6)]
    fillers = [f"filler{i}" for i in range(1, 11)]
    origenes = [f"o{i}" for i in range(1, 10)]
    destinos = populares + fillers
    urls = origenes + destinos

    crawl = pd.DataFrame({"url": urls, "num_productos": [10] * len(urls)})
    volumenes = (
        [1000 - i * 10 for i in range(len(populares))]
        + [400 - i * 10 for i in range(len(fillers))]
        + [0] * len(origenes)
    )
    volumen = pd.DataFrame(
        {
            "url": destinos + origenes,
            "keyword": [f"kw_{u}" for u in destinos + origenes],
            "volumen": volumenes,
        }
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


def test_reparto_por_rondas_permite_completar_cupo_probando_candidatos_mas_abajo():
    """Aunque 9 categorías origen coincidan en preferir los mismos 5
    destinos "populares" (que solo pueden servir a 8 cada uno por el
    límite de enlaces entrantes nuevos), TODAS deben poder completar su
    cupo normal de 5 enlaces gracias a los destinos "filler": a la
    categoría que se quede sin hueco en algún popular no se le puede
    cortar la posibilidad de seguir probando más abajo en su lista.
    """
    datasets = _make_datasets_muchos_origenes_compiten_por_los_mismos_destinos()
    weights = ScoringWeights(
        volumen_busqueda=1.0,
        muchos_productos=0.0,
        pocos_enlaces_entrantes=0.0,
        afinidad_categoria=0.0,
    )
    resultado = generate_link_proposals(datasets, weights=weights)
    seleccionadas = resultado[resultado["seleccionada"]]

    conteo_por_origen = seleccionadas["categoria_origen"].value_counts()
    origenes = [f"o{i}" for i in range(1, 10)]
    for origen in origenes:
        assert conteo_por_origen.get(origen, 0) == 5, (
            f"{origen} se quedó con {conteo_por_origen.get(origen, 0)} enlaces, "
            "debería haber completado su cupo de 5 usando los destinos filler"
        )

    # Ningún destino (ni popular ni filler) supera el cupo de 8 entrantes.
    conteo_por_destino = seleccionadas["categoria_destino"].value_counts()
    assert (conteo_por_destino <= 8).all()


def _make_datasets_con_enlaces_previos(
    salientes_por_url: dict[str, int],
    entrantes_extra_para: str | None = None,
    baseline_destinos: bool = True,
    profundidad_por_url: dict[str, int] | None = None,
) -> InputDatasets:
    """Catálogo de 20 categorías destino (todas con volumen y taxonomía
    homogéneos, para que el score no dependa de nada salvo las señales de
    origen que se quieren probar) más las categorías origen que se pasen
    en `salientes_por_url` (cada una con el nº de enlaces salientes
    propios indicado, construido literalmente con ese nº de enlaces en el
    dataset de enlaces). Si se indica `entrantes_extra_para`, esa URL
    concreta recibe además 20 enlaces entrantes de más (para simular alta
    autoridad interna).

    `baseline_destinos` (True por defecto) añade a las 20 categorías
    destino un perfil de enlazado propio realista (enlaces salientes Y
    entrantes de base), para que las estadísticas globales del catálogo
    (mediana de salientes, percentil 90 de entrantes) que usan las
    salvaguardas de `_elegibilidad_ampliacion_origen` reflejen un catálogo
    real -donde cualquier URL es a la vez origen y destino de enlaces,
    como el de Sklum (mediana salientes=4.0, p90 entrantes=12.0)- y no un
    catálogo artificial de "sumideros puros" sin enlazado propio. Se pone
    a False específicamente para simular un catálogo genuinamente pobre en
    enlaces de principio a fin (p.ej. sin dataset de enlaces subido).

    `profundidad_por_url` (opcional) fija el nº de clics desde la home de
    las URLs indicadas; el resto del catálogo recibe una profundidad
    "normal" de 4 (hay variación real, no todo a la misma distancia de la
    home) para que las salvaguardas de
    `_elegibilidad_ampliacion_origen` tengan una mediana representativa.
    Si no se pasa nada, no se incluye la columna (igual que un crawl real
    que no trae ese dato: la señal de profundidad simplemente no se usa).
    """
    destinos = [f"d{i}" for i in range(1, 21)]
    origenes = list(salientes_por_url.keys())
    urls = origenes + destinos

    crawl = pd.DataFrame({"url": urls, "num_productos": [10] * len(urls)})
    if profundidad_por_url:
        crawl["profundidad"] = [profundidad_por_url.get(u, 4) for u in urls]
    volumen = pd.DataFrame(
        {"url": urls, "keyword": [f"kw_{u}" for u in urls], "volumen": [500] * len(urls)}
    )
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": ["Muebles"] * len(urls),
            "categoria_secundaria": [""] * len(urls),
        }
    )

    filas_enlaces = []
    for origen, n_salientes in salientes_por_url.items():
        for i in range(n_salientes):
            # Enlaces salientes "de relleno" hacia destinos que NO son
            # candidatos del test (usa nombres fuera de d1..d20 para no
            # interferir con `exclude_existing_links`).
            filas_enlaces.append(
                {
                    "source_url": origen,
                    "destination_url": f"otro_destino_{origen}_{i}",
                    "anchor_text": "",
                    "zona": "Content",
                }
            )

    if baseline_destinos:
        # Perfil de enlazado BASELINE realista para las 20 categorías
        # destino (d1..d20): ver docstring. Se usan URLs de relleno fuera
        # de d1..d20 (y de las categorías origen) para no interferir con
        # el scoring de candidatos del test. El baseline de entrantes (6)
        # se elige por encima del umbral de la salvaguarda de autoridad
        # (5) para que un catálogo "normal" no la desactive por sí solo.
        for destino in destinos:
            for i in range(4):
                filas_enlaces.append(
                    {
                        "source_url": destino,
                        "destination_url": f"otro_destino_baseline_{destino}_{i}",
                        "anchor_text": "",
                        "zona": "Content",
                    }
                )
            for i in range(6):
                filas_enlaces.append(
                    {
                        "source_url": f"otro_origen_baseline_{destino}_{i}",
                        "destination_url": destino,
                        "anchor_text": "",
                        "zona": "Content",
                    }
                )

    if entrantes_extra_para:
        for i in range(20):
            filas_enlaces.append(
                {
                    "source_url": f"otro_origen_{i}",
                    "destination_url": entrantes_extra_para,
                    "anchor_text": "",
                    "zona": "Content",
                }
            )
    enlaces = pd.DataFrame(
        filas_enlaces, columns=["source_url", "destination_url", "anchor_text", "zona"]
    )
    return InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)


def test_ampliacion_excepcional_por_pocos_enlaces_salientes():
    """Una categoría origen que casi no tiene enlaces salientes propios
    (<=2), en un catálogo donde la mediana SÍ es representativa (>=3),
    puede recibir más de los 5 enlaces normales — hasta el techo
    excepcional (10) — y la propuesta explica el motivo. Una categoría
    "normal" del mismo catálogo, con enlaces salientes típicos, NO se
    amplía aunque tenga exactamente los mismos candidatos disponibles:
    no es una norma general.
    """
    datasets = _make_datasets_con_enlaces_previos(
        {"o_pocos": 1, "o_normal_a": 4, "o_normal_b": 4, "o_normal_c": 5, "o_normal_d": 4}
    )
    resultado = generate_link_proposals(datasets)
    seleccionadas = resultado[resultado["seleccionada"]]
    conteo = seleccionadas["categoria_origen"].value_counts()

    assert conteo["o_pocos"] > 5
    assert conteo["o_normal_a"] == 5
    assert conteo["o_normal_b"] == 5

    motivo = resultado.loc[
        resultado["categoria_origen"] == "o_pocos", "motivo_num_enlaces_origen"
    ].iloc[0]
    assert "enlaces salientes" in motivo


def test_ampliacion_excepcional_por_autoridad_interna():
    """Una categoría origen con mucha autoridad interna (muy por encima
    del percentil 90 de enlaces entrantes del catálogo) puede recibir más
    de los 5 enlaces normales, con el motivo explicado.
    """
    datasets = _make_datasets_con_enlaces_previos(
        {"o_autoridad": 4, "o_normal_a": 4, "o_normal_b": 4, "o_normal_c": 4, "o_normal_d": 4},
        entrantes_extra_para="o_autoridad",
    )
    resultado = generate_link_proposals(datasets)
    seleccionadas = resultado[resultado["seleccionada"]]
    conteo = seleccionadas["categoria_origen"].value_counts()

    assert conteo["o_autoridad"] > 5
    assert conteo["o_normal_a"] == 5

    motivo = resultado.loc[
        resultado["categoria_origen"] == "o_autoridad", "motivo_num_enlaces_origen"
    ].iloc[0]
    assert "autoridad interna" in motivo


def test_ampliacion_excepcional_no_se_activa_si_el_catalogo_entero_tiene_pocos_enlaces():
    """Salvaguarda: si TODO el catálogo tiene pocos enlaces salientes (p.ej.
    porque no se ha subido un dataset de enlaces representativo), el
    criterio de "pocos enlaces salientes" NO debe activarse para todas
    las categorías a la vez — eso convertiría la excepción en norma.
    Ninguna categoría debe superar el cupo normal de 5 en este caso.
    """
    datasets = _make_datasets_con_enlaces_previos(
        {f"o{i}": 0 for i in range(1, 8)}, baseline_destinos=False
    )
    resultado = generate_link_proposals(datasets)
    seleccionadas = resultado[resultado["seleccionada"]]
    conteo = seleccionadas["categoria_origen"].value_counts()

    assert (conteo <= 5).all()


def test_menos_de_5_enlaces_siempre_lleva_motivo_explicado():
    """Si una categoría origen se queda por debajo del cupo normal (5),
    la propuesta debe explicar el motivo concreto: o bien no había
    suficientes destinos candidatos, o bien los mejores ya habían
    agotado su cupo de enlaces entrantes con otras categorías mejor
    puntuadas. Nunca debe quedar en blanco.
    """
    # Un grupo aislado pequeño (3 categorías) hace que cualquier origen
    # de ese grupo tenga, como mucho, 2 candidatos posibles (el resto del
    # grupo) — menos que el cupo normal de 5, por pura falta de
    # candidatos, no por reparto.
    datasets = _make_datasets_con_grupos_aislados()
    resultado = generate_link_proposals(datasets)
    seleccionadas = resultado[resultado["seleccionada"]]
    conteo = seleccionadas.groupby("categoria_origen").size()

    origenes_bf = ["bf1", "bf2"]  # grupo "Black Friday" de solo 2 categorías
    for origen in origenes_bf:
        n = conteo.get(origen, 0)
        assert n < 5
        motivo = resultado.loc[
            resultado["categoria_origen"] == origen, "motivo_num_enlaces_origen"
        ].iloc[0]
        assert motivo != ""
        assert "candidata" in motivo


def _make_datasets_un_unico_destino_compartido() -> InputDatasets:
    """9 categorías origen que SOLO tienen una categoría destino candidata
    en todo el catálogo ("d_shared"). Con el cupo normal de 8 enlaces
    entrantes nuevos por destino, 8 de las 9 consiguen su enlace y UNA se
    queda, inevitablemente, con CERO enlaces nuevos (no le queda ningún
    otro candidato al que recurrir) — el caso más extremo de "menos de 5".
    """
    origenes = [f"o{i}" for i in range(1, 10)]
    urls = origenes + ["d_shared"]
    # Los orígenes se marcan como "no indexables" para que la salud técnica
    # los excluya como posibles DESTINOS de los demás orígenes — si no, al
    # compartir taxonomía cualquier origen sería también un destino válido
    # para otro origen y "d_shared" dejaría de ser su único candidato real.
    # La salud técnica solo restringe el lado destino, así que los orígenes
    # siguen pudiendo actuar con normalidad como origen de sus propios
    # enlaces.
    crawl = pd.DataFrame(
        {
            "url": urls,
            "num_productos": [10] * len(urls),
            "indexable": [False] * len(origenes) + [True],
        }
    )
    volumen = pd.DataFrame(
        {"url": urls, "keyword": [f"kw_{u}" for u in urls], "volumen": [500] * len(urls)}
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


def test_origen_sin_ningun_enlace_seleccionado_tambien_lleva_motivo_explicado():
    """Regresión: un origen que se queda con CERO enlaces nuevos (el caso
    extremo de "menos de 5") es el que más necesita una explicación, y sin
    embargo `_motivos_num_enlaces` solo recorría los orígenes presentes en
    `seleccionadas` (es decir, con >=1 enlace elegido) — un origen con 0
    enlaces no tiene ninguna fila con `seleccionada=True`, así que nunca
    aparecía en ese recorrido y se quedaba con motivo en blanco (este es
    exactamente el patrón encontrado al probar con datos reales de Sklum:
    66 categorías origen con 0 enlaces seleccionados, todas con motivo
    vacío antes de este fix). Aquí se fuerza destino-contención real (8 de
    9 orígenes consiguen su único candidato posible, 1 se queda sin
    ninguno) y se comprueba que ese origen también lleva su motivo
    explicado.
    """
    datasets = _make_datasets_un_unico_destino_compartido()
    resultado = generate_link_proposals(datasets)
    seleccionadas = resultado[resultado["seleccionada"]]

    origenes = [f"o{i}" for i in range(1, 10)]
    conteo = seleccionadas["categoria_origen"].value_counts().reindex(origenes, fill_value=0)
    origenes_sin_enlaces = conteo[conteo == 0].index.tolist()
    assert len(origenes_sin_enlaces) == 1, (
        "se esperaba que exactamente 1 de los 9 orígenes se quedara sin "
        f"ningún enlace (cupo de destino=8); conteo real: {conteo.to_dict()}"
    )

    origen_sin_enlaces = origenes_sin_enlaces[0]
    # El origen sigue presente en el resultado (es un candidato válido que
    # perdió la contienda por el destino, no un candidato descartado).
    assert (resultado["categoria_origen"] == origen_sin_enlaces).any()

    motivo = resultado.loc[
        resultado["categoria_origen"] == origen_sin_enlaces, "motivo_num_enlaces_origen"
    ].iloc[0]
    assert motivo != ""
    assert ("agotado" in motivo) or ("candidata" in motivo)


def _make_master_profundidad(
    profundidad_por_url: dict[str, int],
    n_filler: int = 27,
) -> pd.DataFrame:
    """Tabla maestra mínima (no pasa por `generate_link_proposals`, prueba
    `_elegibilidad_ampliacion_origen` de forma aislada) con un catálogo de
    relleno cuya profundidad se reparte de forma realista (1 a 9 clics de
    la home, 3 categorías en cada nivel) para que la mediana y el
    percentil 10 salgan representativos, más las URLs concretas que se
    pasen en `profundidad_por_url`. Los enlaces salientes (10) y entrantes
    (1) se fijan iguales para TODAS las filas a propósito, para que los
    otros dos criterios de ampliación (pocos salientes / muchos
    entrantes) queden desactivados y no interfieran con lo que se quiere
    probar aquí.
    """
    fillers = [f"f{i}" for i in range(1, n_filler + 1)]
    depths_fillers = [(i % 9) + 1 for i in range(n_filler)]
    urls = fillers + list(profundidad_por_url.keys())
    depths = depths_fillers + list(profundidad_por_url.values())
    return pd.DataFrame(
        {
            "url": urls,
            "enlaces_salientes_actuales": [10] * len(urls),
            "enlaces_entrantes_actuales": [1] * len(urls),
            "profundidad": depths,
        }
    )


def test_ampliacion_excepcional_por_profundidad_baja():
    """Una categoría muy cerca de la home (percentil 10 más bajo de
    profundidad del catálogo, con una mediana representativa) puede
    recibir más de los 5 enlaces normales por esta vía, igual que por
    enlaces entrantes — es la misma idea de "autoridad interna", pero
    mirando la posición estructural en la arquitectura de la web.
    """
    master = _make_master_profundidad({"o_cerca": 1, "o_normal": 5})
    motivos = _elegibilidad_ampliacion_origen(master)

    assert "o_cerca" in motivos
    assert "profundidad" not in motivos  # (sanity: no es una URL real)
    assert "cerca de la home" in motivos["o_cerca"]
    assert "o_normal" not in motivos


def test_ampliacion_excepcional_por_profundidad_no_se_activa_en_catalogo_plano():
    """Salvaguarda: si todo el catálogo está a la misma distancia de la
    home (web pequeña o con estructura muy chata), estar "cerca de la
    home" no es nada excepcional, así que el criterio no debe activarse
    para nadie.
    """
    urls = [f"u{i}" for i in range(1, 21)]
    master = pd.DataFrame(
        {
            "url": urls,
            "enlaces_salientes_actuales": [10] * len(urls),
            "enlaces_entrantes_actuales": [1] * len(urls),
            "profundidad": [2] * len(urls),
        }
    )
    motivos = _elegibilidad_ampliacion_origen(master)
    assert motivos == {}


def test_ampliacion_excepcional_por_profundidad_no_aplica_si_el_crawl_no_trae_el_dato():
    """Si el rastreo no incluye ninguna columna de profundidad/nivel (el
    caso normal hoy), el criterio simplemente no se evalúa — no debe
    fallar ni activarse por accidente con datos ausentes.
    """
    urls = [f"u{i}" for i in range(1, 21)]
    master = pd.DataFrame(
        {
            "url": urls,
            "enlaces_salientes_actuales": [10] * len(urls),
            "enlaces_entrantes_actuales": [1] * len(urls),
        }
    )
    motivos = _elegibilidad_ampliacion_origen(master)
    assert motivos == {}


def test_ampliacion_excepcional_por_profundidad_de_extremo_a_extremo():
    """El mismo criterio de profundidad, pero probado a través de
    `generate_link_proposals` completo (no solo la función aislada), para
    confirmar que el dato de profundidad viaja correctamente desde el
    crawl hasta la columna `motivo_num_enlaces_origen` de la propuesta
    final.
    """
    fillers = [f"f{i}" for i in range(1, 28)]
    depths_fillers = [(i % 9) + 1 for i in range(27)]
    destinos = [f"d{i}" for i in range(1, 21)]
    origenes = ["o_cerca", "o_normal"]
    urls = fillers + origenes + destinos
    depths = depths_fillers + [1, 5] + [5] * 20

    crawl = pd.DataFrame(
        {"url": urls, "num_productos": [10] * len(urls), "profundidad": depths}
    )
    volumen = pd.DataFrame(
        {"url": urls, "keyword": [f"kw_{u}" for u in urls], "volumen": [500] * len(urls)}
    )
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": ["Muebles"] * len(urls),
            "categoria_secundaria": [""] * len(urls),
        }
    )
    enlaces = pd.DataFrame(columns=["source_url", "destination_url", "anchor_text", "zona"])
    datasets = InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)

    resultado = generate_link_proposals(datasets)
    seleccionadas = resultado[resultado["seleccionada"]]
    conteo = seleccionadas.groupby("categoria_origen").size()

    assert conteo.get("o_cerca", 0) > 5
    assert conteo.get("o_normal", 0) == 5

    motivo = resultado.loc[
        resultado["categoria_origen"] == "o_cerca", "motivo_num_enlaces_origen"
    ].iloc[0]
    assert "cerca de la home" in motivo


# ---------------------------------------------------------------------------
# Rendimiento en Search Console como factor de "autoridad" para la
# ampliación excepcional (decisión de negocio del 1 oct: "si tienen mucho
# rendimiento en GSC es que tienen autoridad").
# ---------------------------------------------------------------------------


def test_ampliacion_excepcional_por_rendimiento_gsc():
    """Una categoría con muchos clics reales en Search Console (top 10%
    del catálogo, con un umbral mínimo representativo) puede recibir más
    de los 5 enlaces normales, igual que por enlaces entrantes o
    profundidad — es autoridad demostrada con tráfico real, no solo
    enlaces internos.
    """
    fillers = [f"f{i}" for i in range(1, 28)]
    # 10..90 clics, repartidos de forma realista (no todo el catálogo a
    # trivialmente poco tráfico, para que el percentil 90 sea representativo
    # y supere la salvaguarda mínima absoluta).
    clics_fillers = [(i % 9 + 1) * 10 for i in range(27)]
    destinos = [f"d{i}" for i in range(1, 21)]
    origenes = ["o_rendimiento", "o_normal"]
    urls = fillers + origenes + destinos

    crawl = pd.DataFrame({"url": urls, "num_productos": [10] * len(urls)})
    volumen = pd.DataFrame(
        {"url": urls, "keyword": [f"kw_{u}" for u in urls], "volumen": [500] * len(urls)}
    )
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": ["Muebles"] * len(urls),
            "categoria_secundaria": [""] * len(urls),
        }
    )
    enlaces = pd.DataFrame(columns=["source_url", "destination_url", "anchor_text", "zona"])
    datasets = InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)

    # o_rendimiento tiene muchísimos clics reales; o_normal tiene un
    # tráfico típico (ni rendimiento excepcional ni residual).
    sc_urls = fillers + origenes
    sc_clics = clics_fillers + [1000, 50]
    search_console = pd.DataFrame(
        {
            "url": sc_urls,
            "clics_28d": sc_clics,
            "impresiones_28d": [c * 20 for c in sc_clics],
            "posicion_media": [15.0] * len(sc_urls),
        }
    )

    resultado = generate_link_proposals(datasets, search_console=search_console)
    seleccionadas = resultado[resultado["seleccionada"]]
    conteo = seleccionadas.groupby("categoria_origen").size()

    assert conteo.get("o_rendimiento", 0) > 5
    assert conteo.get("o_normal", 0) == 5

    motivo = resultado.loc[
        resultado["categoria_origen"] == "o_rendimiento", "motivo_num_enlaces_origen"
    ].iloc[0]
    assert "Search Console" in motivo


def test_ampliacion_excepcional_por_gsc_no_se_activa_sin_datos_de_search_console():
    """Si no se sube Search Console (el caso normal), el criterio de
    rendimiento simplemente no se evalúa — nunca debe fallar ni activarse
    por accidente con datos ausentes.
    """
    urls = [f"u{i}" for i in range(1, 21)]
    master = pd.DataFrame(
        {
            "url": urls,
            "enlaces_salientes_actuales": [10] * len(urls),
            "enlaces_entrantes_actuales": [1] * len(urls),
        }
    )
    motivos = _elegibilidad_ampliacion_origen(master)
    assert motivos == {}


def test_ampliacion_excepcional_por_gsc_no_se_activa_si_el_trafico_es_residual():
    """Salvaguarda: si todo el catálogo tiene muy pocos clics (p.ej. un
    export de GSC de un sitio nuevo o con tráfico casi nulo), tener algo
    más de clics que el resto no es ninguna "autoridad demostrada" real,
    así que el criterio no se activa.
    """
    urls = [f"u{i}" for i in range(1, 21)]
    master = pd.DataFrame(
        {
            "url": urls,
            "enlaces_salientes_actuales": [10] * len(urls),
            "enlaces_entrantes_actuales": [1] * len(urls),
            "clics_28d": [1] * 19 + [5],  # el "mejor" apenas tiene 5 clics
        }
    )
    motivos = _elegibilidad_ampliacion_origen(master)
    assert motivos == {}


# ---------------------------------------------------------------------------
# Rescate de mínimo por congestión (decisión de negocio del 1 oct: "lo
# normal es que salgan 5 y solo en casos excepcionales que salgan menos,
# pero no quiero varias categorías con 1 enlace"). Si un origen tiene
# candidatos de sobra pero ha perdido todas las rondas frente a otros
# mejor puntuados, se le garantiza un mínimo "robando" el hueco al
# ocupante más prescindible de un destino lleno.
# ---------------------------------------------------------------------------


def _fila_validas(rows: dict) -> pd.DataFrame:
    return pd.DataFrame.from_dict(rows, orient="index")


def test_rescate_de_minimo_desaloja_al_ocupante_mas_prescindible():
    """Un origen con un único enlace seleccionado (y candidatos de sobra)
    debe llegar al mínimo robando el hueco de un destino lleno — y debe
    desalojar al ocupante de PEOR score entre los que pueden permitirse
    perder un enlace sin caer ellos mismos por debajo del mínimo.
    """
    rows: dict = {}
    idx = 0

    def add(origen, destino, score):
        nonlocal idx
        rows[idx] = {"categoria_origen": origen, "categoria_destino": destino, "score": score}
        idx += 1
        return idx - 1

    seleccionadas_idx = []
    origen_count: dict[str, int] = {}
    destino_count: dict[str, int] = {"pop": 8, "otro": 1, "otro2": 0}

    for i in range(1, 9):
        o = f"o_rico_{i}"
        r = add(o, "pop", score=0.5 + i * 0.01)
        seleccionadas_idx.append(r)
        origen_count[o] = 6

    r_otro = add("o_necesitado", "otro", score=0.9)
    seleccionadas_idx.append(r_otro)
    origen_count["o_necesitado"] = 1
    add("o_necesitado", "pop", score=0.95)  # candidato no elegido, destino lleno
    add("o_necesitado", "otro2", score=0.3)  # candidato no elegido, destino con hueco libre

    validas = _fila_validas(rows)
    seleccionadas_final, donantes = _rescatar_minimo_por_congestion(
        validas, origen_count, destino_count, seleccionadas_idx, max_destino=8, max_origen_normal=5
    )

    assert origen_count["o_necesitado"] == _MINIMO_SI_HAY_CANDIDATOS_DE_SOBRA
    # El desalojado tiene que ser el de peor score en "pop" (o_rico_1, 0.51).
    assert donantes == {"o_rico_1": 1}
    assert origen_count["o_rico_1"] == 5
    # Nadie más perdió nada, y "pop" sigue exactamente en su cupo (8).
    conteo_pop = sum(
        1 for i in seleccionadas_final if validas.at[i, "categoria_destino"] == "pop"
    )
    assert conteo_pop == 8


def test_rescate_de_minimo_nunca_crea_una_nueva_victima_por_debajo_del_minimo():
    """Si NINGÚN ocupante de los destinos candidatos puede permitirse
    perder un enlace sin caer él mismo por debajo del mínimo, el rescate
    no debe desalojar a nadie — el origen necesitado se queda como estaba
    (su motivo ya queda explicado por otra vía, ver
    `test_menos_de_5_enlaces_siempre_lleva_motivo_explicado`).
    """
    rows: dict = {}
    idx = 0

    def add(origen, destino, score):
        nonlocal idx
        rows[idx] = {"categoria_origen": origen, "categoria_destino": destino, "score": score}
        idx += 1
        return idx - 1

    seleccionadas_idx = []
    origen_count: dict[str, int] = {}
    destino_count: dict[str, int] = {"pop": 2}

    # Los 2 ocupantes de "pop" están exactamente en el mínimo (3): no se
    # les puede quitar nada sin dejarlos a ellos por debajo.
    for i in range(1, 3):
        o = f"o_al_minimo_{i}"
        r = add(o, "pop", score=0.5 + i * 0.01)
        seleccionadas_idx.append(r)
        origen_count[o] = _MINIMO_SI_HAY_CANDIDATOS_DE_SOBRA

    r_necesitado = add("o_necesitado", "otro", score=0.9)
    seleccionadas_idx.append(r_necesitado)
    origen_count["o_necesitado"] = 1
    add("o_necesitado", "pop", score=0.95)

    validas = _fila_validas(rows)
    seleccionadas_final, donantes = _rescatar_minimo_por_congestion(
        validas, origen_count, destino_count, seleccionadas_idx, max_destino=2, max_origen_normal=5
    )

    assert donantes == {}
    assert origen_count["o_necesitado"] == 1  # no se pudo rescatar, y no pasa nada
    assert origen_count["o_al_minimo_1"] == _MINIMO_SI_HAY_CANDIDATOS_DE_SOBRA
    assert origen_count["o_al_minimo_2"] == _MINIMO_SI_HAY_CANDIDATOS_DE_SOBRA


def test_rescate_de_minimo_respeta_un_cupo_normal_configurado_por_debajo_del_minimo():
    """Si alguien configura `max_enlaces_nuevos_por_origen` por debajo del
    mínimo habitual (3) -poco común, pero technically posible-, el
    rescate NUNCA debe forzar más enlaces de los que ese cupo normal
    permite.
    """
    datasets = _make_datasets()
    limites = LimitesPropuesta(max_enlaces_nuevos_por_origen=1, score_minimo=0.0)
    resultado = generate_link_proposals(datasets, limites=limites)

    for origen, grupo in resultado.groupby("categoria_origen"):
        assert grupo["seleccionada"].sum() <= 1


def test_rescate_de_minimo_extremo_a_extremo_evita_categorias_con_un_solo_enlace():
    """El caso real que motivó este cambio: muchos orígenes compiten por
    los mismos destinos "buenos" y, sin rescate, alguno se queda con un
    único enlace pese a tener de sobra más candidatos válidos. Con el
    rescate, ningún origen con candidatos de sobra debe quedarse con
    menos de `_MINIMO_SI_HAY_CANDIDATOS_DE_SOBRA`.
    """
    # 12 orígenes, todos prefiriendo los mismos 2 destinos "muy buenos"
    # (cupo 8 cada uno = 16 huecos para 12*5=60 enlaces deseados) y SIN
    # ningún destino "filler" de refuerzo: sin rescate, varios orígenes se
    # quedarían con 1-2 enlaces simplemente por perder todas las rondas.
    buenos = ["bueno1", "bueno2"]
    origenes = [f"o{i}" for i in range(1, 13)]
    urls = origenes + buenos
    crawl = pd.DataFrame({"url": urls, "num_productos": [10] * len(urls)})
    volumen = pd.DataFrame(
        {
            "url": urls,
            "keyword": [f"kw_{u}" for u in urls],
            "volumen": [1000, 990] + [0] * len(origenes),
        }
    )
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": ["Muebles"] * len(urls),
            "categoria_secundaria": [""] * len(urls),
        }
    )
    enlaces = pd.DataFrame(columns=["source_url", "destination_url", "anchor_text", "zona"])
    datasets = InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)

    weights = ScoringWeights(
        volumen_busqueda=1.0,
        muchos_productos=0.0,
        pocos_enlaces_entrantes=0.0,
        afinidad_categoria=0.0,
    )
    resultado = generate_link_proposals(datasets, weights=weights)
    seleccionadas = resultado[resultado["seleccionada"]]
    conteo = seleccionadas.groupby("categoria_origen").size().reindex(origenes, fill_value=0)

    assert (conteo >= _MINIMO_SI_HAY_CANDIDATOS_DE_SOBRA).all(), conteo.to_dict()


# ---------------------------------------------------------------------------
# Rescate de mínimo por destino (decisión de negocio del 1 oct: "todas las
# categorías tienen que tener, no podemos dejar una categoría sin
# enlazar"). Simétrico al rescate de mínimo por origen de arriba, pero
# mirando a quien RECIBE: ninguna categoría destino con al menos un
# candidato válido debe quedarse en 0 enlaces entrantes nuevos solo por
# perder la competición por hueco frente a destinos con mejor score.
# ---------------------------------------------------------------------------


def test_recorte_de_bloque_conserva_destinos_de_score_bajo_aunque_no_sean_el_mejor_de_ningun_origen():
    """Un destino con score bajo que nunca entra en el margen de NINGÚN
    origen (todos prefieren siempre a los mismos destinos "buenos") no
    debe desaparecer del todo en `_recortar_bloque_a_lo_relevante` — tiene
    que sobrevivir vía su propio margen por destino, para que la selección
    final (y su rescate) tengan con qué trabajar.
    """
    limites = LimitesPropuesta()  # margen por origen por defecto (>= 100)
    filas = []
    # 5 orígenes, cada uno con 150 destinos candidatos: "flojo" siempre en
    # último lugar (peor score), muy por debajo del margen de cualquier
    # origen individual.
    for o in range(1, 6):
        for d in range(150):
            filas.append(
                {
                    "origen": f"o{o}",
                    "destino": f"bueno{d}",
                    "score": 1.0 - d * 0.001,
                    "pendiente_confirmar": False,
                }
            )
        filas.append(
            {
                "origen": f"o{o}",
                "destino": "flojo",
                "score": 0.0001,
                "pendiente_confirmar": False,
            }
        )
    pairs = pd.DataFrame(filas)

    recortado = _recortar_bloque_a_lo_relevante(pairs, limites)

    assert (recortado["destino"] == "flojo").sum() == 5, (
        "El destino 'flojo' debería sobrevivir el recorte vía su propio "
        "margen por destino, una vez por cada origen que lo tenía como "
        "candidato en este bloque"
    )


def test_rescate_de_minimo_destino_anade_directo_si_el_origen_tiene_hueco():
    """Si el mejor candidato de un destino necesitado tiene un origen que
    todavía no ha agotado su propio cupo, se añade sin desalojar a nadie.
    """
    rows: dict = {}
    idx = 0

    def add(origen, destino, score):
        nonlocal idx
        rows[idx] = {"categoria_origen": origen, "categoria_destino": destino, "score": score}
        idx += 1
        return idx - 1

    add("o_con_hueco", "necesitado", score=0.7)  # candidato no elegido
    validas = _fila_validas(rows)

    seleccionadas_final = _rescatar_minimo_destino_por_congestion(
        validas,
        origen_count={"o_con_hueco": 2},
        destino_count={"necesitado": 0},
        seleccionadas_idx=[],
        max_origen_normal=5,
        max_origen_excepcional=10,
        ampliacion_origen={},
        max_destino=8,
    )

    assert len(seleccionadas_final) == 1
    assert validas.at[seleccionadas_final[0], "categoria_destino"] == "necesitado"


def test_rescate_de_minimo_destino_desaloja_al_ocupante_mas_prescindible():
    """Si el único origen candidato de un destino necesitado ya está al
    tope de su propio cupo, se le desaloja uno de sus enlaces actuales
    -el de PEOR score, y solo entre los que apuntan a un destino que
    puede permitirse perderlo (le quedan más ocupantes)- para hacerle
    sitio. El total de enlaces de ese origen no cambia.
    """
    rows: dict = {}
    idx = 0

    def add(origen, destino, score):
        nonlocal idx
        rows[idx] = {"categoria_origen": origen, "categoria_destino": destino, "score": score}
        idx += 1
        return idx - 1

    seleccionadas_idx = []
    # o_rico ya está al tope (cupo 2): un enlace a "d1" (score alto, con
    # otro ocupante de sobra) y otro a "d2" (score bajo, también con otro
    # ocupante de sobra).
    r1 = add("o_rico", "d1", score=0.9)
    seleccionadas_idx.append(r1)
    r2 = add("o_rico", "d2", score=0.2)
    seleccionadas_idx.append(r2)
    # Otros orígenes que mantienen d1 y d2 con más de un ocupante (para que
    # desalojar a o_rico de cualquiera de los dos sea seguro).
    r3 = add("otro1", "d1", score=0.8)
    seleccionadas_idx.append(r3)
    r4 = add("otro2", "d2", score=0.1)
    seleccionadas_idx.append(r4)

    add("o_rico", "necesitado", score=0.5)  # candidato no elegido

    validas = _fila_validas(rows)
    origen_count = {"o_rico": 2, "otro1": 1, "otro2": 1}
    # d1 y d2 tienen 5 enlaces cada uno -por encima del mínimo de destino
    # (2)-, así que cualquiera de los dos puede perder uno sin caer por
    # debajo de SU propio mínimo garantizado.
    destino_count = {"d1": 5, "d2": 5, "necesitado": 0}

    seleccionadas_final = _rescatar_minimo_destino_por_congestion(
        validas,
        origen_count,
        destino_count,
        seleccionadas_idx,
        max_origen_normal=2,
        max_origen_excepcional=10,
        ampliacion_origen={},
        max_destino=8,
    )

    destinos_de_o_rico = {
        validas.at[i, "categoria_destino"] for i in seleccionadas_final
        if validas.at[i, "categoria_origen"] == "o_rico"
    }
    # o_rico sigue teniendo exactamente 2 enlaces (ni más ni menos), pero
    # ahora a "d1" (el de mejor score, se conserva) y "necesitado" (el
    # rescatado) — "d2" (el de peor score) es el desalojado.
    assert destinos_de_o_rico == {"d1", "necesitado"}
    assert (validas.loc[seleccionadas_final, "categoria_origen"] == "o_rico").sum() == 2
    # d2 se queda con su otro ocupante (otro2), no en 0.
    assert "otro2" in set(
        validas.loc[
            [i for i in seleccionadas_final if validas.at[i, "categoria_destino"] == "d2"],
            "categoria_origen",
        ]
    )


def test_rescate_de_minimo_destino_nunca_deja_a_otro_destino_en_cero():
    """Si el único origen candidato de un destino necesitado está al tope
    y NINGUNO de sus enlaces actuales se puede desalojar sin dejar a OTRO
    destino en 0, el rescate no debe tocar nada: el destino necesitado se
    queda sin rescatar antes que crear una nueva víctima.
    """
    rows: dict = {}
    idx = 0

    def add(origen, destino, score):
        nonlocal idx
        rows[idx] = {"categoria_origen": origen, "categoria_destino": destino, "score": score}
        idx += 1
        return idx - 1

    r1 = add("o_al_tope", "unico_destino", score=0.9)
    add("o_al_tope", "necesitado", score=0.5)  # candidato no elegido

    validas = _fila_validas(rows)
    seleccionadas_final = _rescatar_minimo_destino_por_congestion(
        validas,
        origen_count={"o_al_tope": 1},
        destino_count={"unico_destino": 1, "necesitado": 0},
        seleccionadas_idx=[r1],
        max_origen_normal=1,
        max_origen_excepcional=10,
        ampliacion_origen={},
        max_destino=8,
    )

    # Nada cambia: "necesitado" se queda en 0 (no hay forma segura de
    # rescatarlo), "unico_destino" conserva su único enlace.
    assert seleccionadas_final == [r1]


def test_rescate_de_minimo_destino_extremo_a_extremo_evita_categorias_en_cero():
    """Caso real que motivó este cambio (ver conversación del 1 oct: "todas
    las categorías tienen que tener, no podemos dejar una categoría sin
    enlazar"): con un catálogo donde unos pocos destinos "buenos" ganan
    siempre la competición por hueco, un destino de score mucho más bajo
    (p.ej. por tener ya muchos enlaces entrantes de partida) puede acabar
    en 0 enlaces nuevos en TODAS las rondas normales. Con el rescate de
    mínimo por destino, debe acabar con al menos 1.
    """
    buenos = ["bueno1", "bueno2"]
    flojo = ["flojo"]
    origenes = [f"o{i}" for i in range(1, 13)]
    urls = origenes + buenos + flojo
    crawl = pd.DataFrame({"url": urls, "num_productos": [10] * len(urls)})
    volumen = pd.DataFrame(
        {
            "url": urls,
            "keyword": [f"kw_{u}" for u in urls],
            # "flojo" tiene muchísimo menos volumen que los "buenos": va a
            # perder la competición por hueco en todos los orígenes.
            "volumen": [0] * len(origenes) + [1000, 990, 5],
        }
    )
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": ["Muebles"] * len(urls),
            "categoria_secundaria": [""] * len(urls),
        }
    )
    enlaces = pd.DataFrame(columns=["source_url", "destination_url", "anchor_text", "zona"])
    datasets = InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)

    weights = ScoringWeights(
        volumen_busqueda=1.0,
        muchos_productos=0.0,
        pocos_enlaces_entrantes=0.0,
        afinidad_categoria=0.0,
    )
    # Cupo de destino muy ajustado (2) para que "bueno1"/"bueno2" se llenen
    # enseguida con los 12 orígenes compitiendo por ellos.
    limites = LimitesPropuesta(max_enlaces_nuevos_por_destino=2)
    resultado = generate_link_proposals(datasets, weights=weights, limites=limites)

    seleccionadas = resultado[resultado["seleccionada"]]
    enlaces_a_flojo = (seleccionadas["categoria_destino"] == "flojo").sum()
    assert enlaces_a_flojo >= _MINIMO_DESTINO_SI_HAY_CANDIDATOS


# ---------------------------------------------------------------------------
# Última red de seguridad: `_rescatar_destinos_en_cero_absoluto` (petición
# explícita y reiterada del 1 oct: "tiene que tener 2 enlaces entrantes
# todas las categorías como mínimo", sin excepciones). A diferencia de
# `_rescatar_minimo_destino_por_congestion`, esta solo mira el TOTAL
# (existentes + nuevos) y, si hace falta, recurre a candidatos pendientes
# de confirmar.
# ---------------------------------------------------------------------------


def _fila_resultado_cero_absoluto(rows: dict) -> pd.DataFrame:
    df = pd.DataFrame.from_dict(rows, orient="index")
    df["afinidad"] = df.get("afinidad", 0.5)
    return df


def test_rescate_absoluto_mide_solo_enlaces_nuevos_no_el_total_existente():
    """El mínimo se mide SOLO sobre enlaces NUEVOS de la propuesta (decisión
    de negocio del 1 oct: "quiero 2 enlaces en la propuesta de interlinking;
    si ya tiene enlaces en breadcrumbs/bolitas eso va aparte"). Un destino
    con muchos enlaces existentes pero 0 nuevos (y solo candidatos
    pendientes) SÍ se rescata igual, porque lo existente no cuenta para
    este mínimo.
    """
    rows = {
        0: {
            "categoria_origen": "o1",
            "categoria_destino": "normal_con_historial",
            "pendiente_confirmar": True,
            "enlaces_entrantes_actuales_destino": 27,
            "afinidad": 1.0,
            "grupo_aislado_destino": "",
        },
        1: {
            "categoria_origen": "o2",
            "categoria_destino": "normal_con_historial",
            "pendiente_confirmar": True,
            "enlaces_entrantes_actuales_destino": 27,
            "afinidad": 0.8,
            "grupo_aislado_destino": "",
        },
    }
    resultado = _fila_resultado_cero_absoluto(rows)
    seleccionadas_final = _rescatar_destinos_en_cero_absoluto(
        resultado,
        seleccionadas_idx=[],
        origen_count={},
        max_origen_normal=5,
        max_origen_excepcional=10,
        ampliacion_origen={},
        max_destino=8,
    )
    assert set(seleccionadas_final) == {0, 1}


def test_rescate_absoluto_exime_a_los_grupos_aislados():
    """Excepción de negocio (1 oct: "podemos hacer excepción con black
    friday, navidad, rebajas y special price"): un destino que pertenece a
    uno de los 4 grupos aislados NUNCA se rescata por esta vía, aunque
    tenga 0 enlaces nuevos y candidatos pendientes disponibles -suelen
    estar ya saturados de enlaces internos (breadcrumb) entre sus propios
    miembros, y forzar aquí un mínimo no tiene sentido de negocio-.
    """
    rows = {
        0: {
            "categoria_origen": "o1_black_friday",
            "categoria_destino": "hub_black_friday",
            "pendiente_confirmar": True,
            "enlaces_entrantes_actuales_destino": 0,
            "afinidad": 1.0,
            "grupo_aislado_destino": "black friday",
        },
    }
    resultado = _fila_resultado_cero_absoluto(rows)
    seleccionadas_final = _rescatar_destinos_en_cero_absoluto(
        resultado,
        seleccionadas_idx=[],
        origen_count={},
        max_origen_normal=5,
        max_origen_excepcional=10,
        ampliacion_origen={},
        max_destino=8,
    )
    assert seleccionadas_final == []


def test_rescate_absoluto_rescata_destino_sin_ningun_candidato_valido():
    """Un destino en cero enlaces (ni existentes ni nuevos) y con TODOS sus
    candidatos pendientes de confirmar (p.ej. le falta el volumen de
    búsqueda) debe rescatarse igualmente hasta el mínimo, priorizando los
    candidatos de mejor afinidad de categoría.
    """
    rows = {
        0: {
            "categoria_origen": "o_misma_categoria",
            "categoria_destino": "huerfano",
            "pendiente_confirmar": True,
            "enlaces_entrantes_actuales_destino": 0,
            "afinidad": 1.0,
        },
        1: {
            "categoria_origen": "o_otra_categoria",
            "categoria_destino": "huerfano",
            "pendiente_confirmar": True,
            "enlaces_entrantes_actuales_destino": 0,
            "afinidad": 0.15,
        },
        2: {
            "categoria_origen": "o_tercero",
            "categoria_destino": "huerfano",
            "pendiente_confirmar": True,
            "enlaces_entrantes_actuales_destino": 0,
            "afinidad": 0.6,
        },
    }
    resultado = _fila_resultado_cero_absoluto(rows)
    seleccionadas_final = _rescatar_destinos_en_cero_absoluto(
        resultado,
        seleccionadas_idx=[],
        origen_count={},
        max_origen_normal=5,
        max_origen_excepcional=10,
        ampliacion_origen={},
        max_destino=8,
    )
    # Mínimo = 2: se rescatan los 2 de mejor afinidad (idx 0 y 2), no el de
    # peor afinidad (idx 1).
    assert set(seleccionadas_final) == {0, 2}


def test_rescate_absoluto_respeta_el_cupo_del_origen():
    """El rescate nunca empuja a un origen por encima de su propio cupo,
    aunque eso signifique que el destino se quede por debajo del mínimo.
    """
    rows = {
        0: {
            "categoria_origen": "o_al_tope",
            "categoria_destino": "huerfano",
            "pendiente_confirmar": True,
            "enlaces_entrantes_actuales_destino": 0,
            "afinidad": 1.0,
        },
    }
    resultado = _fila_resultado_cero_absoluto(rows)
    seleccionadas_final = _rescatar_destinos_en_cero_absoluto(
        resultado,
        seleccionadas_idx=[],
        # o_al_tope ya está al tope de su cupo normal (2 de 2).
        origen_count={"o_al_tope": 2},
        max_origen_normal=2,
        max_origen_excepcional=2,
        ampliacion_origen={},
        max_destino=8,
    )
    assert seleccionadas_final == []


# ---------------------------------------------------------------------------
# id_origen/id_destino y h1_origen/h1_destino en el resultado (petición del
# usuario del 1 oct: poder ver el ID y el H1 de cada categoría en la
# propuesta, no solo la URL completa).
# ---------------------------------------------------------------------------


def _make_datasets_con_urls_realistas() -> InputDatasets:
    urls = [
        "https://www.sklum.com/es/524-comprar-mesas-de-salon",
        "https://www.sklum.com/es/901-comprar-sillas-de-comedor",
    ]
    crawl = pd.DataFrame({"url": urls, "num_productos": [50, 40]})
    volumen = pd.DataFrame({"url": urls, "keyword": ["kw1", "kw2"], "volumen": [500, 300]})
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": ["Muebles", "Muebles"],
            "categoria_secundaria": ["Salon", "Comedor"],
        }
    )
    enlaces = pd.DataFrame(columns=["source_url", "destination_url", "anchor_text", "zona"])
    return InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)


def test_derivar_titulo_desde_url_quita_id_y_prefijo_comprar():
    assert (
        _derivar_titulo_desde_url("https://www.sklum.com/es/524-comprar-mesas-de-salon")
        == "Mesas de salon"
    )


def test_derivar_titulo_desde_url_sin_prefijo_comprar_tambien_funciona():
    assert _derivar_titulo_desde_url("https://www.sklum.com/es/524-mesas-de-salon") == "Mesas de salon"


def test_derivar_titulo_desde_url_vacia_devuelve_vacio():
    assert _derivar_titulo_desde_url(None) == ""
    assert _derivar_titulo_desde_url("") == ""


def test_build_master_table_rellena_h1_e_id_cuando_no_hay_h1_real():
    datasets = _make_datasets_con_urls_realistas()
    master = build_master_table(datasets).set_index("url")

    fila = master.loc["https://www.sklum.com/es/524-comprar-mesas-de-salon"]
    assert fila["id"] == "524"
    # Sin columna H1 real en el crawl -> se aproxima desde el slug, nunca
    # queda vacío.
    assert fila["h1"] == "Mesas de salon"


def test_build_master_table_usa_el_h1_real_si_el_crawl_lo_trae():
    """Si el export del rastreo SÍ trae una columna de H1 real, esa es la
    que se usa (nunca se sobreescribe con la aproximación del slug)."""
    datasets = _make_datasets_con_urls_realistas()
    datasets.crawl["h1"] = ["Mesas de Salón Nórdicas", ""]  # la 2ª URL sigue sin H1 real
    master = build_master_table(datasets).set_index("url")

    assert master.loc["https://www.sklum.com/es/524-comprar-mesas-de-salon", "h1"] == "Mesas de Salón Nórdicas"
    # La URL sin H1 real sigue cayendo al título aproximado desde el slug.
    assert master.loc["https://www.sklum.com/es/901-comprar-sillas-de-comedor", "h1"] == "Sillas de comedor"


def test_generate_link_proposals_incluye_id_y_h1_de_origen_y_destino():
    datasets = _make_datasets_con_urls_realistas()
    resultado = generate_link_proposals(datasets)

    assert {"id_origen", "id_destino", "h1_origen", "h1_destino"} <= set(resultado.columns)
    fila = resultado.iloc[0]
    assert fila["id_origen"] in {"524", "901"}
    assert fila["id_destino"] in {"524", "901"}
    assert fila["h1_origen"] in {"Mesas de salon", "Sillas de comedor"}


def test_build_formato_ancho_incluye_h1_de_origen_y_de_cada_enlace():
    datasets = _make_datasets_con_urls_realistas()
    resultado = generate_link_proposals(datasets)
    ancho = build_formato_ancho(resultado)

    assert "h1" in ancho.columns
    assert "linked_h1_1" in ancho.columns
    fila = ancho.iloc[0]
    assert fila["h1"] != ""
    assert fila["linked_h1_1"] != ""


# ---------------------------------------------------------------------------
# build_formato_it: mismo formato exacto que el documento que el equipo pasó
# a IT en marzo 2025 (hoja "Info a IT"), pero generado desde la propuesta
# actual en vez de copiarlo/pegarlo a mano.
# ---------------------------------------------------------------------------


def _make_datasets_con_urls_normalizadas() -> InputDatasets:
    """A diferencia de `_make_datasets_con_urls_realistas` (URLs con
    'https://www.' incluido a propósito, tal cual las usan los tests que
    comparten esa fixture), aquí las URLs van SIN protocolo/www -- tal y
    como quedan de verdad tras pasar por `normalize_url` en el flujo real
    (carga -> `build_master_table`). `build_formato_it` siempre antepone
    'https://www.' al mostrar la URL (iguial que el documento de IT de
    marzo 2025), así que su fixture de pruebas debe partir de una URL ya
    normalizada o el resultado sale duplicado.
    """
    urls = ["sklum.com/es/524-comprar-mesas-de-salon", "sklum.com/es/901-comprar-sillas-de-comedor"]
    crawl = pd.DataFrame({"url": urls, "num_productos": [50, 40]})
    volumen = pd.DataFrame({"url": urls, "keyword": ["kw1", "kw2"], "volumen": [500, 300]})
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": ["Muebles", "Muebles"],
            "categoria_secundaria": ["Salon", "Comedor"],
        }
    )
    enlaces = pd.DataFrame(columns=["source_url", "destination_url", "anchor_text", "zona"])
    return InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)


def test_build_formato_it_genera_la_sentencia_sql_esperada():
    datasets = _make_datasets_con_urls_normalizadas()
    resultado = generate_link_proposals(datasets)

    formato_it = build_formato_it(resultado)

    assert list(formato_it.columns) == [
        "ID CAT MAIN",
        "URL",
        "Identificadores de las categorías, lista 2",
        "UPDATE",
    ]
    assert len(formato_it) == 2

    fila_524 = formato_it[formato_it["ID CAT MAIN"] == 524].iloc[0]
    assert fila_524["URL"] == "https://www.sklum.com/es/524-comprar-mesas-de-salon"
    assert fila_524["Identificadores de las categorías, lista 2"] == "901"
    assert fila_524["UPDATE"] == (
        "UPDATE led_category_shop SET `id_list_two`='901' WHERE  "
        f"`id_category`=524 AND `id_shop`in ({IT_SHOPS_DEFAULT});"
    )


def test_build_formato_it_refleja_el_numero_real_de_enlaces_no_siempre_5():
    """A diferencia del documento de marzo 2025 (siempre 5 IDs por fila),
    aquí la lista tiene tantos IDs como enlaces se hayan seleccionado de
    verdad -- mismo escenario que
    `test_rescate_de_minimo_extremo_a_extremo_evita_categorias_con_un_solo_enlace`
    (12 orígenes compitiendo por solo 2 destinos "buenos", sin fillers),
    pero con URLs que llevan un ID numérico para poder pasar por
    `build_formato_it`."""
    buenos = ["sklum.com/es/900-bueno-1", "sklum.com/es/901-bueno-2"]
    origenes = [f"sklum.com/es/{100 + i}-origen-{i}" for i in range(1, 13)]
    urls = origenes + buenos
    crawl = pd.DataFrame({"url": urls, "num_productos": [10] * len(urls)})
    volumen = pd.DataFrame(
        {
            "url": urls,
            "keyword": [f"kw_{u}" for u in urls],
            "volumen": [0] * len(origenes) + [1000, 990],
        }
    )
    taxonomia = pd.DataFrame(
        {
            "url": urls,
            "categoria_principal": ["Muebles"] * len(urls),
            "categoria_secundaria": [""] * len(urls),
        }
    )
    enlaces = pd.DataFrame(columns=["source_url", "destination_url", "anchor_text", "zona"])
    datasets = InputDatasets(crawl=crawl, enlaces=enlaces, volumen=volumen, taxonomia=taxonomia)

    weights = ScoringWeights(
        volumen_busqueda=1.0,
        muchos_productos=0.0,
        pocos_enlaces_entrantes=0.0,
        afinidad_categoria=0.0,
    )
    # Cupo por destino muy ajustado (1) a propósito: con 14 nodos
    # queriendo 5 enlaces cada uno, la escasez real de huecos fuerza a
    # que varios se queden por debajo de 5 (nunca por debajo del mínimo
    # garantizado), en vez de que todos lleguen a 5 usándose unos a otros
    # de "relleno" sin más.
    limites = LimitesPropuesta(max_enlaces_nuevos_por_destino=1)
    resultado = generate_link_proposals(datasets, weights=weights, limites=limites)
    formato_it = build_formato_it(resultado)

    conteos = formato_it["Identificadores de las categorías, lista 2"].str.split(",").apply(len)
    assert (conteos >= _MINIMO_SI_HAY_CANDIDATOS_DE_SOBRA).all(), conteos.tolist()
    assert (conteos < 5).any(), "se esperaba que al menos alguna fila tuviera menos de 5 (no todas iguales)"


def test_build_formato_it_acepta_una_lista_de_tiendas_distinta():
    datasets = _make_datasets_con_urls_normalizadas()
    resultado = generate_link_proposals(datasets)

    formato_it = build_formato_it(resultado, shops="11,15")
    assert "`id_shop`in (11,15);" in formato_it.iloc[0]["UPDATE"]


def test_build_formato_it_sin_propuesta_devuelve_tabla_vacia_con_columnas():
    vacio = pd.DataFrame(columns=["categoria_origen", "categoria_destino", "score", "seleccionada"])
    formato_it = build_formato_it(vacio)
    assert formato_it.empty
    assert list(formato_it.columns) == [
        "ID CAT MAIN",
        "URL",
        "Identificadores de las categorías, lista 2",
        "UPDATE",
    ]

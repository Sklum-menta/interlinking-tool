import pandas as pd

from core.config import AffinityScores, LimitesPropuesta, ScoringWeights
from core.data_loader import InputDatasets
from core.scoring import affinity_score, build_master_table, generate_link_proposals


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
        volumen_busqueda=1.0, pocos_productos=0.0, pocos_enlaces_entrantes=0.0, afinidad_categoria=0.0
    )
    resultado = generate_link_proposals(datasets, weights=weights)

    desde_c = resultado[
        (resultado["categoria_origen"] == "c") & (~resultado["pendiente_confirmar"])
    ].sort_values("score", ascending=False)

    # Con peso 100% en volumen, "b" (volumen 8000) debe ir antes que "a" (volumen 1000)
    orden_destinos = desde_c["categoria_destino"].tolist()
    assert orden_destinos.index("b") < orden_destinos.index("a")


def test_generate_link_proposals_respeta_limite_por_origen():
    datasets = _make_datasets()
    limites = LimitesPropuesta(max_enlaces_nuevos_por_origen=1, score_minimo=0.0)
    resultado = generate_link_proposals(datasets, limites=limites)

    for origen, grupo in resultado.groupby("categoria_origen"):
        assert grupo["seleccionada"].sum() <= 1

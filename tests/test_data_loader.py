"""Tests del reconocimiento de columnas del crawl en `core.data_loader`,
centrados en la columna OPCIONAL de profundidad/nivel de rastreo añadida
para alimentar el criterio de "autoridad interna estructural" de
`core.scoring._elegibilidad_ampliacion_origen` (ver ese módulo de tests
para la lógica de negocio). No existía hasta ahora un fichero de tests
dedicado a `data_loader`, así que este cubre solo la pieza nueva —no
pretende ser una suite completa del loader.
"""
import io

import pandas as pd

from core.data_loader import _extract_crawl, load_crawl_productos


def _csv_file(df: pd.DataFrame):
    buf = io.BytesIO(df.to_csv(index=False).encode("utf-8"))
    buf.name = "crawl.csv"
    return buf


def test_profundidad_se_reconoce_con_nombre_screaming_frog():
    df = pd.DataFrame(
        {
            "URL": ["a", "b"],
            "Nº_Productos": [10, 20],
            "Crawl Depth": [1, 3],
        }
    )
    crawl = _extract_crawl(df)
    assert "profundidad" in crawl.columns
    assert list(crawl["profundidad"]) == [1, 3]


def test_profundidad_se_reconoce_con_nombre_en_espanol():
    df = pd.DataFrame(
        {
            "URL": ["a", "b"],
            "Nº_Productos": [10, 20],
            "Nivel": [2, 4],
        }
    )
    crawl = _extract_crawl(df)
    assert list(crawl["profundidad"]) == [2, 4]


def test_profundidad_admite_texto_con_el_numero_mezclado():
    df = pd.DataFrame(
        {
            "URL": ["a", "b"],
            "Nº_Productos": [10, 20],
            "Nivel": ["Nivel 2", "Profundidad: 5"],
        }
    )
    crawl = _extract_crawl(df)
    assert list(crawl["profundidad"]) == [2, 5]


def test_profundidad_ausente_no_rompe_nada():
    """Si el crawl no trae ninguna columna de profundidad (el caso normal
    hoy), la columna sigue existiendo pero vacía (NaN) — nunca un error.
    """
    df = pd.DataFrame({"URL": ["a", "b"], "Nº_Productos": [10, 20]})
    crawl = _extract_crawl(df)
    assert "profundidad" in crawl.columns
    assert crawl["profundidad"].isna().all()


def test_load_crawl_productos_desde_csv_con_profundidad():
    df = pd.DataFrame(
        {
            "URL": ["https://a.com/x", "https://a.com/y"],
            "Nº_Productos": [10, 20],
            "Nivel": [1, 2],
        }
    )
    crawl = load_crawl_productos(_csv_file(df))
    assert list(crawl["profundidad"]) == [1, 2]

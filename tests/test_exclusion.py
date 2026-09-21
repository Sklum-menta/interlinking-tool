import pandas as pd

from core.data_loader import load_enlaces, normalize_url
from core.scoring import exclude_existing_links


def test_exclude_existing_links_quita_pares_ya_enlazados():
    candidates = pd.DataFrame(
        {
            "origen": ["a", "a", "b"],
            "destino": ["b", "c", "c"],
        }
    )
    enlaces = pd.DataFrame(
        {
            "source_url": ["a"],
            "destination_url": ["b"],
            "anchor_text": [""],
            "zona": ["Content"],
        }
    )

    resultado = exclude_existing_links(candidates, enlaces)

    assert list(resultado.itertuples(index=False)) == [("a", "c"), ("b", "c")]


def test_exclude_existing_links_no_bloquea_la_direccion_contraria():
    # Que exista un enlace b -> a no debe impedir proponer a -> b.
    candidates = pd.DataFrame({"origen": ["a"], "destino": ["b"]})
    enlaces = pd.DataFrame(
        {
            "source_url": ["b"],
            "destination_url": ["a"],
            "anchor_text": [""],
            "zona": ["Content"],
        }
    )

    resultado = exclude_existing_links(candidates, enlaces)

    assert len(resultado) == 1
    assert resultado.iloc[0]["origen"] == "a"
    assert resultado.iloc[0]["destino"] == "b"


def test_exclude_existing_links_quita_auto_enlaces():
    candidates = pd.DataFrame({"origen": ["a", "a"], "destino": ["a", "b"]})
    enlaces = pd.DataFrame(columns=["source_url", "destination_url", "anchor_text", "zona"])

    resultado = exclude_existing_links(candidates, enlaces)

    assert list(resultado.itertuples(index=False)) == [("a", "b")]


def test_load_enlaces_formato_long_normaliza_urls_y_excluye_pares_existentes():
    df = pd.DataFrame(
        {
            "Source": ["https://www.ejemplo.com/a/"],
            "Destination": ["HTTPS://WWW.EJEMPLO.COM/B"],
            "Anchor": ["ver b"],
            "Link Position": ["Content"],
        }
    )
    enlaces = load_enlaces(_as_named_csv(df, "outlinks.csv"))

    assert enlaces.iloc[0]["source_url"] == normalize_url("https://www.ejemplo.com/a/")
    assert enlaces.iloc[0]["destination_url"] == normalize_url("https://www.ejemplo.com/b")


def test_load_enlaces_formato_wide_por_zona():
    df = pd.DataFrame(
        {
            "URL": ["https://www.ejemplo.com/origen"],
            "Enlaces_Breadcrumb": ["https://www.ejemplo.com/destino1|https://www.ejemplo.com/destino2"],
            "Enlaces_Footer": ["https://www.ejemplo.com/destino3"],
        }
    )
    enlaces = load_enlaces(_as_named_csv(df, "wide.csv"))

    destinos = set(enlaces["destination_url"])
    assert destinos == {
        normalize_url("https://www.ejemplo.com/destino1"),
        normalize_url("https://www.ejemplo.com/destino2"),
        normalize_url("https://www.ejemplo.com/destino3"),
    }
    zonas = set(enlaces["zona"])
    assert zonas == {"breadcrumb", "footer"}


def _as_named_csv(df: pd.DataFrame, filename: str):
    import io

    buffer = io.BytesIO(df.to_csv(index=False).encode("utf-8"))
    buffer.name = filename  # type: ignore[attr-defined]
    return buffer

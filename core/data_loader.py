"""Carga y normalización de los 3 datasets de entrada.

Contratos de columnas soportados (ver README.md para el detalle completo
con ejemplos). Los nombres de columna se reconocen de forma flexible
(case-insensitive y sin acentos), así que no hace falta que el fichero
tenga exactamente estos nombres, basta con que sean reconocibles.

1) Crawl / nº de productos — una fila por URL:
   URL, Nº_Productos

2) Enlaces existentes — se aceptan DOS formatos, autodetectados:

   a) Formato "long" (recomendado, tipo export "All Outlinks" de
      Screaming Frog): una fila por enlace.
      Source, Destination, Anchor, Link Position

   b) Formato "wide": una fila por URL origen, con una columna por cada
      zona (breadcrumb, contenido/texto, footer...) que contiene la
      lista de URLs de destino enlazadas desde esa zona, separadas por
      "|", ";" o ",".
      URL, Enlaces_Breadcrumb, Enlaces_Texto, Enlaces_Footer

3) Volumen de búsqueda — una fila por URL:
   URL, Keyword, Volumen

4) Taxonomía — una fila por URL:
   URL, Categoria_Principal, Categoria_Secundaria

Todos los ficheros pueden ser CSV (con separador coma o punto y coma,
se autodetecta) o Excel (.xlsx).
"""
from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence

import pandas as pd

# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------


class DataLoadError(ValueError):
    """Error de negocio al leer o interpretar un fichero de entrada."""


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _clean_key(text: str) -> str:
    text = _strip_accents(str(text)).lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def normalize_url(url: str | float | None) -> str:
    """Normaliza una URL para poder cruzarla entre los 3 ficheros:
    quita espacios, fuerza minúsculas en esquema/host, quita "www.",
    fuerza https como esquema canónico y elimina la barra final.

    No pretende ser un normalizador RFC-completo: es deliberadamente
    simple y predecible, pensado para URLs de categorías de un
    e-commerce (sin querystring relevante ni fragmentos).
    """
    if url is None or (isinstance(url, float) and pd.isna(url)):
        return ""
    text = str(url).strip()
    if not text:
        return ""
    text = text.split("#", 1)[0]
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^www\.", "", text, flags=re.IGNORECASE)
    text = text.rstrip("/")
    return text.lower()


def _find_column(
    df: pd.DataFrame,
    candidates: Sequence[str],
    *,
    required: bool = True,
    label: str = "columna",
) -> str | None:
    """Busca en df.columns una columna cuyo nombre "limpio" coincida con
    alguno de los candidatos (también limpios). Devuelve el nombre
    ORIGINAL de la columna en df.
    """
    cleaned_map = {_clean_key(col): col for col in df.columns}
    for candidate in candidates:
        key = _clean_key(candidate)
        if key in cleaned_map:
            return cleaned_map[key]
    # Segunda pasada: coincidencia parcial (contiene al candidato)
    for candidate in candidates:
        key = _clean_key(candidate)
        for cleaned, original in cleaned_map.items():
            if key and key in cleaned:
                return original
    if required:
        raise DataLoadError(
            f"No se ha encontrado la {label}. Se esperaba alguna de estas "
            f"columnas: {', '.join(candidates)}. Columnas disponibles en el "
            f"fichero: {', '.join(map(str, df.columns))}"
        )
    return None


def _read_any(file, *, sheet_name=0) -> pd.DataFrame:
    """Lee un CSV (con autodetección de separador y encoding) o un
    Excel a partir de un path o de un objeto tipo fichero (por ejemplo
    lo que devuelve st.file_uploader).
    """
    name = getattr(file, "name", None) or (file if isinstance(file, str) else "")
    is_excel = str(name).lower().endswith((".xlsx", ".xls"))

    if is_excel:
        return pd.read_excel(file, sheet_name=sheet_name)

    # CSV: si es un objeto tipo fichero (uploader de Streamlit) leemos los
    # bytes una vez para poder reintentar con distintos encodings.
    if hasattr(file, "read"):
        raw = file.read()
        if hasattr(file, "seek"):
            file.seek(0)
    else:
        with open(file, "rb") as fh:
            raw = fh.read()

    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
            return pd.read_csv(io.StringIO(text), sep=None, engine="python")
        except Exception as exc:  # noqa: BLE001 - queremos probar varios encodings
            last_error = exc
            continue
    raise DataLoadError(f"No se ha podido leer el CSV '{name}': {last_error}")


# ---------------------------------------------------------------------------
# 1) Crawl / nº de productos
# ---------------------------------------------------------------------------

URL_CANDIDATES = ["URL", "Address", "Direccion", "Page", "Pagina"]
PRODUCTOS_CANDIDATES = [
    "Nº_Productos",
    "N_Productos",
    "Numero_Productos",
    "Num_Productos",
    "Productos",
    "Product_Count",
    "Products",
    "Nº Productos XPath",
]


def load_crawl_productos(file) -> pd.DataFrame:
    df = _read_any(file)
    col_url = _find_column(df, URL_CANDIDATES, label="columna de URL (crawl)")
    col_productos = _find_column(
        df, PRODUCTOS_CANDIDATES, label="columna de nº de productos"
    )
    out = pd.DataFrame(
        {
            "url": df[col_url].map(normalize_url),
            "num_productos": pd.to_numeric(
                df[col_productos].astype(str).str.replace(r"[.\s]", "", regex=True).str.replace(",", "."),
                errors="coerce",
            ),
        }
    )
    out = out[out["url"] != ""].drop_duplicates(subset="url", keep="first")
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2) Enlaces existentes (formato long o wide, autodetectado)
# ---------------------------------------------------------------------------

SOURCE_CANDIDATES = ["Source", "Origen", "URL_Origen", "Source URL", "Address"]
DESTINATION_CANDIDATES = ["Destination", "Destino", "URL_Destino", "Destination URL"]
ANCHOR_CANDIDATES = ["Anchor", "Anchor Text", "Texto_Ancla", "Anchor_Text", "Alt Text"]
ZONA_CANDIDATES = ["Link Position", "Zona", "Position", "Link_Position", "Ubicacion", "Type"]

# Nombres de columna "wide" que representan una zona de la página con una
# lista de URLs de destino dentro.
WIDE_ZONE_KEYWORDS = {
    "breadcrumb": "breadcrumb",
    "migas": "breadcrumb",
    "footer": "footer",
    "pie": "footer",
    "texto": "contenido",
    "contenido": "contenido",
    "body": "contenido",
    "sidebar": "sidebar",
    "menu": "menu",
    "navegacion": "menu",
    "navigation": "menu",
    "header": "header",
    "cabecera": "header",
}

_SPLIT_RE = re.compile(r"[|;\n]+")


def _looks_like_long_format(df: pd.DataFrame) -> bool:
    has_source = _find_column(df, SOURCE_CANDIDATES, required=False) is not None
    has_destination = _find_column(df, DESTINATION_CANDIDATES, required=False) is not None
    return has_source and has_destination


def _wide_zone_columns(df: pd.DataFrame) -> dict[str, str]:
    """Devuelve {nombre_columna_original: zona_normalizada} para las
    columnas que parecen contener listas de URLs por zona.
    """
    found: dict[str, str] = {}
    for col in df.columns:
        cleaned = _clean_key(col)
        for keyword, zona in WIDE_ZONE_KEYWORDS.items():
            if keyword in cleaned:
                found[col] = zona
                break
    return found


def _load_enlaces_long(df: pd.DataFrame) -> pd.DataFrame:
    col_source = _find_column(df, SOURCE_CANDIDATES, label="columna de URL origen")
    col_dest = _find_column(df, DESTINATION_CANDIDATES, label="columna de URL destino")
    col_anchor = _find_column(df, ANCHOR_CANDIDATES, required=False, label="texto ancla")
    col_zona = _find_column(df, ZONA_CANDIDATES, required=False, label="zona del enlace")

    out = pd.DataFrame(
        {
            "source_url": df[col_source].map(normalize_url),
            "destination_url": df[col_dest].map(normalize_url),
            "anchor_text": df[col_anchor] if col_anchor else "",
            "zona": df[col_zona].astype(str) if col_zona else "desconocida",
        }
    )
    return out


def _load_enlaces_wide(df: pd.DataFrame, zone_columns: dict[str, str]) -> pd.DataFrame:
    col_url = _find_column(df, URL_CANDIDATES, label="columna de URL origen (formato wide)")
    rows: list[dict] = []
    for _, row in df.iterrows():
        source = normalize_url(row[col_url])
        if not source:
            continue
        for col, zona in zone_columns.items():
            raw_value = row.get(col)
            if raw_value is None or (isinstance(raw_value, float) and pd.isna(raw_value)):
                continue
            for dest_raw in _SPLIT_RE.split(str(raw_value)):
                dest = normalize_url(dest_raw)
                if dest:
                    rows.append(
                        {
                            "source_url": source,
                            "destination_url": dest,
                            "anchor_text": "",
                            "zona": zona,
                        }
                    )
    return pd.DataFrame(rows, columns=["source_url", "destination_url", "anchor_text", "zona"])


def load_enlaces(file) -> pd.DataFrame:
    """Carga el export de enlaces existentes y lo normaliza SIEMPRE al
    formato canónico: source_url, destination_url, anchor_text, zona.
    Detecta automáticamente si el fichero viene en formato long (una
    fila por enlace, con columnas Source/Destination) o wide (una fila
    por URL origen, con una columna por zona conteniendo la lista de
    URLs de destino).
    """
    df = _read_any(file)

    if _looks_like_long_format(df):
        out = _load_enlaces_long(df)
    else:
        zone_columns = _wide_zone_columns(df)
        if not zone_columns:
            raise DataLoadError(
                "No se ha podido interpretar el fichero de enlaces existentes: "
                "no tiene columnas Source/Destination (formato long) ni columnas "
                "reconocibles por zona como 'Breadcrumb', 'Texto' o 'Footer' "
                "(formato wide). Revisa el README para ver los formatos soportados."
            )
        out = _load_enlaces_wide(df, zone_columns)

    out = out[(out["source_url"] != "") & (out["destination_url"] != "")]
    out = out[out["source_url"] != out["destination_url"]]
    return out.drop_duplicates().reset_index(drop=True)


# ---------------------------------------------------------------------------
# 3) Volumen de búsqueda
# ---------------------------------------------------------------------------

KEYWORD_CANDIDATES = ["Keyword", "Palabra_Clave", "Termino", "Query"]
VOLUMEN_CANDIDATES = ["Volumen", "Volume", "Search_Volume", "Avg_Monthly_Searches", "Busquedas"]


def load_volumen(file) -> pd.DataFrame:
    df = _read_any(file)
    col_url = _find_column(df, URL_CANDIDATES, label="columna de URL (volumen)")
    col_keyword = _find_column(df, KEYWORD_CANDIDATES, label="columna de keyword")
    col_volumen = _find_column(df, VOLUMEN_CANDIDATES, label="columna de volumen de búsqueda")

    volumen_raw = df[col_volumen].astype(str).str.replace(r"[.\s]", "", regex=True).str.replace(",", ".")
    out = pd.DataFrame(
        {
            "url": df[col_url].map(normalize_url),
            "keyword": df[col_keyword].astype(str).str.strip(),
            "volumen": pd.to_numeric(volumen_raw, errors="coerce"),
        }
    )
    out = out[out["url"] != ""]
    # Si hay varias keywords por URL, nos quedamos con la de mayor volumen
    # (se considera la "keyword principal" de facto).
    out = out.sort_values("volumen", ascending=False).drop_duplicates(subset="url", keep="first")
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 4) Taxonomía
# ---------------------------------------------------------------------------

CATEGORIA_PRINCIPAL_CANDIDATES = ["Categoria_Principal", "Categoria Principal", "Main_Category", "Categoria1"]
CATEGORIA_SECUNDARIA_CANDIDATES = ["Categoria_Secundaria", "Categoria Secundaria", "Sub_Category", "Categoria2"]


def load_taxonomia(file) -> pd.DataFrame:
    df = _read_any(file)
    col_url = _find_column(df, URL_CANDIDATES, label="columna de URL (taxonomía)")
    col_principal = _find_column(
        df, CATEGORIA_PRINCIPAL_CANDIDATES, label="columna de categoría principal"
    )
    col_secundaria = _find_column(
        df, CATEGORIA_SECUNDARIA_CANDIDATES, required=False, label="columna de categoría secundaria"
    )

    out = pd.DataFrame(
        {
            "url": df[col_url].map(normalize_url),
            "categoria_principal": df[col_principal].astype(str).str.strip(),
            "categoria_secundaria": (
                df[col_secundaria].astype(str).str.strip() if col_secundaria else ""
            ),
        }
    )
    out = out[out["url"] != ""].drop_duplicates(subset="url", keep="first")
    return out.reset_index(drop=True)


@dataclass
class InputDatasets:
    """Contenedor de los 3 (o 4, si los enlaces vienen aparte) datasets
    ya cargados y normalizados, listo para pasar a `core.scoring`.
    """

    crawl: pd.DataFrame
    enlaces: pd.DataFrame
    volumen: pd.DataFrame
    taxonomia: pd.DataFrame

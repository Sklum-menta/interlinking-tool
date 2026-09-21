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

5) Search Console (opcional) — una fila por URL:
   URL, Clics_28d, Impresiones_28d, Posicion_Media (esta última opcional
   dentro del propio fichero).

6) Plantilla unificada (recomendada, ver `load_plantilla_unificada`) —
   los 4 datasets base en UN solo fichero, una fila por URL:
   URL, Nº_Productos, Status_Code, Indexable (estas dos últimas
   opcionales), Categoria_Principal, Categoria_Secundaria,
   Keyword_1, Volumen, Enlace_Bolita_1..N, Enlace_Breadcrumb_1..N.

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


def _parse_numeric_es(raw: pd.Series) -> pd.Series:
    """Convierte una columna a numérico, sin asumir a ciegas que es
    texto con formato es-ES (punto de miles, coma decimal).

    Cuando el fichero es un .xlsx, pandas ya lee las columnas numéricas
    con su dtype real (int64/float64) — en ese caso se usan tal cual.
    Solo cuando la columna es texto (típico en un CSV, o en un Excel
    donde el número viene como texto) se limpia asumiendo separador de
    miles "." y decimal ",".

    Esto es importante: convertir primero a texto con `astype(str)` un
    valor numérico real como 550000.0 da la cadena "550000.0", y si
    luego se le quita el punto asumiendo que es un separador de miles,
    el resultado es 5500000 — ¡multiplicado por 10! Por eso hay que
    mirar el dtype ANTES de tocar nada.
    """
    if pd.api.types.is_numeric_dtype(raw):
        return pd.to_numeric(raw, errors="coerce")
    text = raw.astype(str).str.strip()
    return pd.to_numeric(text.map(_clean_es_number_text), errors="coerce")


_TRAILING_DECIMAL_RE = re.compile(r"^-?\d+\.\d{1,2}$")


def _clean_es_number_text(value: str) -> str:
    """Limpia un número dado como texto, sin asumir siempre que el punto
    es separador de miles.

    Una agrupación de miles válida en es-ES siempre tiene exactamente 3
    dígitos en el último grupo ("1.234", "1.234.567"). Si el texto tiene
    un solo punto seguido de 1 o 2 dígitos ("550000.0", "37.5"), ese
    punto no puede ser de miles: es un separador decimal (típico cuando
    un número que ya era float, p.ej. al pasar por una hoja de cálculo o
    un script, se serializa como texto). En ese caso se deja tal cual;
    en cualquier otro caso se asume el formato es-ES habitual (punto de
    miles, coma decimal).
    """
    value = value.strip()
    if _TRAILING_DECIMAL_RE.match(value):
        return value
    return re.sub(r"[.\s]", "", value).replace(",", ".")


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

# En los ficheros "maestros" multi-mercado de Sklum (taxonomía, keyword
# research...) la columna de URL no se llama "URL": se llama con el
# código del país/idioma de esa URL (p.ej. "ES" para España, que es la
# que se usa siempre para interlinking). Solo se aceptan por
# coincidencia EXACTA del nombre de columna (nunca por substring, a
# diferencia del resto de columnas) para no confundir un código de dos
# letras con cualquier otra columna que casualmente lo contenga.
LOCALE_URL_CANDIDATES = ["ES", "FR", "IT", "PT", "EN", "DE", "NL", "PL", "UK", "IE"]


def _find_locale_url_column(df: pd.DataFrame) -> str | None:
    cleaned_map = {_clean_key(col): col for col in df.columns}
    for candidate in LOCALE_URL_CANDIDATES:
        key = _clean_key(candidate)
        if key in cleaned_map:
            return cleaned_map[key]
    return None


def _find_url_column(df: pd.DataFrame, *, label: str = "columna de URL") -> str:
    """Como `_find_column(df, URL_CANDIDATES, ...)`, pero si no encuentra
    nada intenta también una columna con nombre de código de país/idioma
    (ver `LOCALE_URL_CANDIDATES`) antes de rendirse.
    """
    col = _find_column(df, URL_CANDIDATES, required=False)
    if col is not None:
        return col
    col = _find_locale_url_column(df)
    if col is not None:
        return col
    raise DataLoadError(
        f"No se ha encontrado la {label}. Se esperaba alguna de estas "
        f"columnas: {', '.join(URL_CANDIDATES)}; o, en ficheros "
        f"multi-mercado, una columna con el código de país/idioma "
        f"(p.ej. {', '.join(LOCALE_URL_CANDIDATES[:3])}...). Columnas "
        f"disponibles en el fichero: {', '.join(map(str, df.columns))}"
    )


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


_NUM_PRODUCTOS_DE_RE = re.compile(r"de\s+([\d.,]+)\s*$", re.IGNORECASE)


def _parse_num_productos(raw: pd.Series) -> pd.Series:
    """Convierte la columna de nº de productos a numérico.

    Soporta dos formatos reales observados:
    - Un número "limpio" (bien como dtype numérico real al leer un
      .xlsx, bien como texto con posible separador de miles con punto y
      decimales con coma, estilo es-ES: "3.978" -> 3978).
    - El texto que genera una extracción por XPath de un paginador tipo
      Screaming Frog cuando el XPath captura el texto completo del
      contador, p.ej. "Has visto 50 productos de 66": el nº real de
      productos de la categoría es el que va después de "de" al final
      de la cadena (66), no el "50" (productos mostrados en esa
      página).
    """
    if pd.api.types.is_numeric_dtype(raw):
        # Si la columna ya es numérica no puede contener el texto del
        # paginador, así que no hay nada más que limpiar.
        return pd.to_numeric(raw, errors="coerce")

    text = raw.astype(str).str.strip()

    directo = pd.to_numeric(text.map(_clean_es_number_text), errors="coerce")

    de_match = text.str.extract(_NUM_PRODUCTOS_DE_RE)[0]
    de_valor = pd.to_numeric(
        de_match.map(lambda v: _clean_es_number_text(v) if isinstance(v, str) else v),
        errors="coerce",
    )

    return directo.where(directo.notna(), de_valor)


STATUS_CODE_CANDIDATES = ["Status_Code", "Status Code", "Codigo_Estado", "HTTP_Status", "Codigo Estado"]
INDEXABLE_CANDIDATES = ["Indexable", "Indexability", "Indexabilidad"]

# Valores de la columna "Indexability" que exporta Screaming Frog de
# forma nativa: solo "Indexable" cuenta como indexable; cualquier otro
# valor ("Non-Indexable", "Canonicalised", "Redirected", "Blocked by
# robots.txt"...) se trata como no indexable.
_INDEXABLE_TRUE_VALUES = {"indexable", "si", "sí", "true", "1", "yes"}
_INDEXABLE_FALSE_VALUES = {"non-indexable", "no indexable", "no", "false", "0"}


def _parse_indexable(raw: pd.Series) -> pd.Series:
    """Convierte la columna de indexabilidad a booleano. Un valor vacío
    o no reconocido se deja como NaN (no se asume nada) en vez de
    forzarlo a True/False, para no excluir por error URLs cuyo dato de
    indexabilidad viene en un formato no previsto.
    """

    def _parse_one(value):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        text = str(value).strip().lower()
        if text in _INDEXABLE_TRUE_VALUES:
            return True
        if text in _INDEXABLE_FALSE_VALUES:
            return False
        # Cualquier otro texto no vacío de Screaming Frog (p.ej.
        # "Canonicalised", "Redirected") se considera no indexable: solo
        # "Indexable" a secas cuenta como indexable.
        return False

    return raw.map(_parse_one)


def _extract_crawl(df: pd.DataFrame) -> pd.DataFrame:
    col_url = _find_url_column(df, label="columna de URL (crawl)")
    col_productos = _find_column(
        df, PRODUCTOS_CANDIDATES, label="columna de nº de productos"
    )
    col_status = _find_column(df, STATUS_CODE_CANDIDATES, required=False, label="columna de status code")
    col_indexable = _find_column(df, INDEXABLE_CANDIDATES, required=False, label="columna de indexabilidad")

    out = pd.DataFrame(
        {
            "url": df[col_url].map(normalize_url),
            "num_productos": _parse_num_productos(df[col_productos]),
            "status_code": (
                pd.to_numeric(df[col_status], errors="coerce") if col_status else float("nan")
            ),
            "indexable": _parse_indexable(df[col_indexable]) if col_indexable else None,
        }
    )
    out = out[out["url"] != ""].drop_duplicates(subset="url", keep="first")
    return out.reset_index(drop=True)


def load_crawl_productos(file) -> pd.DataFrame:
    return _extract_crawl(_read_any(file))


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
    "bolita": "bolitas",
}

# Además de contener una palabra de zona, la columna tiene que "sonar" a
# columna de enlaces (contener "url" o "enlace"), para no confundir
# columnas de metadatos del crawl que casualmente contienen palabras como
# "contenido" o "texto" (p.ej. "Tipo de contenido", "Proporción de texto"
# en un export de Screaming Frog) con columnas reales de listas de URLs.
_LOOKS_LIKE_LINK_COLUMN_RE = re.compile(r"url|enlace")

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
        if not _LOOKS_LIKE_LINK_COLUMN_RE.search(cleaned):
            continue
        for keyword, zona in WIDE_ZONE_KEYWORDS.items():
            if keyword in cleaned:
                found[col] = zona
                break
    return found


def _resolve_url(raw: str | float | None, *, base_domain: str = "") -> str:
    """Como `normalize_url`, pero si el valor es una ruta relativa (empieza
    por "/", sin esquema ni dominio) la resuelve primero contra
    `base_domain` (el dominio ya normalizado de la URL origen de esa
    misma fila).

    Esto es necesario porque en exports reales de Screaming Frog no todas
    las columnas de enlaces traen la URL absoluta: por ejemplo, una
    extracción personalizada por XPath que capture solo el atributo
    `href` de unos enlaces de "texto SEO" puede devolver rutas relativas
    como "/es/4057-comprar-aparadores", mientras que otras zonas
    (breadcrumbs, "bolitas"...) sí traen la URL absoluta. Sin esta
    resolución, esas rutas relativas nunca cruzarían con las URLs
    absolutas del crawl y el enlace existente no se detectaría (riesgo de
    proponer un enlace que ya existe).
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    text = str(raw).strip()
    if base_domain and text.startswith("/"):
        text = f"https://{base_domain}{text}"
    return normalize_url(text)


def _load_enlaces_long(df: pd.DataFrame) -> pd.DataFrame:
    col_source = _find_column(df, SOURCE_CANDIDATES, label="columna de URL origen")
    col_dest = _find_column(df, DESTINATION_CANDIDATES, label="columna de URL destino")
    col_anchor = _find_column(df, ANCHOR_CANDIDATES, required=False, label="texto ancla")
    col_zona = _find_column(df, ZONA_CANDIDATES, required=False, label="zona del enlace")

    sources = df[col_source].map(normalize_url)
    base_domains = sources.str.split("/", n=1).str[0]
    destinations = [
        _resolve_url(dest_raw, base_domain=domain)
        for dest_raw, domain in zip(df[col_dest], base_domains)
    ]

    out = pd.DataFrame(
        {
            "source_url": sources,
            "destination_url": destinations,
            "anchor_text": df[col_anchor] if col_anchor else "",
            "zona": df[col_zona].astype(str) if col_zona else "desconocida",
        }
    )
    return out


def _load_enlaces_wide(df: pd.DataFrame, zone_columns: dict[str, str]) -> pd.DataFrame:
    col_url = _find_url_column(df, label="columna de URL origen (formato wide)")
    rows: list[dict] = []
    for _, row in df.iterrows():
        source = normalize_url(row[col_url])
        if not source:
            continue
        base_domain = source.split("/", 1)[0]
        for col, zona in zone_columns.items():
            raw_value = row.get(col)
            if raw_value is None or (isinstance(raw_value, float) and pd.isna(raw_value)):
                continue
            for dest_raw in _SPLIT_RE.split(str(raw_value)):
                dest = _resolve_url(dest_raw, base_domain=base_domain)
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


def _extract_enlaces(df: pd.DataFrame) -> pd.DataFrame:
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


def load_enlaces(file) -> pd.DataFrame:
    """Carga el export de enlaces existentes y lo normaliza SIEMPRE al
    formato canónico: source_url, destination_url, anchor_text, zona.
    Detecta automáticamente si el fichero viene en formato long (una
    fila por enlace, con columnas Source/Destination) o wide (una fila
    por URL origen, con una o varias columnas por zona: bien una lista
    de URLs separadas por "|"/";" en una sola columna, bien varias
    columnas numeradas -Enlace_Bolita_1, Enlace_Bolita_2...- con una URL
    completa cada una, que es el formato que usa la plantilla unificada).
    """
    return _extract_enlaces(_read_any(file))


# ---------------------------------------------------------------------------
# 3) Volumen de búsqueda
# ---------------------------------------------------------------------------

KEYWORD_CANDIDATES = ["Keyword", "Palabra_Clave", "Termino", "Query"]
VOLUMEN_CANDIDATES = ["Volumen", "Volume", "Search_Volume", "Avg_Monthly_Searches", "Busquedas", "SV"]

# El keyword research real de Sklum trae, en una sola fila por URL, hasta
# 14 pares de columnas "Keyword N" + su volumen de búsqueda (normalmente
# repetida como "SV" en las 14, así que al leer el fichero pandas las
# desambigua solas como SV, SV.1, SV.2...). El emparejamiento es
# POSICIONAL: la columna de volumen de cada "Keyword N" es la que va
# justo después en el fichero, sea como se llame.
_KEYWORD_COL_RE = re.compile(r"^keyword_?\d*$")


def _wide_keyword_volume_pairs(df: pd.DataFrame) -> list[tuple[str, str]]:
    columns = list(df.columns)
    pairs: list[tuple[str, str]] = []
    for i, col in enumerate(columns):
        if _KEYWORD_COL_RE.match(_clean_key(col)) and i + 1 < len(columns):
            pairs.append((col, columns[i + 1]))
    return pairs


def _parse_volumen_series(raw: pd.Series) -> pd.Series:
    return _parse_numeric_es(raw)


def _extract_volumen(df: pd.DataFrame) -> pd.DataFrame:
    col_url = _find_url_column(df, label="columna de URL (volumen)")

    keyword_pairs = _wide_keyword_volume_pairs(df)
    if keyword_pairs:
        # Formato "wide" real: una fila por URL, con varios pares
        # Keyword N / volumen (hasta 14 en el keyword research de
        # Sklum). Por decisión explícita del equipo, se usa siempre la
        # "Keyword 1" (la keyword principal ya elegida a mano por el
        # equipo de SEO) y su volumen — no la de mayor SV entre las 14,
        # que podría no ser la keyword que realmente representa la
        # categoría.
        col_keyword, col_volumen = keyword_pairs[0]
        out = pd.DataFrame(
            {
                "url": df[col_url].map(normalize_url),
                "keyword": df[col_keyword].astype(str).str.strip(),
                "volumen": _parse_volumen_series(df[col_volumen]),
            }
        )
    else:
        # Formato "long" simple: una fila por URL+keyword, con una sola
        # columna de keyword y una sola de volumen.
        col_keyword = _find_column(df, KEYWORD_CANDIDATES, label="columna de keyword")
        col_volumen = _find_column(df, VOLUMEN_CANDIDATES, label="columna de volumen de búsqueda")
        out = pd.DataFrame(
            {
                "url": df[col_url].map(normalize_url),
                "keyword": df[col_keyword].astype(str).str.strip(),
                "volumen": _parse_volumen_series(df[col_volumen]),
            }
        )

    out = out[out["url"] != ""]
    # Si hay varias keywords por URL (varias filas, en el formato long), nos
    # quedamos con la de mayor volumen (se considera la "keyword principal"
    # de facto).
    out = out.sort_values("volumen", ascending=False).drop_duplicates(subset="url", keep="first")
    return out.reset_index(drop=True)


def load_volumen(file) -> pd.DataFrame:
    return _extract_volumen(_read_any(file))


# ---------------------------------------------------------------------------
# 4) Taxonomía
# ---------------------------------------------------------------------------

CATEGORIA_PRINCIPAL_CANDIDATES = [
    "Categoria_Principal",
    "Categoria Principal",
    "Main_Category",
    "Categoria1",
    "Categoria",
]
CATEGORIA_SECUNDARIA_CANDIDATES = [
    "Categoria_Secundaria",
    "Categoria Secundaria",
    "Sub_Category",
    "Categoria2",
    "Subcategoria",
]


def _extract_taxonomia(df: pd.DataFrame) -> pd.DataFrame:
    col_url = _find_url_column(df, label="columna de URL (taxonomía)")
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


def load_taxonomia(file) -> pd.DataFrame:
    return _extract_taxonomia(_read_any(file))


# ---------------------------------------------------------------------------
# 5) Search Console (opcional): clics e impresiones de los últimos 28 días
#    y posición media, por URL. Si no se sube este dataset, las señales
#    de "oportunidad SEO" del scoring quedan a 0 (no afectan al orden de
#    la propuesta) — es un dataset opcional, no bloquea la generación.
# ---------------------------------------------------------------------------

SC_URL_CANDIDATES = ["URL", "Address", "Direccion", "Page", "Pagina", "Pagina_mas_frecuente", "Landing Page"]
CLICS_CANDIDATES = ["Clics_28d", "Clics", "Clicks", "Clics 28 dias", "Clicks_28d"]
IMPRESIONES_CANDIDATES = ["Impresiones_28d", "Impresiones", "Impressions", "Impresiones 28 dias"]
POSICION_CANDIDATES = ["Posicion_Media", "Posicion Media", "Position", "Average Position", "Posicion"]


def load_search_console(file) -> pd.DataFrame:
    """Carga el export de Search Console: una fila por URL con clics e
    impresiones de los últimos 28 días y la posición media. Los nombres
    de columna se reconocen de forma flexible, igual que el resto de
    ficheros de la herramienta (incluida la columna de URL con código de
    país/idioma en los ficheros multi-mercado, ver `_find_url_column`).
    """
    df = _read_any(file)
    col_url = _find_url_column(df, label="columna de URL (Search Console)")
    col_clics = _find_column(df, CLICS_CANDIDATES, label="columna de clics")
    col_impresiones = _find_column(df, IMPRESIONES_CANDIDATES, label="columna de impresiones")
    col_posicion = _find_column(df, POSICION_CANDIDATES, required=False, label="columna de posición media")

    out = pd.DataFrame(
        {
            "url": df[col_url].map(normalize_url),
            "clics_28d": _parse_numeric_es(df[col_clics]),
            "impresiones_28d": _parse_numeric_es(df[col_impresiones]),
            "posicion_media": (
                _parse_numeric_es(df[col_posicion]) if col_posicion else float("nan")
            ),
        }
    )
    out = out[out["url"] != ""].drop_duplicates(subset="url", keep="first")
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 6) Ajustes manuales de negocio (opcionales): relevancia por categoría y
#    prioridad de negocio por URL. Se editan normalmente desde la propia
#    interfaz (st.data_editor), pero estos loaders permiten volver a
#    subir una tabla que se guardó un mes anterior.
# ---------------------------------------------------------------------------

RELEVANCIA_CANDIDATES = ["Relevancia", "Puntuacion Asignada", "Puntuacion"]


def load_relevancia_manual(file) -> pd.DataFrame:
    df = _read_any(file)
    col_principal = _find_column(
        df, CATEGORIA_PRINCIPAL_CANDIDATES, label="columna de categoría principal"
    )
    col_secundaria = _find_column(
        df, CATEGORIA_SECUNDARIA_CANDIDATES, required=False, label="columna de categoría secundaria"
    )
    col_relevancia = _find_column(df, RELEVANCIA_CANDIDATES, label="columna de relevancia")

    out = pd.DataFrame(
        {
            "categoria_principal": df[col_principal].astype(str).str.strip(),
            "categoria_secundaria": (
                df[col_secundaria].astype(str).str.strip() if col_secundaria else ""
            ),
            "relevancia": pd.to_numeric(df[col_relevancia], errors="coerce"),
        }
    )
    return out[out["categoria_principal"] != ""].reset_index(drop=True)


PRIORIDAD_CANDIDATES = ["Prioridad_Negocio", "Prioridad de Negocio", "Pri. Negocio", "Prioridad"]


def load_prioridad_negocio(file) -> pd.DataFrame:
    df = _read_any(file)
    col_url = _find_url_column(df, label="columna de URL (prioridad de negocio)")
    col_prioridad = _find_column(df, PRIORIDAD_CANDIDATES, label="columna de prioridad de negocio")

    out = pd.DataFrame(
        {
            "url": df[col_url].map(normalize_url),
            "prioridad_negocio": pd.to_numeric(df[col_prioridad], errors="coerce"),
        }
    )
    return out[out["url"] != ""].reset_index(drop=True)


@dataclass
class InputDatasets:
    """Contenedor de los 3 (o 4, si los enlaces vienen aparte) datasets
    ya cargados y normalizados, listo para pasar a `core.scoring`.
    """

    crawl: pd.DataFrame
    enlaces: pd.DataFrame
    volumen: pd.DataFrame
    taxonomia: pd.DataFrame


# ---------------------------------------------------------------------------
# 7) Plantilla unificada: los 4 datasets base en UN solo fichero (una fila
#    por URL), pensada para poder copiar y pegar los datos del rastreo
#    directamente y subir un único Excel/CSV a la herramienta en vez de
#    los 4 ficheros por separado. Ver plantilla en
#    sample_data/plantilla_unificada.xlsx y el README (sección 2.6). El
#    dataset de Search Console (opcional) sigue siendo un fichero aparte,
#    ver `load_search_console`.
# ---------------------------------------------------------------------------


def load_plantilla_unificada(file) -> InputDatasets:
    """Carga los 4 datasets base (crawl, enlaces, volumen, taxonomía) a
    partir de un único fichero con una fila por URL, con estas columnas
    (nombres reconocidos de forma flexible, igual que en el resto de la
    herramienta):

    - `URL`
    - `Nº_Productos` (admite también el texto "Has visto 50 productos de 66")
    - `Status_Code`, `Indexable` (opcionales — si el crawl las trae, se
      excluyen automáticamente como destino las URLs con status distinto
      de 200 o no indexables; ver `_parse_indexable`)
    - `Categoria_Principal`, `Categoria_Secundaria`
    - `Keyword_1`, `Volumen` (volumen de búsqueda de esa keyword)
    - `Enlace_Bolita_1`, `Enlace_Bolita_2`, ... (tantas columnas como
      enlaces existentes tenga la zona de "categorías relacionadas"/bolitas,
      cada una con la URL completa de destino, o vacía si no aplica)
    - `Enlace_Breadcrumb_1`, `Enlace_Breadcrumb_2`, ... (igual, para los
      enlaces del breadcrumb)

    Internamente reutiliza exactamente la misma lógica de reconocimiento
    de columnas y de zonas que los 4 loaders individuales (`load_crawl_productos`,
    `load_enlaces`, `load_volumen`, `load_taxonomia`): el fichero se lee
    una sola vez y cada dataset se extrae de las columnas que le
    corresponden, así que columnas de más (u otro orden) no dan problema.
    """
    df = _read_any(file)
    return InputDatasets(
        crawl=_extract_crawl(df),
        enlaces=_extract_enlaces(df),
        volumen=_extract_volumen(df),
        taxonomia=_extract_taxonomia(df),
    )

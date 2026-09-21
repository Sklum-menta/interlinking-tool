# Herramienta de Interlinking SEO — Sklum

Sustituye el flujo manual de Google Sheets + Apps Script por una app
web interna (Streamlit) que genera propuestas de interlinking entre
categorías, con login restringido y lista para desplegar en Streamlit
Community Cloud.

## 1. Instalación local

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# Edita .streamlit/secrets.toml: como mínimo pon
# INTERLINKING_AUTH_MODE = "password" y una INTERLINKING_SHARED_PASSWORD
# para probar sin configurar todavía OAuth de Google.

streamlit run app.py
```

Con `INTERLINKING_AUTH_MODE = "password"` puedes entrar directamente con
la contraseña compartida que hayas definido, sin depender de tener ya
creada la app de OAuth en Google Cloud. Es el modo recomendado para
desarrollo; en producción usa `"google"`.

Para probar la herramienta sin datos reales de Sklum, sube los 3
ficheros de `sample_data/`:

- `sample_data/crawl_productos.csv`
- `sample_data/enlaces_existentes.csv`
- `sample_data/volumen_keywords.csv`
- `sample_data/taxonomia.csv`

## 2. Formato de los ficheros de entrada

Los nombres de columna se reconocen de forma flexible (mayúsculas/minúsculas,
con o sin acentos, nombres en inglés o español razonablemente parecidos), pero
**la información que tienen que contener es la siguiente**:

### 2.1. Crawl — nº de productos por categoría

Una fila por URL indexable.

| Columna | Descripción |
|---|---|
| `URL` | URL de la categoría |
| `Nº_Productos` | Nº de productos de la categoría (el que hoy extraéis por XPath) |

```csv
URL,Nº_Productos
https://www.sklum.com/sofas,120
```

### 2.2. Enlaces existentes entre categorías

**Se aceptan dos formatos** — la herramienta detecta automáticamente cuál
es y los normaliza internamente a lo mismo. Elige el que te sea más fácil
de exportar; si tu export de Screaming Frog puede darte cualquiera de los
dos, se recomienda el formato **(a)**, porque conserva el texto ancla y
es el que genera de forma nativa el "Bulk Export → All Outlinks".

**(a) Formato "long" — una fila por enlace (recomendado):**

| Columna | Descripción |
|---|---|
| `Source` | URL origen del enlace |
| `Destination` | URL destino del enlace |
| `Anchor` | Texto ancla (opcional) |
| `Link Position` | Zona del enlace: `breadcrumb`, `content`, `footer`, `navigation`... (opcional; si tu export no la trae, todos los enlaces se tratan como zona "desconocida" a efectos de exclusión, que es lo único que realmente importa: que el enlace ya exista, sin importar dónde) |

```csv
Source,Destination,Anchor,Link Position
https://www.sklum.com/sofas,https://www.sklum.com/sillones,sillones a juego,Content
```

Truco para Screaming Frog: la clasificación automática de "Link
Position" (Navigation / Header / Footer / Content / Sidebar) está
disponible de forma nativa en el "Bulk Export → All Outlinks" a partir
de las versiones recientes, basada en las etiquetas HTML5 semánticas
(`<nav>`, `<header>`, `<footer>`, `<main>`, `<aside>`) de la plantilla.
Si vuestra plantilla no usa esas etiquetas, tendréis que usar el
formato (b) con una extracción personalizada (custom extraction) por
zona.

**(b) Formato "wide" — una fila por URL origen, una columna por zona:**

| Columna | Descripción |
|---|---|
| `URL` | URL origen |
| `Enlaces_Breadcrumb`, `Enlaces_Texto`, `Enlaces_Footer`, ... | Lista de URLs de destino enlazadas desde esa zona, separadas por `\|`, `;` o salto de línea |

```csv
URL,Enlaces_Breadcrumb,Enlaces_Texto,Enlaces_Footer
https://www.sklum.com/sofas,https://www.sklum.com/salon,https://www.sklum.com/sillones|https://www.sklum.com/mesas-auxiliares,
```

Se reconocen columnas cuyo nombre contenga: `breadcrumb`/`migas`,
`footer`/`pie`, `texto`/`contenido`/`body`, `sidebar`, `menu`/`navegacion`,
`header`/`cabecera`. En este formato no se puede recuperar el texto
ancla (queda vacío), porque la lista solo trae URLs.

> Si vuestro export real no encaja exactamente en ninguno de los dos
> formatos, lo más simple suele ser transformarlo a (a) con una fórmula
> o script antes de subirlo — es el formato más simple y estándar.

### 2.3. Volumen de búsqueda

Una fila por URL (si hay varias keywords candidatas por URL, se usa la
de mayor volumen como keyword principal).

| Columna | Descripción |
|---|---|
| `URL` | URL de la categoría |
| `Keyword` | Keyword principal |
| `Volumen` | Volumen de búsqueda mensual |

### 2.4. Taxonomía

| Columna | Descripción |
|---|---|
| `URL` | URL de la categoría |
| `Categoria_Principal` | Categoría principal |
| `Categoria_Secundaria` | Categoría secundaria (opcional) |

### 2.5. Formato de fichero

Cualquiera de los 4 ficheros puede subirse como **CSV** (con separador
`,` o `;`, se autodetecta; codificación UTF-8 o Latin-1) o como
**Excel (.xlsx)**.

## 3. Lógica de scoring

Para cada categoría **destino** candidata se calcula:

```
score = w1 · volumen_busqueda_normalizado
      + w2 · (1 - productos_normalizado)      # menos productos → más prioridad
      + w3 · (1 - enlaces_entrantes_normalizado)  # menos enlaces entrantes → más prioridad
      + w4 · afinidad_categoria(origen, destino)
```

Los pesos `w1..w4` y los 3 niveles de afinidad de categoría (misma
principal+secundaria / misma principal / distinta) son configurables
desde la interfaz. Las variables numéricas se normalizan por min-max
sobre el conjunto de categorías del crawl.

Se excluyen siempre: la propia URL como destino de sí misma, y
cualquier par origen→destino para el que ya exista un enlace entre esas
dos URLs (en cualquier zona). Un enlace destino→origen en sentido
contrario **no** bloquea proponer origen→destino: son enlaces distintos.

**Filas "pendiente de confirmar":** si a una categoría destino le falta
volumen de búsqueda, o le falta categorización (a ella o a la categoría
origen), la fila se marca como `pendiente_confirmar = True` con el
motivo, y su `score` queda vacío en vez de asumir un valor por defecto
(p.ej. volumen = 0). Estas filas **nunca** se seleccionan
automáticamente como propuesta — hay que revisarlas y completar el
dato que falta.

Por cada categoría origen, solo se marcan como `seleccionada = True` las
`N` propuestas de mayor score (con score ≥ score mínimo configurado),
siendo `N` el "máx. de enlaces nuevos por categoría origen" configurado
en la interfaz.

## 4. Seguridad y acceso multiusuario

- **Modo Google (recomendado):** login nativo de Streamlit (`st.login`)
  contra Google OAuth, restringido a los dominios/correos definidos en
  `INTERLINKING_ALLOWED_DOMAINS` / `INTERLINKING_ALLOWED_EMAILS`.
  Necesitas crear unas credenciales OAuth "Web application" en [Google
  Cloud Console](https://console.cloud.google.com/apis/credentials) y
  rellenar la sección `[auth]` de `secrets.toml` (ver
  `.streamlit/secrets.toml.example`).
- **Modo password (fallback simple):** usuario/contraseña compartida
  guardada en `INTERLINKING_SHARED_PASSWORD` (secret de la plataforma),
  nunca en el código. Pensado para desarrollo o para no depender de
  OAuth desde el primer día.

Ningún dato de clientes ni credenciales se guarda en el repositorio:
todo se resuelve vía variables de entorno / `st.secrets`.

## 5. Variables de entorno / secrets disponibles

| Variable | Descripción | Por defecto |
|---|---|---|
| `INTERLINKING_AUTH_MODE` | `google` o `password` | `google` |
| `INTERLINKING_ALLOWED_DOMAINS` | Dominios de correo permitidos, separados por coma | `digitalmenta.com` |
| `INTERLINKING_ALLOWED_EMAILS` | Correos concretos permitidos aunque no sean del dominio | *(vacío)* |
| `INTERLINKING_SHARED_PASSWORD` | Contraseña compartida (solo modo `password`) | *(sin valor)* |
| `INTERLINKING_DATA_DIR` | Carpeta desde la que leer automáticamente el export más reciente (Drive sincronizado, bucket montado...) | *(vacío = solo subida manual)* |

## 6. Despliegue en Streamlit Community Cloud

1. Sube este repositorio a GitHub (no subas `.streamlit/secrets.toml`,
   ya está en `.gitignore`).
2. Entra en [share.streamlit.io](https://share.streamlit.io), conecta el
   repositorio y selecciona `app.py` como fichero principal.
3. En "Advanced settings → Secrets", pega el contenido de
   `.streamlit/secrets.toml.example` ya rellenado con vuestros valores
   reales (client_id/secret de Google, dominios permitidos, etc.).
4. En Google Cloud Console, añade como "Authorized redirect URI" la URL
   que te da Streamlit Cloud + `/oauth2callback`, por ejemplo:
   `https://sklum-interlinking.streamlit.app/oauth2callback`.
5. Despliega. Si el catálogo de Sklum resulta demasiado pesado para el
   plan gratuito (límites de RAM o de tiempo de proceso — el cruce de
   categorías es O(n²), así que con catálogos muy grandes puede notarse),
   el mismo código se despliega sin apenas cambios en Render o Railway
   con un plan de pago con más recursos.

## 7. Estructura del proyecto

```
interlinking-tool/
├── app.py                  # Interfaz Streamlit
├── auth.py                 # Login (Google OAuth + fallback password)
├── core/
│   ├── config.py           # Pesos por defecto, lectura de env vars / secrets
│   ├── data_loader.py      # Carga y normalización de los 3 (4) datasets
│   └── scoring.py          # Cruce, scoring, exclusión, generación de propuesta
├── tests/
│   ├── test_scoring.py
│   └── test_exclusion.py
├── sample_data/            # Dataset de ejemplo ficticio
├── .streamlit/
│   ├── config.toml
│   └── secrets.toml.example
├── requirements.txt
└── README.md
```

`core/` es independiente de Streamlit a propósito: se puede testear con
`pytest` sin levantar la interfaz.

## 8. Tests

```bash
pytest
```

## 9. Próximos pasos sugeridos

- Automatizar el volcado periódico del export de Screaming Frog CLI a
  la carpeta que apunte `INTERLINKING_DATA_DIR` (cron / Programador de
  tareas + Google Drive sincronizado o bucket).
- Si el catálogo crece mucho, sustituir el cruce O(n²) en memoria por un
  cálculo por bloques (por categoría principal) o migrar a una base de
  datos ligera (DuckDB/SQLite) manteniendo la misma capa `core/`.

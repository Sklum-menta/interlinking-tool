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

Para probar la herramienta sin datos reales de Sklum, sube los 4
ficheros de `sample_data/`:

- `sample_data/crawl_productos.csv`
- `sample_data/enlaces_existentes.csv`
- `sample_data/volumen_keywords.csv`
- `sample_data/taxonomia.csv`

Incluye 6 categorías ficticias de los 3 grupos aislados (2 de Black
Friday, 2 de Rebajas y 2 de Special Price) para poder ver en acción, con
este mismo dataset, el aislamiento descrito en la sección 3.2: al generar
la propuesta, cada grupo solo se enlaza entre sus 2 URLs (p.ej.
"black-friday-sofas" y "black-friday-sillas" solo se enlazan entre sí,
nunca con "rebajas-sofas"/"rebajas-sillas", con
"special-price-sofas"/"special-price-sillas" ni con el resto del
catálogo), y así con los tres grupos.

## 2. Formato de los ficheros de entrada

Los nombres de columna se reconocen de forma flexible (mayúsculas/minúsculas,
con o sin acentos, nombres en inglés o español razonablemente parecidos), pero
**la información que tienen que contener es la siguiente**.

**Forma más rápida de empezar:** en vez de preparar los 4 ficheros por
separado (secciones 2.1 a 2.4), sube un único fichero con el formato de
la sección 2.6 ("plantilla unificada") — es la opción marcada como
recomendada en la interfaz.

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

Validado contra un export real de Screaming Frog de Sklum: la columna de
nº de productos puede llamarse "Nº de productos 1" (con el índice de la
extracción XPath al final) y, si el XPath capturó el contador de
paginación completo, contener un texto como `Has visto 50 productos de
348` en vez de un número limpio — la herramienta detecta este patrón y
usa el número real (348), no el de la página (50).

**Columnas opcionales de salud técnica** (`Status_Code`, `Indexable`):
si el crawl las trae (Screaming Frog las exporta de forma nativa como
"Status Code" e "Indexability"), la herramienta **excluye
automáticamente como destino** cualquier URL cuyo status no sea 200 o
que no sea indexable — nunca tiene sentido proponer un enlace hacia una
categoría caída, redirigida o en noindex. Es una restricción dura (como
los grupos aislados), no una penalización de score. Si no se aportan
estas columnas, no se aplica ningún filtro por este motivo.

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

Se reconocen columnas cuyo nombre contenga, además de "url" o "enlace"
(para no confundirlas con columnas de metadatos del crawl que
casualmente contengan esas mismas palabras, p.ej. "Tipo de contenido"):
`breadcrumb`/`migas`, `footer`/`pie`, `texto`/`contenido`/`body`,
`sidebar`, `menu`/`navegacion`, `header`/`cabecera`, `bolita` (el
típico widget de "categorías relacionadas" en forma de bolitas/chips).
En este formato no se puede recuperar el texto ancla (queda vacío),
porque la lista solo trae URLs.

Si alguna columna de zona trae rutas relativas (p.ej.
`/es/4057-comprar-aparadores`, sin dominio) en vez de la URL completa —
algo que puede pasar si el XPath solo captura el atributo `href` — la
herramienta las resuelve automáticamente contra el dominio de la URL
origen de esa misma fila.

> Si vuestro export real no encaja exactamente en ninguno de los dos
> formatos, lo más simple suele ser transformarlo a (a) con una fórmula
> o script antes de subirlo — es el formato más simple y estándar.

### 2.3. Volumen de búsqueda

Formato simple, una fila por URL (si hay varias keywords candidatas por
URL en filas distintas, se usa la de mayor volumen como keyword
principal):

| Columna | Descripción |
|---|---|
| `URL` | URL de la categoría |
| `Keyword` | Keyword principal |
| `Volumen` | Volumen de búsqueda mensual |

También se soporta el formato real del keyword research de Sklum: una
sola fila por URL con hasta 14 pares de columnas `Keyword N` + su
volumen (normalmente llamada `SV`, repetida en las 14 — se detecta de
forma posicional, la columna de volumen es la que va justo después de
cada `Keyword N`, sea cual sea su nombre). En ese caso se usa siempre
**la Keyword 1** (la keyword principal ya elegida a mano por el equipo
de SEO) y su volumen — no la de mayor SV entre las 14 — por decisión
explícita del equipo.

### 2.4. Taxonomía

| Columna | Descripción |
|---|---|
| `URL` | URL de la categoría |
| `Categoria_Principal` | Categoría principal |
| `Categoria_Secundaria` | Categoría secundaria (opcional) |

También se reconocen los nombres `Categoria`/`Subcategoria` (los que usa
el fichero maestro real de Sklum).

En los ficheros de volumen y de taxonomía también se reconoce, en vez de
`URL`, una columna con el código de país/idioma de esa URL (`ES`, `FR`,
`IT`, `PT`...) — habitual en los ficheros maestros multi-mercado de
Sklum, donde `ES` es la que se usa para interlinking en español.

### 2.5. Formato de fichero

Cualquiera de los ficheros puede subirse como **CSV** (con separador
`,` o `;`, se autodetecta; codificación UTF-8 o Latin-1) o como
**Excel (.xlsx)**.

### 2.6. Plantilla unificada (recomendado)

En vez de preparar 4 ficheros separados, se puede subir **un único
fichero** con una fila por URL y todas las columnas juntas. Es el modo
por defecto en la interfaz ("Plantilla única"), y hay una plantilla
vacía lista para rellenar en `sample_data/plantilla_unificada.xlsx`
(también descargable desde el propio botón de la interfaz).

| Columna | Descripción |
|---|---|
| `URL` | URL de la categoría |
| `Nº_Productos` | Nº de productos (admite el texto del paginador, ver 2.1) |
| `Status_Code` (opcional) | Código de estado HTTP, ver 2.1 |
| `Indexable` (opcional) | Indexabilidad ("Indexable"/"Non-Indexable"...), ver 2.1 |
| `Categoria_Principal` | Categoría principal |
| `Categoria_Secundaria` | Subcategoría (opcional) |
| `Keyword_1` | Keyword principal ya elegida para esa categoría |
| `Volumen` | Volumen de búsqueda mensual de `Keyword_1` |
| `Enlace_Bolita_1`, `Enlace_Bolita_2`, ... | URL completa de cada enlace existente del widget de categorías relacionadas ("bolitas"). Deja vacías las que no apliquen; añade más columnas numeradas si hacen falta más de las que trae la plantilla — no hay ningún límite, y no pasa nada si unas filas usan más columnas que otras. |
| `Enlace_Breadcrumb_1`, `Enlace_Breadcrumb_2`, ... | Igual que las bolitas, para los enlaces del breadcrumb. |

Internamente la herramienta usa exactamente el mismo reconocimiento de
columnas que los 4 ficheros separados (`load_plantilla_unificada` en
`core/data_loader.py`), así que columnas de más, en otro orden, o con
nombres ligeramente distintos no dan problema — solo hace falta que
sean reconocibles.

### 2.7. Search Console (opcional)

Fichero **aparte** de la plantilla unificada (se sube en el desplegable
"📈 Search Console (opcional)" de la propia herramienta), una fila por
URL:

| Columna | Descripción |
|---|---|
| `URL` | URL de la categoría |
| `Clics_28d` | Clics de los últimos 28 días |
| `Impresiones_28d` | Impresiones de los últimos 28 días |
| `Posicion_Media` (opcional) | Posición media en Google |

Activa dos señales adicionales del scoring (ver 3.3) que, sin este
fichero, no tienen ningún efecto aunque se suban sus pesos por encima
de 0. Si además subes el export del mes anterior en el segundo
desplegable, la herramienta muestra en los resultados una comparación
URL a URL (clics, impresiones y posición) para ver si las categorías
que recibieron enlaces nuevos van mejorando — ver 3.6.

## 3. Lógica de scoring

Para cada categoría **destino** candidata se calcula:

```
score = w1  · volumen_busqueda_normalizado
      + w2  · (1 - productos_normalizado)         # menos productos → más prioridad
      + w3  · (1 - enlaces_entrantes_normalizado)  # menos enlaces entrantes (destino) → más prioridad
      + w4  · afinidad_categoria(origen, destino)
      + w5  · relevancia_manual_categoria(destino)    # opcional, ver 3.1
      + w6  · prioridad_negocio_manual(destino)       # opcional, ver 3.1
      + w7  · enlaces_entrantes_normalizado(origen)   # más autoridad en origen → más prioridad, ver 3.2
      + w8  · (1 - enlaces_salientes_normalizado(origen))  # menos "presupuesto" gastado en origen → más prioridad, ver 3.2
      + w9  · oportunidad_posicion(destino)           # opcional, ver 3.3
      + w10 · impresiones_normalizado(destino)        # opcional, ver 3.3
```

Los pesos `w1..w10` y los 3 niveles de afinidad de categoría (misma
principal+secundaria / misma principal / distinta) son configurables
desde la interfaz — todos se normalizan automáticamente para que sumen
100%, así que no hace falta que cuadren a mano. Las variables numéricas
(volumen, productos, enlaces entrantes/salientes, impresiones) se
normalizan por min-max sobre el conjunto de categorías del crawl;
`relevancia_manual_categoria` y `prioridad_negocio_manual` se usan tal
cual (ya están pensadas para introducirse en escala 0–1);
`oportunidad_posicion` se calcula con su propia fórmula (ver 3.3).

### 3.1. Ajustes manuales de negocio (opcional)

Desde la sección "Ajustes manuales de negocio" de la interfaz se pueden
editar, cada vez que se genera la propuesta (por ejemplo mensualmente),
dos tablas — sin tocar ficheros ni código:

- **Relevancia manual por categoría/subcategoría** (0–1): para dar más o
  menos importancia como *destino* a categorías completas. El botón
  "Rellenar categorías desde la taxonomía cargada" precarga todas las
  combinaciones categoría/subcategoría detectadas con relevancia 0.5
  (neutra) para que solo haya que ajustar las que interesen. Dejar
  `categoria_secundaria` vacía aplica el valor a toda la categoría
  principal.
- **Prioridad de negocio manual por URL**: para penalizar o beneficiar
  URLs concretas por motivos puntuales (p.ej. de -1 a 1; 0 = neutro).

Ambas son opcionales por diseño: si se dejan vacías, su valor por
defecto (0.5 / 0.0 respectivamente) es neutro y **no altera el orden**
de la propuesta aunque su peso (`w5`/`w6`) sea mayor que 0. Cada tabla
tiene un botón "Descargar esta tabla (para el mes que viene)" y un
uploader para volver a cargar la tabla del mes anterior y seguir
ajustándola, ya que el hosting gratuito de Streamlit Community Cloud no
garantiza almacenamiento persistente entre despliegues.

Esta idea viene directamente de la plantilla de Sheets que usaba el
equipo antes (pestañas de "relevancia por categoría/subcategoría" y
"prioridad de negocio"): ver el contexto guardado en el proyecto de
Claude si se quiere más detalle de cómo lo hacían.

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

### 3.2. Señales sobre la página origen (autoridad y presupuesto de enlaces)

A diferencia del resto de pesos (que miran a la categoría *destino*),
estos dos miran a la categoría **origen** — la que va a enlazar:

- **Autoridad del origen** (`w7`): prioriza enlazar *desde* categorías
  que ya reciben muchos enlaces internos propios, porque una página con
  más autoridad interna transmite más valor al enlazar hacia otra.
- **Presupuesto de enlaces del origen** (`w8`): prioriza enlazar *desde*
  categorías que hoy tienen pocos enlaces salientes en total (contando
  los que ya existen, no solo los nuevos que se van a añadir) — cada
  enlace nuevo desde una página que ya enlaza a muchísimas otras diluye
  más el valor que reparte.

Ambos usan datos que ya aporta el dataset de enlaces existentes (no
hace falta ningún fichero nuevo) y valen 0 por defecto.

### 3.3. Search Console — oportunidad SEO (opcional)

Requieren subir el dataset de Search Console (ver 2.7); si no se sube,
estos dos pesos no tienen ningún efecto aunque estén por encima de 0
(mismo comportamiento "neutro por defecto" que los ajustes manuales de
3.1):

- **Posición en zona de oportunidad** (`w9`): prioriza categorías
  destino cuya posición media está dentro de un rango configurable
  desde la interfaz (por defecto, posiciones 4-20 — "a las puertas" de
  la primera página). Dentro del rango vale 1.0 (máxima prioridad); por
  debajo (ya muy bien posicionada) decae hacia 0 según se acerca a la
  posición 0; por encima decae linealmente a 0 a lo largo de una
  ventana configurable (30 posiciones por defecto). Es la señal
  pensada para detectar categorías "a punto de subir a primera
  página", que son las que más se benefician de un empujón de enlaces
  internos.
- **Impresiones en Google** (`w10`): prioriza categorías destino con
  más impresiones (más visibilidad potencial en buscadores, aunque hoy
  generen pocos clics).

### 3.4. Salud técnica del destino

Restricción dura (no un peso): si el crawl trae `Status_Code` y/o
`Indexable` (ver 2.1), nunca se propone como destino una URL cuyo
status no sea 200 o que no sea indexable — no tiene sentido enlazar
hacia una categoría caída, redirigida o en noindex. Si no se aportan
esas columnas, no se aplica ningún filtro por este motivo.

### 3.5. Categorías aisladas (Black Friday, Rebajas, Special Price...)

Regla de negocio explícita de Sklum: **Black Friday solo enlaza con
Black Friday, Rebajas solo enlaza con Rebajas, y Special Price solo
enlaza con Special Price**. Ninguna categoría "normal" del catálogo
puede enlazar hacia esas categorías (ni al revés), y estos tres grupos
tampoco enlazan entre sí — cada grupo queda completamente aislado del
resto, incluidos los otros grupos aislados.

En el catálogo real de Sklum, estas categorías especiales se
identifican por `Categoria_Secundaria = "Black Friday"`,
`"Rebajas / Promociones"` o `"Special Price"` (la `Categoria_Principal`
suele ser la categoría normal del producto — p.ej. "Muebles",
"Decoración" — no un paraguas común; también puede reflejarse de forma
redundante en el slug de la URL, pero no es necesario detectarlo ahí
porque ya viene en la taxonomía). La herramienta no depende de esos
nombres concretos: en la sección "Categorías aisladas" de la interfaz
se configura una lista de **patrones de texto, uno por línea** (por
defecto `Black Friday`, `Rebajas` y `Special Price`), y cada URL cuya
`Categoria_Principal` + `Categoria_Secundaria` contenga alguno de esos
patrones (sin distinguir mayúsculas/minúsculas) queda asignada a ese
grupo — por eso basta el patrón `Rebajas` para capturar también
`"Rebajas / Promociones"`.

`Special Price` se descubrió al validar la herramienta contra la
taxonomía real: algunas de sus URLs incluyen "black-friday" en el
propio slug (p.ej. `12092-comprar-ofertas-sofas-black-friday`), lo que
sugiere que parte de "Special Price" son en realidad campañas de Black
Friday etiquetadas de otra forma. Por decisión explícita del equipo, se
trata como un tercer grupo aislado independiente (no se fusiona con
Black Friday).

Importante: esto **no excluye** esas categorías de la herramienta ni
las trata de forma distinta en el scoring — dentro de cada grupo se
sigue generando una propuesta normal, con el mismo cálculo de score
(volumen, productos, enlaces entrantes, afinidad, ajustes manuales).
Lo único que cambia es que el cruce de pares origen→destino se filtra
primero para que un par solo sea candidato si ambas URLs están en el
mismo grupo (o ambas son categorías normales, es decir, ningún grupo).
Esto está implementado como un filtro estructural "duro" antes del
scoring (`core/scoring.py::_detectar_grupo_aislado` +
`build_master_table`), no como un peso que se pueda diluir subiendo
otros pesos a 0 — así se garantiza que nunca se proponga por error un
enlace entre, por ejemplo, "Sofás" y "Black Friday", o entre "Black
Friday" y "Rebajas".

Si se deja la lista de patrones vacía, no se aplica ningún
aislamiento y todas las categorías compiten entre sí como antes.

### 3.6. Evolución mes a mes (Search Console)

Pensado para responder a "¿está funcionando la estrategia de
enlazado?": si en el desplegable de Search Console (ver 2.7) subes,
además del export del mes actual, el export del mes anterior, la
sección de resultados muestra una tabla comparando ambos URL a URL —
clics, impresiones y posición media, con su variación (`delta_clics`,
`delta_impresiones`, `delta_posicion`; este último positivo = ha
mejorado, ha subido puestos). Es una comparación directa entre los dos
ficheros, sin cruzar automáticamente con qué categorías recibieron
enlaces nuevos el mes pasado — para eso, lo más simple es guardar
también la propuesta descargada de ese mes (sección 5) y mirar a mano
las categorías marcadas `seleccionada = True` en ella.

El mismo patrón que los ajustes manuales de 3.1 (descargar/subir tabla)
se usa aquí porque el hosting gratuito de Streamlit Community Cloud no
garantiza guardar nada entre despliegues — así que cada mes se sube el
export nuevo de Search Console junto con el del mes anterior (el que se
usó como "actual" el mes pasado).

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
  datos ligera (DuckDB/SQLite) manteniendo la misma capa `core/`. Con
  ~1.250 URLs reales (el tamaño del rastreo de Sklum compartido), generar
  la propuesta completa tarda unos 15 segundos — asumible para un uso
  puntual/mensual, pero a vigilar si el catálogo crece bastante más.

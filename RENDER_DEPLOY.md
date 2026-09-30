# Migrar de Streamlit Community Cloud a Render

Streamlit Community Cloud (plan gratuito) da ~1 GB de RAM. Con el
catálogo real de Sklum la app se ha quedado sin memoria al generar o
descargar la propuesta ("Oh no. Error running app." sin más detalle,
que es como Streamlit Cloud muestra un crash del contenedor, no un
error de código). Render con un plan de más RAM soluciona esto sin
tocar ni una línea de la app.

## Pasos

1. **Crear cuenta en Render** (si no existe ya): https://render.com
   (esto lo tiene que hacer una persona del equipo, no un asistente).

2. **Nuevo Blueprint**: Dashboard → "New" → "Blueprint" → conectar el
   repositorio `Sklum-menta/interlinking-tool` (rama `main`). Render
   detecta automáticamente `render.yaml` en la raíz del repo y propone
   crear el servicio `interlinking-tool`.

3. **Elegir Instance Type**: durante la creación, selecciona un plan
   con **al menos 2 GB de RAM** (p.ej. "Standard"). El plan gratuito de
   Render tiene el mismo problema de RAM que Streamlit Cloud, así que
   no sirve para este caso.

4. **Secret File — variables de entorno y login con Google**: en el
   servicio ya creado, ve a "Environment" → "Secret Files" → "Add
   Secret File":
   - **Filename** (ruta dentro del contenedor): `.streamlit/secrets.toml`
   - **Contents**: el mismo contenido que ya usáis en Streamlit Cloud
     (Settings → Secrets allí), con un solo cambio: la línea
     `redirect_uri` dentro de `[auth]` debe apuntar a la URL que Render
     os asigne, por ejemplo:
     ```
     redirect_uri = "https://interlinking-tool.onrender.com/oauth2callback"
     ```

5. **Autorizar la nueva URL en Google Cloud Console**: proyecto
   `graphite-post-403211` → APIs & Services → Credentials → el OAuth
   Client ID que ya usáis → "Authorized redirect URIs" → añadir la
   misma URL del paso anterior (`https://.../oauth2callback`). Sin este
   paso el login con Google fallará en la nueva URL aunque el resto
   funcione.

6. **Deploy**. Render instala dependencias (`requirements.txt`) y
   arranca la app con Uvicorn/Streamlit en el puerto que él mismo
   asigna (`$PORT`, ya configurado en `render.yaml`).

7. **Verificar**: abrir la nueva URL, comprobar que el login con Google
   funciona y volver a probar el flujo completo (subir la plantilla
   real → generar propuesta → descargar) para confirmar que ya no se
   queda sin memoria.

8. Una vez verificado, esa nueva URL de Render pasa a ser la oficial
   (se puede seguir compartiendo la de Streamlit Cloud como respaldo,
   pero como ya ha fallado dos días seguidos con datos reales, lo
   razonable es que el equipo use la de Render a partir de ahora).

## Notas

- No hace falta tocar el código de la app: la lógica de scoring, las
  descargas CSV/Excel y el login son exactamente los mismos ficheros
  que en GitHub. El único cambio es dónde se ejecuta el contenedor y
  cuánta RAM tiene disponible.
- Cualquier futuro push a `main` en GitHub puede configurarse en Render
  para redeploy automático (misma idea que Streamlit Cloud), en
  "Settings" → "Auto-Deploy".

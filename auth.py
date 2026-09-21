"""Autenticación de la app.

Dos modos, seleccionables con la variable de entorno / secret
`INTERLINKING_AUTH_MODE`:

- "google" (por defecto, recomendado): usa el login nativo de Streamlit
  (`st.login`) contra Google OAuth. Requiere configurar `[auth]` en
  `.streamlit/secrets.toml` (ver README.md y `.streamlit/secrets.toml.example`).
  El acceso se restringe además a una lista de dominios y/o correos
  concretos (`INTERLINKING_ALLOWED_DOMAINS`, `INTERLINKING_ALLOWED_EMAILS`).

- "password": modo simple de usuario/contraseña compartida, pensado
  para arrancar y probar la herramienta en local sin tener que crear
  todavía una app de OAuth en Google Cloud. La contraseña se lee de
  `INTERLINKING_SHARED_PASSWORD` (variable de entorno o secret), nunca
  hardcodeada en el código.

Ningún dato de acceso queda escrito en el repositorio: todo se resuelve
en tiempo de ejecución vía `core.config.AppConfig`.
"""
from __future__ import annotations

import streamlit as st

from core.config import AppConfig


def _is_allowed(email: str, config: AppConfig) -> bool:
    email = email.strip().lower()
    if config.allowed_emails and email in config.allowed_emails:
        return True
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    return domain in config.allowed_domains


def _login_google(config: AppConfig) -> str:
    # OJO: `st.user` es un objeto tipo diccionario que, antes de iniciar
    # sesión, puede estar "vacío" (sin claves) y por tanto evaluarse como
    # falsy en un `bool()`/`or`. Comprobar su EXISTENCIA con `hasattr`
    # (no su valor de verdad) es lo correcto para saber si esta versión
    # de Streamlit soporta el login nativo.
    if not hasattr(st, "user"):
        st.error(
            "Esta versión de Streamlit no soporta `st.login` (login nativo). "
            "Actualiza streamlit>=1.42 o cambia INTERLINKING_AUTH_MODE a 'password'."
        )
        st.stop()
    user = st.user

    if not getattr(user, "is_logged_in", False):
        st.title("🔗 Interlinking SEO — Sklum")
        st.write("Accede con tu cuenta de Google corporativa para continuar.")
        st.button("Iniciar sesión con Google", on_click=st.login, args=("google",))
        st.stop()

    email = getattr(user, "email", "") or ""
    if not _is_allowed(email, config):
        st.error(
            f"El correo **{email}** no tiene acceso a esta herramienta. "
            f"Dominios permitidos: {', '.join(config.allowed_domains) or '(ninguno configurado)'}."
        )
        st.button("Cerrar sesión", on_click=st.logout)
        st.stop()

    with st.sidebar:
        st.caption(f"Conectado como {email}")
        st.button("Cerrar sesión", on_click=st.logout)

    return email


def _login_password(config: AppConfig) -> str:
    if st.session_state.get("_authenticated"):
        with st.sidebar:
            st.caption("Sesión iniciada (modo contraseña compartida)")
            if st.button("Cerrar sesión"):
                st.session_state["_authenticated"] = False
                st.rerun()
        return st.session_state.get("_authenticated_email", "usuario@compartido")

    st.title("🔗 Interlinking SEO — Sklum")
    st.info(
        "Modo de acceso simple (usuario/contraseña compartida). "
        "Recomendado solo para desarrollo local; en producción usa el login con Google."
    )
    with st.form("login_password"):
        email = st.text_input("Email (solo a efectos de identificación)")
        password = st.text_input("Contraseña", type="password")
        submitted = st.form_submit_button("Entrar")

    if submitted:
        import os

        expected = os.environ.get("INTERLINKING_SHARED_PASSWORD")
        if not expected:
            # `st.secrets` lanza `StreamlitSecretNotFoundError` (no devuelve
            # None) cuando no existe ningún secrets.toml en absoluto, así que
            # el acceso va protegido con try/except en vez de con
            # `hasattr(st, "secrets")` (que siempre es True).
            try:
                expected = st.secrets.get("INTERLINKING_SHARED_PASSWORD")
            except Exception:
                expected = None
        if not expected:
            st.error(
                "No hay contraseña configurada. Define INTERLINKING_SHARED_PASSWORD "
                "como variable de entorno o en .streamlit/secrets.toml."
            )
        elif password == expected:
            st.session_state["_authenticated"] = True
            st.session_state["_authenticated_email"] = email or "usuario@compartido"
            st.rerun()
        else:
            st.error("Contraseña incorrecta.")
    st.stop()


def require_login(config: AppConfig) -> str:
    """Punto de entrada único: bloquea la ejecución (st.stop) hasta que
    el usuario esté autenticado y autorizado, y devuelve su email.
    """
    if config.auth_mode == "password":
        return _login_password(config)
    return _login_google(config)

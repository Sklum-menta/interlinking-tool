"""Configuración por defecto y lectura de variables de entorno / secrets.

Nada de esto contiene datos de clientes ni credenciales: todo son valores
por defecto que pueden sobreescribirse vía variables de entorno o vía
`st.secrets` cuando la app corre en Streamlit Community Cloud.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _get_env(name: str, default: str | None = None) -> str | None:
    """Lee una variable de entorno, y si no existe intenta leer de
    `st.secrets` (solo si streamlit está disponible e inicializado).
    Se hace el import de streamlit de forma perezosa para que este
    módulo de configuración pueda importarse y testearse sin Streamlit.
    """
    value = os.environ.get(name)
    if value is not None:
        return value
    try:
        import streamlit as st  # noqa: WPS433 (import perezoso intencional)

        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        # No hay contexto de Streamlit (p.ej. estamos en un test) o no
        # existe la clave en secrets. No es un error: seguimos con el
        # valor por defecto.
        pass
    return default


@dataclass
class ScoringWeights:
    """Pesos configurables del scoring. Todos en escala 0-1 (se
    normalizan internamente si no suman 1, así que el usuario puede
    moverlos libremente desde la interfaz sin preocuparse de que sumen
    exactamente 100%).
    """

    volumen_busqueda: float = 0.40
    pocos_productos: float = 0.20
    pocos_enlaces_entrantes: float = 0.20
    afinidad_categoria: float = 0.20

    def normalizados(self) -> "ScoringWeights":
        total = (
            self.volumen_busqueda
            + self.pocos_productos
            + self.pocos_enlaces_entrantes
            + self.afinidad_categoria
        )
        if total <= 0:
            # Evita división por cero si el usuario pone todo a 0.
            return ScoringWeights(0.25, 0.25, 0.25, 0.25)
        return ScoringWeights(
            volumen_busqueda=self.volumen_busqueda / total,
            pocos_productos=self.pocos_productos / total,
            pocos_enlaces_entrantes=self.pocos_enlaces_entrantes / total,
            afinidad_categoria=self.afinidad_categoria / total,
        )


@dataclass
class AffinityScores:
    """Puntuación de afinidad entre categoría origen y destino.

    misma_principal_y_secundaria: ambas categorías coinciden (máxima afinidad).
    misma_principal: solo coincide la categoría principal.
    distinta: no coincide ni la principal ni la secundaria.
    """

    misma_principal_y_secundaria: float = 1.0
    misma_principal: float = 0.6
    distinta: float = 0.15


@dataclass
class LimitesPropuesta:
    max_enlaces_nuevos_por_origen: int = 5
    score_minimo: float = 0.0


@dataclass
class AppConfig:
    """Configuración global de la app, resuelta a partir de variables de
    entorno / secrets con valores por defecto razonables.
    """

    allowed_domains: tuple[str, ...] = field(default_factory=tuple)
    allowed_emails: tuple[str, ...] = field(default_factory=tuple)
    auth_mode: str = "google"  # "google" | "password"
    shared_data_dir: str | None = None

    @classmethod
    def from_env(cls) -> "AppConfig":
        domains_raw = _get_env("INTERLINKING_ALLOWED_DOMAINS", "digitalmenta.com") or ""
        emails_raw = _get_env("INTERLINKING_ALLOWED_EMAILS", "") or ""
        auth_mode = (_get_env("INTERLINKING_AUTH_MODE", "google") or "google").lower()
        shared_dir = _get_env("INTERLINKING_DATA_DIR", None)
        domains = tuple(d.strip().lower() for d in domains_raw.split(",") if d.strip())
        emails = tuple(e.strip().lower() for e in emails_raw.split(",") if e.strip())
        return cls(
            allowed_domains=domains,
            allowed_emails=emails,
            auth_mode=auth_mode,
            shared_data_dir=shared_dir,
        )

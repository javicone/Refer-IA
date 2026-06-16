"""Capa de agente de ReferIA.

Un único agente (orquestador LLM) interpreta el comando de voz transcrito y
*propone* una acción. El procesamiento real lo dirige el Worker; los Guardrails
validan de forma determinista; las Herramientas ejecutan; y el paso HITL exige
confirmación humana antes de escribir en la base de datos.

Glosario y diseño completo: ver `README.md`.
"""

from __future__ import annotations

from .types import (
    Comando,
    ContextoPartido,
    EstadoClasificacion,
    EstadoComando,
    PropuestaAccion,
    ResultadoClasificacion,
    ResultadoEjecucion,
)

__all__ = [
    "Comando",
    "ContextoPartido",
    "EstadoClasificacion",
    "EstadoComando",
    "PropuestaAccion",
    "ResultadoClasificacion",
    "ResultadoEjecucion",
]

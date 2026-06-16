"""Modelo de dominio y terminología de la capa de agente.

Estos tipos fijan la terminología acordada para evitar ambigüedades (ver el
glosario en `arquitectura.md`). Son deliberadamente simples y serializables.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class EstadoClasificacion(str, Enum):
    """Resultado de clasificar un comando frente al dominio permitido."""

    CLARA = "CLARA"        # Acción permitida y con parámetros completos -> proponer tool.
    AMBIGUA = "AMBIGUA"    # Acción permitida pero faltan datos / hay incertidumbre.
    INVALIDA = "INVALIDA"  # Acción fuera de la allow-list (fallo de guardrail).


class EstadoComando(str, Enum):
    """Ciclo de vida de un comando (máquina de estados de `arquitectura.md`)."""

    CAPTURADO = "CAPTURADO"
    TRANSCRITO = "TRANSCRITO"
    CLASIFICADO = "CLASIFICADO"
    PENDIENTE_CONFIRMACION = "PENDIENTE_CONFIRMACION"
    CONFIRMADO = "CONFIRMADO"
    RECHAZADO = "RECHAZADO"
    EJECUTADO = "EJECUTADO"
    PERSISTIDO = "PERSISTIDO"
    ERROR_BD = "ERROR_BD"


@dataclass(frozen=True)
class Comando:
    """Texto transcrito de UNA intervención de voz del árbitro. Unidad de trabajo."""

    texto: str
    id_partido: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)


@dataclass
class ContextoPartido:
    """Estado actual del partido que se entrega al agente para clasificar."""

    id_partido: str
    equipos: dict[str, str]  # {"A": "Local", "B": "Visitante"}
    jugadores: dict[str, list[str]] = field(default_factory=dict)  # {"A": [...], "B": [...]}
    marcador: dict[str, int] = field(default_factory=lambda: {"A": 0, "B": 0})
    minuto_actual: int = 0
    finalizado: bool = False
    inicio_timestamp: float | None = None  # UNIX time cuando se confirmó inicio_partido


@dataclass(frozen=True)
class PropuestaAccion:
    """Acción que el agente propone ejecutar (≤ 1 herramienta)."""

    herramienta: str
    parametros: dict
    descripcion_legible: str


@dataclass(frozen=True)
class ResultadoClasificacion:
    """Salida combinada de agente + guardrails para un comando."""

    estado: EstadoClasificacion
    propuesta: PropuestaAccion | None = None
    motivo: str = ""  # Explicación para AMBIGUA / INVALIDA (se notifica al usuario).


@dataclass(frozen=True)
class ResultadoEjecucion:
    """Resultado de ejecutar una herramienta (escritura en BD)."""

    ok: bool
    evento_id: str | None = None
    duplicado: bool = False  # True si la idempotencia evitó una segunda escritura.
    error: str | None = None

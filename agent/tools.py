"""Set de herramientas del agente (recomendación #3).

Decisión de diseño:

- **Ni *gold hammer*** — NO existe un `registrar_evento(tipo, ...)` genérico que
  concentre todas las funciones en una sola herramienta.
- **Ni exceso de herramientas** — son exactamente 6 acciones de dominio, cada
  una con parámetros y efectos distintos, para no sobrecargar el contexto del
  agente.

Cada herramienta declara un esquema JSON (para *tool use* del LLM y para la
validación determinista de Guardrails) y una función `ejecutar` que escribe UN
evento en el `EventStore`. Las descripciones son **prescriptivas** ("Llama a
esto cuando…") porque mejora la fiabilidad de selección de herramienta.

Nota de coherencia: el set debe coincidir con el dominio real. Eventos como
"corner" o "penalti" (que el WebSocket de demo emitía) NO están permitidos
todavía; Guardrails los marcará como INVALIDA hasta que se añadan aquí.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .types import Comando, ContextoPartido, PropuestaAccion, ResultadoEjecucion

_EQUIPO = {
    "type": "string",
    "enum": ["A", "B"],
    "description": "Equipo: 'A' (local) o 'B' (visitante).",
}
_MINUTO = {
    "type": "integer",
    "minimum": 0,
    "maximum": 130,
    "description": "Minuto de juego en el que ocurre el evento.",
}


@dataclass(frozen=True)
class ToolSpec:
    nombre: str
    descripcion: str
    input_schema: dict
    tipo_evento: str

    def definicion_openai(self) -> dict:
        """Definición en el formato `tools` (function calling) de OpenAI."""
        return {
            "type": "function",
            "function": {
                "name": self.nombre,
                "description": self.descripcion,
                "parameters": self.input_schema,
            },
        }


# --- Catálogo de herramientas (allow-list) -----------------------------------

TOOLS: dict[str, ToolSpec] = {
    "add_gol": ToolSpec(
        nombre="add_gol",
        descripcion=(
            "Llama a esto cuando el árbitro indique que se ha marcado un GOL. "
            "Registra un gol para un equipo."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "equipo": _EQUIPO,
                "jugador": {"type": "string", "description": "Autor del gol (opcional)."},
                "minuto": _MINUTO,
            },
            "required": ["equipo"],
            "additionalProperties": False,
        },
        tipo_evento="gol",
    ),
    "add_falta": ToolSpec(
        nombre="add_falta",
        descripcion=(
            "Llama a esto cuando el árbitro señale una FALTA o tarjeta. "
            "Registra una falta de un equipo, opcionalmente con tarjeta."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "equipo": _EQUIPO,
                "jugador": {"type": "string", "description": "Infractor (opcional)."},
                "minuto": _MINUTO,
                "tipo": {
                    "type": "string",
                    "enum": ["normal", "amarilla", "roja"],
                    "description": "Tipo de falta/tarjeta. Por defecto 'normal'.",
                },
            },
            "required": ["equipo"],
            "additionalProperties": False,
        },
        tipo_evento="falta",
    ),
    "cambio": ToolSpec(
        nombre="cambio",
        descripcion=(
            "Llama a esto cuando el árbitro indique un CAMBIO/sustitución de jugadores. "
            "Registra qué jugador sale y cuál entra."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "equipo": _EQUIPO,
                "sale": {"type": "string", "description": "Jugador que sale."},
                "entra": {"type": "string", "description": "Jugador que entra."},
                "minuto": _MINUTO,
            },
            "required": ["equipo", "sale", "entra"],
            "additionalProperties": False,
        },
        tipo_evento="cambio",
    ),
    "extra_time": ToolSpec(
        nombre="extra_time",
        descripcion=(
            "Llama a esto cuando el árbitro anuncie TIEMPO AÑADIDO/descuento. "
            "Registra los minutos de prolongación."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "minutos": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "description": "Minutos de tiempo añadido.",
                },
            },
            "required": ["minutos"],
            "additionalProperties": False,
        },
        tipo_evento="tiempo_anadido",
    ),
    "fin_partido": ToolSpec(
        nombre="fin_partido",
        descripcion=(
            "Llama a esto SOLO cuando el árbitro declare el FINAL del partido. "
            "Acción terminal e irreversible: requiere confirmación reforzada."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        tipo_evento="fin_partido",
    ),
    "inicio_partido": ToolSpec(
        nombre="inicio_partido",
        descripcion=(
            "Llama a esto cuando el árbitro declare el INICIO o COMIENZO del partido "
            "o de una parte (primera parte, segunda parte, prórroga). "
            "Marca el inicio del encuentro y resetea el reloj."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "parte": {
                    "type": "integer",
                    "enum": [1, 2, 3, 4],
                    "description": "Parte del partido: 1=primera, 2=segunda, 3=prórroga 1ª, 4=prórroga 2ª. Por defecto 1.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        tipo_evento="inicio_partido",
    ),
}

# Acción irreversible/terminal: el HITL la trata con confirmación reforzada.
HERRAMIENTAS_IRREVERSIBLES: frozenset[str] = frozenset({"fin_partido"})

ALLOWLIST: frozenset[str] = frozenset(TOOLS.keys())


def definiciones_openai() -> list[dict]:
    """Lista de definiciones de herramientas para function calling de OpenAI."""
    return [spec.definicion_openai() for spec in TOOLS.values()]


def descripcion_legible(propuesta: PropuestaAccion) -> str:
    """Texto humano para mostrar en el paso de confirmación (HITL)."""
    p = propuesta.parametros
    n = propuesta.herramienta

    if n == "add_gol":
        autor = f" de {p['jugador']}" if p.get("jugador") else ""
        return f"GOL del equipo {p['equipo']}{autor} (min {p.get('minuto')})"
    
    if n == "add_falta":
        tipo = p.get("tipo", "normal")
        quien = f" a {p['jugador']}" if p.get("jugador") else ""
        return f"Falta ({tipo}) del equipo {p['equipo']}{quien} (min {p.get('minuto')})"
    
    if n == "cambio":
        return f"Cambio equipo {p['equipo']}: sale {p.get('sale')}, entra {p.get('entra')} (min {p.get('minuto')})"
    
    if n == "extra_time":
        return f"Tiempo añadido: {p.get('minutos')} min"
    
    if n == "fin_partido":
        return "FIN DEL PARTIDO"
    
    if n == "inicio_partido":
        parte = p.get("parte", 1)
        nombres = {1: "primera parte", 2: "segunda parte", 3: "prórroga 1ª", 4: "prórroga 2ª"}
        return f"INICIO — {nombres.get(parte, f'parte {parte}')}"
    return f"{n} {p}"


def ejecutar(
    propuesta: PropuestaAccion,
    comando: Comando,
    contexto: ContextoPartido,
    event_store,
) -> ResultadoEjecucion:
    """Ejecuta la herramienta propuesta: escribe el evento en la BD.

    La escritura es atómica e idempotente (ver `db.EventStore`).
    """
    spec = TOOLS[propuesta.herramienta]
    datos = {**propuesta.parametros}
    try:
        evento_id, duplicado = event_store.guardar_evento(
            comando_id=comando.id,
            id_partido=comando.id_partido,
            tipo=spec.tipo_evento,
            datos=datos,
        )
    except Exception as exc:  # noqa: BLE001 — se reporta como ERROR_BD, sin efecto.
        return ResultadoEjecucion(ok=False, error=str(exc))

    # Efectos secundarios en el contexto en memoria.
    if not duplicado:
        # Avanzar el reloj al minuto del evento (nunca retroceder).
        minuto = propuesta.parametros.get("minuto")
        if isinstance(minuto, int) and minuto > contexto.minuto_actual:
            contexto.minuto_actual = minuto

        if spec.tipo_evento == "gol":
            contexto.marcador[propuesta.parametros["equipo"]] += 1
        if spec.tipo_evento == "inicio_partido":
            contexto.inicio_timestamp = time.time()
            contexto.minuto_actual = 0
        if spec.tipo_evento == "fin_partido":
            contexto.finalizado = True

    return ResultadoEjecucion(ok=True, evento_id=evento_id, duplicado=duplicado)

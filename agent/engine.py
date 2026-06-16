"""Motor del agente: UN único orquestador que *propone* (no ejecuta).

La interfaz `AgentEngine` desacopla el resto del sistema del proveedor LLM
concreto. El método `proponer` recibe un comando + contexto y devuelve una
`PropuestaAccion` (≤ 1 herramienta) o `None` si no puede decidir (ambiguo).

- `OpenAIAgentEngine` — implementación por defecto en producción. Usa el SDK
  `openai` (Chat Completions + *function calling*) con un **bucle manual** (no el
  tool-runner automático) para que el Worker pueda insertar la pausa HITL antes
  de ejecutar. Los guardrails viajan también como allow-list explícita en el
  system prompt. Compatible con OpenRouter (incluidos modelos Claude vía proxy).
- `StubAgentEngine` — reglas deterministas por palabras clave, para tests sin red.

La decisión CLARA/AMBIGUA/INVALIDA NO la toma el motor: la combina el Worker
aplicando Guardrails sobre la propuesta (ver `agent/worker.py`).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Protocol

log = logging.getLogger("referia.engine")

from .tools import (
    TOOLS,
    definiciones_openai,
    descripcion_legible,
)
from .types import Comando, ContextoPartido, PropuestaAccion

MODELO_OPENAI_POR_DEFECTO = "gpt-4o-mini"


class AgentEngine(Protocol):
    def proponer(
        self, comando: Comando, contexto: ContextoPartido
    ) -> tuple[PropuestaAccion | None, str]:
        """Devuelve `(propuesta, motivo_ambiguedad)`.

        Si no puede proponer una acción clara, `propuesta` es None y
        `motivo_ambiguedad` explica qué falta (se notifica al usuario).
        """
        ...


def _system_prompt(contexto: ContextoPartido) -> str:
    permitidas = "\n".join(
        f"- {spec.nombre}: {spec.descripcion}" for spec in TOOLS.values()
    )
    return (
        "Eres el asistente de un árbitro de fútbol. Recibes la transcripción de "
        "un comando de voz y debes traducirlo a EXACTAMENTE UNA de las acciones "
        "permitidas, llamando a su herramienta con los parámetros correctos.\n\n"
        "ACCIONES PERMITIDAS (y solo estas):\n"
        f"{permitidas}\n\n"
        "REGLAS ESTRICTAS:\n"
        "- Cualquier otra acción NO está permitida: no llames a ninguna herramienta.\n"
        "- Si el comando es ambiguo o el equipo es desconocido, pide aclaración.\n"
        "- NO incluyas el parámetro 'minuto' en ninguna herramienta: el servidor lo calcula "
        "automáticamente a partir del reloj del partido.\n"
        "- Como máximo una herramienta por comando.\n\n"
        "CONTEXTO DEL PARTIDO:\n"
        f"- Equipo A ({contexto.equipos.get('A', 'Local')}): "
        f"{', '.join(contexto.jugadores.get('A', [])) or 'sin alineación'}\n"
        f"- Equipo B ({contexto.equipos.get('B', 'Visitante')}): "
        f"{', '.join(contexto.jugadores.get('B', [])) or 'sin alineación'}\n"
        f"- Finalizado: {contexto.finalizado}\n"
        "Cuando el árbitro diga 'local' o el nombre del equipo A, usa equipo='A'. "
        "Cuando diga 'visitante' o el nombre del equipo B, usa equipo='B'."
    )


class OpenAIAgentEngine:
    """Implementación con OpenAI (Chat Completions + function calling).

    Bucle manual idéntico al de Claude: NO ejecutamos la herramienta; extraemos
    el `tool_call` propuesto y lo devolvemos para que el Worker lo valide
    (guardrails) y lo gatee (HITL).
    """

    def __init__(self, modelo: str | None = None, client=None) -> None:
        self._modelo = modelo or os.environ.get(
            "REFERIA_OPENAI_MODEL", MODELO_OPENAI_POR_DEFECTO
        )
        self._client = client  # Inyectable en tests; perezoso si no se da.

    def _ensure_client(self):
        if self._client is None:
            import openai

            self._client = openai.OpenAI(
                api_key=os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY"),
                base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            )
        return self._client

    def proponer(
        self, comando: Comando, contexto: ContextoPartido
    ) -> tuple[PropuestaAccion | None, str]:
        log.info("→ OpenAI [%s] modelo=%s texto=%r", comando.id_partido, self._modelo, comando.texto)
        client = self._ensure_client()
        respuesta = client.chat.completions.create(
            model=self._modelo,
            max_tokens=1024,
            tools=definiciones_openai(),
            tool_choice="auto",
            messages=[
                {"role": "system", "content": _system_prompt(contexto)},
                {"role": "user", "content": comando.texto},
            ],
        )

        mensaje = respuesta.choices[0].message
        tool_calls = mensaje.tool_calls or []
        if not tool_calls:
            texto = (mensaje.content or "").strip() or "Comando no reconocido o incompleto."
            log.info("← Sin tool_call — respuesta texto: %r", texto)
            return None, texto

        llamada = tool_calls[0]  # Como máximo una herramienta por comando.
        try:
            parametros = json.loads(llamada.function.arguments or "{}")
        except json.JSONDecodeError:
            log.warning("← JSON inválido en argumentos: %r", llamada.function.arguments)
            return None, "No se pudieron interpretar los parámetros del comando."

        propuesta = PropuestaAccion(
            herramienta=llamada.function.name,
            parametros=dict(parametros),
            descripcion_legible="",
        )
        log.info("← Tool call: %s %s", llamada.function.name, parametros)
        return _con_descripcion(propuesta), ""


class StubAgentEngine:
    """Motor determinista por palabras clave (tests y demo sin red)."""

    def proponer(
        self, comando: Comando, contexto: ContextoPartido
    ) -> tuple[PropuestaAccion | None, str]:
        texto = comando.texto.lower()
        equipo = "A" if (" a" in f" {texto}" or "local" in texto) else (
            "B" if ("b" in texto or "visitante" in texto) else None
        )
        minuto = _extraer_minuto(texto)

        if "fin" in texto and "partido" in texto:
            return _con_descripcion(PropuestaAccion("fin_partido", {}, "")), ""

        if "gol" in texto:
            if equipo is None or minuto is None:
                return None, "Aclara el equipo y el minuto del gol."
            return _con_descripcion(
                PropuestaAccion("add_gol", {"equipo": equipo, "minuto": minuto}, "")
            ), ""

        if "falta" in texto or "tarjeta" in texto:
            if equipo is None or minuto is None:
                return None, "Aclara el equipo y el minuto de la falta."
            tipo = "amarilla" if "amarilla" in texto else ("roja" if "roja" in texto else "normal")
            return _con_descripcion(
                PropuestaAccion(
                    "add_falta", {"equipo": equipo, "minuto": minuto, "tipo": tipo}, ""
                )
            ), ""

        return None, "Comando no reconocido."


def _con_descripcion(propuesta: PropuestaAccion) -> PropuestaAccion:
    return PropuestaAccion(
        herramienta=propuesta.herramienta,
        parametros=propuesta.parametros,
        descripcion_legible=descripcion_legible(propuesta),
    )


def _extraer_minuto(texto: str) -> int | None:
    import re

    m = re.search(r"minuto\s+(\d{1,3})", texto) or re.search(r"\b(\d{1,3})\b", texto)
    if m:
        return int(m.group(1))
    return None

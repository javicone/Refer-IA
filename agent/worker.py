"""Worker (recomendación #5).

Cola en proceso (`asyncio.Queue`) + UNA corrutina consumidora que procesa los
comandos **de uno en uno**. La serialización garantiza orden y consistencia: no
hay dos escrituras solapadas ni confirmaciones cruzadas.

Orquesta el flujo completo de la máquina de estados:

    dequeue → contexto → agente.proponer → guardrails → branch por estado
      ├─ INVALIDA / AMBIGUA → notificar
      └─ CLARA → registrar HITL + notificar propuesta → esperar decisión
                   ├─ rechazado  → notificar
                   └─ confirmado → ejecutar tool (BD) → notificar resultado

Para el caso local, la "Cola Remota (RabbitMQ/SQS)" del diagrama es opcional;
este Worker es la implementación del MVP.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from . import guardrails, tools
from .engine import AgentEngine
from .hitl import RegistroHITL
from .tools import descripcion_legible
from .types import (
    Comando,
    ContextoPartido,
    EstadoClasificacion,
    EstadoComando,
    PropuestaAccion,
    ResultadoClasificacion,
)

log = logging.getLogger("referia.worker")

# Notificador: empuja un mensaje (dict serializable) hacia el usuario (p.ej. WS).
Notificador = Callable[[dict], Awaitable[None]]
# Proveedor de contexto: devuelve el estado actual de un partido.
ProveedorContexto = Callable[[str], ContextoPartido]

TIMEOUT_CONFIRMACION_S = 120.0


def clasificar(
    engine: AgentEngine, comando: Comando, contexto: ContextoPartido
) -> ResultadoClasificacion:
    """Combina agente (propone) + guardrails (validan) en un estado final."""
    log.info("[%s] Clasificando: %r", comando.id_partido, comando.texto)
    propuesta, motivo = engine.proponer(comando, contexto)
    if propuesta is None:
        log.info("[%s] AMBIGUA — %s", comando.id_partido, motivo)
        return ResultadoClasificacion(EstadoClasificacion.AMBIGUA, motivo=motivo)

    ok, motivo_gr = guardrails.validar(propuesta, contexto)
    if not ok:
        log.warning("[%s] INVALIDA — guardrail: %s", comando.id_partido, motivo_gr)
        return ResultadoClasificacion(EstadoClasificacion.INVALIDA, motivo=motivo_gr)

    log.info("[%s] CLARA — herramienta=%s params=%s", comando.id_partido, propuesta.herramienta, propuesta.parametros)
    return ResultadoClasificacion(EstadoClasificacion.CLARA, propuesta=propuesta)


class ReferIAWorker:
    def __init__(
        self,
        *,
        engine: AgentEngine,
        event_store,
        proveedor_contexto: ProveedorContexto,
        notificador: Notificador,
        hitl: RegistroHITL | None = None,
        timeout_confirmacion_s: float = TIMEOUT_CONFIRMACION_S,
    ) -> None:
        self._engine = engine
        self._store = event_store
        self._contexto_de = proveedor_contexto
        self._notificar = notificador
        self.hitl = hitl or RegistroHITL()
        self._timeout = timeout_confirmacion_s
        self._cola: asyncio.Queue[Comando] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    # --- API pública ---------------------------------------------------------

    async def encolar(self, comando: Comando) -> None:
        await self._cola.put(comando)

    async def join(self) -> None:
        """Espera a que se procesen todos los comandos encolados."""
        await self._cola.join()

    def iniciar(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._bucle())

    async def detener(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # --- Bucle del worker ----------------------------------------------------

    async def _bucle(self) -> None:
        log.info("Worker iniciado, esperando comandos…")
        while True:
            comando = await self._cola.get()
            try:
                await self.procesar(comando)
            except Exception as exc:  # noqa: BLE001 — un comando no debe tumbar el worker.
                log.exception("Error inesperado procesando comando %s", comando.id)
                await self._notificar(
                    {"tipo": "error", "comando_id": comando.id, "mensaje": str(exc)}
                )
            finally:
                self._cola.task_done()

    async def procesar(self, comando: Comando) -> EstadoComando:
        """Procesa un comando completo. Devuelve el estado final (útil en tests)."""
        log.info("[%s] ▶ Comando recibido: %r", comando.id_partido, comando.texto)
        contexto = self._contexto_de(comando.id_partido)
        resultado = clasificar(self._engine, comando, contexto)

        if resultado.estado is EstadoClasificacion.INVALIDA:
            log.warning("[%s] ✖ INVALIDA — %s", comando.id_partido, resultado.motivo)
            await self._notificar(
                {"tipo": "no_permitida", "comando_id": comando.id, "mensaje": resultado.motivo}
            )
            return EstadoComando.CLASIFICADO

        if resultado.estado is EstadoClasificacion.AMBIGUA:
            log.info("[%s] ❓ AMBIGUA — %s", comando.id_partido, resultado.motivo)
            await self._notificar(
                {"tipo": "aclaracion", "comando_id": comando.id, "mensaje": resultado.motivo}
            )
            return EstadoComando.CLASIFICADO

        # CLARA -> validaciones de contexto e inyección del minuto calculado.
        propuesta = resultado.propuesta
        assert propuesta is not None

        # Validar que el partido haya comenzado (salvo para inicio_partido en sí).
        if propuesta.herramienta != "inicio_partido" and contexto.inicio_timestamp is None:
            log.warning("[%s] ✖ Partido no iniciado — herramienta=%s", comando.id_partido, propuesta.herramienta)
            await self._notificar({
                "tipo": "no_permitida",
                "comando_id": comando.id,
                "mensaje": "El partido no ha comenzado todavía. Di 'inicio del partido' primero.",
            })
            return EstadoComando.CLASIFICADO

        # Inyectar el minuto calculado por el servidor (timestamp_comando − timestamp_inicio).
        # El agente no necesita determinar el minuto: el server lo calcula de forma fiable.
        if contexto.inicio_timestamp is not None:
            spec = tools.TOOLS.get(propuesta.herramienta)
            if spec and "minuto" in spec.input_schema.get("properties", {}):
                delta = max(0.0, comando.timestamp - contexto.inicio_timestamp)
                minuto_servidor = min(130, int(delta / 60))
                nuevos_params = {**propuesta.parametros, "minuto": minuto_servidor}
                propuesta = PropuestaAccion(
                    herramienta=propuesta.herramienta,
                    parametros=nuevos_params,
                    descripcion_legible=descripcion_legible(
                        PropuestaAccion(propuesta.herramienta, nuevos_params, "")
                    ),
                )
                log.info("[%s] ⏱ Minuto inyectado: %d (Δ=%.1fs)", comando.id_partido, minuto_servidor, delta)

        irreversible = propuesta.herramienta in tools.HERRAMIENTAS_IRREVERSIBLES
        confirmation_id, future = self.hitl.registrar(
            comando, propuesta, irreversible=irreversible
        )
        log.info("[%s] ⏳ Esperando confirmación HITL (%s): %s",
                 comando.id_partido, propuesta.herramienta, propuesta.descripcion_legible)
        await self._notificar(
            {
                "tipo": "propuesta",
                "comando_id": comando.id,
                "confirmation_id": confirmation_id,
                "herramienta": propuesta.herramienta,
                "parametros": propuesta.parametros,
                "descripcion": propuesta.descripcion_legible,
                "irreversible": irreversible,
            }
        )

        try:
            confirmado = await asyncio.wait_for(future, timeout=self._timeout)
        except asyncio.TimeoutError:
            log.warning("[%s] ⏱ Confirmación expirada para %s", comando.id_partido, confirmation_id)
            await self._notificar(
                {"tipo": "expirada", "comando_id": comando.id, "confirmation_id": confirmation_id}
            )
            return EstadoComando.RECHAZADO

        if not confirmado:
            log.info("[%s] 🗑 Rechazado por el árbitro: %s", comando.id_partido, propuesta.herramienta)
            await self._notificar(
                {"tipo": "descartado", "comando_id": comando.id}
            )
            return EstadoComando.RECHAZADO

        # CONFIRMADO -> ejecutar herramienta (escritura atómica e idempotente).
        log.info("[%s] ✔ Confirmado — ejecutando %s", comando.id_partido, propuesta.herramienta)
        ejecucion = tools.ejecutar(propuesta, comando, contexto, self._store)
        if not ejecucion.ok:
            log.error("[%s] ✖ Error de BD: %s", comando.id_partido, ejecucion.error)
            await self._notificar(
                {"tipo": "error_bd", "comando_id": comando.id, "mensaje": ejecucion.error}
            )
            return EstadoComando.ERROR_BD

        log.info("[%s] 💾 Persistido — evento_id=%s duplicado=%s",
                 comando.id_partido, ejecucion.evento_id, ejecucion.duplicado)
        await self._notificar(
            {
                "tipo": "confirmado",
                "comando_id": comando.id,
                "evento_id": ejecucion.evento_id,
                "duplicado": ejecucion.duplicado,
                "descripcion": propuesta.descripcion_legible,
            }
        )
        return EstadoComando.PERSISTIDO

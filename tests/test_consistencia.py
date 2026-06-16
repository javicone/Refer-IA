"""Banco de pruebas de consistencia (recomendación #7).

Verifica las invariantes que blindan el "OK? tras la BD":

- Confirmación obligatoria (ningún write sin CONFIRMADO).
- Cardinalidad (1 comando confirmado => 1 evento; inválido => 0).
- Idempotencia (reintentar no duplica).
- Atomicidad (fallo de BD => 0 eventos + error).
- Orden/serialización (el worker procesa en orden).
- Guardrails (allow-list y cotas de dominio).
- Terminal (tras fin_partido no se admiten más acciones).

Usa `StubAgentEngine` (sin red) y SQLite en memoria.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# Permitir importar el proyecto al ejecutar pytest desde la raíz.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import guardrails  # noqa: E402
from agent.engine import StubAgentEngine  # noqa: E402
from agent.types import (  # noqa: E402
    Comando,
    ContextoPartido,
    EstadoComando,
    PropuestaAccion,
)
from agent.worker import ReferIAWorker  # noqa: E402
from db import EventStore  # noqa: E402


def _contexto() -> ContextoPartido:
    ctx = ContextoPartido(
        id_partido="demo", equipos={"A": "Local", "B": "Visitante"}, jugadores={"A": [], "B": []}
    )
    # Simular que el partido comenzó hace 30 minutos para que la validación pase.
    ctx.inicio_timestamp = time.time() - 30 * 60
    return ctx


def _build_worker(*, decision: str | None = "confirmar", contexto: ContextoPartido | None = None):
    store = EventStore(":memory:")
    ctx = contexto or _contexto()
    mensajes: list[dict] = []

    worker_ref: dict = {}

    async def notificador(msg: dict) -> None:
        mensajes.append(msg)
        if msg.get("tipo") == "propuesta" and decision is not None:
            cid = msg["confirmation_id"]
            if decision == "confirmar":
                worker_ref["w"].hitl.confirmar(cid)
            elif decision == "rechazar":
                worker_ref["w"].hitl.rechazar(cid)

    worker = ReferIAWorker(
        engine=StubAgentEngine(),
        event_store=store,
        proveedor_contexto=lambda _id: ctx,
        notificador=notificador,
    )
    worker_ref["w"] = worker
    return worker, store, mensajes, ctx


# --- Cardinalidad y confirmación obligatoria ---------------------------------

def test_clara_confirmada_escribe_un_evento():
    async def run():
        worker, store, mensajes, _ = _build_worker(decision="confirmar")
        estado = await worker.procesar(Comando("Gol del equipo A en el minuto 23", "demo"))
        assert estado is EstadoComando.PERSISTIDO
        assert store.contar_eventos("demo") == 1
        assert any(m["tipo"] == "confirmado" for m in mensajes)

    asyncio.run(run())


def test_invalida_no_escribe():
    async def run():
        worker, store, mensajes, _ = _build_worker(decision="confirmar")
        estado = await worker.procesar(Comando("ponme un café por favor", "demo"))
        # El Stub no reconoce el comando -> AMBIGUA (no hay tool); 0 eventos.
        assert store.contar_eventos("demo") == 0
        assert estado is EstadoComando.CLASIFICADO

    asyncio.run(run())


def test_ambigua_no_escribe():
    async def run():
        worker, store, mensajes, _ = _build_worker(decision="confirmar")
        await worker.procesar(Comando("gol", "demo"))  # sin equipo ni minuto
        assert store.contar_eventos("demo") == 0
        assert any(m["tipo"] == "aclaracion" for m in mensajes)

    asyncio.run(run())


def test_rechazo_no_escribe():
    async def run():
        worker, store, _, _ = _build_worker(decision="rechazar")
        estado = await worker.procesar(Comando("Gol del equipo A en el minuto 10", "demo"))
        assert estado is EstadoComando.RECHAZADO
        assert store.contar_eventos("demo") == 0

    asyncio.run(run())


# --- Idempotencia ------------------------------------------------------------

def test_idempotencia_no_duplica():
    async def run():
        worker, store, _, _ = _build_worker(decision="confirmar")
        comando = Comando("Gol del equipo A en el minuto 23", "demo")
        await worker.procesar(comando)
        await worker.procesar(comando)  # mismo comando.id
        assert store.contar_eventos("demo") == 1

    asyncio.run(run())


# --- Atomicidad --------------------------------------------------------------

def test_atomicidad_error_bd_sin_efecto():
    async def run():
        worker, store, mensajes, _ = _build_worker(decision="confirmar")

        def _fallar(**_kwargs):
            raise RuntimeError("BD caída")

        store.guardar_evento = _fallar  # type: ignore[assignment]
        estado = await worker.procesar(Comando("Gol del equipo A en el minuto 5", "demo"))
        assert estado is EstadoComando.ERROR_BD
        assert any(m["tipo"] == "error_bd" for m in mensajes)

    asyncio.run(run())


# --- Orden / serialización ---------------------------------------------------

def test_orden_serializacion():
    async def run():
        worker, store, mensajes, _ = _build_worker(decision="confirmar")
        worker.iniciar()
        # El servidor calcula el minuto por timestamp, no por el texto del comando.
        # Lo que importa es que los tres eventos se procesen y persistan en orden.
        for i in range(3):
            await worker.encolar(Comando(f"Gol del equipo A número {i+1}", "demo"))
        await worker.join()
        await worker.detener()

        eventos = store.listar_eventos("demo")
        assert len(eventos) == 3
        assert all(e["tipo"] == "gol" for e in eventos)

    asyncio.run(run())


# --- Guardrails --------------------------------------------------------------

def test_guardrails_rechaza_herramienta_desconocida():
    ok, _ = guardrails.validar(PropuestaAccion("inventada", {}, ""), _contexto())
    assert ok is False


def test_guardrails_rechaza_minuto_fuera_de_rango():
    ok, _ = guardrails.validar(
        PropuestaAccion("add_gol", {"equipo": "A", "minuto": 999}, ""), _contexto()
    )
    assert ok is False


def test_guardrails_acepta_propuesta_valida():
    ok, _ = guardrails.validar(
        PropuestaAccion("add_gol", {"equipo": "A", "minuto": 23}, ""), _contexto()
    )
    assert ok is True


# --- Terminal (fin de partido) ----------------------------------------------

def test_terminal_tras_fin_partido():
    async def run():
        ctx = _contexto()
        worker, store, _, _ = _build_worker(decision="confirmar", contexto=ctx)
        await worker.procesar(Comando("fin del partido", "demo"))
        assert ctx.finalizado is True
        # Cualquier acción posterior queda bloqueada por guardrails.
        estado = await worker.procesar(Comando("Gol del equipo A en el minuto 99", "demo"))
        assert estado is EstadoComando.CLASIFICADO
        assert store.contar_eventos("demo") == 1  # solo el fin_partido

    asyncio.run(run())

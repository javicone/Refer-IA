"""Human-in-the-loop (recomendación #1).

Mantiene las confirmaciones pendientes: cuando el Worker clasifica un comando
como CLARA, registra aquí una confirmación pendiente y espera la decisión
humana (`confirmar` / `rechazar`) antes de ejecutar la herramienta. Ninguna
escritura en BD ocurre sin pasar por aquí.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from .types import Comando, PropuestaAccion


class DecisionInvalida(Exception):
    """Se intentó resolver una confirmación inexistente o ya resuelta."""


@dataclass
class _Pendiente:
    comando: Comando
    propuesta: PropuestaAccion
    irreversible: bool
    future: "asyncio.Future[bool]"


class RegistroHITL:
    """Registro en memoria de confirmaciones pendientes, indexado por id."""

    def __init__(self) -> None:
        self._pendientes: dict[str, _Pendiente] = {}

    def registrar(
        self,
        comando: Comando,
        propuesta: PropuestaAccion,
        *,
        irreversible: bool = False,
    ) -> tuple[str, "asyncio.Future[bool]"]:
        """Crea una confirmación pendiente y devuelve `(confirmation_id, future)`.

        El Worker debe `await` el future; resolverá a True (confirmado) o
        False (rechazado).
        """
        confirmation_id = uuid.uuid4().hex
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pendientes[confirmation_id] = _Pendiente(
            comando=comando,
            propuesta=propuesta,
            irreversible=irreversible,
            future=future,
        )
        return confirmation_id, future

    def _resolver(self, confirmation_id: str, decision: bool) -> None:
        pendiente = self._pendientes.pop(confirmation_id, None)
        if pendiente is None or pendiente.future.done():
            raise DecisionInvalida(confirmation_id)
        pendiente.future.set_result(decision)

    def confirmar(self, confirmation_id: str) -> None:
        self._resolver(confirmation_id, True)

    def rechazar(self, confirmation_id: str) -> None:
        self._resolver(confirmation_id, False)

    def pendientes(self) -> list[str]:
        return list(self._pendientes)

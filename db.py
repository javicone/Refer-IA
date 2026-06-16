"""Almacén de eventos de ReferIA (SQLite).

Garantías de consistencia (recomendación #7 — "cuidado con el OK? tras la BD"):

- **Atomicidad**: cada escritura ocurre dentro de una transacción; si falla, no
  deja efectos parciales.
- **Idempotencia**: `comando_id` es UNIQUE, de modo que reintentar el mismo
  comando confirmado no duplica el evento ("1 comando confirmado ⇒ 1 evento").

Estas invariantes están cubiertas por el banco de pruebas en `tests/`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

# Configurable vía `REFERIA_DB_PATH` para poder apuntar a un volumen en Docker.
DEFAULT_DB_PATH = Path(os.environ.get("REFERIA_DB_PATH", "referia.db"))


class EventStore:
    """Persistencia de eventos de partido sobre SQLite."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        # `check_same_thread=False` permite usarlo desde el worker async.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._crear_esquema()

    def _crear_esquema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS eventos (
                    id          TEXT PRIMARY KEY,
                    comando_id  TEXT NOT NULL UNIQUE,
                    id_partido  TEXT NOT NULL,
                    tipo        TEXT NOT NULL,
                    datos       TEXT NOT NULL,
                    creado_en   REAL NOT NULL DEFAULT (strftime('%s','now'))
                )
                """
            )

    def guardar_evento(
        self,
        *,
        comando_id: str,
        id_partido: str,
        tipo: str,
        datos: dict,
    ) -> tuple[str, bool]:
        """Escribe un evento de forma atómica e idempotente.

        Devuelve `(evento_id, duplicado)`. Si ya existía un evento para
        `comando_id`, no escribe nada y `duplicado` es True.
        """
        existente = self._conn.execute(
            "SELECT id FROM eventos WHERE comando_id = ?", (comando_id,)
        ).fetchone()
        if existente is not None:
            return existente["id"], True

        evento_id = uuid.uuid4().hex
        with self._conn:  # transacción: commit al salir, rollback ante excepción.
            self._conn.execute(
                "INSERT INTO eventos (id, comando_id, id_partido, tipo, datos) "
                "VALUES (?, ?, ?, ?, ?)",
                (evento_id, comando_id, id_partido, tipo, json.dumps(datos)),
            )
        return evento_id, False

    def listar_eventos(self, id_partido: str) -> list[dict]:
        filas = self._conn.execute(
            "SELECT id, comando_id, id_partido, tipo, datos, creado_en "
            "FROM eventos WHERE id_partido = ? ORDER BY rowid",  # rowid = orden de inserción
            (id_partido,),
        ).fetchall()
        eventos = []
        for fila in filas:
            evento = dict(fila)
            evento["datos"] = json.loads(evento["datos"])
            eventos.append(evento)
        return eventos

    def buscar_ultimo_evento_tipo(self, id_partido: str, tipo: str) -> dict | None:
        """Devuelve el último evento de un tipo concreto, o None si no existe."""
        fila = self._conn.execute(
            "SELECT id, comando_id, id_partido, tipo, datos, creado_en "
            "FROM eventos WHERE id_partido = ? AND tipo = ? ORDER BY rowid DESC LIMIT 1",
            (id_partido, tipo),
        ).fetchone()
        if fila is None:
            return None
        ev = dict(fila)
        ev["datos"] = json.loads(ev["datos"])
        return ev

    def contar_eventos(self, id_partido: str | None = None) -> int:
        if id_partido is None:
            fila = self._conn.execute("SELECT COUNT(*) AS n FROM eventos").fetchone()
        else:
            fila = self._conn.execute(
                "SELECT COUNT(*) AS n FROM eventos WHERE id_partido = ?", (id_partido,)
            ).fetchone()
        return int(fila["n"])

    def limpiar_todo(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM eventos")

    def close(self) -> None:
        self._conn.close()

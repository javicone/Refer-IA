from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(name)s  %(message)s",
)

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
import tempfile

from agent.engine import OpenAIAgentEngine, StubAgentEngine
from agent.hitl import DecisionInvalida
from agent.types import Comando, ContextoPartido
from agent.worker import ReferIAWorker
from db import EventStore

# Import `transcribe_audio` lazily inside the endpoint to avoid import-time failures
transcribe_audio = None


# --- Estado de la aplicación -------------------------------------------------

class ConnectionManager:
    """Gestiona las conexiones WebSocket y difunde notificaciones del worker."""

    def __init__(self) -> None:
        self._activos: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._activos.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._activos:
            self._activos.remove(ws)

    async def broadcast(self, mensaje: dict) -> None:
        muertos = []
        for ws in self._activos:
            try:
                await ws.send_text(json.dumps(mensaje))
            except Exception:  # noqa: BLE001
                muertos.append(ws)
        for ws in muertos:
            self.disconnect(ws)


# Registro de partidos en memoria (MVP). En producción vendría de una BD.
_PARTIDOS: dict[str, ContextoPartido] = {
    "demo": ContextoPartido(
        id_partido="demo",
        equipos={"A": "FC Barcelona", "B": "Real Madrid"},
        jugadores={
            "A": [
                "Iñaki Peña", "Jules Koundé", "Pau Cubarsí", "Íñigo Martínez", "Alejandro Balde",
                "Frenkie de Jong", "Marc Casadó", "Pedri", "Lamine Yamal", "Robert Lewandowski", "Raphinha",
            ],
            "B": [
                "Thibaut Courtois", "Lucas Vázquez", "Éder Militão", "Antonio Rüdiger", "Fran García",
                "Federico Valverde", "Aurélien Tchouaméni", "Luka Modrić", "Rodrygo", "Kylian Mbappé", "Vinícius Jr.",
            ],
        },
    )
}


def _contexto_de(id_partido: str) -> ContextoPartido:
    if id_partido not in _PARTIDOS:
        _PARTIDOS[id_partido] = ContextoPartido(
            id_partido=id_partido, equipos={"A": "Local", "B": "Visitante"}
        )
    return _PARTIDOS[id_partido]


def _crear_engine():
    """Selecciona el motor del agente vía `REFERIA_ENGINE`.

    - `openai` (por defecto en Docker) → OpenAI/OpenRouter Chat Completions + function calling.
    - `stub` (por defecto en local/tests) → reglas deterministas, sin red.
    """
    motor = os.environ.get("REFERIA_ENGINE", "stub").lower()
    if motor == "openai":
        return OpenAIAgentEngine()
    return StubAgentEngine()


manager = ConnectionManager()
event_store: EventStore | None = None
worker: ReferIAWorker | None = None


import logging as _logging
_log = _logging.getLogger("referia.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global event_store, worker
    event_store = EventStore()
    event_store.limpiar_todo()
    _log.info("BD reiniciada — todos los eventos eliminados (modo pruebas)")

    # Resetear estado en memoria de todos los partidos.
    for ctx in _PARTIDOS.values():
        ctx.marcador = {"A": 0, "B": 0}
        ctx.minuto_actual = 0
        ctx.finalizado = False
        ctx.inicio_timestamp = None

    worker = ReferIAWorker(
        engine=_crear_engine(),
        event_store=event_store,
        proveedor_contexto=_contexto_de,
        notificador=manager.broadcast,
    )
    worker.iniciar()
    try:
        yield
    finally:
        await worker.detener()
        event_store.close()


app = FastAPI(title="ReferIA", lifespan=lifespan)


# --- Páginas -----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    path = Path("templates") / "transcribe.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/resultados")
async def get_resultados():
    path = Path("templates") / "index.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --- API ---------------------------------------------------------------------

@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="El archivo de audio está vacío.")

    suffix = Path(file.filename or "command.webm").suffix or ".webm"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(audio_bytes)

        try:
            global transcribe_audio
            if transcribe_audio is None:
                try:
                    from stt import transcribe_audio as _trans

                    transcribe_audio = _trans
                except ModuleNotFoundError as exc:
                    raise HTTPException(
                        status_code=500,
                        detail="Dependencia faltante: instala el paquete 'openai' para transcribir audio.",
                    ) from exc

            text = transcribe_audio(temp_path)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=500, detail="No se pudo ejecutar ffmpeg para leer el audio."
            ) from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=500, detail=f"No se pudo transcribir el audio: {exc}"
            ) from exc

        return JSONResponse({"text": text})
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


@app.post("/api/comando")
async def encolar_comando(payload: dict):
    """Encola un comando transcrito para que el agente lo procese (vía Worker)."""
    texto = (payload.get("texto") or "").strip()
    if not texto:
        raise HTTPException(status_code=400, detail="El comando está vacío.")
    id_partido = payload.get("id_partido") or "demo"

    assert worker is not None
    comando = Comando(texto=texto, id_partido=id_partido)
    await worker.encolar(comando)
    return JSONResponse({"comando_id": comando.id, "estado": "encolado"})


@app.get("/api/partido/{id_partido}")
async def get_partido(id_partido: str):
    ctx = _contexto_de(id_partido)
    return JSONResponse({
        "id_partido": ctx.id_partido,
        "equipos": ctx.equipos,
        "jugadores": ctx.jugadores,
        "marcador": ctx.marcador,
        "minuto_actual": ctx.minuto_actual,
        "finalizado": ctx.finalizado,
    })


@app.get("/api/eventos/{id_partido}")
async def listar_eventos(id_partido: str):
    assert event_store is not None
    return JSONResponse({"eventos": event_store.listar_eventos(id_partido)})


# --- WebSocket: canal bidireccional (propuestas/notificaciones + confirmaciones)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            accion = msg.get("accion")
            confirmation_id = msg.get("confirmation_id")
            assert worker is not None
            if accion == "confirmar" and confirmation_id:
                try:
                    worker.hitl.confirmar(confirmation_id)
                except DecisionInvalida:
                    pass
            elif accion == "rechazar" and confirmation_id:
                try:
                    worker.hitl.rechazar(confirmation_id)
                except DecisionInvalida:
                    pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as exc:  # noqa: BLE001
        print(f"WebSocket Error: {exc}")
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

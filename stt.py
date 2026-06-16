"""Transcripción de audio (STT) mediante la API de OpenRouter.

OpenRouter usa JSON + base64, no multipart form-data como OpenAI, por lo que
el SDK openai no es compatible para este endpoint. Usamos urllib (stdlib) para
evitar dependencias extra.

Endpoint: POST {OPENROUTER_BASE_URL}/audio/transcriptions
Body: {"model": "...", "input_audio": {"data": "<base64>", "format": "webm"}}
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("referia.stt")

DEFAULT_MODEL_NAME = os.environ.get("REFERIA_STT_MODEL", "openai/whisper-large-v3-turbo")
_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")


def transcribe_audio(audio_path: str | Path, model_name: str = DEFAULT_MODEL_NAME) -> str:
    path = Path(audio_path)
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Define OPENROUTER_API_KEY en el entorno o en .env")

    fmt = path.suffix.lstrip(".").lower() or "webm"
    audio_b64 = base64.b64encode(path.read_bytes()).decode("ascii")

    body = json.dumps({
        "model": model_name,
        "input_audio": {"data": audio_b64, "format": fmt},
    }).encode("utf-8")

    url = _BASE_URL.rstrip("/") + "/audio/transcriptions"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    log.info("STT → %s (modelo=%s, formato=%s, %.1f KB)",
             url, model_name, fmt, len(audio_b64) * 3 / 4 / 1024)
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        log.error("STT error %s: %s", exc.code, detail)
        raise RuntimeError(f"OpenRouter STT {exc.code}: {detail}") from exc

    texto = (data.get("text") or "").strip()
    log.info("STT ← %r", texto)
    return texto

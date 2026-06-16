"""Guardrails deterministas (recomendaciones #4 y #6).

NO es un agente ni una llamada al LLM: es código determinista que **envuelve**
la salida del agente y acota el espacio de trabajo de forma explícita.

Política: se define lo que está permitido (la allow-list de 6 herramientas con
su esquema y cotas de dominio); **cualquier otra acción está prohibida** y se
clasifica como INVALIDA.
"""

from __future__ import annotations

from .tools import ALLOWLIST, TOOLS
from .types import ContextoPartido, PropuestaAccion

MINUTO_MAX = 130


def validar(propuesta: PropuestaAccion, contexto: ContextoPartido) -> tuple[bool, str]:
    """Valida una propuesta del agente.

    Devuelve `(ok, motivo)`. Si `ok` es False, `motivo` explica por qué la
    acción no está permitida (se notifica al usuario).
    """
    nombre = propuesta.herramienta
    params = propuesta.parametros

    # 1) Allow-list: solo las herramientas declaradas están permitidas.
    if nombre not in ALLOWLIST:
        return False, f"Acción no permitida: '{nombre}'."

    # 2) Partido finalizado: solo se rechaza cualquier nueva escritura.
    if contexto.finalizado:
        return False, "El partido ya está finalizado; no se admiten más acciones."

    spec = TOOLS[nombre]
    schema = spec.input_schema
    propiedades: dict = schema.get("properties", {})

    # 3) Campos obligatorios presentes.
    for requerido in schema.get("required", []):
        if requerido not in params or params[requerido] in (None, ""):
            return False, f"Falta el dato obligatorio '{requerido}' para {nombre}."

    # 4) Sin campos desconocidos.
    if schema.get("additionalProperties") is False:
        extra = set(params) - set(propiedades)
        if extra:
            return False, f"Parámetros no reconocidos en {nombre}: {sorted(extra)}."

    # 5) Validación por campo (tipo, enum, rango) y cotas de dominio.
    for clave, valor in params.items():
        regla = propiedades.get(clave, {})
        tipo = regla.get("type")

        if tipo == "integer" and not isinstance(valor, int):
            return False, f"'{clave}' debe ser un entero en {nombre}."
        if tipo == "string" and not isinstance(valor, str):
            return False, f"'{clave}' debe ser texto en {nombre}."

        if "enum" in regla and valor not in regla["enum"]:
            return False, f"Valor inválido para '{clave}': {valor!r}."
        if "minimum" in regla and isinstance(valor, int) and valor < regla["minimum"]:
            return False, f"'{clave}' por debajo del mínimo en {nombre}."
        if "maximum" in regla and isinstance(valor, int) and valor > regla["maximum"]:
            return False, f"'{clave}' por encima del máximo en {nombre}."

    # 6) Cotas de dominio adicionales.
    equipo = params.get("equipo")
    if equipo is not None and equipo not in contexto.equipos:
        return False, f"Equipo desconocido: {equipo!r}."

    return True, ""

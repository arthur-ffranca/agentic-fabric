"""Cliente mínimo de modelo. A interface é completar(); o transporte é HTTP."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Protocol

from fabrica.erros import ErroLLM

MENSAGEM_SEM_CHAVE = (
    "Nenhuma chave de modelo encontrada. Defina OPENAI_API_KEY "
    "(ou FABRICA_LLM_API_KEY). Para um servidor compatível, defina também "
    "OPENAI_BASE_URL e OPENAI_MODEL. Sem a chave a fábrica não inventa patch."
)


class ClienteLLM(Protocol):
    def completar(self, *, sistema: str, usuario: str, temperatura: float = 0.2) -> str:
        """Devolve o texto do modelo. Quem implementa pode trocar o fornecedor."""


class ClienteOpenAICompativel:
    """Fala o endpoint /v1/chat/completions. Serve a OpenAI e servidores compatíveis."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        modelo: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = (api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("FABRICA_LLM_API_KEY") or "").strip()
        self.base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.modelo = (
            modelo or os.environ.get("OPENAI_MODEL") or os.environ.get("FABRICA_LLM_MODELO") or "gpt-4o-mini"
        ).strip()
        if timeout is None:
            bruto = os.environ.get("FABRICA_LLM_TIMEOUT") or "120"
            try:
                timeout = float(bruto)
            except ValueError:
                timeout = 120.0
        self.timeout = timeout
        if not self.api_key:
            raise ErroLLM(MENSAGEM_SEM_CHAVE)

    def completar(self, *, sistema: str, usuario: str, temperatura: float = 0.2) -> str:
        corpo = {
            "model": self.modelo,
            "temperature": temperatura,
            "messages": [
                {"role": "system", "content": sistema},
                {"role": "user", "content": usuario},
            ],
        }
        pedido = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(corpo).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as resposta:
                payload = json.loads(resposta.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detalhe = ""
            if exc.fp is not None:
                detalhe = exc.read().decode("utf-8", errors="replace")[:400]
            raise ErroLLM(f"O modelo respondeu HTTP {exc.code}. {detalhe}".strip()) from exc
        except urllib.error.URLError as exc:
            raise ErroLLM(
                f"Não foi possível falar com o modelo em {self.base_url}. {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise ErroLLM("O modelo não respondeu a tempo.") from exc
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ErroLLM("A resposta do modelo não trouxe uma mensagem.") from exc


def extrair_json(texto: str) -> dict:
    bruto = texto.strip()
    cerca = re.search(r"```(?:json)?\s*(.*?)```", bruto, re.DOTALL)
    if cerca:
        bruto = cerca.group(1).strip()
    inicio = bruto.find("{")
    fim = bruto.rfind("}")
    if inicio < 0 or fim < inicio:
        raise ErroLLM("A resposta do modelo não veio em JSON.")
    try:
        valor = json.loads(bruto[inicio : fim + 1])
    except json.JSONDecodeError as exc:
        raise ErroLLM(f"JSON inválido do modelo: {exc}") from exc
    if not isinstance(valor, dict):
        raise ErroLLM("A resposta do modelo não é um objeto JSON.")
    return valor


def solicitar_json(llm: ClienteLLM, sistema: str, usuario: str) -> dict:
    texto = llm.completar(sistema=sistema, usuario=usuario, temperatura=0.2)
    try:
        return extrair_json(texto)
    except ErroLLM:
        texto = llm.completar(
            sistema=sistema,
            usuario=usuario + "\n\nA resposta anterior não era um objeto JSON. Responda só o JSON.",
            temperatura=0,
        )
        return extrair_json(texto)

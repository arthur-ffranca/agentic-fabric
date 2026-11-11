"""Cliente mínimo de modelo. A interface é completar(); o transporte é HTTP."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Protocol

from fabrica.erros import ErroLLM
from fabrica.uso import medidor_atual

# O modelo fica fixo aqui. OPENAI_MODEL no ambiente é só para um teste ou uma volta rápida.
MODELO_PADRAO = "gpt-5.4"

MENSAGEM_SEM_CHAVE = (
    "Nenhuma chave de modelo encontrada. Defina OPENAI_API_KEY "
    "(ou FABRICA_LLM_API_KEY). Para um servidor compatível, defina também "
    "OPENAI_BASE_URL e OPENAI_MODEL. Sem a chave a fábrica não inventa patch."
)

# Respostas que valem repetir: limite de taxa, sobrecarga e falha passageira do servidor.
_REPETIR = {408, 409, 429, 500, 502, 503, 504}


class ClienteLLM(Protocol):
    def completar(self, *, sistema: str, usuario: str, temperatura: float = 0.2) -> str:
        """Devolve o texto do modelo. Quem implementa pode trocar o fornecedor."""


def _numero(nome: str, padrao: float) -> float:
    try:
        return float(os.environ.get(nome) or padrao)
    except ValueError:
        return padrao


class ClienteOpenAICompativel:
    """Fala o endpoint /v1/chat/completions. Serve a OpenAI e servidores compatíveis."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        modelo: str | None = None,
        timeout: float | None = None,
        tentativas: int | None = None,
        esperar=time.sleep,
    ) -> None:
        self.api_key = (api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("FABRICA_LLM_API_KEY") or "").strip()
        self.base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.modelo = (
            modelo or os.environ.get("OPENAI_MODEL") or os.environ.get("FABRICA_LLM_MODELO") or MODELO_PADRAO
        ).strip()
        self.timeout = _numero("FABRICA_LLM_TIMEOUT", 120.0) if timeout is None else timeout
        self.tentativas = max(1, int(_numero("FABRICA_LLM_TENTATIVAS", 3) if tentativas is None else tentativas))
        self._esperar = esperar
        # Alguns modelos só aceitam a temperatura padrão. Quando um recusa, o cliente para de mandar.
        self._sem_temperatura = False
        if not self.api_key:
            raise ErroLLM(MENSAGEM_SEM_CHAVE)

    def completar(self, *, sistema: str, usuario: str, temperatura: float = 0.2) -> str:
        medidor = medidor_atual()
        if medidor is not None and medidor.estourou():
            raise ErroLLM(
                f"O turno passou do teto de {medidor.teto} tokens e parou aqui. "
                "Reduza o pedido ou ajuste FABRICA_TETO_TOKENS."
            )
        corpo = {
            "model": self.modelo,
            "messages": [
                {"role": "system", "content": sistema},
                {"role": "user", "content": usuario},
            ],
        }
        if not self._sem_temperatura:
            corpo["temperature"] = temperatura
        payload = self._enviar(corpo)
        if medidor is not None:
            uso = payload.get("usage") if isinstance(payload, dict) else None
            uso = uso if isinstance(uso, dict) else {}
            medidor.registrar(uso.get("prompt_tokens") or 0, uso.get("completion_tokens") or 0)
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ErroLLM("A resposta do modelo não trouxe uma mensagem.") from exc

    def _enviar(self, corpo: dict) -> dict:
        tentativa = 1
        while True:
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
                    return json.loads(resposta.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detalhe = ""
                if exc.fp is not None:
                    detalhe = exc.read().decode("utf-8", errors="replace")[:400]
                if exc.code == 400 and "temperature" in corpo and "temperature" in detalhe.lower():
                    self._sem_temperatura = True
                    corpo = {chave: valor for chave, valor in corpo.items() if chave != "temperature"}
                    continue
                if exc.code in _REPETIR and tentativa < self.tentativas:
                    self._esperar(self._pausa(tentativa, exc.headers))
                    tentativa += 1
                    continue
                raise ErroLLM(f"O modelo respondeu HTTP {exc.code}. {detalhe}".strip()) from exc
            except urllib.error.URLError as exc:
                if tentativa < self.tentativas:
                    self._esperar(self._pausa(tentativa, None))
                    tentativa += 1
                    continue
                raise ErroLLM(
                    f"Não foi possível falar com o modelo em {self.base_url}. {exc.reason}"
                ) from exc
            except TimeoutError as exc:
                if tentativa < self.tentativas:
                    self._esperar(self._pausa(tentativa, None))
                    tentativa += 1
                    continue
                raise ErroLLM("O modelo não respondeu a tempo.") from exc

    @staticmethod
    def _pausa(tentativa: int, cabecalhos) -> float:
        pedida = None
        if cabecalhos is not None:
            try:
                pedida = float(cabecalhos.get("Retry-After") or "")
            except (TypeError, ValueError):
                pedida = None
        if pedida is not None:
            return min(max(pedida, 0.0), 30.0)
        return min(2.0 ** (tentativa - 1), 20.0)


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

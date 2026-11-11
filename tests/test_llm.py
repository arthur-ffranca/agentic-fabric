import io
import json
import urllib.error
import urllib.request

import pytest

from fabrica.erros import ErroLLM
from fabrica.llm import ClienteOpenAICompativel, extrair_json


def test_extrai_json_de_cerca():
    dados = extrair_json('texto\n```json\n{"veredito": "aprovar"}\n```')
    assert dados["veredito"] == "aprovar"


def test_chat_completions(monkeypatch):
    capturado = {}

    class Resposta:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "diff"}}]}).encode()

    def falso(req, timeout=0):
        capturado["url"] = req.full_url
        capturado["corpo"] = json.loads(req.data.decode())
        capturado["auth"] = req.get_header("Authorization")
        return Resposta()

    monkeypatch.setattr(urllib.request, "urlopen", falso)
    cliente = ClienteOpenAICompativel(
        api_key="sk-test",
        base_url="https://example.test/v1",
        modelo="modelo-teste",
    )
    assert cliente.completar(sistema="sistema", usuario="usuario") == "diff"
    assert capturado["url"] == "https://example.test/v1/chat/completions"
    assert capturado["corpo"]["model"] == "modelo-teste"
    assert capturado["corpo"]["messages"][0] == {"role": "system", "content": "sistema"}
    assert capturado["corpo"]["messages"][1]["content"] == "usuario"
    assert capturado["auth"] == "Bearer sk-test"


def test_http_recusado_explica(monkeypatch):
    def falso(req, timeout=0):
        raise urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b"chave recusada"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", falso)
    cliente = ClienteOpenAICompativel(api_key="sk-test", base_url="https://example.test/v1", modelo="m")
    with pytest.raises(ErroLLM) as erro:
        cliente.completar(sistema="s", usuario="u")
    assert "401" in str(erro.value)

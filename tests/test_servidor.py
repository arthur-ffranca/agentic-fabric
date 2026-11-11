"""A API: quem pode chamar, de onde, e o turno em segundo plano."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from apoio import criar_repo, instantaneo, llm_roteiro
from fabrica.cli import construir_padrao
from fabrica.erros import ErroFabrica
from fabrica.memoria import Memoria
from fabrica.servidor import criar_servidor

PEDIDO = "Adiciona validação de tipo na função soma em app.py"


class Servico:
    def __init__(self, tmp_path, *, token="", raizes=None, atraso=0.0, falha_em=None, host="127.0.0.1"):
        self.exemplo = criar_repo(tmp_path)
        self.llm, _ = llm_roteiro(self.exemplo.diff_app, atraso=atraso, falha_em=falha_em)
        self.memoria = Memoria(tmp_path / "mem")
        self.orq = construir_padrao(self.llm, self.memoria)
        self.orq.raizes = raizes
        self.httpd = criar_servidor(host, 0, orquestrador=self.orq, token=token)
        self.porta = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def pedir(self, metodo, caminho, corpo=None, cabecalhos=None):
        dados = None if corpo is None else (corpo if isinstance(corpo, bytes) else json.dumps(corpo).encode())
        base = {"Content-Type": "application/json"} if metodo == "POST" else {}
        base.update(cabecalhos or {})
        pedido = urllib.request.Request(f"http://127.0.0.1:{self.porta}{caminho}", data=dados, method=metodo, headers=base)
        try:
            with urllib.request.urlopen(pedido, timeout=20) as resposta:
                return resposta.status, json.loads(resposta.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def esperar(self, trabalho, cabecalhos=None, prazo=20.0):
        limite = time.time() + prazo
        while time.time() < limite:
            _, visto = self.pedir("GET", f"/api/trabalho?id={trabalho}", cabecalhos=cabecalhos)
            if visto["estado"] != "rodando":
                return visto
            time.sleep(0.05)
        raise AssertionError("o turno não terminou a tempo")

    def fechar(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def servico(tmp_path):
    criados = []

    def abrir(**kwargs):
        criados.append(Servico(tmp_path, **kwargs))
        return criados[-1]

    yield abrir
    for item in criados:
        item.fechar()


def _turno(s):
    return {"pedido": PEDIDO, "repo": str(s.exemplo.caminho)}


def test_pagina_de_outro_site_nao_dispara_turno(servico):
    s = servico()
    antes = instantaneo(s.exemplo.caminho)
    corpo = json.dumps(_turno(s)).encode()
    codigo, _ = s.pedir("POST", "/api/turno", corpo, {"Content-Type": "text/plain"})
    assert codigo == 415
    codigo, _ = s.pedir("POST", "/api/turno", corpo, {"Origin": "https://site-malicioso.example"})
    assert codigo == 403
    codigo, _ = s.pedir("POST", "/api/turno", corpo, {"Origin": "null"})
    assert codigo == 403
    assert s.llm.chamadas == []
    assert s.memoria.listar() == []
    assert instantaneo(s.exemplo.caminho) == antes


def test_host_estranho_nao_le_nem_aplica(servico):
    s = servico()
    rebind = {"Host": "rebind.atacante.example"}
    assert s.pedir("GET", "/api/estado", cabecalhos=rebind)[0] == 403
    assert s.pedir("POST", "/api/aplicar", {"caso_id": "0123456789ab"}, rebind)[0] == 403
    assert s.pedir("GET", "/api/estado")[0] == 200
    mesma_origem = {"Origin": f"http://127.0.0.1:{s.porta}"}
    assert s.pedir("POST", "/api/aplicar", {"caso_id": "0123456789ab"}, mesma_origem)[0] == 400


def test_com_token_a_api_so_responde_a_quem_tem_o_token(servico):
    s = servico(token="segredo-de-teste")
    assert s.pedir("GET", "/api/estado")[0] == 401
    codigo, corpo = s.pedir("GET", "/api/estado", cabecalhos={"Authorization": "Bearer errado"})
    assert codigo == 401 and corpo["precisa_token"] is True
    assert s.pedir("POST", "/api/turno", _turno(s))[0] == 401
    certo = {"Authorization": "Bearer segredo-de-teste"}
    assert s.pedir("GET", "/api/estado", cabecalhos=certo)[0] == 200
    codigo, aberto = s.pedir("POST", "/api/turno", _turno(s), certo)
    assert codigo == 202
    assert s.esperar(aberto["trabalho"], certo)["estado"] == "concluido"
    assert s.llm.chamadas, "só com o token o turno roda"


def test_fora_do_loopback_exige_token_e_pasta_de_repositorios(tmp_path, monkeypatch):
    monkeypatch.delenv("FABRICA_TOKEN", raising=False)
    monkeypatch.delenv("FABRICA_REPOS", raising=False)
    with pytest.raises(ErroFabrica) as erro:
        criar_servidor("0.0.0.0", 0, memoria=Memoria(tmp_path / "m"))
    assert "FABRICA_TOKEN" in str(erro.value)
    with pytest.raises(ErroFabrica) as erro:
        criar_servidor("0.0.0.0", 0, memoria=Memoria(tmp_path / "m"), token="x")
    assert "FABRICA_REPOS" in str(erro.value)
    monkeypatch.setenv("FABRICA_REPOS", str(tmp_path))
    criar_servidor("0.0.0.0", 0, memoria=Memoria(tmp_path / "m"), token="x").server_close()


def test_repositorio_fora_da_pasta_permitida_e_recusado(servico, tmp_path):
    permitida = tmp_path / "permitida"
    permitida.mkdir()
    s = servico(raizes=[permitida])
    codigo, aberto = s.pedir("POST", "/api/turno", _turno(s))
    assert codigo == 202
    visto = s.esperar(aberto["trabalho"])
    assert visto["estado"] == "erro"
    assert "fora das pastas permitidas" in visto["erro"]
    assert s.llm.chamadas == []
    assert s.memoria.listar() == []


def test_turno_roda_em_segundo_plano_e_mostra_o_andamento(servico):
    s = servico(atraso=0.5)
    inicio = time.time()
    codigo, aberto = s.pedir("POST", "/api/turno", _turno(s))
    assert codigo == 202
    assert time.time() - inicio < 0.4, "a requisição não espera o turno"
    time.sleep(0.25)
    _, meio = s.pedir("GET", f"/api/trabalho?id={aberto['trabalho']}")
    assert meio["estado"] == "rodando"
    assert meio["passos"] == ["auditor:inicio"]
    assert meio["caso_id"]
    _, estado = s.pedir("GET", "/api/estado")
    assert estado["trabalho"] == aberto["trabalho"], "recarregar a página retoma o acompanhamento"
    recusado, erro = s.pedir("POST", "/api/aplicar", {"caso_id": meio["caso_id"]})
    assert recusado == 400 and "andamento" in erro["erro"]
    fim = s.esperar(aberto["trabalho"])
    assert fim["estado"] == "concluido"
    assert fim["passos"] == ["auditor:inicio", "codigo", "verificador", "revisor", "auditor:fim"]
    atual = fim["estado_caso"]["atual"]
    assert atual["pode_aplicar"] is True
    assert atual["verificacao"]["estado"] == "passou"
    assert [etapa["estado"] for etapa in atual["linhas"][0]["verificacao"]["etapas"]] == [
        "passou",
        "passou",
        "passou",
        "nao_conta",
    ]
    assert s.pedir("GET", "/api/estado")[1]["trabalho"] is None
    assert s.pedir("GET", "/api/trabalho?id=naoexiste")[0] == 404


def test_turno_que_falha_no_fim_devolve_o_erro_e_o_patch_guardado(servico):
    s = servico(falha_em="AUDITOR_FIM")
    _, aberto = s.pedir("POST", "/api/turno", _turno(s))
    fim = s.esperar(aberto["trabalho"])
    assert fim["estado"] == "erro"
    assert "429" in fim["erro"]
    atual = fim["estado_caso"]["atual"]
    assert atual["incompleto"]
    assert atual["pode_aplicar"] is False
    assert "parou antes do fim" in atual["resumo"]
    assert "TypeError" in atual["linhas"][0]["codigo"]["resultado"]["patch"]

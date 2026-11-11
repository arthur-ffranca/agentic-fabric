"""Uma reprodução por achado da avaliação de 11/11. Cada teste falhava antes da correção."""

import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from apoio import _git, criar_repo, diff_de, instantaneo, llm_roteiro
from fabrica.cli import construir_padrao
from fabrica.codigo import Codigo
from fabrica.erros import ErroLLM
from fabrica.llm import MODELO_PADRAO, ClienteOpenAICompativel
from fabrica.memoria import Memoria
from fabrica.orquestrador import OCUPADO
from fabrica.repositorio import (
    _recorte_por_termos,
    arquivos_importados,
    caminho_sensivel,
    coletar_fatos,
    listar_arquivos,
    protecoes_fixas,
)
from fabrica.tarefa import Tarefa, novo_id
from fabrica.uso import Medidor

PEDIDO = "Adiciona validação de tipo na função soma em app.py"


def _turno(tmp_path, llm, pedido=PEDIDO):
    exemplo = criar_repo(tmp_path)
    memoria = Memoria(tmp_path / "mem")
    orq = construir_padrao(llm(exemplo) if callable(llm) else llm, memoria)
    mensagem = orq.executar_turno(repo=str(exemplo.caminho), pedido=pedido, caso_id=None)
    return exemplo, memoria, orq, mensagem


# --- C3: o commit leva só os arquivos do patch


def test_commit_nao_leva_o_que_ja_estava_no_index(tmp_path):
    exemplo = criar_repo(tmp_path)
    (exemplo.caminho / "rascunho.txt").write_text("meu, sem relação\n", encoding="utf-8")
    _git(exemplo.caminho, "add", "rascunho.txt")
    llm, _ = llm_roteiro(exemplo.diff_app)
    memoria = Memoria(tmp_path / "mem")
    orq = construir_padrao(llm, memoria)
    mensagem = orq.executar_turno(repo=str(exemplo.caminho), pedido=PEDIDO, caso_id=None)
    assert orq.aplicar(mensagem.caso_id).codigo == 0
    assert orq.commit(mensagem.caso_id).codigo == 0
    no_commit = _git(exemplo.caminho, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert no_commit == ["app.py"]
    assert "A  rascunho.txt" in _git(exemplo.caminho, "status", "--porcelain").stdout


# --- C5: a aprovação vale para o estado revisado


def test_aplicar_recusa_quando_o_repositorio_ganhou_commit(tmp_path):
    exemplo, memoria, orq, mensagem = _turno(tmp_path, lambda ex: llm_roteiro(ex.diff_readme)[0])
    (exemplo.caminho / "app.py").write_text("def soma(a, b):\n    return a - b\n", encoding="utf-8")
    _git(exemplo.caminho, "commit", "-am", "muda soma")
    bloqueio = orq.aplicar(mensagem.caso_id)
    assert bloqueio.codigo == 1
    assert "mudou desde a revisão" in bloqueio.texto
    assert "Soma dois números.\n" == (exemplo.caminho / "README.md").read_text(encoding="utf-8").split("\n\n")[1]


def test_aplicar_recusa_quando_o_arquivo_do_patch_mudou(tmp_path):
    exemplo, memoria, orq, mensagem = _turno(tmp_path, lambda ex: llm_roteiro(ex.diff_app)[0])
    alvo = exemplo.caminho / "app.py"
    alvo.write_text(alvo.read_text(encoding="utf-8") + "\n\ndef outra():\n    return 1\n", encoding="utf-8")
    bloqueio = orq.aplicar(mensagem.caso_id)
    assert bloqueio.codigo == 1
    assert "app.py" in bloqueio.texto and "mudaram desde a revisão" in bloqueio.texto


# --- C6: um caso roda um passo por vez


def test_segundo_turno_no_mesmo_caso_e_recusado_e_nada_se_perde(tmp_path):
    exemplo = criar_repo(tmp_path)
    llm, _ = llm_roteiro(exemplo.diff_app, atraso=0.4)
    memoria = Memoria(tmp_path / "mem")
    orq = construir_padrao(llm, memoria)
    primeiro = orq.executar_turno(repo=str(exemplo.caminho), pedido=PEDIDO, caso_id=None)
    respostas = []

    def rodar(sufixo):
        respostas.append(orq.executar_turno(repo=None, pedido=f"{PEDIDO} ({sufixo})", caso_id=primeiro.caso_id))

    threads = [threading.Thread(target=rodar, args=(nome,)) for nome in ("dois", "tres")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    aceitos = [resposta for resposta in respostas if resposta.codigo == 0]
    recusados = [resposta for resposta in respostas if resposta.codigo != 0]
    assert len(aceitos) == 1 and len(recusados) == 1
    assert recusados[0].texto == OCUPADO
    assert len(memoria.carregar(primeiro.caso_id).turnos) == 2
    assert not list(memoria.dir_casos.glob("*.tmp"))


# --- C7: falha passageira não derruba o turno


class _Resposta:
    def __init__(self, corpo):
        self.corpo = corpo

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.corpo).encode()


def _erro_http(codigo, texto=b"", cabecalhos=None):
    return urllib.error.HTTPError("https://example.test", codigo, "erro", hdrs=cabecalhos, fp=io.BytesIO(texto))


def _cliente(monkeypatch, respostas, **extra):
    vistos = []
    esperas = []

    def falso(req, timeout=0):
        vistos.append(json.loads(req.data.decode()))
        atual = respostas.pop(0)
        if isinstance(atual, Exception):
            raise atual
        return _Resposta(atual)

    monkeypatch.setattr(urllib.request, "urlopen", falso)
    cliente = ClienteOpenAICompativel(api_key="sk-test", base_url="https://example.test/v1", esperar=esperas.append, **extra)
    return cliente, vistos, esperas


OK = {"choices": [{"message": {"content": "pronto"}}], "usage": {"prompt_tokens": 120, "completion_tokens": 30}}


def test_cliente_repete_em_429_e_respeita_retry_after(monkeypatch):
    cliente, vistos, esperas = _cliente(
        monkeypatch, [_erro_http(429, b"limite", {"Retry-After": "7"}), _erro_http(503, b"fora"), OK]
    )
    assert cliente.completar(sistema="s", usuario="u") == "pronto"
    assert len(vistos) == 3
    assert esperas == [7.0, 2.0]


def test_cliente_desiste_depois_das_tentativas(monkeypatch):
    cliente, vistos, _ = _cliente(monkeypatch, [_erro_http(429, b"limite")] * 3)
    with pytest.raises(ErroLLM) as erro:
        cliente.completar(sistema="s", usuario="u")
    assert "429" in str(erro.value) and len(vistos) == 3


def test_falha_no_ultimo_passo_guarda_o_patch_sem_liberar(tmp_path):
    exemplo, memoria, orq, mensagem = _turno(
        tmp_path, lambda ex: llm_roteiro(ex.diff_app, falha_em="AUDITOR_FIM")[0]
    )
    assert mensagem.codigo == 1
    assert "ficou guardado" in mensagem.texto
    caso = memoria.carregar(mensagem.caso_id)
    assert len(caso.turnos) == 1
    assert "TypeError" in caso.patch
    assert caso.turnos[-1]["incompleto"]
    assert caso.pode_aplicar()[0] is False
    assert orq.aplicar(mensagem.caso_id).codigo == 1
    assert instantaneo(exemplo.caminho)["status"] == ""


# --- M1, M2, M3: modelo fixo, temperatura que se adapta, uso medido e com teto


def test_modelo_padrao_e_fixo_no_codigo(monkeypatch):
    cliente, vistos, _ = _cliente(monkeypatch, [OK])
    cliente.completar(sistema="s", usuario="u")
    assert MODELO_PADRAO != "gpt-4o-mini"
    assert vistos[0]["model"] == MODELO_PADRAO


def test_modelo_que_recusa_temperatura_recebe_o_pedido_sem_ela(monkeypatch):
    recusa = _erro_http(400, b'{"error": {"message": "Unsupported value: temperature does not support 0.2"}}')
    cliente, vistos, esperas = _cliente(monkeypatch, [recusa, OK, OK])
    assert cliente.completar(sistema="s", usuario="u") == "pronto"
    assert "temperature" in vistos[0] and "temperature" not in vistos[1]
    cliente.completar(sistema="s", usuario="u")
    assert "temperature" not in vistos[2], "depois da recusa, não manda mais"
    assert esperas == []


def test_uso_e_medido_e_o_teto_para_o_turno(monkeypatch):
    cliente, vistos, _ = _cliente(monkeypatch, [OK, OK])
    medidor = Medidor(teto=200)
    with medidor.ativo():
        cliente.completar(sistema="s", usuario="u")
        assert medidor.total() == {"chamadas": 1, "tokens_entrada": 120, "tokens_saida": 30, "teto": 200}
        cliente.completar(sistema="s", usuario="u")
        with pytest.raises(ErroLLM) as erro:
            cliente.completar(sistema="s", usuario="u")
    assert "teto de 200 tokens" in str(erro.value)
    assert len(vistos) == 2, "a chamada acima do teto não sai"


def test_o_turno_guarda_o_uso(tmp_path):
    exemplo, memoria, orq, mensagem = _turno(tmp_path, lambda ex: llm_roteiro(ex.diff_app)[0])
    uso = memoria.carregar(mensagem.caso_id).turnos[-1]["uso"]
    assert set(uso) == {"chamadas", "tokens_entrada", "tokens_saida", "teto"}


# --- S3: segredo não vai ao modelo


@pytest.mark.parametrize(
    "caminho",
    [".env", ".env.production", "config/prod.env", "credentials.json", "secrets.yml", "chave.pem", "id_rsa", ".aws/credentials", "service-account-prod.json", ".npmrc"],
)
def test_arquivos_sensiveis(caminho):
    assert caminho_sensivel(caminho) is True


@pytest.mark.parametrize("caminho", [".env.example", "app.py", "credentials.py", "docs/secrets.md", "environment.ts", "token_utils.py"])
def test_arquivos_comuns_nao_sao_barrados(caminho):
    assert caminho_sensivel(caminho) is False


def test_segredo_nao_entra_no_recorte_nem_no_prompt(tmp_path):
    exemplo = criar_repo(tmp_path)
    (exemplo.caminho / ".env").write_text("OPENAI_API_KEY=sk-NAO-DEVERIA-SAIR\nSOMA_TIMEOUT=3\n", encoding="utf-8")
    (exemplo.caminho / "credentials.json").write_text('{"token": "NAO-DEVERIA-SAIR", "soma": true}\n', encoding="utf-8")
    pedido = "Ajusta o timeout da soma em app.py e em credentials.json e no .env"
    fatos = coletar_fatos(exemplo.caminho, pedido)
    assert "credentials.json" not in fatos["conteudos"] and ".env" not in fatos["conteudos"]
    assert {".env", "credentials.json"} <= set(protecoes_fixas(pedido, listar_arquivos(exemplo.caminho)))
    llm, _ = llm_roteiro(exemplo.diff_app)
    construir_padrao(llm, Memoria(tmp_path / "mem")).executar_turno(repo=str(exemplo.caminho), pedido=pedido, caso_id=None)
    assert not [c for c in llm.chamadas if "NAO-DEVERIA-SAIR" in c["usuario"]]


def test_patch_que_toca_segredo_nao_e_liberado(tmp_path):
    exemplo = criar_repo(tmp_path)
    (exemplo.caminho / ".env").write_text("A=1\n", encoding="utf-8")
    _git(exemplo.caminho, "add", "-f", ".env")
    _git(exemplo.caminho, "commit", "-m", "env")
    llm, _ = llm_roteiro(diff_de(exemplo.caminho, ".env", "A=2\n"))
    memoria = Memoria(tmp_path / "mem")
    orq = construir_padrao(llm, memoria)
    mensagem = orq.executar_turno(repo=str(exemplo.caminho), pedido="Troca o valor de A no .env do projeto", caso_id=None)
    assert memoria.carregar(mensagem.caso_id).pode_aplicar()[0] is False


# --- M4, M5: arquivo grande e escolha por import e símbolo


def test_arquivo_grande_mostra_a_regiao_citada_e_marca_o_que_omitiu():
    linhas = [f"valor_{numero} = {numero}" for numero in range(6000)]
    linhas[4200] = "def calcular_frete(pedido):"
    recorte = _recorte_por_termos("\n".join(linhas), ["calcular_frete"], limite=20_000)
    assert len(recorte) <= 21_000
    assert "def calcular_frete(pedido):" in recorte
    assert "valor_0 = 0" in recorte
    assert "… [linhas 61–4160 omitidas na inspeção] …" in recorte


def _repo_com_pacote(tmp_path):
    exemplo = criar_repo(tmp_path)
    pacote = exemplo.caminho / "loja"
    (pacote / "util").mkdir(parents=True)
    (pacote / "__init__.py").write_text("", encoding="utf-8")
    (pacote / "erros.py").write_text("class ErroLoja(Exception):\n    pass\n", encoding="utf-8")
    (pacote / "util" / "__init__.py").write_text("", encoding="utf-8")
    (pacote / "util" / "moeda.py").write_text("def arredondar(valor):\n    return round(valor, 2)\n", encoding="utf-8")
    (pacote / "frete.py").write_text(
        "from loja.erros import ErroLoja\nfrom .util import moeda\n\n\ndef calcular_frete(peso):\n    return moeda.arredondar(peso * 2)\n",
        encoding="utf-8",
    )
    for numero in range(12):
        (exemplo.caminho / f"nota_{numero}.txt").write_text("frete frete frete calcular\n", encoding="utf-8")
    _git(exemplo.caminho, "add", ".")
    _git(exemplo.caminho, "commit", "-m", "pacote")
    return exemplo


def test_o_que_o_arquivo_citado_importa_entra_no_recorte(tmp_path):
    exemplo = _repo_com_pacote(tmp_path)
    arquivos = listar_arquivos(exemplo.caminho)
    assert set(arquivos_importados(exemplo.caminho, "loja/frete.py", arquivos)) >= {"loja/erros.py", "loja/util/moeda.py"}
    fatos = coletar_fatos(exemplo.caminho, "Corrige o cálculo do frete em loja/frete.py para peso negativo")
    assert fatos["relevantes"][0] == "loja/frete.py"
    assert {"loja/erros.py", "loja/util/moeda.py"} <= set(fatos["relevantes"])


def test_quem_define_o_simbolo_citado_entra_no_recorte(tmp_path):
    exemplo = _repo_com_pacote(tmp_path)
    fatos = coletar_fatos(exemplo.caminho, "A função calcular_frete precisa recusar peso negativo")
    assert "loja/frete.py" in fatos["relevantes"]


# --- O4: arquivo em CRLF


def test_diff_em_lf_aplica_em_arquivo_crlf(tmp_path):
    exemplo = criar_repo(tmp_path)
    alvo = exemplo.caminho / "janela.py"
    alvo.write_bytes(b"def soma(a, b):\r\n    return a + b\r\n")
    _git(exemplo.caminho, "add", ".")
    _git(exemplo.caminho, "commit", "-m", "crlf")
    diff_lf = (
        "diff --git a/janela.py b/janela.py\n--- a/janela.py\n+++ b/janela.py\n"
        "@@ -1,2 +1,3 @@\n def soma(a, b):\n+    # nota\n     return a + b\n"
    )
    assert _git(exemplo.caminho, "apply", "--check", "-", entrada=diff_lf).returncode != 0

    class LLM:
        def completar(self, **kwargs):
            return diff_lf

    tarefa = Tarefa(
        id=novo_id(),
        destinatario="codigo",
        tipo="executar",
        o_que_fazer="nota",
        contexto={},
        entrada={"repo": str(exemplo.caminho), "pedido": "Adiciona nota em janela.py", "conteudos": {}},
        resultado_esperado="patch",
    )
    resultado = Codigo(LLM(), tentativas=1).executar(tarefa).resultado
    assert resultado["valido"] is True
    assert _git(exemplo.caminho, "apply", "-", entrada=resultado["patch"]).returncode == 0
    assert alvo.read_bytes() == b"def soma(a, b):\r\n    # nota\r\n    return a + b\r\n"


# --- o turno inteiro pelo cliente de verdade, contra um servidor compatível falso


def test_turno_inteiro_pelo_cliente_http_conta_o_uso_e_dispensa_a_temperatura(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    exemplo = criar_repo(tmp_path)
    roteiro, _ = llm_roteiro(exemplo.diff_app)
    recebidos = []

    class Modelo(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
            recebidos.append(corpo)
            if "temperature" in corpo:
                saida, codigo = {"error": {"message": "Unsupported parameter: 'temperature'"}}, 400
            else:
                texto = roteiro.completar(sistema=corpo["messages"][0]["content"], usuario=corpo["messages"][1]["content"])
                saida = {"choices": [{"message": {"content": texto}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 50}}
                codigo = 200
            dados = json.dumps(saida).encode()
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(dados)))
            self.end_headers()
            self.wfile.write(dados)

    servidor = ThreadingHTTPServer(("127.0.0.1", 0), Modelo)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    try:
        cliente = ClienteOpenAICompativel(api_key="sk-test", base_url=f"http://127.0.0.1:{servidor.server_address[1]}/v1")
        memoria = Memoria(tmp_path / "mem")
        orq = construir_padrao(cliente, memoria)
        mensagem = orq.executar_turno(repo=str(exemplo.caminho), pedido=PEDIDO, caso_id=None)
    finally:
        servidor.shutdown()
        servidor.server_close()
    assert mensagem.codigo == 0, mensagem.texto
    caso = memoria.carregar(mensagem.caso_id)
    assert caso.pode_aplicar() == (True, "")
    uso = caso.turnos[-1]["uso"]
    assert uso["chamadas"] == 6
    assert uso["tokens_entrada"] == 6000 and uso["tokens_saida"] == 300
    com_temperatura = [corpo for corpo in recebidos if "temperature" in corpo]
    assert 1 <= len(com_temperatura) <= 2, "só a primeira chamada de cada thread tenta com temperatura"
    assert "6 chamadas" in mensagem.texto

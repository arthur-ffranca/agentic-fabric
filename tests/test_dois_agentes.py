import json
import threading
import time
import urllib.error
import urllib.request

from apoio import LLMScript, _diff_e_restaura, criar_repo, instantaneo
from fabrica.auditor import Auditor
from fabrica.codigo import Codigo
from fabrica.fila import FilaEmProcesso
from fabrica.memoria import Memoria
from fabrica.orquestrador import Orquestrador
from fabrica.performance import Performance
from fabrica.revisor import Revisor
from fabrica.servidor import criar_servidor
from fabrica.tarefa import Tarefa, novo_id
from fabrica.triagem import Triagem, planejar_heuristica
from fabrica.verificador import Verificador


class FilaEspia(FilaEmProcesso):
    def __init__(self):
        super().__init__()
        self.destinos = []

    def despachar(self, tarefa, fn):
        self.destinos.append(tarefa.destinatario)
        return super().despachar(tarefa, fn)


def _pedido_triagem(repo: str) -> Tarefa:
    return Tarefa(
        id=novo_id(),
        destinatario="triagem",
        tipo="resolver",
        o_que_fazer="Resolver este turno",
        contexto={"caso_id": "c1", "turnos_anteriores": []},
        entrada={
            "pedido": "Adiciona validação de tipo na função soma em app.py",
            "repo": repo,
            "foco": ".",
        },
        resultado_esperado="Turno resolvido",
    )


def test_heuristica_separa_so_arquivos_diferentes():
    duas = planejar_heuristica(
        "Adiciona validação de tipo na função soma em app.py e documenta a função em outro/notas.txt"
    )
    assert [item["arquivos"] for item in duas] == [["app.py"], ["outro/notas.txt"]]
    uma = planejar_heuristica("Adiciona validação de tipo na função soma em app.py para rejeitar texto")
    assert len(uma) == 1
    assert uma[0]["o_que_fazer"].startswith("Adiciona validação")


def test_tarefas_em_arquivos_diferentes_correm_juntas(tmp_path):
    fila = FilaEspia()
    auditor = _AuditorConta()
    liberou = {"a": threading.Event(), "b": threading.Event()}
    visto = {}

    class CodigoA:
        def executar(self, tarefa):
            liberou["a"].set()
            assert liberou["b"].wait(2), "o segundo agente não começou junto"
            tarefa.resultado = {"patch": "PATCH-A\n", "valido": True, "arquivos": ["app.py"], "notas": ""}
            tarefa.status = "concluida"
            return tarefa

    class CodigoB:
        def executar(self, tarefa):
            liberou["b"].set()
            assert liberou["a"].wait(2), "o primeiro agente não começou junto"
            tarefa.resultado = {"patch": "PATCH-B\n", "valido": True, "arquivos": ["outro/notas.txt"], "notas": ""}
            tarefa.status = "concluida"
            return tarefa

    class Revisor:
        def executar(self, tarefa):
            tarefa.resultado = {"veredito": "aprovar", "motivos": ["cumpre a tarefa"], "correcoes": ""}
            tarefa.status = "concluida"
            return tarefa

    class Performance:
        def executar(self, tarefa, caixa):
            visto["patch"] = caixa.esperar(timeout=2)
            tarefa.resultado = {
                "propostas": [
                    {
                        "o_que_fazer": "Memoizar chamadas repetidas",
                        "onde": "app.py",
                        "o_que_melhora": "menos conta",
                    }
                ]
            }
            tarefa.status = "concluida"
            return tarefa

    triagem = Triagem(
        auditor,
        CodigoA(),
        Revisor(),
        Performance(),
        fila,
        codigo_b=CodigoB(),
        planejar=lambda pedido, restricoes: [
            {"titulo": "validação na soma", "o_que_fazer": "validar app.py", "arquivos": ["app.py"]},
            {"titulo": "nota ao lado", "o_que_fazer": "documentar outro/notas.txt", "arquivos": ["outro/notas.txt"]},
        ],
        timeout_performance=2,
    )
    resposta = triagem.receber(_pedido_triagem(str(tmp_path)))
    resultado = resposta.resultado
    assert auditor.fases == ["inicio", "fim", "fim"]
    assert resultado["paralelo"] is True
    assert [linha["nome"] for linha in resultado["linhas"]] == ["Código 1", "Código 2"]
    assert "PATCH-A" in resultado["patch"] and "PATCH-B" in resultado["patch"]
    assert "Memoizar" not in resultado["patch"]
    assert "PATCH-A" in visto["patch"] and "PATCH-B" in visto["patch"]
    assert fila.destinos == ["performance"]
    assert resultado["revisor"]["veredito"] == "aprovar"
    assert resultado["auditor_fim"]["comprometido"] is False


def test_mesmo_arquivo_espera(tmp_path):
    ordem = []
    vistos = []
    trava = threading.Lock()

    class Agente:
        def __init__(self, nome):
            self.nome = nome

        def executar(self, tarefa):
            with trava:
                ordem.append(("ini", self.nome))
                vistos.append(tarefa.entrada.get("patches_anteriores") or "")
            time.sleep(0.05)
            with trava:
                ordem.append(("fim", self.nome))
            tarefa.resultado = {
                "patch": f"PATCH-{self.nome}\n",
                "valido": True,
                "arquivos": ["app.py"],
                "notas": "",
            }
            tarefa.status = "concluida"
            return tarefa

    class Revisor:
        def executar(self, tarefa):
            tarefa.resultado = {"veredito": "aprovar", "motivos": ["ok"], "correcoes": ""}
            tarefa.status = "concluida"
            return tarefa

    class Performance:
        def executar(self, tarefa, caixa):
            caixa.esperar(timeout=2)
            tarefa.resultado = {"propostas": []}
            tarefa.status = "concluida"
            return tarefa

    triagem = Triagem(
        _AuditorConta(),
        Agente("a"),
        Revisor(),
        Performance(),
        FilaEmProcesso(),
        codigo_b=Agente("b"),
        planejar=lambda pedido, restricoes: [
            {"titulo": "primeira", "o_que_fazer": "muda app.py primeiro", "arquivos": ["app.py"]},
            {"titulo": "segunda", "o_que_fazer": "muda app.py depois", "arquivos": ["app.py"]},
        ],
        timeout_performance=2,
    )
    resposta = triagem.receber(_pedido_triagem(str(tmp_path)))
    assert ordem == [("ini", "a"), ("fim", "a"), ("ini", "b"), ("fim", "b")]
    assert vistos[0] == ""
    assert "PATCH-a" in vistos[1]
    assert resposta.resultado["paralelo"] is False
    assert "PATCH-a" in resposta.resultado["patch"] and "PATCH-b" in resposta.resultado["patch"]


class _AuditorConta:
    def __init__(self):
        self.fases = []

    def executar(self, tarefa):
        self.fases.append(tarefa.entrada["fase"])
        if tarefa.entrada["fase"] == "inicio":
            tarefa.resultado = {
                "resumo_estado": "ok",
                "o_que_ja_funciona": ["soma"],
                "nao_tocar": ["Não mexer em criar_pedido."],
                "nao_tocar_caminhos": [],
                "riscos": [],
                "conteudos": {},
                "fatos": {"arquivos": ["app.py", "outro/notas.txt"]},
            }
        else:
            tarefa.resultado = {"comprometido": False, "relato": "Nada que já existia foi comprometido."}
        tarefa.status = "concluida"
        return tarefa


def _llm_duas_tarefas(diff_app: str, diff_notas: str):
    def responder(sistema, usuario):
        if sistema.startswith("PAPEL: TRIAGEM"):
            return json.dumps(
                {
                    "tarefas": [
                        {
                            "titulo": "validação na função soma",
                            "o_que_fazer": "Adiciona validação de tipo na função soma em app.py",
                            "arquivos": ["app.py"],
                        },
                        {
                            "titulo": "nota ao lado da função",
                            "o_que_fazer": "Documenta a função em outro/notas.txt",
                            "arquivos": ["outro/notas.txt"],
                        },
                    ]
                }
            )
        if sistema.startswith("PAPEL: AUDITOR_INICIO"):
            assert "return a + b" in usuario
            assert "nada a ver com soma" in usuario
            return json.dumps(
                {
                    "resumo_estado": "soma em app.py e uma nota ao lado.",
                    "o_que_ja_funciona": ["soma devolve a soma dos argumentos"],
                    "nao_tocar": ["Não mexer em README.md."],
                    "nao_tocar_caminhos": ["README.md"],
                    "riscos": [],
                }
            )
        if sistema.startswith("PAPEL: CODIGO"):
            pedido = usuario.split("O que já funciona")[0]
            if "notas.txt" in pedido:
                return diff_notas
            return diff_app
        if sistema.startswith("PAPEL: REVISOR"):
            return json.dumps({"veredito": "aprovar", "motivos": ["cumpre a tarefa"], "correcoes": ""})
        if sistema.startswith("PAPEL: AUDITOR_FIM"):
            return json.dumps({"comprometido": False, "relato": "A mudança fica no arquivo desta tarefa."})
        if sistema.startswith("PAPEL: PERFORMANCE"):
            return json.dumps(
                {
                    "propostas": [
                        {
                            "o_que_fazer": "Memoizar chamadas repetidas de soma em app.py",
                            "onde": "app.py",
                            "o_que_melhora": "evita somar de novo os mesmos números",
                            "entrada": "Cache no módulo.",
                            "resultado_esperado": "Patch que cacheia soma.",
                        }
                    ]
                }
            )
        raise AssertionError(sistema)

    return LLMScript(responder)


def _commits(repo) -> int:
    import subprocess

    saida = subprocess.check_output(["git", "-C", str(repo), "rev-list", "--count", "HEAD"], text=True)
    return int(saida.strip())


def _nome_arquivos_do_head(repo) -> str:
    import subprocess

    return subprocess.check_output(
        ["git", "-C", str(repo), "show", "--name-only", "--pretty=format:", "HEAD"],
        text=True,
    )


def test_aplicar_e_commit_travados_na_interface(tmp_path, monkeypatch):
    exemplo = criar_repo(tmp_path)
    diff_notas = _diff_e_restaura(exemplo.caminho, "outro/notas.txt", "nada a ver com soma\n\nDocumentado.\n")
    antes = instantaneo(exemplo.caminho)
    llm = _llm_duas_tarefas(exemplo.diff_app, diff_notas)
    memoria = Memoria(tmp_path / "memoria")
    triagem = Triagem(
        Auditor(llm),
        Codigo(llm),
        Revisor(llm),
        Performance(llm),
        FilaEmProcesso(),
        codigo_b=Codigo(llm),
        llm=llm,
        verificador=Verificador(),
    )
    orq = Orquestrador(memoria, triagem)
    httpd = criar_servidor("127.0.0.1", 0, orquestrador=orq)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    porta = httpd.server_address[1]
    try:
        pagina = urllib.request.urlopen(f"http://127.0.0.1:{porta}/").read().decode()
        assert "Fábrica de agentes" in pagina
        assert "Rodar turno" in pagina
        assert ">Aplicar<" in pagina
        assert ">Commit<" in pagina
        estado = _get(porta, "/api/estado")
        assert estado["atual"] is None
        assert instantaneo(exemplo.caminho) == antes

        pedido = "Adiciona validação de tipo na função soma em app.py e documenta a função em outro/notas.txt"
        codigo, aberto = _post(porta, "/api/turno", {"pedido": pedido, "repo": str(exemplo.caminho)})
        assert codigo == 202, aberto
        trabalho = _esperar(porta, aberto["trabalho"])
        assert trabalho["estado"] == "concluido", trabalho
        assert trabalho["passos"][0] == "auditor:inicio"
        assert trabalho["passos"].count("verificador") == 3
        corpo = trabalho["estado_caso"]
        atual = corpo["atual"]
        assert atual["verificacao"]["estado"] == "passou"
        assert len(atual["linhas"]) == 2
        assert atual["paralelo"] is True
        assert atual["aplicado"] is False
        assert atual["pode_aplicar"] is True
        assert atual["pode_commitar"] is False
        assert "Memoizar" not in atual["linhas"][0]["codigo"]["resultado"]["patch"]
        assert "Memoizar" not in atual["linhas"][1]["codigo"]["resultado"]["patch"]
        assert instantaneo(exemplo.caminho) == antes
        assert _commits(exemplo.caminho) == 1
        inicios = [c for c in llm.chamadas if c["sistema"].startswith("PAPEL: AUDITOR_INICIO")]
        fins = [c for c in llm.chamadas if c["sistema"].startswith("PAPEL: AUDITOR_FIM")]
        assert len(inicios) == 1
        assert len(fins) == 2

        recusado, erro = _post(porta, "/api/commit", {"caso_id": corpo["caso_id"]})
        assert recusado == 400
        assert "Aplique" in erro["erro"]
        assert _commits(exemplo.caminho) == 1

        codigo, aplicado = _post(porta, "/api/aplicar", {"caso_id": corpo["caso_id"]})
        assert codigo == 200, aplicado
        assert aplicado["atual"]["aplicado"] is True
        assert aplicado["atual"]["commitado"] is False
        assert _commits(exemplo.caminho) == 1
        assert b"TypeError" in (exemplo.caminho / "app.py").read_bytes()
        assert b"Documentado." in (exemplo.caminho / "outro/notas.txt").read_bytes()
        assert "Não mexa neste arquivo." not in (exemplo.caminho / "README.md").read_text(encoding="utf-8")

        codigo, commitado = _post(porta, "/api/commit", {"caso_id": corpo["caso_id"]})
        assert codigo == 200, commitado
        assert commitado["atual"]["commitado"] is True
        assert commitado["atual"]["repo_limpo"] is True
        assert _commits(exemplo.caminho) == 2
        nomes = _nome_arquivos_do_head(exemplo.caminho)
        assert "app.py" in nomes
        assert "outro/notas.txt" in nomes
        assert "README.md" not in nomes
        remoto = __import__("subprocess").check_output(
            ["git", "-C", str(exemplo.caminho), "remote"], text=True
        )
        assert remoto.strip() == ""

        de_novo, erro = _post(porta, "/api/commit", {"caso_id": corpo["caso_id"]})
        assert de_novo == 400
        assert _commits(exemplo.caminho) == 2
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_sem_chave_a_interface_nao_inventa_patch(tmp_path, monkeypatch):
    for nome in ("OPENAI_API_KEY", "FABRICA_LLM_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(nome, raising=False)
    monkeypatch.setenv("FABRICA_HOME", str(tmp_path / "home"))
    exemplo = criar_repo(tmp_path)
    antes = instantaneo(exemplo.caminho)
    memoria = Memoria(tmp_path / "memoria")
    httpd = criar_servidor("127.0.0.1", 0, memoria=memoria)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    porta = httpd.server_address[1]
    try:
        codigo, corpo = _post(
            porta,
            "/api/turno",
            {"pedido": "Adiciona validação de tipo na função soma em app.py", "repo": str(exemplo.caminho)},
        )
        assert codigo == 400
        assert "OPENAI_API_KEY" in corpo["erro"]
        assert "não inventa patch" in corpo["erro"]
        assert instantaneo(exemplo.caminho) == antes
        assert memoria.listar() == []
    finally:
        httpd.shutdown()
        httpd.server_close()


def _esperar(porta: int, trabalho: str, prazo: float = 20.0) -> dict:
    limite = time.time() + prazo
    while time.time() < limite:
        visto = _get(porta, f"/api/trabalho?id={trabalho}")
        if visto["estado"] != "rodando":
            return visto
        time.sleep(0.05)
    raise AssertionError("o turno não terminou a tempo")


def _get(porta: int, caminho: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{porta}{caminho}") as resposta:
        return json.loads(resposta.read().decode())


def _post(porta: int, caminho: str, payload: dict) -> tuple[int, dict]:
    pedido = urllib.request.Request(
        f"http://127.0.0.1:{porta}{caminho}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(pedido) as resposta:
            return resposta.status, json.loads(resposta.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())

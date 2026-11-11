import threading

from apoio import criar_repo, instantaneo, llm_de_cadeia
from fabrica.fila import FilaEmProcesso
from fabrica.memoria import Memoria
from fabrica.orquestrador import Orquestrador
from fabrica.tarefa import Tarefa, novo_id
from fabrica.triagem import Triagem, pedido_ambiguo


class FilaEspia(FilaEmProcesso):
    def __init__(self):
        super().__init__()
        self.destinos: list[str] = []

    def despachar(self, tarefa, fn):
        self.destinos.append(tarefa.destinatario)
        assert tarefa.tipo == "executar"
        return super().despachar(tarefa, fn)


class Bomba:
    def executar(self, *args, **kwargs):
        raise AssertionError("especialista não deveria ser chamado")


def test_pedido_ambiguo():
    assert pedido_ambiguo("oi") is True
    assert pedido_ambiguo("melhora isso") is True
    assert pedido_ambiguo("arruma o bug") is True
    assert pedido_ambiguo("mexe em app.py") is True
    assert pedido_ambiguo("melhora isso por favor agora") is True
    assert pedido_ambiguo("corrige o bug do login") is False
    assert pedido_ambiguo("Adiciona validação de tipo na função soma em app.py") is False
    assert pedido_ambiguo("cria o arquivo utils/moeda.py com formatar_real") is False
    assert pedido_ambiguo("melhora a função soma para não aceitar string") is False
    assert pedido_ambiguo("ajusta a validação em app.py") is False


def test_triagem_pergunta_sem_despachar(tmp_path):
    fila = FilaEspia()
    triagem = Triagem(Bomba(), Bomba(), Bomba(), Bomba(), fila, timeout_performance=2)
    resposta = triagem.receber(
        Tarefa(
            id=novo_id(),
            destinatario="triagem",
            tipo="resolver",
            o_que_fazer="Resolver este turno",
            contexto={"caso_id": "c1", "pergunta_pendente": None, "turnos_anteriores": []},
            entrada={"pedido": "melhora isso", "repo": str(tmp_path), "foco": "."},
            resultado_esperado="Pergunta ou turno resolvido",
        )
    )
    assert resposta.tipo == "perguntar"
    assert resposta.destinatario == "orquestrador"
    assert "melhora isso" in resposta.entrada["pergunta"]
    assert resposta.contexto["pedido_original"] == "melhora isso"
    assert fila.destinos == []


def test_performance_corre_em_paralelo_e_nao_entra_no_patch():
    sync = {
        "comecou": threading.Event(),
        "viu_patch": threading.Event(),
        "aud_dentro": threading.Event(),
        "viu_auditor": threading.Event(),
    }
    patch = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n"

    class Auditor:
        def executar(self, tarefa):
            if tarefa.entrada["fase"] == "inicio":
                assert sync["comecou"].wait(2), "a performance não começou durante o auditor inicial"
                tarefa.resultado = {
                    "resumo_estado": "ok",
                    "o_que_ja_funciona": ["soma"],
                    "nao_tocar": [],
                    "nao_tocar_caminhos": [],
                    "riscos": [],
                    "arquivos_relevantes": [{"caminho": "app.py", "trecho": "def soma"}],
                    "conteudos": {"app.py": "def soma\n"},
                    "fatos": {"ramo": "main", "commit": "abc", "assunto": "inicio", "sujo": False, "total_arquivos": 1},
                }
                return tarefa
            assert sync["viu_patch"].wait(2), "a performance não esperava o patch durante o auditor final"
            sync["aud_dentro"].set()
            assert sync["viu_auditor"].wait(2), "a performance não seguiu junto com o auditor final"
            tarefa.resultado = {"comprometido": False, "relato": "nada comprometido", "por_caminhos": []}
            return tarefa

    class Codigo:
        def executar(self, tarefa):
            tarefa.resultado = {
                "patch": patch,
                "valido": True,
                "erro_apply": None,
                "notas": "",
                "arquivos": ["app.py"],
            }
            return tarefa

    class Revisor:
        def executar(self, tarefa):
            tarefa.resultado = {"veredito": "aprovar", "motivos": ["ok"], "correcoes": ""}
            return tarefa

    class Performance:
        def executar(self, tarefa, caixa):
            sync["comecou"].set()
            assert caixa.esperar(timeout=2) == patch
            sync["viu_patch"].set()
            assert sync["aud_dentro"].wait(2)
            sync["viu_auditor"].set()
            proposta = Tarefa(
                id="abc123abc123",
                destinatario="codigo",
                tipo="executar",
                o_que_fazer="Extrair a validação de soma para uma função nova em app.py",
                contexto={"origem": "performance", "onde": "app.py", "o_que_melhora": "deixa soma menor"},
                entrada={"onde": "app.py", "o_que_melhora": "deixa soma menor", "detalhe": "sem mudar o resultado"},
                resultado_esperado="Patch que extrai a validação.",
            )
            tarefa.resultado = {"propostas": [proposta.to_dict()]}
            return tarefa

    fila = FilaEspia()
    triagem = Triagem(Auditor(), Codigo(), Revisor(), Performance(), fila, timeout_performance=3)
    resposta = triagem.receber(
        Tarefa(
            id=novo_id(),
            destinatario="triagem",
            tipo="resolver",
            o_que_fazer="Resolver este turno",
            contexto={"caso_id": "c1", "pergunta_pendente": None, "turnos_anteriores": []},
            entrada={
                "pedido": "Adiciona validação de tipo na função soma em app.py",
                "repo": "/tmp/nao-lido",
                "foco": ".",
            },
            resultado_esperado="Turno resolvido",
        )
    )
    assert resposta.tipo == "resolver"
    assert fila.destinos == ["performance"]
    assert resposta.resultado["trilha"] == ["auditor:inicio", "codigo", "revisor", "auditor:fim"]
    assert resposta.resultado["patch"] == patch
    assert "Extrair a validação" not in resposta.resultado["patch"]
    assert resposta.resultado["propostas"][0]["destinatario"] == "codigo"
    assert resposta.resultado["propostas"][0]["tipo"] == "executar"
    assert resposta.resultado["propostas"][0]["o_que_fazer"].startswith("Extrair a validação")


def _montar(llm, tmp_path, *, max_ciclos=3, fila=None):
    from fabrica.auditor import Auditor
    from fabrica.codigo import Codigo
    from fabrica.performance import Performance
    from fabrica.revisor import Revisor

    memoria = Memoria(tmp_path / "mem")
    fila = fila or FilaEmProcesso()
    triagem = Triagem(
        Auditor(llm),
        Codigo(llm),
        Revisor(llm),
        Performance(llm),
        fila,
        max_ciclos=max_ciclos,
        timeout_performance=8,
    )
    return Orquestrador(memoria, triagem), memoria, fila


def test_orquestrador_delega_resolver_e_o_reparo_volta_ao_codigo(tmp_path):
    exemplo = criar_repo(tmp_path)
    antes = instantaneo(exemplo.caminho)
    llm, estado = llm_de_cadeia(exemplo, primeiro_patch=exemplo.diff_readme)
    fila = FilaEspia()
    orq, memoria, _ = _montar(llm, tmp_path, fila=fila)
    recebidas = []
    real = orq.triagem.receber

    def receber(tarefa):
        recebidas.append(tarefa)
        return real(tarefa)

    orq.triagem.receber = receber
    mensagem = orq.executar_turno(
        repo=str(exemplo.caminho),
        pedido="Adiciona validação de tipo na função soma em app.py",
        caso_id=None,
    )
    assert mensagem.codigo == 0
    assert recebidas[0].tipo == "resolver"
    assert recebidas[0].destinatario == "triagem"
    assert recebidas[0].entrada["pedido"] == "Adiciona validação de tipo na função soma em app.py"
    assert "turnos_anteriores" in recebidas[0].contexto
    assert fila.destinos == ["performance"]
    performance = [c for c in llm.chamadas if c["sistema"].startswith("PAPEL: PERFORMANCE")]
    assert performance and "TypeError" in performance[-1]["usuario"]
    caso = memoria.carregar(mensagem.caso_id)
    assert caso.turnos[-1]["trilha"] == [
        "auditor:inicio",
        "codigo",
        "revisor",
        "codigo",
        "revisor",
        "auditor:fim",
    ]
    assert estado["codigo"] == 2
    assert caso.veredito_revisor == "aprovar"
    assert caso.auditor_final_comprometido is False
    assert "TypeError" in caso.patch
    assert "Memoizar" not in caso.patch
    assert caso.propostas_pendentes[0]["tipo"] == "executar"
    assert caso.propostas_pendentes[0]["destinatario"] == "codigo"
    assert instantaneo(exemplo.caminho) == antes
    assert "Nada foi aplicado" in mensagem.texto
    assert "correções pedidas" in mensagem.texto
    assert "Veredito: aprovado" in mensagem.texto
    assert "auditor (início) → código → revisor → código → revisor → auditor (fim)" in mensagem.texto


def test_auditor_final_nao_e_pulado_quando_o_revisor_recusa_ate_o_fim(tmp_path):
    exemplo = criar_repo(tmp_path)
    antes = instantaneo(exemplo.caminho)
    llm, estado = llm_de_cadeia(exemplo, primeiro_patch=exemplo.diff_readme, sempre_ruim=True)
    orq, memoria, fila = _montar(llm, tmp_path, max_ciclos=2)
    mensagem = orq.executar_turno(
        repo=str(exemplo.caminho),
        pedido="Adiciona validação de tipo na função soma em app.py",
        caso_id=None,
    )
    caso = memoria.carregar(mensagem.caso_id)
    assert caso.turnos[-1]["trilha"] == [
        "auditor:inicio",
        "codigo",
        "revisor",
        "codigo",
        "revisor",
        "auditor:fim",
    ]
    assert estado["codigo"] == 2
    assert caso.veredito_revisor == "pedir_correcoes"
    assert caso.auditor_final_comprometido is True
    bloqueio = orq.aplicar(mensagem.caso_id)
    assert bloqueio.codigo == 1
    assert "bloqueada" in bloqueio.texto.lower()
    assert instantaneo(exemplo.caminho) == antes


def test_aplicar_bloqueado_quando_o_auditor_final_aponta_compromisso(tmp_path):
    exemplo = criar_repo(tmp_path)
    antes = instantaneo(exemplo.caminho)
    llm, _ = llm_de_cadeia(exemplo, primeiro_patch=exemplo.diff_app, auditor_compromete=True)
    orq, memoria, _ = _montar(llm, tmp_path)
    mensagem = orq.executar_turno(
        repo=str(exemplo.caminho),
        pedido="Adiciona validação de tipo na função soma em app.py",
        caso_id=None,
    )
    caso = memoria.carregar(mensagem.caso_id)
    assert caso.veredito_revisor == "aprovar"
    assert caso.auditor_final_comprometido is True
    bloqueio = orq.aplicar(mensagem.caso_id)
    assert bloqueio.codigo == 1
    assert instantaneo(exemplo.caminho) == antes


def test_aceitar_proposta_abre_turno_novo_na_mesma_cadeia(tmp_path):
    exemplo = criar_repo(tmp_path)
    llm, estado = llm_de_cadeia(exemplo, primeiro_patch=exemplo.diff_app)
    orq, memoria, _ = _montar(llm, tmp_path)
    primeiro = orq.executar_turno(
        repo=str(exemplo.caminho),
        pedido="Adiciona validação de tipo na função soma em app.py",
        caso_id=None,
    )
    caso = memoria.carregar(primeiro.caso_id)
    proposta = caso.propostas_pendentes[0]
    assert proposta["o_que_fazer"] == "Memoizar chamadas repetidas de soma em app.py"
    chamadas_antes = estado["codigo"]
    segundo = orq.aceitar(primeiro.caso_id, proposta["id"])
    assert segundo.codigo == 0
    assert estado["codigo"] == chamadas_antes + 1
    caso = memoria.carregar(primeiro.caso_id)
    assert len(caso.turnos) == 2
    assert "Memoizar chamadas repetidas" in caso.turnos[-1]["pedido"]
    codigos = [c["usuario"] for c in llm.chamadas if c["sistema"].startswith("PAPEL: CODIGO")]
    assert "Memoizar chamadas repetidas" in codigos[-1]
    assert caso.propostas_aceitas[0]["id"] == proposta["id"]
    assert instantaneo(exemplo.caminho)["arquivos"]["app.py"].decode().startswith("def soma")
    assert "aceite" in segundo.texto.lower()

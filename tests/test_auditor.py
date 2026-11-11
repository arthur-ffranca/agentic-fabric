import json

from apoio import LLMScript, LOCK, README, criar_repo, instantaneo
from fabrica.auditor import Auditor
from fabrica.tarefa import Tarefa, novo_id


def _tarefa(fase: str, repo, pedido: str, **entrada) -> Tarefa:
    return Tarefa(
        id=novo_id(),
        destinatario="auditor",
        tipo="executar",
        o_que_fazer="Auditar",
        contexto={},
        entrada={"fase": fase, "repo": str(repo), "foco": ".", "pedido": pedido, **entrada},
        resultado_esperado="Restrições" if fase == "inicio" else "Parecer de compromisso",
    )


def test_auditor_inicial_usa_fatos_do_disco_e_nao_o_que_o_modelo_inventar(tmp_path):
    exemplo = criar_repo(tmp_path)
    antes = instantaneo(exemplo.caminho)

    def responder(sistema, usuario):
        assert sistema.startswith("PAPEL: AUDITOR_INICIO")
        assert "return a + b" in usuario
        assert "inicio" in usuario
        assert "app.py" in usuario
        return json.dumps(
            {
                "ramo": "mentira",
                "resumo_estado": "Função soma isolada em app.py.",
                "o_que_ja_funciona": ["soma devolve a + b"],
                "nao_tocar": ["README.md não foi pedido"],
                "nao_tocar_caminhos": [],
                "riscos": [],
            }
        )

    llm = LLMScript(responder)
    tarefa = Auditor(llm).executar(
        _tarefa("inicio", exemplo.caminho, "Adiciona validação de tipo na função soma em app.py")
    )
    fatos = tarefa.resultado["fatos"]
    assert fatos["ramo"] == "main"
    assert fatos["assunto"] == "inicio"
    assert "return a + b" in fatos["conteudos"]["app.py"]
    assert tarefa.resultado.get("ramo") is None
    assert "poetry.lock" in tarefa.resultado["nao_tocar_caminhos"]
    assert "README.md" not in tarefa.resultado["nao_tocar_caminhos"]
    assert instantaneo(exemplo.caminho) == antes
    assert (exemplo.caminho / "poetry.lock").read_text(encoding="utf-8") == LOCK


def test_auditor_final_marca_compromisso_quando_o_patch_toca_caminho_proibido(tmp_path):
    exemplo = criar_repo(tmp_path)
    restricoes = {
        "nao_tocar": ["README.md não foi pedido"],
        "nao_tocar_caminhos": ["README.md"],
        "o_que_ja_funciona": ["soma devolve a + b"],
        "fatos": {"conteudos": {"app.py": "def soma(a, b):\n    return a + b\n"}},
    }

    def responder(sistema, usuario):
        assert sistema.startswith("PAPEL: AUDITOR_FIM")
        assert "README" in usuario or "Não mexa" in usuario
        return json.dumps({"comprometido": False, "relato": "O modelo deixaria passar."})

    llm = LLMScript(responder)
    ruim = Auditor(llm).executar(
        _tarefa(
            "fim",
            exemplo.caminho,
            "Adiciona validação de tipo na função soma em app.py",
            patch=exemplo.diff_readme,
            restricoes=restricoes,
        )
    )
    assert ruim.resultado["comprometido"] is True
    assert "README.md" in ruim.resultado["por_caminhos"]

    bom = Auditor(llm).executar(
        _tarefa(
            "fim",
            exemplo.caminho,
            "Adiciona validação de tipo na função soma em app.py",
            patch=exemplo.diff_app,
            restricoes=restricoes,
        )
    )
    assert bom.resultado["comprometido"] is False
    assert instantaneo(exemplo.caminho)["arquivos"]["README.md"] == README.encode()

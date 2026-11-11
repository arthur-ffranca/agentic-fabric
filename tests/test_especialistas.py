import json

from apoio import LLMScript, criar_repo, instantaneo
from fabrica.codigo import Codigo
from fabrica.revisor import Revisor
from fabrica.tarefa import Tarefa, novo_id


def _tarefa_codigo(repo, pedido, diff_esperado=None, **extra):
    return Tarefa(
        id=novo_id(),
        destinatario="codigo",
        tipo="executar",
        o_que_fazer=pedido,
        contexto={},
        entrada={
            "repo": str(repo),
            "pedido": pedido,
            "restricoes": {
                "nao_tocar": ["README.md não foi pedido"],
                "nao_tocar_caminhos": ["README.md", "poetry.lock"],
                "o_que_ja_funciona": ["soma devolve a + b"],
            },
            "conteudos": {"app.py": (repo / "app.py").read_text(encoding="utf-8")},
            "correcoes": extra.get("correcoes"),
            "patch_anterior": extra.get("patch_anterior"),
            "historico": [],
        },
        resultado_esperado="Patch unificado aplicável e ainda não aplicado",
    )


def test_codigo_devolve_patch_sem_aplicar(tmp_path):
    exemplo = criar_repo(tmp_path)
    antes = instantaneo(exemplo.caminho)
    diff = exemplo.diff_app

    def responder(sistema, usuario):
        assert sistema.startswith("PAPEL: CODIGO")
        assert "return a + b" in usuario
        assert "README.md" in usuario
        return f"Segue a alteração pedida.\n```diff\n{diff}```\n"

    tarefa = Codigo(LLMScript(responder)).executar(
        _tarefa_codigo(exemplo.caminho, "Adiciona validação de tipo na função soma em app.py")
    )
    assert tarefa.resultado["valido"] is True
    assert "TypeError" in tarefa.resultado["patch"]
    assert tarefa.resultado["arquivos"] == ["app.py"]
    assert instantaneo(exemplo.caminho) == antes


def test_codigo_nao_devolve_patch_que_escapa_do_repositorio(tmp_path):
    exemplo = criar_repo(tmp_path)
    antes = instantaneo(exemplo.caminho)
    ruim = "diff --git a/../segredo b/../segredo\n--- a/../segredo\n+++ b/../segredo\n@@ -0,0 +1 @@\n+x\n"

    tarefa = Codigo(LLMScript(lambda sistema, usuario: ruim)).executar(
        _tarefa_codigo(exemplo.caminho, "Adiciona validação de tipo na função soma em app.py")
    )
    assert tarefa.resultado["valido"] is False
    assert instantaneo(exemplo.caminho) == antes
    assert not (tmp_path / "segredo").exists()


def test_revisor_nao_aprova_caminho_proibido_mesmo_se_o_modelo_aprovar(tmp_path):
    exemplo = criar_repo(tmp_path)

    def responder(sistema, usuario):
        return json.dumps({"veredito": "aprovar", "motivos": ["parece ok"], "correcoes": ""})

    tarefa = Tarefa(
        id=novo_id(),
        destinatario="revisor",
        tipo="executar",
        o_que_fazer="Revisar o patch",
        contexto={},
        entrada={
            "repo": str(exemplo.caminho),
            "pedido": "Adiciona validação de tipo na função soma em app.py",
            "patch": exemplo.diff_readme,
            "restricoes": {"nao_tocar_caminhos": ["README.md"], "nao_tocar": ["README.md"], "o_que_ja_funciona": []},
            "conteudos": {},
        },
        resultado_esperado="aprovar ou pedir correções",
    )
    resultado = Revisor(LLMScript(responder)).executar(tarefa).resultado
    assert resultado["veredito"] == "pedir_correcoes"
    assert "README.md" in resultado["correcoes"]

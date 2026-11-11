import pytest

from fabrica.tarefa import Tarefa, novo_id


def test_contrato_da_tarefa_e_o_mesmo_para_os_tres_usos():
    tarefa = Tarefa(
        id=novo_id(),
        destinatario="triagem",
        tipo="resolver",
        o_que_fazer="Resolver este turno",
        contexto={"caso_id": "abc"},
        entrada={"pedido": "Adiciona validação"},
        resultado_esperado="Turno resolvido ou uma pergunta",
    )
    assert tarefa.resultado is None
    assert tarefa.status == "pendente"
    dados = tarefa.to_dict()
    for campo in (
        "id",
        "destinatario",
        "tipo",
        "o_que_fazer",
        "contexto",
        "entrada",
        "resultado_esperado",
        "resultado",
    ):
        assert campo in dados
    de_volta = Tarefa.from_dict(dados)
    assert de_volta == tarefa

    concluida = Tarefa(
        id=novo_id(),
        destinatario="codigo",
        tipo="executar",
        o_que_fazer="Alterar app.py",
        contexto={},
        entrada={},
        resultado_esperado="Um patch",
        resultado={"patch": "diff"},
        status="concluida",
    )
    assert Tarefa.from_dict(concluida.to_dict()).resultado == {"patch": "diff"}


def test_rejeita_tipo_que_nao_e_tarefa():
    with pytest.raises(ValueError):
        Tarefa(
            id="x",
            destinatario="triagem",
            tipo="chat",
            o_que_fazer="conversar",
            contexto={},
            entrada={},
            resultado_esperado="nada",
        )


def test_tres_tipos_validos():
    for tipo, destino in (
        ("resolver", "triagem"),
        ("executar", "auditor"),
        ("perguntar", "orquestrador"),
    ):
        tarefa = Tarefa(
            id=novo_id(),
            destinatario=destino,
            tipo=tipo,
            o_que_fazer="fazer",
            contexto={},
            entrada={},
            resultado_esperado="pronto",
        )
        assert tarefa.tipo == tipo

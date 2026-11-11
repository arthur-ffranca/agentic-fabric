"""Texto que o orquestrador devolve à ponte. Especialista não fala com o usuário."""

from __future__ import annotations

_ROTULO = {
    "auditor:inicio": "auditor (início)",
    "codigo": "código",
    "revisor": "revisor",
    "verificador": "verificação",
    "auditor:fim": "auditor (fim)",
}


def _cadeia(trilha: list[str]) -> str:
    return " → ".join(_ROTULO.get(passo, passo) for passo in trilha)


def _bloco(itens: list[str] | None) -> list[str]:
    if not itens:
        return ["(nada registrado)"]
    return [f"- {item}" for item in itens]


def apresentar_pergunta(caso, tarefa) -> str:
    return (
        f"Caso {caso.id}\n"
        f"Repositório: {caso.repo}\n\n"
        "A triagem precisa de um detalhe antes de mexer no repositório.\n\n"
        f"{tarefa.entrada.get('pergunta')}\n\n"
        "Nada foi alterado.\n"
        f'Continuar: fabrica turno --caso {caso.id} --pedido "sua resposta"\n'
    )


def apresentar_turno(caso, resultado: dict, origem_proposta: dict | None = None) -> str:
    inicio = resultado.get("auditor_inicio") or {}
    fatos = inicio.get("fatos") or {}
    fim = resultado.get("auditor_fim") or {}
    revisor = resultado.get("revisor") or {}
    commit = (fatos.get("commit") or "")[:7]
    linhas = [
        f"Caso {caso.id}",
        f"Repositório: {caso.repo}",
        f'Ramo {fatos.get("ramo") or "?"}, commit {commit or "?"}, "{fatos.get("assunto") or ""}".',
    ]
    if fatos.get("sujo"):
        linhas.append("Worktree com alteração local. O patch é calculado em cima do que está no disco.")
    else:
        linhas.append("Worktree limpo no início da inspeção.")
    if caso.foco not in {"", "."}:
        linhas.append(f"Recorte: {caso.foco}")
    linhas.append(f"O git lista {fatos.get('total_arquivos', '?')} arquivos.")
    if origem_proposta:
        linhas.append(
            f"Turno aberto a partir da proposta {origem_proposta.get('id')}, com o seu aceite."
        )
    linhas.append("Cadeia: " + _cadeia(resultado.get("trilha") or []))
    linhas.append("")
    linhas.append("Auditor (início)")
    if inicio.get("resumo_estado"):
        linhas.append(inicio["resumo_estado"])
    lidos = inicio.get("arquivos_relevantes") or []
    if lidos:
        linhas.append("Arquivos lidos: " + ", ".join(item["caminho"] for item in lidos) + ".")
        for item in lidos:
            linhas.append(f"\n{item['caminho']}\n{item.get('trecho') or ''}")
    linhas.append("O que já funciona:")
    linhas.extend(_bloco(inicio.get("o_que_ja_funciona")))
    linhas.append("Não tocar:")
    linhas.extend(_bloco(inicio.get("nao_tocar")))
    if inicio.get("nao_tocar_caminhos"):
        linhas.append("Caminhos protegidos: " + ", ".join(inicio["nao_tocar_caminhos"]) + ".")
    if inicio.get("riscos"):
        linhas.append("Riscos:")
        linhas.extend(_bloco(inicio.get("riscos")))
    linhas.append("")
    linhas.append("Código")
    arquivos = resultado.get("arquivos_patch") or []
    if arquivos:
        linhas.append("Arquivos no patch: " + ", ".join(arquivos) + ".")
    if resultado.get("codigo_valido") is False:
        linhas.append("O patch não ficou aplicável.")
        if resultado.get("codigo_notas"):
            linhas.append(str(resultado["codigo_notas"]))
    patch = resultado.get("patch") or ""
    linhas.append(patch if patch else "(sem patch)")
    linhas.append("Ainda não aplicado.")
    linhas.append("")
    linhas.append("Revisor")
    rodadas = resultado.get("rodadas_revisor") or ([revisor] if revisor else [])
    for indice, rodada in enumerate(rodadas, start=1):
        if len(rodadas) > 1:
            linhas.append(f"Rodada {indice}")
        if rodada.get("veredito") == "aprovar":
            linhas.append("Veredito: aprovado")
        else:
            linhas.append("Veredito: correções pedidas")
        for motivo in rodada.get("motivos") or []:
            linhas.append(f"- {motivo}")
        if rodada.get("veredito") != "aprovar" and rodada.get("correcoes"):
            linhas.append(str(rodada["correcoes"]))
    verificacao = resultado.get("verificacao")
    if isinstance(verificacao, dict):
        linhas.append("")
        linhas.append("Verificação")
        linhas.append("Resultado: passou" if verificacao.get("ok") else "Resultado: falhou. Aplicação bloqueada.")
        for etapa in verificacao.get("etapas") or []:
            marca = {True: "ok", False: "falhou"}.get(etapa.get("ok"), "não conta")
            linhas.append(f"- {etapa.get('nome')}: {marca}. {etapa.get('detalhe') or ''}".rstrip())
    linhas.append("")
    linhas.append("Auditor (fim)")
    if fim.get("comprometido"):
        linhas.append("Compromisso do que já funciona: sim. Aplicação bloqueada.")
    elif fim.get("comprometido") is False:
        linhas.append("Compromisso do que já funciona: nenhum detectado.")
    else:
        linhas.append("Compromisso do que já funciona: auditoria final ausente. Aplicação bloqueada.")
    if fim.get("relato"):
        linhas.append(str(fim["relato"]))
    linhas.append("")
    linhas.append("Performance")
    linhas.append("Fora da cadeia de entrega. Nada disto foi escrito no patch.")
    if resultado.get("aviso_performance"):
        linhas.append(f"Não concluiu: {resultado['aviso_performance']}")
        linhas.append("A cadeia de entrega acima não foi alterada por isso.")
    propostas = resultado.get("propostas") or []
    if not propostas and not resultado.get("aviso_performance"):
        linhas.append("Nenhuma proposta concreta neste turno.")
    for indice, proposta in enumerate(propostas, start=1):
        entrada = proposta.get("entrada") or {}
        contexto = proposta.get("contexto") or {}
        onde = entrada.get("onde") or contexto.get("onde") or ""
        melhora = entrada.get("o_que_melhora") or contexto.get("o_que_melhora") or ""
        linhas.append(f"{indice}. {proposta.get('id')}")
        linhas.append(f"   Fazer: {proposta.get('o_que_fazer')}")
        linhas.append(f"   Onde: {onde}")
        linhas.append(f"   Melhora: {melhora}")
        linhas.append(
            f"   Aceitar só esta: fabrica aceitar --caso {caso.id} --proposta {proposta.get('id')}"
        )
    uso = resultado.get("uso") or {}
    if uso.get("chamadas"):
        linhas.append("")
        linhas.append(
            f"Modelo: {uso['chamadas']} chamadas, {uso.get('tokens_entrada', 0)} tokens de entrada "
            f"e {uso.get('tokens_saida', 0)} de saída."
        )
    linhas.append("")
    linhas.append(f"Nada foi aplicado em {caso.repo}.")
    liberado, _ = caso.pode_aplicar()
    if liberado:
        linhas.append(f"Aplicar este patch: fabrica aplicar --caso {caso.id}")
    else:
        linhas.append(
            "Aplicar permanece bloqueado até a verificação passar, o revisor aprovar "
            "e o auditor final não apontar compromisso."
        )
    return "\n".join(linhas).rstrip() + "\n"

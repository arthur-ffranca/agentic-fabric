"""Texto compartilhado pelos especialistas. Não é uma camada nova do fluxo."""

from __future__ import annotations


def papel(nome: str, instrucao: str) -> str:
    return f"PAPEL: {nome}\n{instrucao.strip()}\n"


def lista_de_textos(valor) -> list[str]:
    if valor is None:
        return []
    if isinstance(valor, str):
        texto = valor.strip()
        return [texto] if texto else []
    if isinstance(valor, list):
        return [str(item).strip() for item in valor if str(item).strip()]
    texto = str(valor).strip()
    return [texto] if texto else []


def formatar_itens(valor) -> str:
    itens = lista_de_textos(valor)
    if not itens:
        return "(nada registrado)"
    return "\n".join(f"- {item}" for item in itens)

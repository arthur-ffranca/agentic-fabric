"""Revisor aprova ou devolve o patch ao código. Não escreve no repositório."""

from __future__ import annotations

from pathlib import Path

from fabrica.erros import ErroLLM
from fabrica.llm import solicitar_json
from fabrica.repositorio import caminho_proibido, checar_patch, formatar_conteudos
from fabrica.tarefa import Tarefa
from fabrica.texto import formatar_itens, lista_de_textos, papel


class Revisor:
    def __init__(self, llm) -> None:
        self.llm = llm

    def executar(self, tarefa: Tarefa) -> Tarefa:
        repo = Path(tarefa.entrada["repo"])
        patch = tarefa.entrada.get("patch") or ""
        restricoes = tarefa.entrada.get("restricoes") or {}
        proibidos = list(restricoes.get("nao_tocar_caminhos") or [])
        duros: list[str] = []
        if not patch.strip():
            duros.append("O código não entregou um patch.")
        else:
            valido, erro, caminhos = checar_patch(repo, patch, tarefa.entrada.get("patches_anteriores") or "")
            if not valido:
                duros.append(erro or "O patch não aplica no worktree.")
            else:
                ruins = [caminho for caminho in caminhos if caminho_proibido(caminho, proibidos)]
                if ruins:
                    duros.append("O patch altera caminho que não pode ser tocado: " + ", ".join(ruins) + ".")
        parecer = None
        if patch.strip():
            sistema = papel(
                "REVISOR",
                """
Você revisa se o patch cumpre o pedido e respeita as restrições do auditor.
Responda só com JSON:
{"veredito": "aprovar" ou "pedir_correcoes", "motivos": ["..."], "correcoes": "instrução concreta se pedir correção, senão string vazia"}
Não aprove patch que fuja do pedido ou quebre o que já funciona.
Não escreva outro diff. Não aplique nada.
""",
            )
            usuario = (
                f"Pedido:\n{tarefa.entrada.get('pedido')}\n\n"
                f"O que já funciona:\n{formatar_itens(restricoes.get('o_que_ja_funciona'))}\n\n"
                f"Não tocar:\n{formatar_itens(restricoes.get('nao_tocar'))}\n"
                f"Caminhos proibidos: {', '.join(proibidos) or '(nenhum)'}\n\n"
                "Arquivos de referência:\n"
                + formatar_conteudos(tarefa.entrada.get("conteudos") or {})
                + f"\nPatch:\n{patch}\n"
            )
            try:
                parecer = solicitar_json(self.llm, sistema, usuario)
            except ErroLLM as exc:
                if not duros:
                    raise
                duros.append(f"O modelo não revisou: {exc}")
        if duros:
            veredito = "pedir_correcoes"
            correcoes = "\n".join(duros)
            if parecer and parecer.get("correcoes"):
                correcoes += "\n" + str(parecer.get("correcoes"))
            motivos = duros + lista_de_textos((parecer or {}).get("motivos"))
        else:
            veredito = (parecer or {}).get("veredito")
            if veredito not in {"aprovar", "pedir_correcoes"}:
                veredito = "pedir_correcoes"
            motivos = lista_de_textos((parecer or {}).get("motivos"))
            correcoes = "" if veredito == "aprovar" else str((parecer or {}).get("correcoes") or "")
            if veredito != "aprovar" and not correcoes:
                correcoes = "O revisor não aprovou. Refaça o patch para cumprir o pedido sem violar as restrições."
        tarefa.resultado = {"veredito": veredito, "motivos": motivos, "correcoes": correcoes}
        tarefa.status = "concluida"
        return tarefa

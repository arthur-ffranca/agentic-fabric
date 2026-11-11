"""Auditor somente leitura. Os fatos vêm do disco; o modelo só interpreta."""

from __future__ import annotations

from pathlib import Path

from fabrica.erros import ErroFabrica, ErroLLM
from fabrica.llm import solicitar_json
from fabrica.repositorio import (
    caminho_inseguro,
    caminho_proibido,
    caminhos_conhecidos,
    caminhos_do_patch,
    coletar_fatos,
    formatar_conteudos,
    protecoes_fixas,
)
from fabrica.tarefa import Tarefa
from fabrica.texto import formatar_itens, lista_de_textos, papel


class Auditor:
    def __init__(self, llm) -> None:
        self.llm = llm

    def executar(self, tarefa: Tarefa) -> Tarefa:
        fase = tarefa.entrada.get("fase")
        if fase == "inicio":
            tarefa.resultado = self._inicio(tarefa)
        elif fase == "fim":
            tarefa.resultado = self._fim(tarefa)
        else:
            raise ErroFabrica("Fase de auditoria desconhecida.")
        tarefa.status = "concluida"
        return tarefa

    def _inicio(self, tarefa: Tarefa) -> dict:
        repo = Path(tarefa.entrada["repo"])
        pedido = tarefa.entrada.get("pedido") or ""
        foco = tarefa.entrada.get("foco") or "."
        fatos = coletar_fatos(repo, pedido, foco=foco)
        sistema = papel(
            "AUDITOR_INICIO",
            """
Você é o auditor, somente leitura. Não escreva patch e não sugira aplicar nada.
Responda em português, só com um objeto JSON, usando exclusivamente os fatos recebidos.
Campos:
- resumo_estado: string
- o_que_ja_funciona: lista de strings
- nao_tocar: lista de regras em texto
- nao_tocar_caminhos: lista de caminhos relativos que já existem e NÃO devem ser modificados
- riscos: lista de strings
nao_tocar_caminhos não pode incluir arquivo que o pedido precise alterar.
Não invente arquivo, commit ou comportamento que não esteja nos fatos.
Não coloque lockfile em nao_tocar_caminhos: esses arquivos já são protegidos à parte.
""",
        )
        usuario = (
            f"Pedido:\n{pedido}\n\n"
            f"Foco: {foco}\n"
            f"Ramo: {fatos['ramo']}\n"
            f"Commit: {fatos['commit']}\n"
            f"Assunto: {fatos['assunto']}\n"
            f"Worktree sujo: {fatos['sujo']}\n"
            "Status:\n"
            + ("\n".join(fatos["status"]) if fatos["status"] else "(limpo)")
            + "\n\nÁrvore:\n"
            + fatos["arvore_resumida"]
            + "\n\nArquivos lidos:\n"
            + formatar_conteudos(fatos["conteudos"])
        )
        modelo = solicitar_json(self.llm, sistema, usuario)
        caminhos = caminhos_conhecidos(lista_de_textos(modelo.get("nao_tocar_caminhos")), fatos["arquivos"])
        for extra in protecoes_fixas(pedido, fatos["arquivos"]):
            if extra not in caminhos:
                caminhos.append(extra)
        relevantes = []
        for caminho, texto in fatos["conteudos"].items():
            linhas = texto.splitlines()
            trecho = "\n".join(linhas[:12])
            if len(linhas) > 12:
                trecho += "\n…"
            relevantes.append({"caminho": caminho, "trecho": trecho})
        return {
            "resumo_estado": str(modelo.get("resumo_estado") or "").strip(),
            "o_que_ja_funciona": lista_de_textos(modelo.get("o_que_ja_funciona")),
            "nao_tocar": lista_de_textos(modelo.get("nao_tocar")),
            "nao_tocar_caminhos": caminhos,
            "riscos": lista_de_textos(modelo.get("riscos")),
            "arquivos_relevantes": relevantes,
            "conteudos": fatos["conteudos"],
            "fatos": fatos,
        }

    def _fim(self, tarefa: Tarefa) -> dict:
        patch = tarefa.entrada.get("patch") or ""
        restricoes = tarefa.entrada.get("restricoes") or {}
        proibidos = list(restricoes.get("nao_tocar_caminhos") or [])
        caminhos = caminhos_do_patch(patch) if patch.strip() else []
        ruins = [caminho for caminho in caminhos if caminho_inseguro(caminho) or caminho_proibido(caminho, proibidos)]
        sistema = papel(
            "AUDITOR_FIM",
            """
Você é o auditor final, somente leitura. Compare o patch com o que já funciona e com as restrições.
Diga se alguma coisa que já existia foi comprometida. Responda só com JSON:
{"comprometido": false, "relato": "texto em português"}
comprometido é true se o patch muda comportamento que deveria permanecer, apaga cobertura que não foi pedida, ou foge das restrições.
Não aprove aplicação. Você não escreve no repositório.
""",
        )
        usuario = (
            f"Pedido:\n{tarefa.entrada.get('pedido')}\n\n"
            f"O que já funciona:\n{formatar_itens(restricoes.get('o_que_ja_funciona'))}\n\n"
            f"Não tocar:\n{formatar_itens(restricoes.get('nao_tocar'))}\n"
            f"Caminhos proibidos: {', '.join(proibidos) or '(nenhum)'}\n\n"
            f"Patch:\n{patch or '(vazio)'}\n"
        )
        try:
            modelo = solicitar_json(self.llm, sistema, usuario)
        except ErroLLM as exc:
            if not ruins:
                raise
            return {
                "comprometido": True,
                "relato": (
                    "O patch viola restrições ("
                    + ", ".join(ruins)
                    + f"). O modelo não concluiu o parecer: {exc}"
                ),
                "por_caminhos": ruins,
            }
        relato = str(modelo.get("relato") or "").strip()
        if "comprometido" not in modelo:
            parecer = True
            relato = relato or "O parecer do modelo veio incompleto."
        else:
            parecer = bool(modelo.get("comprometido"))
        if ruins:
            return {
                "comprometido": True,
                "relato": "O patch altera caminho protegido: " + ", ".join(ruins) + ". " + relato,
                "por_caminhos": ruins,
                "parecer_modelo": parecer,
            }
        return {
            "comprometido": parecer,
            "relato": relato or ("Compromisso apontado." if parecer else "Nenhum compromisso apontado."),
            "por_caminhos": [],
            "parecer_modelo": parecer,
        }

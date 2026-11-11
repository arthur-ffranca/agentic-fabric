"""Código devolve um patch. Não aplica no repositório alvo."""

from __future__ import annotations

from pathlib import Path

from fabrica.repositorio import (
    adaptar_fim_de_linha,
    caminhos_do_patch,
    checar_patch,
    extrair_diff,
    formatar_conteudos,
)
from fabrica.tarefa import Tarefa
from fabrica.texto import formatar_itens, papel


class Codigo:
    def __init__(self, llm, tentativas: int = 3) -> None:
        self.llm = llm
        self.tentativas = tentativas

    def executar(self, tarefa: Tarefa) -> Tarefa:
        repo = Path(tarefa.entrada["repo"])
        pedido = tarefa.entrada.get("pedido") or ""
        restricoes = tarefa.entrada.get("restricoes") or {}
        conteudos = tarefa.entrada.get("conteudos") or {}
        correcoes = (tarefa.entrada.get("correcoes") or "").strip()
        anterior = tarefa.entrada.get("patch_anterior") or ""
        historico = tarefa.entrada.get("historico") or []
        sistema = papel(
            "CODIGO",
            """
Você implementa a mudança pedida e devolve somente um diff unificado, compatível com git apply,
com caminhos relativos à raiz do repositório.
Não aplique o patch. Não rode git commit. Não explique fora do diff.
Se usar cerca markdown, o conteúdo dela é só o diff.
Não altere caminhos proibidos. Não reverta mudança local que não faça parte do pedido.
Contexto do diff tem que bater com o arquivo atual.
Se não der para fazer com segurança, responda exatamente SEM_PATCH e, na linha seguinte, o motivo.
""",
        )
        base = (
            f"Pedido:\n{pedido}\n\n"
            f"O que já funciona e deve continuar:\n{formatar_itens(restricoes.get('o_que_ja_funciona'))}\n\n"
            f"Não tocar:\n{formatar_itens(restricoes.get('nao_tocar'))}\n"
            "Caminhos proibidos:\n"
            + "\n".join(restricoes.get("nao_tocar_caminhos") or ["(nenhum)"])
            + "\n\nHistórico recente do caso:\n"
            + (str(historico) if historico else "(primeiro turno)")
            + "\n\nConteúdo atual dos arquivos relevantes:\n"
            + formatar_conteudos(conteudos)
        )
        if correcoes:
            base += (
                "\nO revisor devolveu o trabalho. Não repita o patch recusado.\n"
                f"Correção:\n{correcoes}\n"
            )
        if anterior:
            base += f"\nPatch anterior recusado:\n{anterior}\n"
        foco = tarefa.entrada.get("arquivos_desta_tarefa") or []
        if foco:
            base += "\nEsta tarefa só pode alterar: " + ", ".join(foco) + ".\n"
        anteriores = tarefa.entrada.get("patches_anteriores") or ""
        if anteriores:
            if tarefa.entrada.get("conteudos_com_anteriores"):
                aviso = (
                    "\nOutro agente já produziu este patch, ainda sem aplicar no disco. "
                    "O conteúdo dos arquivos acima já inclui esse patch, e o seu diff precisa aplicar depois dele.\n"
                )
            else:
                aviso = (
                    "\nOutro agente já produziu este patch, ainda sem aplicar no disco. "
                    "O seu diff precisa aplicar depois dele.\n"
                )
            base += aviso + anteriores + "\n"
        usuario = base
        patch = ""
        valido = False
        ultimo_erro = None
        caminhos: list[str] = []
        notas = ""
        for tentativa in range(self.tentativas):
            texto = self.llm.completar(sistema=sistema, usuario=usuario, temperatura=0.1)
            if texto.strip().startswith("SEM_PATCH"):
                notas = texto.strip()
                patch = ""
                valido = False
                ultimo_erro = notas
                caminhos = []
                break
            patch = extrair_diff(texto)
            valido, ultimo_erro, caminhos = checar_patch(repo, patch, anteriores)
            if not valido and patch:
                # Arquivo em CRLF no disco e diff em LF do modelo: tenta de novo com o fim de linha do arquivo.
                adaptado = adaptar_fim_de_linha(repo, patch)
                if adaptado != patch:
                    valido_crlf, _, caminhos_crlf = checar_patch(repo, adaptado, anteriores)
                    if valido_crlf:
                        patch, valido, ultimo_erro, caminhos = adaptado, True, None, caminhos_crlf
            if valido:
                notas = ""
                break
            if tentativa + 1 >= self.tentativas:
                break
            usuario = (
                base
                + "\nO diff anterior foi recusado:\n"
                + (ultimo_erro or "inválido")
                + "\nDevolva um diff unificado corrigido, e nada além disso.\nDiff recusado:\n"
                + patch
            )
        if not valido and patch:
            caminhos = caminhos_do_patch(patch)
        tarefa.resultado = {
            "patch": patch,
            "valido": valido,
            "erro_apply": ultimo_erro,
            "notas": notas,
            "arquivos": caminhos,
        }
        tarefa.status = "concluida"
        return tarefa

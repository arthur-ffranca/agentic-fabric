"""Performance observa o recorte e o patch. Não escreve e não altera o patch."""

from __future__ import annotations

from fabrica.erros import ErroLLM, ErroRepositorio
from fabrica.llm import solicitar_json
from fabrica.repositorio import coletar_fatos, formatar_conteudos
from fabrica.tarefa import Tarefa, novo_id
from fabrica.texto import papel


class Performance:
    def __init__(self, llm) -> None:
        self.llm = llm

    def executar(self, tarefa: Tarefa, caixa) -> Tarefa:
        repo = str(tarefa.entrada.get("repo") or "")
        pedido = tarefa.entrada.get("pedido") or ""
        foco = tarefa.entrada.get("foco") or "."
        try:
            fatos = coletar_fatos(repo, pedido, foco=foco)
        except (ErroRepositorio, OSError) as exc:
            fatos = {
                "conteudos": {},
                "ramo": "",
                "assunto": "",
                "arvore_resumida": str(exc),
                "relevantes": [],
            }
        patch = caixa.esperar()
        if not patch and getattr(caixa, "erro", None):
            tarefa.resultado = {"propostas": [], "aviso": caixa.erro}
            tarefa.status = "falhou"
            return tarefa
        sistema = papel(
            "PERFORMANCE",
            """
Você observa o recorte do repositório e o patch já proposto.
Não altere o patch. Não escreva diff. Não peça para aplicar nada agora.
Devolva só JSON com até 3 propostas concretas, ou uma lista vazia se não houver ganho real.
{"propostas": [{"o_que_fazer": "...", "onde": "caminho ou símbolo", "o_que_melhora": "...", "entrada": "detalhe para implementar", "resultado_esperado": "Patch que ..."}]}
Cada proposta diz o que mudar, onde, e o que melhora, com base nos arquivos lidos.
Não repita o que o patch deste turno já faz. Não invente arquivo que você não viu.
""",
        )
        usuario = (
            f"Pedido deste turno:\n{pedido}\n\n"
            f"Ramo: {fatos.get('ramo')} · {fatos.get('assunto')}\n"
            f"Árvore:\n{fatos.get('arvore_resumida')}\n\n"
            f"Patch proposto, que você não deve modificar:\n{patch or '(sem patch)'}\n\n"
            "Arquivos lidos:\n"
            + formatar_conteudos(fatos.get("conteudos") or {})
        )
        try:
            dados = solicitar_json(self.llm, sistema, usuario)
        except ErroLLM as exc:
            tarefa.resultado = {"propostas": [], "aviso": str(exc)}
            tarefa.status = "falhou"
            return tarefa
        propostas = []
        for item in (dados.get("propostas") or [])[:3]:
            if not isinstance(item, dict):
                continue
            o_que = str(item.get("o_que_fazer") or "").strip()
            onde = str(item.get("onde") or "").strip()
            melhora = str(item.get("o_que_melhora") or "").strip()
            if not o_que or not onde or not melhora:
                continue
            esperado = str(item.get("resultado_esperado") or "").strip() or f"Patch que aplica: {o_que}"
            propostas.append(
                Tarefa(
                    id=novo_id(),
                    destinatario="codigo",
                    tipo="executar",
                    o_que_fazer=o_que,
                    contexto={
                        "origem": "performance",
                        "repo": repo,
                        "onde": onde,
                        "o_que_melhora": melhora,
                    },
                    entrada={
                        "onde": onde,
                        "o_que_melhora": melhora,
                        "detalhe": str(item.get("entrada") or o_que).strip(),
                    },
                    resultado_esperado=esperado,
                ).to_dict()
            )
        tarefa.resultado = {"propostas": propostas}
        tarefa.status = "concluida"
        return tarefa

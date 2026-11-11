"""Monta o estado que a interface desenha. Não roda agente e não escreve no repositório."""

from __future__ import annotations

from datetime import datetime, timezone

from fabrica.memoria import Caso
from fabrica.repositorio import git


def estado_vazio() -> dict:
    return {
        "caso_id": None,
        "repo": "",
        "pergunta": None,
        "atual": None,
        "historico": [],
    }


def estado_caso(caso: Caso) -> dict:
    historico = []
    total = len(caso.turnos)
    for indice, turno in enumerate(caso.turnos, start=1):
        historico.append(_volta(caso, turno, indice, ultima=indice == total))
    return {
        "caso_id": caso.id,
        "repo": caso.repo,
        "pergunta": (caso.pergunta_pendente or {}).get("pergunta") if caso.pergunta_pendente else None,
        "atual": historico[-1] if historico else None,
        "historico": historico,
    }


def _volta(caso: Caso, turno: dict, numero: int, *, ultima: bool) -> dict:
    linhas = _linhas(turno)
    aplicado = bool(turno.get("aplicado")) if not ultima else bool(caso.aplicado)
    commitado = bool(turno.get("commitado")) if not ultima else bool(caso.commitado)
    commit = turno.get("commit") if not ultima else caso.commit_hash
    if ultima:
        limpo = worktree_limpa(caso.repo)
        pode_aplicar, motivo_aplicar = caso.pode_aplicar()
        pode_commitar, motivo_commit = caso.pode_commitar()
    else:
        limpo = bool(turno.get("repo_limpo"))
        pode_aplicar, motivo_aplicar = False, "Só a volta atual pode ser aplicada."
        pode_commitar, motivo_commit = False, "Só a volta atual pode ser commitada."
    revisor = turno.get("revisor") or {}
    auditor_fim = turno.get("auditor_fim") or {}
    aprovado = revisor.get("veredito") == "aprovar" and auditor_fim.get("comprometido") is False
    if linhas:
        aprovado = all(
            (linha.get("revisor") or {}).get("veredito") == "aprovar"
            and (linha.get("auditor_fim") or {}).get("comprometido") is False
            for linha in linhas
        )
    return {
        "volta": numero,
        "quando": _hora(turno.get("quando")),
        "titulo": _titulo_volta(linhas, turno),
        "pedido": turno.get("pedido_usuario") or turno.get("pedido") or "",
        "repo": caso.repo,
        "caso_id": caso.id,
        "resumo": _resumo(linhas, bool(turno.get("paralelo")), aplicado, commitado),
        "aprovado": aprovado,
        "aplicado": aplicado,
        "commitado": commitado,
        "commit": commit,
        "repo_limpo": limpo,
        "eh_ultima": ultima,
        "pode_aplicar": pode_aplicar,
        "motivo_aplicar": motivo_aplicar,
        "pode_commitar": pode_commitar,
        "motivo_commit": motivo_commit,
        "paralelo": bool(turno.get("paralelo")) and len(linhas) > 1,
        "auditor_inicio": {
            "resultado": {
                "restricoes": _restricoes(turno.get("auditor_inicio")),
                "resumo_estado": (turno.get("auditor_inicio") or {}).get("resumo_estado") or "",
            }
        },
        "linhas": [_linha_ui(linha, aplicado) for linha in linhas],
        "performance": {"resultado": {"propostas": _propostas(turno.get("propostas") or [])}},
    }


def worktree_limpa(repo: str) -> bool:
    if not repo:
        return False
    proc = git(repo, "status", "--porcelain")
    return proc.returncode == 0 and not (proc.stdout or "").strip()


def _linhas(turno: dict) -> list[dict]:
    linhas = turno.get("linhas") or []
    if linhas:
        return linhas
    if not turno.get("patch") and not turno.get("revisor"):
        return []
    return [
        {
            "nome": "Código",
            "titulo": _curto(turno.get("pedido_usuario") or turno.get("pedido") or "turno"),
            "o_que_fazer": turno.get("pedido") or "",
            "arquivos": [],
            "patch": turno.get("patch") or "",
            "revisor": turno.get("revisor") or {},
            "auditor_fim": turno.get("auditor_fim") or {},
            "paralelo": False,
        }
    ]


def _linha_ui(linha: dict, aplicado: bool) -> dict:
    revisor = linha.get("revisor") or {}
    auditor = linha.get("auditor_fim") or {}
    arquivos = list(linha.get("arquivos") or [])
    veredito = revisor.get("veredito") or ""
    pontos = list(revisor.get("motivos") or [])
    if not pontos and revisor.get("correcoes"):
        pontos = [revisor.get("correcoes")]
    return {
        "nome": linha.get("nome") or "Código",
        "titulo": linha.get("titulo") or _curto(linha.get("o_que_fazer") or ""),
        "o_que_cuida": linha.get("o_que_fazer") or linha.get("titulo") or "",
        "codigo": {
            "resultado": {
                "patch": linha.get("patch") or "",
                "arquivos_tocados": arquivos,
                "aplicado_no_repo": aplicado,
            }
        },
        "revisor": {"resultado": {"veredito": veredito, "pontos": pontos}},
        "auditor_fim": {
            "resultado": {
                "comprometeu_algo": auditor.get("comprometido") is True,
                "parecer": auditor.get("relato") or "",
            }
        },
    }


def _propostas(propostas: list) -> list[dict]:
    saida = []
    for proposta in propostas:
        if not isinstance(proposta, dict):
            continue
        entrada = proposta.get("entrada") or {}
        contexto = proposta.get("contexto") or {}
        saida.append(
            {
                "id": proposta.get("id"),
                "o_que_fazer": proposta.get("o_que_fazer") or "",
                "onde": entrada.get("onde") or contexto.get("onde") or "",
                "o_que_melhora": entrada.get("o_que_melhora") or contexto.get("o_que_melhora") or "",
            }
        )
    return saida


def _restricoes(inicio: dict | None) -> list[str]:
    itens = [str(item) for item in ((inicio or {}).get("nao_tocar") or []) if str(item).strip()]
    if not any("aplicar" in item.lower() for item in itens):
        itens.append("Não aplicar mudança no repositório.")
    return itens


def _resumo(linhas: list[dict], paralelo: bool, aplicado: bool, commitado: bool) -> str:
    if not linhas:
        return "O turno não gerou patch."
    if len(linhas) == 1:
        abertura = f"Neste turno um agente de código trabalhou na tarefa {linhas[0].get('titulo') or 'pedida'}."
    elif paralelo:
        abertura = (
            "Neste turno dois agentes de código trabalharam em paralelo, "
            "cada um numa tarefa independente."
        )
    else:
        abertura = (
            "Neste turno dois agentes de código trabalharam em sequência, "
            "porque as tarefas tocam o mesmo arquivo."
        )
    partes = []
    for indice, linha in enumerate(linhas, start=1):
        titulo = linha.get("titulo") or f"tarefa {indice}"
        aprovou = (linha.get("revisor") or {}).get("veredito") == "aprovar"
        revisao = "o revisor aprovou" if aprovou else "o revisor pediu correção"
        if (linha.get("auditor_fim") or {}).get("comprometido") is False:
            auditoria = "nada que já existia foi comprometido"
        else:
            auditoria = "o auditor final apontou compromisso"
        partes.append(f"o código {indice} devolveu o patch de {titulo}, {revisao}, e {auditoria}")
    if commitado:
        fecho = "Os patches foram aplicados e commitados no repositório local."
    elif aplicado:
        fecho = "Os patches foram aplicados no worktree. Ainda não há commit."
    else:
        fecho = "Nenhum patch foi aplicado."
    return abertura + " " + "; ".join(partes) + ". " + fecho


def _titulo_volta(linhas: list[dict], turno: dict) -> str:
    if len(linhas) >= 2:
        return _curto(f"{linhas[0].get('titulo') or 'tarefa 1'} e {linhas[1].get('titulo') or 'tarefa 2'}", 90)
    if linhas:
        return _curto(linhas[0].get("titulo") or turno.get("pedido_usuario") or "turno", 90)
    return _curto(turno.get("pedido_usuario") or turno.get("pedido") or "turno", 90)


def _curto(texto: str, limite: int = 80) -> str:
    linha = " ".join(str(texto or "").split())
    if len(linha) <= limite:
        return linha
    return linha[: limite - 3].rstrip() + "..."


def _hora(valor: str | None) -> str:
    if not valor:
        return ""
    texto = str(valor)
    if len(texto) == 5 and texto[2] == ":":
        return texto
    try:
        instante = datetime.fromisoformat(texto)
    except ValueError:
        return ""
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=timezone.utc)
    return instante.astimezone().strftime("%H:%M")

"""Ponte de linha de comando. Fala só com o orquestrador."""

from __future__ import annotations

import argparse
import sys

from fabrica.auditor import Auditor
from fabrica.codigo import Codigo
from fabrica.erros import ErroFabrica, ErroLLM
from fabrica.fila import FilaEmProcesso
from fabrica.llm import ClienteOpenAICompativel
from fabrica.memoria import Memoria
from fabrica.orquestrador import Orquestrador
from fabrica.performance import Performance
from fabrica.revisor import Revisor
from fabrica.triagem import Triagem
from fabrica.verificador import Verificador


def construir_padrao(llm=None, memoria=None, *, max_ciclos: int = 3, timeout_performance: float = 180) -> Orquestrador:
    memoria = memoria or Memoria()
    if llm is None:
        llm = ClienteOpenAICompativel()
    triagem = Triagem(
        Auditor(llm),
        Codigo(llm),
        Revisor(llm),
        Performance(llm),
        FilaEmProcesso(),
        codigo_b=Codigo(llm),
        llm=llm,
        verificador=Verificador(),
        max_ciclos=max_ciclos,
        timeout_performance=timeout_performance,
    )
    return Orquestrador(memoria, triagem)


def main(argv: list[str] | None = None, *, construir=None) -> int:
    _utf8()
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.comando == "casos":
            _imprimir(_texto_casos(Memoria()))
            return 0
        if args.comando == "patch":
            return _imprimir_mensagem_texto(*_texto_patch(Memoria(), args.caso))
        if args.comando == "propostas":
            return _imprimir_mensagem_texto(*_texto_propostas(Memoria(), args.caso))
        if args.comando == "aplicar":
            mensagem = Orquestrador(Memoria()).aplicar(args.caso)
            _imprimir(mensagem.texto)
            return mensagem.codigo
        if args.comando == "commit":
            mensagem = Orquestrador(Memoria()).commit(args.caso)
            _imprimir(mensagem.texto)
            return mensagem.codigo
        try:
            orquestrador = (construir or construir_padrao)()
        except ErroLLM as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.comando in {"turno", "continuar"}:
            mensagem = orquestrador.executar_turno(
                repo=getattr(args, "repo", None),
                pedido=_pedido(args),
                caso_id=getattr(args, "caso", None),
            )
        elif args.comando == "aceitar":
            mensagem = orquestrador.aceitar(args.caso, args.proposta)
        else:
            parser.error(f"comando desconhecido: {args.comando}")
            return 2
        _imprimir(mensagem.texto)
        return mensagem.codigo
    except ErroFabrica as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fabrica",
        description=(
            "Fábrica de agentes. O caminho do dia é a interface web (python -m fabrica). "
            "Este CLI continua o mesmo turno. Nada é aplicado sozinho, e commit é outro passo."
        ),
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    turno = sub.add_parser("turno", help="Roda um turno com um pedido em português")
    turno.add_argument("--repo", help="Caminho do repositório git. Obrigatório para abrir um caso.")
    turno.add_argument("--caso", help="Continua um caso já aberto.")
    turno.add_argument("--pedido", required=True, help='O que mudar. Use "-" para ler o stdin.')

    continuar = sub.add_parser("continuar", help="Continua um caso com outro pedido ou com a resposta")
    continuar.add_argument("--caso", required=True)
    continuar.add_argument("--pedido", required=True, help='Resposta ou pedido novo. Use "-" para ler o stdin.')

    sub.add_parser("casos", help="Lista os casos guardados nesta máquina")

    patch = sub.add_parser("patch", help="Mostra o último patch do caso")
    patch.add_argument("--caso", required=True)

    propostas = sub.add_parser("propostas", help="Lista as propostas de performance ainda não aceitas")
    propostas.add_argument("--caso", required=True)

    aceitar = sub.add_parser("aceitar", help="Aceita uma proposta e roda de novo a mesma cadeia")
    aceitar.add_argument("--caso", required=True)
    aceitar.add_argument("--proposta", required=True, help="Id da tarefa de performance")

    aplicar = sub.add_parser(
        "aplicar",
        help="Aplica o último patch no worktree, só se o revisor aprovou e o auditor final liberou",
    )
    aplicar.add_argument("--caso", required=True)

    commit = sub.add_parser("commit", help="Cria um commit local depois de aplicar. Não empurra para o remoto.")
    commit.add_argument("--caso", required=True)
    return parser


def _pedido(args) -> str:
    texto = args.pedido
    if texto == "-":
        return sys.stdin.read().strip()
    return texto


def _texto_casos(memoria: Memoria) -> str:
    casos = memoria.listar()
    if not casos:
        return "Nenhum caso ainda.\n"
    linhas = []
    for caso in casos:
        if caso.turnos:
            pedido = (caso.turnos[-1].get("pedido_usuario") or caso.turnos[-1].get("pedido") or "").replace("\n", " ")
            pedido = pedido[:80]
        elif caso.pergunta_pendente:
            pedido = "pergunta em aberto"
        else:
            pedido = ""
        linhas.append(f"{caso.id}  {caso.repo}  {pedido}".rstrip())
    return "\n".join(linhas) + "\n"


def _texto_patch(memoria: Memoria, caso_id: str) -> tuple[str, int]:
    caso = memoria.carregar(caso_id)
    if not caso.patch:
        return "Este caso ainda não tem patch.\n", 1
    texto = caso.patch if caso.patch.endswith("\n") else caso.patch + "\n"
    return texto, 0


def _texto_propostas(memoria: Memoria, caso_id: str) -> tuple[str, int]:
    caso = memoria.carregar(caso_id)
    if not caso.propostas_pendentes:
        return "Nenhuma proposta pendente.\n", 0
    linhas = []
    for proposta in caso.propostas_pendentes:
        entrada = proposta.get("entrada") or {}
        contexto = proposta.get("contexto") or {}
        linhas.append(proposta.get("o_que_fazer") or "")
        onde = entrada.get("onde") or contexto.get("onde")
        melhora = entrada.get("o_que_melhora") or contexto.get("o_que_melhora")
        if onde:
            linhas.append(f"Onde: {onde}")
        if melhora:
            linhas.append(f"Melhora: {melhora}")
        linhas.append(f"fabrica aceitar --caso {caso.id} --proposta {proposta.get('id')}")
        linhas.append("")
    return "\n".join(linhas).rstrip() + "\n", 0


def _imprimir(texto: str) -> None:
    if texto and not texto.endswith("\n"):
        texto += "\n"
    print(texto, end="")


def _imprimir_mensagem_texto(texto: str, codigo: int) -> int:
    _imprimir(texto)
    return codigo


def _utf8() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        reconfigure = getattr(fluxo, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

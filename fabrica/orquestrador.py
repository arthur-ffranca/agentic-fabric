"""O orquestrador lê o caso, manda o turno à triagem e é o único que fala com a ponte.

Não escolhe especialista e não aplica mudança por conta própria.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from pathlib import Path

from fabrica.apresentacao import apresentar_pergunta, apresentar_turno
from fabrica.erros import ErroFabrica, ErroRepositorio
from fabrica.memoria import Memoria, agora
from fabrica.repositorio import (
    aplicar_patch,
    caminho_inseguro,
    caminho_proibido,
    caminho_sensivel,
    caminhos_do_patch,
    commit_atual,
    criar_commit,
    dentro_das_raizes,
    impressao_dos_arquivos,
    localizar_repositorio,
    raizes_do_ambiente,
)
from fabrica.tarefa import Tarefa, novo_id


@dataclass
class Mensagem:
    caso_id: str
    texto: str
    codigo: int = 0


OCUPADO = "Este caso já tem um passo em andamento. Espere terminar e tente de novo."


class Orquestrador:
    def __init__(self, memoria: Memoria, triagem=None, raizes=None) -> None:
        self.memoria = memoria
        self.triagem = triagem
        # Pastas em que um repositório alvo pode estar. None: qualquer caminho (uso local).
        self.raizes = raizes_do_ambiente() if raizes is None else raizes
        self._travas: dict[str, threading.Lock] = {}
        self._trava_das_travas = threading.Lock()

    def _trava(self, caso_id: str) -> threading.Lock:
        with self._trava_das_travas:
            return self._travas.setdefault(caso_id, threading.Lock())

    def executar_turno(
        self,
        *,
        repo: str | None,
        pedido: str,
        caso_id: str | None,
        origem_proposta: dict | None = None,
        progresso=None,
    ) -> Mensagem:
        """Um caso roda um passo por vez. O segundo pedido ao mesmo tempo é recusado, não perdido em silêncio."""
        if not caso_id:
            return self._turno(repo, pedido, None, origem_proposta, progresso)
        trava = self._trava(caso_id)
        if not trava.acquire(blocking=False):
            return Mensagem(caso_id, OCUPADO, codigo=1)
        try:
            return self._turno(repo, pedido, caso_id, origem_proposta, progresso)
        finally:
            trava.release()

    def _turno(self, repo, pedido, caso_id, origem_proposta, progresso) -> Mensagem:
        pedido = (pedido or "").strip()
        if not pedido:
            return Mensagem(caso_id or "", "O pedido está vazio.", codigo=1)
        caso, erro = self._abrir(repo, caso_id)
        if erro:
            return erro
        assert caso is not None
        if caso_id:
            return self._rodar(caso, pedido, origem_proposta, progresso)
        # Caso recém-aberto: a trava começa a valer aqui, para aplicar ou outro turno não entrarem no meio.
        with self._trava(caso.id):
            return self._rodar(caso, pedido, origem_proposta, progresso)

    def _rodar(self, caso, pedido, origem_proposta, progresso) -> Mensagem:
        tarefa = Tarefa(
            id=novo_id(),
            destinatario="triagem",
            tipo="resolver",
            o_que_fazer="Resolver este turno",
            contexto={
                "caso_id": caso.id,
                "repo": caso.repo,
                "foco": caso.foco,
                "turnos_anteriores": [
                    {
                        "pedido": turno.get("pedido"),
                        "veredito": turno.get("veredito_revisor"),
                    }
                    for turno in caso.turnos[-5:]
                ],
                "pergunta_pendente": caso.pergunta_pendente,
            },
            entrada={"pedido": pedido, "repo": caso.repo, "foco": caso.foco},
            resultado_esperado=(
                "Turno resolvido com restrições, patch, revisão, auditoria final e propostas, "
                "ou uma pergunta ao usuário."
            ),
        )
        if self.triagem is None:
            raise ErroFabrica("A triagem não está disponível.")
        # A ponte chama o orquestrador direto, e daqui a triagem também é chamada direta.
        if progresso is not None:
            progresso("caso:" + caso.id)
        try:
            if progresso is None:
                resposta = self.triagem.receber(tarefa)
            else:
                resposta = self.triagem.receber(tarefa, ao_passo=progresso)
        except ErroFabrica as exc:
            parcial = getattr(exc, "parcial", None)
            guardado = ""
            if isinstance(parcial, dict) and (parcial.get("patch") or "").strip():
                # O turno parou no meio. O patch fica no caso, sem liberação para aplicar.
                self._gravar_turno(caso, pedido, parcial, origem_proposta, incompleto=str(exc))
                guardado = " O patch feito até ali ficou guardado no caso, sem liberação para aplicar."
            caso.ultimo_erro = str(exc)
            self.memoria.salvar(caso)
            return Mensagem(caso.id, f"Caso {caso.id}\n{exc}\nNada foi aplicado.{guardado}", codigo=1)
        if resposta.tipo == "perguntar":
            caso.pergunta_pendente = {
                "tarefa_id": resposta.id,
                "pergunta": resposta.entrada.get("pergunta"),
                "pedido_original": resposta.contexto.get("pedido_original"),
            }
            caso.ultimo_erro = None
            self.memoria.salvar(caso)
            return Mensagem(caso.id, self._com_memoria(caso, apresentar_pergunta(caso, resposta)), codigo=0)
        resultado = resposta.resultado or {}
        self._gravar_turno(caso, pedido, resultado, origem_proposta)
        return Mensagem(
            caso.id,
            self._com_memoria(caso, apresentar_turno(caso, resultado, origem_proposta=origem_proposta)),
            codigo=0,
        )

    def aceitar(self, caso_id: str, proposta_id: str, progresso=None) -> Mensagem:
        caso = self.memoria.carregar(caso_id)
        if caso.pergunta_pendente:
            return Mensagem(
                caso.id,
                "Há uma pergunta em aberto. Responda o turno antes de aceitar uma proposta.\n"
                f'fabrica turno --caso {caso.id} --pedido "sua resposta"',
                codigo=1,
            )
        proposta = next((item for item in caso.propostas_pendentes if item.get("id") == proposta_id), None)
        if not proposta:
            return Mensagem(caso.id, f"Proposta {proposta_id} não está pendente neste caso.", codigo=1)
        entrada = proposta.get("entrada") or {}
        contexto = proposta.get("contexto") or {}
        pedido = (
            f"{proposta.get('o_que_fazer')}\n"
            f"Onde: {entrada.get('onde') or contexto.get('onde')}\n"
            f"O que melhora: {entrada.get('o_que_melhora') or contexto.get('o_que_melhora')}\n"
            f"Resultado esperado: {proposta.get('resultado_esperado')}\n"
            "Não amplie o escopo além desta proposta."
        )
        return self.executar_turno(
            repo=caso.repo,
            pedido=pedido,
            caso_id=caso.id,
            origem_proposta=proposta,
            progresso=progresso,
        )

    def aplicar(self, caso_id: str) -> Mensagem:
        trava = self._trava(caso_id)
        if not trava.acquire(blocking=False):
            return Mensagem(caso_id, OCUPADO, codigo=1)
        try:
            return self._aplicar(caso_id)
        finally:
            trava.release()

    def commit(self, caso_id: str) -> Mensagem:
        trava = self._trava(caso_id)
        if not trava.acquire(blocking=False):
            return Mensagem(caso_id, OCUPADO, codigo=1)
        try:
            return self._commit(caso_id)
        finally:
            trava.release()

    def _aplicar(self, caso_id: str) -> Mensagem:
        caso = self.memoria.carregar(caso_id)
        liberado, motivo = caso.pode_aplicar()
        if not liberado:
            return Mensagem(caso.id, f"Aplicação bloqueada. {motivo}", codigo=1)
        if not dentro_das_raizes(caso.repo, self.raizes):
            return Mensagem(caso.id, "Aplicação bloqueada. O repositório do caso está fora das pastas permitidas.", codigo=1)
        caminhos = caminhos_do_patch(caso.patch or "")
        ruins = [
            caminho
            for caminho in caminhos
            if caminho_inseguro(caminho)
            or caminho_sensivel(caminho)
            or caminho_proibido(caminho, caso.nao_tocar_caminhos or [])
        ]
        if ruins:
            return Mensagem(
                caso.id,
                "Aplicação bloqueada. O patch mexe em caminho protegido: " + ", ".join(ruins) + ".",
                codigo=1,
            )
        # A revisão e a verificação valem para o estado em que foram feitas.
        atual = commit_atual(caso.repo)
        if caso.base_commit and atual and atual != caso.base_commit:
            return Mensagem(
                caso.id,
                "Aplicação bloqueada. O repositório mudou desde a revisão "
                f"(era {caso.base_commit[:7]}, agora {atual[:7]}). Rode o turno de novo.",
                codigo=1,
            )
        if caso.base_arquivos:
            agora_ = impressao_dos_arquivos(caso.repo, list(caso.base_arquivos))
            mudados = [nome for nome, marca in caso.base_arquivos.items() if agora_.get(nome) != marca]
            if mudados:
                return Mensagem(
                    caso.id,
                    "Aplicação bloqueada. Estes arquivos mudaram desde a revisão: "
                    + ", ".join(mudados)
                    + ". Rode o turno de novo.",
                    codigo=1,
                )
        try:
            aplicar_patch(Path(caso.repo), caso.patch or "")
        except ErroRepositorio as exc:
            return Mensagem(caso.id, f"Aplicação bloqueada. {exc}", codigo=1)
        caso.aplicado = True
        caso.commitado = False
        caso.commit_hash = None
        caso.atualizado_em = agora()
        self._marcar_ultimo(caso, aplicado=True, commitado=False, commit=None, repo_limpo=False)
        self.memoria.salvar(caso)
        lista = ", ".join(caminhos) if caminhos else "(nenhum arquivo identificado)"
        return Mensagem(
            caso.id,
            f"Patch aplicado no worktree de {caso.repo}. Nenhum commit foi criado.\nArquivos: {lista}\n",
            codigo=0,
        )

    def _commit(self, caso_id: str) -> Mensagem:
        caso = self.memoria.carregar(caso_id)
        liberado, motivo = caso.pode_commitar()
        if not liberado:
            return Mensagem(caso.id, f"Commit bloqueado. {motivo}", codigo=1)
        if not dentro_das_raizes(caso.repo, self.raizes):
            return Mensagem(caso.id, "Commit bloqueado. O repositório do caso está fora das pastas permitidas.", codigo=1)
        caminhos = caminhos_do_patch(caso.patch or "")
        pedido = ""
        if caso.turnos:
            pedido = (caso.turnos[-1].get("pedido_usuario") or caso.turnos[-1].get("pedido") or "").strip()
        mensagem_commit = " ".join(pedido.split())[:72] or "Aplica o turno da fábrica"
        try:
            hash_completo = criar_commit(Path(caso.repo), caminhos, mensagem_commit)
        except ErroRepositorio as exc:
            return Mensagem(caso.id, f"Commit bloqueado. {exc}", codigo=1)
        caso.commitado = True
        caso.commit_hash = hash_completo
        caso.atualizado_em = agora()
        self._marcar_ultimo(caso, commitado=True, commit=hash_completo, aplicado=True)
        self.memoria.salvar(caso)
        return Mensagem(
            caso.id,
            f"Commit {hash_completo[:7]} criado em {caso.repo}. Nada foi enviado ao remoto.\n",
            codigo=0,
        )

    def _abrir(self, repo: str | None, caso_id: str | None):
        if caso_id:
            caso = self.memoria.carregar(caso_id)
            if not dentro_das_raizes(caso.repo, self.raizes):
                return None, Mensagem(caso.id, "O repositório do caso está fora das pastas permitidas.", codigo=1)
            if repo:
                try:
                    topo, foco = localizar_repositorio(repo)
                except ErroRepositorio as exc:
                    return None, Mensagem(caso.id, str(exc), codigo=1)
                if Path(caso.repo).resolve() != topo:
                    return None, Mensagem(caso.id, "O repositório informado não é o deste caso.", codigo=1)
                if caso.foco == "." and foco != ".":
                    caso.foco = foco
            elif not Path(caso.repo).exists():
                return None, Mensagem(caso.id, f"O repositório do caso não está mais em {caso.repo}.", codigo=1)
            return caso, None
        if not repo:
            return None, Mensagem("", "Informe --repo para abrir um caso.", codigo=1)
        if not dentro_das_raizes(repo, self.raizes):
            return None, Mensagem("", "Este caminho está fora das pastas permitidas (FABRICA_REPOS).", codigo=1)
        try:
            topo, foco = localizar_repositorio(repo)
        except ErroRepositorio as exc:
            return None, Mensagem("", str(exc), codigo=1)
        if not dentro_das_raizes(topo, self.raizes):
            return None, Mensagem("", "Este repositório está fora das pastas permitidas (FABRICA_REPOS).", codigo=1)
        return self.memoria.criar(repo=str(topo), foco=foco), None

    def _gravar_turno(
        self, caso, pedido: str, resultado: dict, origem_proposta: dict | None, incompleto: str | None = None
    ) -> None:
        salvo = copy.deepcopy(resultado)
        inicio = _enxugar(salvo.get("auditor_inicio"))
        revisor = salvo.get("revisor") or {}
        auditor_fim = salvo.get("auditor_fim") or {}
        caso.turnos.append(
            {
                "pedido": salvo.get("pedido") or pedido,
                "pedido_usuario": pedido,
                "quando": agora(),
                "auditor_inicio": inicio,
                "patch": salvo.get("patch") or "",
                "revisor": revisor,
                "auditor_fim": auditor_fim,
                "propostas": salvo.get("propostas") or [],
                "trilha": salvo.get("trilha") or [],
                "aviso_performance": salvo.get("aviso_performance"),
                "ciclos_revisor": salvo.get("ciclos_revisor"),
                "veredito_revisor": revisor.get("veredito"),
                "linhas": salvo.get("linhas") or [],
                "paralelo": bool(salvo.get("paralelo")),
                "verificacao": salvo.get("verificacao"),
                "uso": salvo.get("uso"),
                "incompleto": incompleto,
                "aplicado": False,
                "commitado": False,
                "commit": None,
                "repo_limpo": True,
            }
        )
        caso.pergunta_pendente = None
        caso.patch = salvo.get("patch") or ""
        caso.veredito_revisor = revisor.get("veredito")
        comprometido = auditor_fim.get("comprometido") if auditor_fim else None
        caso.auditor_final_comprometido = comprometido
        caso.nao_tocar_caminhos = list((resultado.get("auditor_inicio") or {}).get("nao_tocar_caminhos") or [])
        caso.verificacao = salvo.get("verificacao")
        # Guarda em que estado o patch foi revisado, para o aplicar recusar se o repositório mudar.
        caso.base_commit = commit_atual(caso.repo) if caso.patch else None
        caso.base_arquivos = impressao_dos_arquivos(caso.repo, caminhos_do_patch(caso.patch)) if caso.patch else {}
        caso.propostas_pendentes = salvo.get("propostas") or []
        caso.aplicado = False
        caso.commitado = False
        caso.commit_hash = None
        caso.ultimo_erro = incompleto
        if origem_proposta and not incompleto:
            caso.propostas_aceitas.append(
                {
                    "id": origem_proposta.get("id"),
                    "quando": agora(),
                    "o_que_fazer": origem_proposta.get("o_que_fazer"),
                }
            )
        self.memoria.salvar(caso)

    def _marcar_ultimo(self, caso, **campos) -> None:
        if caso.turnos:
            caso.turnos[-1].update(campos)

    def _com_memoria(self, caso, texto: str) -> str:
        caminho = self.memoria.dir_casos / f"{caso.id}.json"
        if not texto.endswith("\n"):
            texto += "\n"
        return texto + f"Memória: {caminho}\n"


def _enxugar(inicio):
    if not isinstance(inicio, dict):
        return inicio
    copia = {chave: valor for chave, valor in inicio.items() if chave != "conteudos"}
    fatos = dict(copia.get("fatos") or {})
    fatos.pop("conteudos", None)
    fatos.pop("arquivos", None)
    copia["fatos"] = fatos
    return copia

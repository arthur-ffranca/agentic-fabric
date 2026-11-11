"""Um processo serve a interface e a API. Não roda turno sozinho ao abrir a página.

Em 127.0.0.1 a API confere Host e Origin, para uma página de outro site não comandar a fábrica.
Fora do loopback ela só sobe com FABRICA_TOKEN e FABRICA_REPOS definidos.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fabrica.cli import construir_padrao
from fabrica.erros import ErroFabrica
from fabrica.memoria import Memoria
from fabrica.repositorio import raizes_do_ambiente
from fabrica.tarefa import novo_id
from fabrica.vista import estado_caso, estado_vazio

UI = Path(__file__).resolve().parent / "ui" / "index.html"
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
LIMITE_CORPO = 1_000_000
MAX_TRABALHOS = 50


class Trabalhos:
    """Turnos rodando em segundo plano. A tela consulta o andamento por id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._itens: dict[str, dict] = {}

    def abrir(self, caso_id: str | None) -> dict:
        trabalho = {
            "id": novo_id(),
            "estado": "rodando",
            "passos": [],
            "caso_id": caso_id,
            "erro": None,
            "estado_caso": None,
            "inicio": time.time(),
        }
        with self._lock:
            self._itens[trabalho["id"]] = trabalho
            sobra = len(self._itens) - MAX_TRABALHOS
            if sobra > 0:
                for chave in [k for k, v in self._itens.items() if v["estado"] != "rodando"][:sobra]:
                    del self._itens[chave]
        return trabalho

    def passo(self, trabalho: dict, nome: str) -> None:
        with self._lock:
            if nome.startswith("caso:"):
                trabalho["caso_id"] = nome[5:]
            else:
                trabalho["passos"].append(nome)

    def fechar(self, trabalho: dict, *, erro: str | None, estado: dict | None) -> None:
        with self._lock:
            trabalho["estado"] = "erro" if erro else "concluido"
            trabalho["erro"] = erro
            trabalho["estado_caso"] = estado

    def ver(self, identificador: str) -> dict | None:
        with self._lock:
            trabalho = self._itens.get(identificador)
            if trabalho is None:
                return None
            copia = dict(trabalho)
            copia["passos"] = list(trabalho["passos"])
            copia["segundos"] = int(time.time() - trabalho["inicio"])
            copia.pop("inicio", None)
            return copia

    def rodando(self, caso_id: str | None) -> str | None:
        with self._lock:
            for trabalho in reversed(list(self._itens.values())):
                if trabalho["estado"] != "rodando":
                    continue
                if caso_id is None or trabalho["caso_id"] == caso_id:
                    return trabalho["id"]
        return None


def _nome_do_host(valor: str) -> str:
    valor = (valor or "").strip().lower()
    if valor.startswith("["):
        return valor.split("]", 1)[0] + "]"
    return valor.rsplit(":", 1)[0] if ":" in valor else valor


def criar_servidor(host: str, porta: int, *, orquestrador=None, construir=None, memoria=None, token=None, hosts=None):
    token = (os.environ.get("FABRICA_TOKEN") or "").strip() if token is None else token
    exposto = host not in LOOPBACK
    if exposto and not token:
        raise ErroFabrica(
            "Para escutar fora de 127.0.0.1 defina FABRICA_TOKEN. Sem ele, qualquer pessoa na rede comanda a fábrica."
        )
    raizes = orquestrador.raizes if orquestrador is not None else raizes_do_ambiente()
    if exposto and not raizes:
        raise ErroFabrica(
            "Para escutar fora de 127.0.0.1 defina FABRICA_REPOS com a pasta dos repositórios permitidos."
        )
    extras = hosts if hosts is not None else [
        parte.strip().lower() for parte in (os.environ.get("FABRICA_HOSTS") or "").split(",") if parte.strip()
    ]
    # Com token e sem lista de hosts, quem protege é o token (atrás de proxy o Host varia).
    confere_host = bool(extras) or not token
    permitidos = set(LOOPBACK) | {host.lower()} | set(extras)
    fabrica = {
        "orquestrador": orquestrador,
        "construir": construir or construir_padrao,
        "memoria": memoria or (orquestrador.memoria if orquestrador is not None else None),
        "trabalhos": Trabalhos(),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _barrado(self, *, escrita: bool) -> bool:
            """Devolve True quando já respondeu com a recusa."""
            host_pedido = _nome_do_host(self.headers.get("Host") or "")
            if confere_host and host_pedido not in permitidos:
                self._json(403, {"erro": "Host não permitido."})
                return True
            origem = (self.headers.get("Origin") or "").strip().lower()
            if origem:
                alvo = urlparse(origem).netloc
                mesmos = {(self.headers.get("Host") or "").strip().lower()}
                if token:
                    # Atrás de um proxy o Host pode ser o interno; com token, vale também o host original.
                    mesmos.add((self.headers.get("X-Forwarded-Host") or "").strip().lower())
                if origem == "null" or alvo not in mesmos:
                    self._json(403, {"erro": "Origem não permitida."})
                    return True
            if token:
                enviado = self.headers.get("Authorization") or ""
                enviado = enviado[7:].strip() if enviado.lower().startswith("bearer ") else ""
                if not enviado or not hmac.compare_digest(enviado.encode(), token.encode()):
                    self._json(401, {"erro": "Token ausente ou inválido.", "precisa_token": True})
                    return True
            if escrita:
                tipo = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if tipo != "application/json":
                    self._json(415, {"erro": "Envie o pedido como application/json."})
                    return True
            return False

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
                return
            if url.path == "/api/estado":
                if self._barrado(escrita=False):
                    return
                caso_id = (parse_qs(url.query).get("caso") or [None])[0]
                try:
                    codigo, corpo = ler_estado(fabrica, caso_id)
                except ErroFabrica as exc:
                    codigo, corpo = 400, {"erro": str(exc)}
                self._json(codigo, corpo)
                return
            if url.path == "/api/trabalho":
                if self._barrado(escrita=False):
                    return
                identificador = (parse_qs(url.query).get("id") or [""])[0]
                trabalho = fabrica["trabalhos"].ver(identificador)
                if trabalho is None:
                    self._json(404, {"erro": "Trabalho não encontrado."})
                else:
                    self._json(200, trabalho)
                return
            self._send(404, b"nao encontrado", "text/plain; charset=utf-8")

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in {"/api/turno", "/api/aplicar", "/api/commit", "/api/aceitar"}:
                self._send(404, b"nao encontrado", "text/plain; charset=utf-8")
                return
            if self._barrado(escrita=True):
                return
            try:
                payload = _ler_json(self)
            except (json.JSONDecodeError, UnicodeError, ValueError):
                self._json(400, {"erro": "O pedido não veio em JSON."})
                return
            try:
                if path == "/api/turno":
                    codigo, corpo = iniciar_turno(fabrica, payload)
                elif path == "/api/aceitar":
                    codigo, corpo = iniciar_aceitar(fabrica, payload)
                elif path == "/api/aplicar":
                    codigo, corpo = executar_aplicar(fabrica, payload)
                else:
                    codigo, corpo = executar_commit(fabrica, payload)
            except ErroFabrica as exc:
                codigo, corpo = 400, {"erro": str(exc)}
            self._json(codigo, corpo)

        def _json(self, codigo: int, corpo: dict) -> None:
            self._send(codigo, json.dumps(corpo, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _send(self, codigo: int, body: bytes, ctype: str) -> None:
            self.send_response(codigo)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, porta), Handler)


def ler_estado(fabrica: dict, caso_id: str | None) -> tuple[int, dict]:
    memoria = _memoria(fabrica)
    if caso_id:
        corpo = estado_caso(memoria.carregar(caso_id))
    else:
        casos = memoria.listar()
        corpo = estado_caso(casos[0]) if casos else estado_vazio()
    trabalhos = fabrica.get("trabalhos")
    if trabalhos is not None:
        corpo["trabalho"] = trabalhos.rodando(corpo.get("caso_id")) if corpo.get("caso_id") else trabalhos.rodando(None)
    return 200, corpo


def _dados_do_turno(payload: dict) -> tuple[str, str | None, str | None, str | None]:
    pedido = (payload.get("pedido") or "").strip()
    repo = (payload.get("repo") or "").strip() or None
    caso_id = (payload.get("caso_id") or "").strip() or None
    if not pedido:
        return pedido, repo, caso_id, "O pedido está vazio."
    if not repo and not caso_id:
        return pedido, repo, caso_id, "Informe o repositório alvo."
    return pedido, repo, caso_id, None


def executar_turno(fabrica: dict, payload: dict, progresso=None) -> tuple[int, dict]:
    """Roda o turno inteiro e devolve o estado. A rota HTTP usa iniciar_turno, que não segura a requisição."""
    pedido, repo, caso_id, erro = _dados_do_turno(payload)
    if erro:
        return 400, {"erro": erro}
    orquestrador = _orquestrador(fabrica)
    mensagem = orquestrador.executar_turno(repo=repo, pedido=pedido, caso_id=caso_id, progresso=progresso)
    return _resposta_do_turno(orquestrador, mensagem)


def _resposta_do_turno(orquestrador, mensagem) -> tuple[int, dict]:
    if mensagem.codigo != 0:
        corpo = {"erro": mensagem.texto.strip() or "O turno falhou."}
        if mensagem.caso_id:
            try:
                corpo["estado_caso"] = estado_caso(orquestrador.memoria.carregar(mensagem.caso_id))
            except ErroFabrica:
                pass
        return 400, corpo
    if not mensagem.caso_id:
        return 400, {"erro": mensagem.texto.strip() or "O turno não abriu um caso."}
    return 200, estado_caso(orquestrador.memoria.carregar(mensagem.caso_id))


def _em_segundo_plano(fabrica: dict, caso_id: str | None, rodar) -> tuple[int, dict]:
    trabalhos: Trabalhos = fabrica["trabalhos"]
    trabalho = trabalhos.abrir(caso_id)

    def correr() -> None:
        try:
            codigo, corpo = rodar(lambda nome: trabalhos.passo(trabalho, nome))
        except ErroFabrica as exc:
            trabalhos.fechar(trabalho, erro=str(exc), estado=None)
            return
        except Exception as exc:  # o trabalho nunca fica preso em "rodando"
            trabalhos.fechar(trabalho, erro=f"Falha interna: {type(exc).__name__}.", estado=None)
            return
        if codigo == 200:
            trabalhos.passo(trabalho, "caso:" + str(corpo.get("caso_id") or ""))
            trabalhos.fechar(trabalho, erro=None, estado=corpo)
        else:
            trabalhos.fechar(trabalho, erro=corpo.get("erro") or "O turno falhou.", estado=corpo.get("estado_caso"))

    threading.Thread(target=correr, name="fabrica-turno", daemon=True).start()
    return 202, {"trabalho": trabalho["id"]}


def iniciar_turno(fabrica: dict, payload: dict) -> tuple[int, dict]:
    _, _, caso_id, erro = _dados_do_turno(payload)
    if erro:
        return 400, {"erro": erro}
    _orquestrador(fabrica)  # sem chave, a recusa sai aqui, antes de abrir o trabalho
    return _em_segundo_plano(fabrica, caso_id, lambda progresso: executar_turno(fabrica, payload, progresso))


def iniciar_aceitar(fabrica: dict, payload: dict) -> tuple[int, dict]:
    caso_id = (payload.get("caso_id") or "").strip()
    proposta_id = (payload.get("proposta_id") or "").strip()
    if not caso_id or not proposta_id:
        return 400, {"erro": "Informe o caso e a proposta."}
    _orquestrador(fabrica)
    return _em_segundo_plano(fabrica, caso_id, lambda progresso: executar_aceitar(fabrica, payload, progresso))


def executar_aplicar(fabrica: dict, payload: dict) -> tuple[int, dict]:
    caso_id = (payload.get("caso_id") or "").strip()
    if not caso_id:
        return 400, {"erro": "Informe o caso."}
    orquestrador = _orquestrador(fabrica)
    mensagem = orquestrador.aplicar(caso_id)
    if mensagem.codigo != 0:
        return 400, {"erro": mensagem.texto.strip()}
    return 200, estado_caso(orquestrador.memoria.carregar(caso_id))


def executar_commit(fabrica: dict, payload: dict) -> tuple[int, dict]:
    caso_id = (payload.get("caso_id") or "").strip()
    if not caso_id:
        return 400, {"erro": "Informe o caso."}
    orquestrador = _orquestrador(fabrica)
    mensagem = orquestrador.commit(caso_id)
    if mensagem.codigo != 0:
        return 400, {"erro": mensagem.texto.strip()}
    return 200, estado_caso(orquestrador.memoria.carregar(caso_id))


def executar_aceitar(fabrica: dict, payload: dict, progresso=None) -> tuple[int, dict]:
    caso_id = (payload.get("caso_id") or "").strip()
    proposta_id = (payload.get("proposta_id") or "").strip()
    if not caso_id or not proposta_id:
        return 400, {"erro": "Informe o caso e a proposta."}
    orquestrador = _orquestrador(fabrica)
    mensagem = orquestrador.aceitar(caso_id, proposta_id, progresso=progresso)
    return _resposta_do_turno(orquestrador, mensagem)


def _memoria(fabrica: dict) -> Memoria:
    if fabrica.get("memoria") is not None:
        return fabrica["memoria"]
    if fabrica.get("orquestrador") is not None:
        return fabrica["orquestrador"].memoria
    memoria = Memoria()
    fabrica["memoria"] = memoria
    return memoria


_construcao = threading.Lock()


def _orquestrador(fabrica: dict):
    with _construcao:
        if fabrica.get("orquestrador") is None:
            fabrica["orquestrador"] = fabrica["construir"]()
            fabrica["memoria"] = fabrica["orquestrador"].memoria
        return fabrica["orquestrador"]


def _ler_json(handler: BaseHTTPRequestHandler) -> dict:
    tamanho = int(handler.headers.get("Content-Length") or 0)
    if tamanho < 0 or tamanho > LIMITE_CORPO:
        raise ValueError("corpo grande demais")
    bruto = handler.rfile.read(tamanho) if tamanho else b"{}"
    dados = json.loads(bruto.decode("utf-8") or "{}")
    if not isinstance(dados, dict):
        raise json.JSONDecodeError("objeto", "", 0)
    return dados


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fabrica", description="Sobe a interface da fábrica e a API no mesmo processo.")
    parser.add_argument("--host", default=os.environ.get("FABRICA_HOST", "127.0.0.1"))
    parser.add_argument("--porta", type=int, default=int(os.environ.get("FABRICA_PORT", "8765")))
    args = parser.parse_args(argv)
    try:
        servidor = criar_servidor(args.host, args.porta)
    except ErroFabrica as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Interface em http://{args.host}:{args.porta}", flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

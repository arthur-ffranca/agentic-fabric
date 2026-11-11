"""Um processo serve a interface e a API. Não roda turno sozinho ao abrir a página."""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fabrica.cli import construir_padrao
from fabrica.erros import ErroFabrica, ErroLLM
from fabrica.memoria import Memoria
from fabrica.vista import estado_caso, estado_vazio

UI = Path(__file__).resolve().parent / "ui" / "index.html"


def criar_servidor(host: str, porta: int, *, orquestrador=None, construir=None, memoria=None):
    fabrica = {
        "orquestrador": orquestrador,
        "construir": construir or construir_padrao,
        "memoria": memoria or (orquestrador.memoria if orquestrador is not None else None),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
                return
            if path == "/api/estado":
                consulta = parse_qs(urlparse(self.path).query)
                caso_id = (consulta.get("caso") or [None])[0]
                codigo, corpo = ler_estado(fabrica, caso_id)
                self._json(codigo, corpo)
                return
            self._send(404, b"nao encontrado", "text/plain; charset=utf-8")

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in {"/api/turno", "/api/aplicar", "/api/commit", "/api/aceitar"}:
                self._send(404, b"nao encontrado", "text/plain; charset=utf-8")
                return
            try:
                payload = _ler_json(self)
            except (json.JSONDecodeError, UnicodeError):
                self._json(400, {"erro": "O pedido não veio em JSON."})
                return
            try:
                if path == "/api/turno":
                    codigo, corpo = executar_turno(fabrica, payload)
                elif path == "/api/aplicar":
                    codigo, corpo = executar_aplicar(fabrica, payload)
                elif path == "/api/commit":
                    codigo, corpo = executar_commit(fabrica, payload)
                else:
                    codigo, corpo = executar_aceitar(fabrica, payload)
            except ErroLLM as exc:
                codigo, corpo = 400, {"erro": str(exc)}
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
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, porta), Handler)


def ler_estado(fabrica: dict, caso_id: str | None) -> tuple[int, dict]:
    memoria = _memoria(fabrica)
    if caso_id:
        caso = memoria.carregar(caso_id)
        return 200, estado_caso(caso)
    casos = memoria.listar()
    if not casos:
        return 200, estado_vazio()
    return 200, estado_caso(casos[0])


def executar_turno(fabrica: dict, payload: dict) -> tuple[int, dict]:
    pedido = (payload.get("pedido") or "").strip()
    repo = (payload.get("repo") or "").strip() or None
    caso_id = (payload.get("caso_id") or "").strip() or None
    if not pedido:
        return 400, {"erro": "O pedido está vazio."}
    if not repo and not caso_id:
        return 400, {"erro": "Informe o repositório alvo."}
    orquestrador = _orquestrador(fabrica)
    mensagem = orquestrador.executar_turno(repo=repo, pedido=pedido, caso_id=caso_id)
    if mensagem.codigo != 0:
        return 400, {"erro": mensagem.texto.strip() or "O turno falhou."}
    if not mensagem.caso_id:
        return 400, {"erro": mensagem.texto.strip() or "O turno não abriu um caso."}
    return 200, estado_caso(orquestrador.memoria.carregar(mensagem.caso_id))


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


def executar_aceitar(fabrica: dict, payload: dict) -> tuple[int, dict]:
    caso_id = (payload.get("caso_id") or "").strip()
    proposta_id = (payload.get("proposta_id") or "").strip()
    if not caso_id or not proposta_id:
        return 400, {"erro": "Informe o caso e a proposta."}
    orquestrador = _orquestrador(fabrica)
    mensagem = orquestrador.aceitar(caso_id, proposta_id)
    if mensagem.codigo != 0:
        return 400, {"erro": mensagem.texto.strip()}
    return 200, estado_caso(orquestrador.memoria.carregar(caso_id))


def _memoria(fabrica: dict) -> Memoria:
    if fabrica.get("memoria") is not None:
        return fabrica["memoria"]
    if fabrica.get("orquestrador") is not None:
        return fabrica["orquestrador"].memoria
    memoria = Memoria()
    fabrica["memoria"] = memoria
    return memoria


def _orquestrador(fabrica: dict):
    if fabrica.get("orquestrador") is None:
        fabrica["orquestrador"] = fabrica["construir"]()
        fabrica["memoria"] = fabrica["orquestrador"].memoria
    return fabrica["orquestrador"]


def _ler_json(handler: BaseHTTPRequestHandler) -> dict:
    tamanho = int(handler.headers.get("Content-Length") or 0)
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
    servidor = criar_servidor(args.host, args.porta)
    print(f"Interface em http://{args.host}:{args.porta}", flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

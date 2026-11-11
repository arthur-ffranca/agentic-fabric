"""Fila em processo. Só existe depois do despacho, e só para o trabalho assíncrono."""

from __future__ import annotations

import threading

from fabrica.erros import ErroFabrica


class CaixaPatch:
    """Entrega o patch final para quem já está rodando em paralelo."""

    def __init__(self) -> None:
        self._evento = threading.Event()
        self._lock = threading.Lock()
        self.patch: str | None = None
        self.erro: str | None = None

    def publicar(self, patch: str) -> None:
        with self._lock:
            self.patch = patch
        self._evento.set()

    def fechar(self, erro: str | None = None) -> None:
        with self._lock:
            if self.patch is None and erro:
                self.erro = erro
        self._evento.set()

    def esperar(self, timeout: float | None = None) -> str:
        if not self._evento.wait(timeout):
            raise ErroFabrica("A performance não recebeu o patch a tempo.")
        with self._lock:
            if self.erro and not self.patch:
                return ""
            return self.patch or ""


class Trabalho:
    def __init__(self, tarefa, fn) -> None:
        self.tarefa = tarefa
        self._fn = fn
        self.valor = None
        self.erro: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="fabrica-performance", daemon=True)

    def iniciar(self) -> Trabalho:
        self.tarefa.status = "em_andamento"
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self.valor = self._fn(self.tarefa)
            if self.tarefa.status == "em_andamento":
                self.tarefa.status = "concluida"
        except Exception as exc:
            self.erro = exc
            self.tarefa.status = "falhou"

    def aguardar(self, timeout: float | None = None):
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise ErroFabrica("A performance não terminou a tempo.")
        if self.erro:
            raise self.erro
        return self.valor


class FilaEmProcesso:
    def despachar(self, tarefa, fn) -> Trabalho:
        return Trabalho(tarefa, fn).iniciar()

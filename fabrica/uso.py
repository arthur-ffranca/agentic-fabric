"""Conta chamadas e tokens de um turno. O cliente do modelo registra; a triagem liga o medidor."""

from __future__ import annotations

import contextlib
import os
import threading

_local = threading.local()

TETO_PADRAO = 2_000_000


def teto_do_ambiente() -> int:
    """Teto de tokens por turno. FABRICA_TETO_TOKENS=0 desliga."""
    bruto = (os.environ.get("FABRICA_TETO_TOKENS") or "").strip()
    if not bruto:
        return TETO_PADRAO
    try:
        return max(0, int(bruto))
    except ValueError:
        return TETO_PADRAO


class Medidor:
    def __init__(self, teto: int | None = None) -> None:
        self.teto = teto_do_ambiente() if teto is None else teto
        self._lock = threading.Lock()
        self.chamadas = 0
        self.entrada = 0
        self.saida = 0

    def registrar(self, entrada: int, saida: int) -> None:
        with self._lock:
            self.chamadas += 1
            self.entrada += max(0, int(entrada or 0))
            self.saida += max(0, int(saida or 0))

    def estourou(self) -> bool:
        with self._lock:
            return bool(self.teto) and self.entrada + self.saida >= self.teto

    def total(self) -> dict:
        with self._lock:
            return {
                "chamadas": self.chamadas,
                "tokens_entrada": self.entrada,
                "tokens_saida": self.saida,
                "teto": self.teto,
            }

    @contextlib.contextmanager
    def ativo(self):
        """Liga o medidor na thread atual. Cada thread do turno precisa ligar o seu."""
        anterior = getattr(_local, "medidor", None)
        _local.medidor = self
        try:
            yield self
        finally:
            _local.medidor = anterior


def medidor_atual() -> Medidor | None:
    return getattr(_local, "medidor", None)

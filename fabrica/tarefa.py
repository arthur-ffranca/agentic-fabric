"""A única mensagem entre orquestrador, triagem e especialistas."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

TIPOS = ("resolver", "executar", "perguntar")
DESTINATARIOS = ("orquestrador", "triagem", "auditor", "codigo", "revisor", "performance")
STATUS = ("pendente", "em_andamento", "concluida", "falhou")


def novo_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Tarefa:
    id: str
    destinatario: str
    tipo: str
    o_que_fazer: str
    contexto: dict
    entrada: dict
    resultado_esperado: str
    resultado: dict | None = None
    status: str = "pendente"

    def __post_init__(self) -> None:
        if self.tipo not in TIPOS:
            raise ValueError(f"tipo de tarefa inválido: {self.tipo}")
        if self.destinatario not in DESTINATARIOS:
            raise ValueError(f"destinatário inválido: {self.destinatario}")
        if self.status not in STATUS:
            raise ValueError(f"status inválido: {self.status}")
        if not isinstance(self.contexto, dict) or not isinstance(self.entrada, dict):
            raise ValueError("contexto e entrada precisam ser objetos")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "destinatario": self.destinatario,
            "tipo": self.tipo,
            "o_que_fazer": self.o_que_fazer,
            "contexto": self.contexto,
            "entrada": self.entrada,
            "resultado_esperado": self.resultado_esperado,
            "resultado": self.resultado,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, dados: dict) -> Tarefa:
        return cls(
            id=dados["id"],
            destinatario=dados["destinatario"],
            tipo=dados["tipo"],
            o_que_fazer=dados["o_que_fazer"],
            contexto=dados.get("contexto") or {},
            entrada=dados.get("entrada") or {},
            resultado_esperado=dados.get("resultado_esperado") or "",
            resultado=dados.get("resultado"),
            status=dados.get("status") or "pendente",
        )

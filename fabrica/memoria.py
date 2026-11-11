"""Memória do caso em arquivo local. Um caso continua no dia seguinte."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fabrica.erros import ErroFabrica
from fabrica.tarefa import novo_id


def agora() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Caso:
    id: str
    repo: str
    foco: str
    criado_em: str
    atualizado_em: str
    pergunta_pendente: dict | None = None
    turnos: list = field(default_factory=list)
    propostas_pendentes: list = field(default_factory=list)
    propostas_aceitas: list = field(default_factory=list)
    patch: str | None = None
    veredito_revisor: str | None = None
    auditor_final_comprometido: bool | None = None
    nao_tocar_caminhos: list = field(default_factory=list)
    aplicado: bool = False
    commitado: bool = False
    commit_hash: str | None = None
    ultimo_erro: str | None = None
    versao: int = 1

    def pode_aplicar(self) -> tuple[bool, str]:
        if not self.patch:
            return False, "Não há patch neste caso."
        if self.aplicado:
            return False, "O patch deste turno já foi aplicado."
        if self.veredito_revisor != "aprovar":
            return False, "O revisor não aprovou o patch."
        if self.auditor_final_comprometido is not False:
            return False, "O auditor final não liberou o patch."
        return True, ""

    def pode_commitar(self) -> tuple[bool, str]:
        if not self.aplicado:
            return False, "Aplique o patch antes de commitar."
        if self.commitado:
            return False, "Este turno já foi commitado."
        if self.veredito_revisor != "aprovar":
            return False, "O revisor não aprovou o patch."
        if self.auditor_final_comprometido is not False:
            return False, "O auditor final não liberou o patch."
        return True, ""


class Memoria:
    def __init__(self, raiz: Path | str | None = None) -> None:
        if raiz is None:
            raiz = os.environ.get("FABRICA_HOME") or str(Path.home() / ".fabrica-agentes")
        self.raiz = Path(raiz)
        self.dir_casos = self.raiz / "casos"
        self.dir_casos.mkdir(parents=True, exist_ok=True)

    def criar(self, *, repo: str, foco: str) -> Caso:
        caso = Caso(
            id=self._novo_id(),
            repo=repo,
            foco=foco,
            criado_em=agora(),
            atualizado_em=agora(),
        )
        self.salvar(caso)
        return caso

    def salvar(self, caso: Caso) -> None:
        caso.atualizado_em = agora()
        destino = self._caminho(caso.id)
        temporario = destino.with_suffix(".json.tmp")
        temporario.write_text(
            json.dumps(asdict(caso), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporario, destino)

    def carregar(self, caso_id: str) -> Caso:
        caminho = self._caminho(caso_id)
        if not caminho.is_file():
            raise ErroFabrica(f"Caso não encontrado: {caso_id}")
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        campos = {nome: dados[nome] for nome in Caso.__dataclass_fields__ if nome in dados}
        return Caso(**campos)

    def listar(self) -> list[Caso]:
        casos = []
        for caminho in self.dir_casos.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{12}", caminho.stem):
                continue
            try:
                casos.append(self.carregar(caminho.stem))
            except (ErroFabrica, json.JSONDecodeError, TypeError):
                continue
        casos.sort(key=lambda caso: caso.atualizado_em, reverse=True)
        return casos

    def _novo_id(self) -> str:
        for _ in range(5):
            identificador = novo_id()
            if not self._caminho(identificador).exists():
                return identificador
        raise ErroFabrica("Não consegui abrir um id de caso.")

    def _caminho(self, caso_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", caso_id or ""):
            raise ErroFabrica(f"Caso não encontrado: {caso_id}")
        return self.dir_casos / f"{caso_id}.json"

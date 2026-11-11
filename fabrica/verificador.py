"""Verificação determinística do patch. Roda numa cópia descartável; não usa modelo e não toca no repositório.

Quatro conferências, na ordem, parando na primeira que falha:
1. o patch (o conjunto do turno) aplica;
2. os caminhos que o git diz que o patch toca são seguros e permitidos;
3. os arquivos tocados continuam com sintaxe válida;
4. o comando de verificação do repositório alvo passa (opcional, definido em .fabrica.toml).
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

from fabrica.erros import ErroRepositorio
from fabrica.repositorio import (
    CONFIG_VERIFICACAO,
    _git,
    caminho_inseguro,
    caminho_proibido,
    caminho_sensivel,
    caminhos_do_patch,
    copia_descartavel,
)

LIMITE_SAIDA = 4_000
TIMEOUT_PADRAO = 300.0


def _conferir_python(texto: str, caminho: str) -> str | None:
    try:
        compile(texto, caminho, "exec", dont_inherit=True)
    except SyntaxError as exc:
        return f"{caminho}, linha {exc.lineno}: {exc.msg}"
    except ValueError as exc:
        return f"{caminho}: {exc}"
    return None


def _conferir_json(texto: str, caminho: str) -> str | None:
    try:
        json.loads(texto)
    except json.JSONDecodeError as exc:
        return f"{caminho}, linha {exc.lineno}: {exc.msg}"
    return None


def _conferir_toml(texto: str, caminho: str) -> str | None:
    try:
        tomllib.loads(texto)
    except tomllib.TOMLDecodeError as exc:
        return f"{caminho}: {exc}"
    return None


_SINTAXE = {".py": _conferir_python, ".json": _conferir_json, ".toml": _conferir_toml}


def _ler(caminho: Path) -> str | None:
    try:
        if not caminho.is_file():
            return None
        return caminho.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _cauda(texto: str) -> str:
    texto = (texto or "").strip()
    if len(texto) <= LIMITE_SAIDA:
        return texto
    return "…\n" + texto[-LIMITE_SAIDA:]


def configuracao(repo: Path | str) -> tuple[str | None, float]:
    """Comando e prazo. O ambiente vence o arquivo; o arquivo é lido do repositório, nunca do patch."""
    comando = (os.environ.get("FABRICA_VERIFICAR_COMANDO") or "").strip() or None
    prazo: float | None = None
    try:
        prazo = float(os.environ.get("FABRICA_VERIFICAR_TIMEOUT") or "")
    except ValueError:
        prazo = None
    arquivo = Path(repo) / CONFIG_VERIFICACAO
    if arquivo.is_file():
        try:
            dados = tomllib.loads(arquivo.read_text(encoding="utf-8")).get("verificar") or {}
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            dados = {}
        if comando is None and isinstance(dados.get("comando"), str) and dados["comando"].strip():
            comando = dados["comando"].strip()
        if prazo is None and isinstance(dados.get("timeout"), (int, float)) and dados["timeout"] > 0:
            prazo = float(dados["timeout"])
    return comando, prazo or TIMEOUT_PADRAO


class Verificador:
    def __init__(self, comando: str | None = None, timeout: float | None = None) -> None:
        self.comando = comando
        self.timeout = timeout

    def verificar(self, repo: Path | str, patch: str, *, proibidos: list[str] | None = None) -> dict:
        etapas: list[dict] = []

        def fechar() -> dict:
            falhas = [etapa for etapa in etapas if etapa["ok"] is False]
            saida = "\n\n".join(f"{etapa['nome']}: {etapa['detalhe']}" for etapa in falhas)
            return {"ok": not falhas, "etapas": etapas, "saida": saida}

        def etapa(nome: str, ok: bool | None, detalhe: str = "") -> bool:
            etapas.append({"nome": nome, "ok": ok, "detalhe": detalhe})
            return ok is not False

        if not patch or not patch.strip():
            etapa("aplicar", False, "O patch está vazio.")
            return fechar()
        origem = Path(repo).resolve()
        caminhos = caminhos_do_patch(patch)
        try:
            with copia_descartavel(origem) as copia:
                aplicado = _git(copia, "apply", "--whitespace=nowarn", "-", entrada=patch)
                if aplicado.returncode != 0:
                    detalhe = (aplicado.stderr or aplicado.stdout).strip() or "git apply recusou o patch."
                    etapa("aplicar", False, "O patch do turno não aplica inteiro.\n" + detalhe)
                    return fechar()
                etapa("aplicar", True, f"{len(caminhos)} arquivo(s).")

                ruins = [
                    caminho
                    for caminho in caminhos
                    if caminho_inseguro(caminho)
                    or caminho_sensivel(caminho)
                    or caminho == CONFIG_VERIFICACAO
                    or caminho_proibido(caminho, list(proibidos or []))
                ]
                if not etapa(
                    "caminhos",
                    not ruins,
                    ("O patch toca caminho protegido: " + ", ".join(ruins) + ".") if ruins else ", ".join(caminhos),
                ):
                    return fechar()

                erros: list[str] = []
                conferidos = 0
                for caminho in caminhos:
                    conferir = _SINTAXE.get(Path(caminho).suffix.lower())
                    if conferir is None:
                        continue
                    depois = _ler(copia / caminho)
                    if depois is None:
                        continue
                    conferidos += 1
                    erro = conferir(depois, caminho)
                    if erro is None:
                        continue
                    antes = _ler(origem / caminho)
                    # Arquivo que já não passava antes do patch não conta contra o patch.
                    if antes is not None and conferir(antes, caminho) is not None:
                        continue
                    erros.append(erro)
                if not etapa(
                    "sintaxe",
                    not erros,
                    ("Erro de sintaxe depois do patch:\n" + "\n".join(erros)) if erros else f"{conferidos} arquivo(s) conferido(s).",
                ):
                    return fechar()

                comando, prazo = configuracao(origem)
                comando = self.comando or comando
                prazo = self.timeout or prazo
                if not comando:
                    etapa("comando", None, f"Nenhum comando configurado em {CONFIG_VERIFICACAO}.")
                    return fechar()
                codigo, saida = self._rodar(comando, copia, origem, prazo)
                if codigo == 0:
                    etapa("comando", True, f"`{comando}` passou.")
                    return fechar()
            # Falhou com o patch. Só conta se o mesmo comando passa sem ele.
            with copia_descartavel(origem) as limpa:
                codigo_base, _ = self._rodar(comando, limpa, origem, prazo)
            if codigo_base != 0:
                etapa("comando", None, f"`{comando}` já falhava antes do patch; não conta contra ele.")
            else:
                etapa("comando", False, f"`{comando}` passava e falhou com o patch (código {codigo}).\n{_cauda(saida)}")
        except (OSError, subprocess.SubprocessError, ErroRepositorio) as exc:
            etapa("aplicar", False, f"Não consegui montar a cópia para verificar: {exc}")
        return fechar()

    @staticmethod
    def _rodar(comando: str, pasta: Path, origem: Path, prazo: float) -> tuple[int, str]:
        ambiente = dict(os.environ)
        ambiente["FABRICA_REPO_ORIGINAL"] = str(origem)
        try:
            proc = subprocess.run(
                comando,
                shell=True,
                cwd=pasta,
                env=ambiente,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=prazo,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 124, f"Passou do prazo de {prazo:.0f} s."
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

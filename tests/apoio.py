"""Apoio dos testes. Não faz parte da ferramenta."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path


APP = "def soma(a, b):\n    return a + b\n"
APP_NOVO = (
    "def soma(a, b):\n"
    "    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):\n"
    '        raise TypeError("numeros")\n'
    "    return a + b\n"
)
README = "# App\n\nSoma dois números.\n"
README_NOVO = "# App\n\nSoma dois números.\n\nNão mexa neste arquivo.\n"
LOCK = "lock\n"


@dataclass
class RepoExemplo:
    caminho: Path
    diff_app: str
    diff_readme: str
    diff_lock: str


def _git(repo: Path, *args: str, entrada: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=entrada,
        capture_output=True,
        text=True,
        check=False,
    )


def instantaneo(repo: Path) -> dict:
    arquivos: dict[str, bytes] = {}
    for caminho in repo.rglob("*"):
        if ".git" in caminho.parts or not caminho.is_file():
            continue
        arquivos[caminho.relative_to(repo).as_posix()] = caminho.read_bytes()
    status = _git(repo, "--no-optional-locks", "status", "--porcelain")
    if status.returncode != 0:
        raise RuntimeError(status.stderr)
    return {"arquivos": arquivos, "status": status.stdout}


def _diff_e_restaura(repo: Path, relativo: str, novo: str) -> str:
    alvo = repo / relativo
    original = alvo.read_text(encoding="utf-8")
    alvo.write_text(novo, encoding="utf-8")
    gerado = _git(repo, "diff", "--", relativo)
    alvo.write_text(original, encoding="utf-8")
    if gerado.returncode != 0 or not gerado.stdout.strip():
        raise RuntimeError(gerado.stderr or "diff vazio")
    diff = gerado.stdout if gerado.stdout.endswith("\n") else gerado.stdout + "\n"
    checagem = _git(repo, "apply", "--check", "-", entrada=diff)
    if checagem.returncode != 0:
        raise RuntimeError(checagem.stderr)
    if instantaneo(repo)["status"].strip():
        raise RuntimeError("o repositório ficou sujo depois de gerar o diff")
    return diff


def criar_repo(raiz: Path) -> RepoExemplo:
    repo = raiz / "repo"
    repo.mkdir()
    iniciado = subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if iniciado.returncode != 0:
        subprocess.run(["git", "init"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, check=True)
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.py").write_text(APP, encoding="utf-8")
    (repo / "README.md").write_text(README, encoding="utf-8")
    (repo / "poetry.lock").write_text(LOCK, encoding="utf-8")
    (repo / "outro").mkdir()
    (repo / "outro" / "notas.txt").write_text("nada a ver com soma\n", encoding="utf-8")
    _git(repo, "add", ".")
    commit = _git(repo, "commit", "-m", "inicio")
    if commit.returncode != 0:
        raise RuntimeError(commit.stderr)
    return RepoExemplo(
        caminho=repo,
        diff_app=_diff_e_restaura(repo, "app.py", APP_NOVO),
        diff_readme=_diff_e_restaura(repo, "README.md", README_NOVO),
        diff_lock=_diff_e_restaura(repo, "poetry.lock", "lock\nnovo\n"),
    )


def llm_de_cadeia(exemplo, *, primeiro_patch: str, sempre_ruim: bool = False, auditor_compromete: bool = False):
    """Modelo falso da cadeia. O primeiro patch ruim exercita o reparo."""
    import json

    estado = {"codigo": 0}

    def responder(sistema, usuario):
        if sistema.startswith("PAPEL: TRIAGEM"):
            return json.dumps(
                {
                    "tarefas": [
                        {
                            "titulo": "validação na função soma",
                            "o_que_fazer": usuario,
                            "arquivos": ["app.py"],
                        }
                    ]
                }
            )
        if sistema.startswith("PAPEL: AUDITOR_INICIO"):
            assert "return a + b" in usuario
            return json.dumps(
                {
                    "resumo_estado": "Função soma em app.py.",
                    "o_que_ja_funciona": ["soma devolve a soma dos argumentos"],
                    "nao_tocar": ["README.md não foi pedido"],
                    "nao_tocar_caminhos": ["README.md"],
                    "riscos": [],
                }
            )
        if sistema.startswith("PAPEL: CODIGO"):
            estado["codigo"] += 1
            if sempre_ruim:
                return exemplo.diff_readme
            if primeiro_patch == exemplo.diff_readme and estado["codigo"] == 1:
                return exemplo.diff_readme
            if primeiro_patch == exemplo.diff_readme:
                assert "README.md" in usuario
            return exemplo.diff_app
        if sistema.startswith("PAPEL: REVISOR"):
            return json.dumps({"veredito": "aprovar", "motivos": ["cumpre o pedido"], "correcoes": ""})
        if sistema.startswith("PAPEL: AUDITOR_FIM"):
            return json.dumps(
                {
                    "comprometido": auditor_compromete,
                    "relato": "O modelo marcou compromisso." if auditor_compromete else "A mudança fica restrita a soma.",
                }
            )
        if sistema.startswith("PAPEL: PERFORMANCE"):
            return json.dumps(
                {
                    "propostas": [
                        {
                            "o_que_fazer": "Memoizar chamadas repetidas de soma em app.py",
                            "onde": "app.py",
                            "o_que_melhora": "evita somar de novo os mesmos números",
                            "entrada": "Cache no módulo, sem mudar o valor devolvido.",
                            "resultado_esperado": "Patch que cacheia soma sem alterar o valor.",
                        }
                    ]
                }
            )
        raise AssertionError(sistema)

    return LLMScript(responder), estado


class LLMScript:
    """Cliente de teste. Mesma interface do cliente real: completar()."""

    def __init__(self, fn):
        self.fn = fn
        self.chamadas: list[dict[str, str]] = []
        self._lock = threading.Lock()

    def completar(self, *, sistema: str, usuario: str, temperatura: float = 0.2) -> str:
        with self._lock:
            self.chamadas.append({"sistema": sistema, "usuario": usuario, "temperatura": str(temperatura)})
        return self.fn(sistema, usuario)

    def papeis(self) -> list[str]:
        with self._lock:
            return [item["sistema"].splitlines()[0] for item in self.chamadas]

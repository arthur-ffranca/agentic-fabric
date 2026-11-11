"""Leitura real do repositório alvo. Escrita só em aplicar_patch."""

from __future__ import annotations

import re
import subprocess
import threading
import unicodedata
from pathlib import Path

from fabrica.erros import ErroRepositorio

EXTENSOES = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".php",
    ".cs",
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".css",
    ".html",
    ".sql",
    ".txt",
}
LOCKS = {
    "poetry.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "uv.lock",
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "Gemfile.lock",
    "packages.lock.json",
}
_STOP = {
    "adiciona",
    "adicionar",
    "adicione",
    "arquivo",
    "arquivos",
    "aquele",
    "aquela",
    "aquilo",
    "codigo",
    "como",
    "cria",
    "criar",
    "crie",
    "deve",
    "disso",
    "dessa",
    "desse",
    "esta",
    "este",
    "essa",
    "esse",
    "estao",
    "fazer",
    "fica",
    "ficar",
    "funcao",
    "funcoes",
    "isso",
    "mais",
    "menos",
    "mesmo",
    "nao",
    "para",
    "pela",
    "pelas",
    "pelo",
    "pelos",
    "pode",
    "podem",
    "precisa",
    "quando",
    "quero",
    "quer",
    "sem",
    "sobre",
    "tambem",
    "teste",
    "testes",
    "validacao",
    "validar",
    "onde",
}
_CAMINHO_PEDIDO = re.compile(
    r"\b[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|php|cs|md|json|yml|yaml|toml|css|html|sql|txt)\b",
    re.IGNORECASE,
)


def sem_acento(texto: str) -> str:
    return "".join(
        caractere
        for caractere in unicodedata.normalize("NFD", texto)
        if unicodedata.category(caractere) != "Mn"
    )


_git_lock = threading.Lock()


def git(repo: Path | str, *args: str, entrada: str | None = None, timeout: float = 30):
    # Dois agentes podem inspecionar o mesmo repo no mesmo turno. O git não aceita isso junto.
    with _git_lock:
        return _git(repo, *args, entrada=entrada, timeout=timeout)


def _git(repo: Path | str, *args: str, entrada: str | None = None, timeout: float = 30):
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        input=entrada,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def localizar_repositorio(caminho: str | Path) -> tuple[Path, str]:
    bruto = Path(caminho).expanduser()
    if not bruto.exists():
        raise ErroRepositorio(f"Caminho não encontrado: {bruto}")
    if not bruto.is_dir():
        raise ErroRepositorio(f"Caminho não é um diretório: {bruto}")
    pedido = bruto.resolve()
    proc = git(pedido, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        raise ErroRepositorio(f"Não é um repositório git: {pedido}")
    topo = Path(proc.stdout.strip()).resolve()
    foco = "." if pedido == topo else pedido.relative_to(topo).as_posix()
    return topo, foco


def listar_arquivos(repo: Path) -> list[str]:
    proc = git(repo, "ls-files", "-co", "--exclude-standard")
    if proc.returncode != 0:
        raise ErroRepositorio((proc.stderr or "git ls-files falhou.").strip())
    return [linha for linha in proc.stdout.splitlines() if linha and not linha.endswith("/")]


def normalizar_caminho(caminho: str) -> str:
    texto = caminho.strip().strip("`").replace("\\", "/")
    if "\t" in texto:
        texto = texto.split("\t", 1)[0].strip()
    if len(texto) >= 2 and texto[0] == '"' and texto[-1] == '"':
        texto = texto[1:-1]
    if texto.startswith("./"):
        texto = texto[2:]
    return texto


def caminho_inseguro(caminho: str) -> bool:
    texto = normalizar_caminho(caminho)
    if texto.startswith("/") or texto.startswith("~") or texto == "/dev/null":
        return True
    partes = [parte for parte in texto.split("/") if parte not in {"", "."}]
    return ".." in partes or bool(partes and partes[0] == ".git")


def caminho_proibido(caminho: str, proibidos: list[str]) -> bool:
    atual = normalizar_caminho(caminho)
    for regra in proibidos:
        base = normalizar_caminho(regra).rstrip("/")
        if not base or base in {".", "*"}:
            continue
        if atual == base or atual.startswith(base + "/"):
            return True
    return False


def _token_caminho(token: str) -> str | None:
    texto = normalizar_caminho(token)
    if texto in {"", "/dev/null"}:
        return None
    if texto.startswith("a/") or texto.startswith("b/"):
        texto = texto[2:]
    if texto in {"", "/dev/null"}:
        return None
    return texto


def caminhos_do_patch(patch: str) -> list[str]:
    vistos: list[str] = []

    def adicionar(token: str | None) -> None:
        caminho = _token_caminho(token or "")
        if caminho and caminho not in vistos:
            vistos.append(caminho)

    for linha in patch.splitlines():
        if linha.startswith("diff --git "):
            resto = linha[len("diff --git ") :]
            if resto.startswith("a/") and " b/" in resto:
                esquerda, direita = resto.split(" b/", 1)
                adicionar(esquerda)
                adicionar("b/" + direita)
            else:
                for parte in resto.split():
                    adicionar(parte)
            continue
        if linha.startswith("--- ") or linha.startswith("+++ "):
            adicionar(linha[4:])
    return vistos


def validar_caminhos_do_patch(patch: str) -> list[str]:
    if "GIT binary patch" in patch:
        raise ErroRepositorio("Patch binário não entra nesta versão.")
    caminhos = caminhos_do_patch(patch)
    ruins = [caminho for caminho in caminhos if caminho_inseguro(caminho)]
    if ruins:
        raise ErroRepositorio("O patch sai do repositório ou mexe em .git: " + ", ".join(ruins))
    return caminhos


def checar_patch(repo: Path | str, patch: str) -> tuple[bool, str | None, list[str]]:
    if not patch or not patch.strip():
        return False, "O patch está vazio.", []
    try:
        caminhos = validar_caminhos_do_patch(patch)
    except ErroRepositorio as exc:
        return False, str(exc), []
    if not caminhos:
        return False, "Não encontrei arquivos no diff.", []
    proc = git(repo, "apply", "--check", "--whitespace=nowarn", "-", entrada=patch)
    if proc.returncode != 0:
        detalhe = (proc.stderr or proc.stdout).strip()
        return False, detalhe or "git apply --check recusou o patch.", caminhos
    return True, None, caminhos


def criar_commit(repo: Path | str, caminhos: list[str], mensagem: str) -> str:
    """Grava um commit local. Não configura identidade e não empurra para remoto."""
    if not caminhos:
        raise ErroRepositorio("Não há arquivos para commitar.")
    for caminho in caminhos:
        if caminho_inseguro(caminho):
            raise ErroRepositorio(f"Commit bloqueado. Caminho inseguro: {caminho}")
    nome = git(repo, "config", "--get", "user.name")
    email = git(repo, "config", "--get", "user.email")
    if nome.returncode != 0 or not (nome.stdout or "").strip() or email.returncode != 0 or not (email.stdout or "").strip():
        raise ErroRepositorio(
            "Commit bloqueado. Este repositório não tem user.name e user.email. "
            "Configure a identidade local do git nele. A fábrica não altera essa configuração e não empurra para o remoto."
        )
    add = git(repo, "add", "--", *caminhos)
    if add.returncode != 0:
        raise ErroRepositorio((add.stderr or add.stdout).strip() or "git add falhou.")
    commit = git(repo, "commit", "-m", mensagem)
    if commit.returncode != 0:
        detalhe = (commit.stderr or commit.stdout).strip()
        raise ErroRepositorio(detalhe or "git commit falhou.")
    rev = git(repo, "rev-parse", "HEAD")
    if rev.returncode != 0 or not rev.stdout.strip():
        raise ErroRepositorio("Não li o commit criado.")
    return rev.stdout.strip()


def aplicar_patch(repo: Path | str, patch: str) -> list[str]:
    caminhos = validar_caminhos_do_patch(patch)
    checagem = git(repo, "apply", "--check", "--whitespace=nowarn", "-", entrada=patch)
    if checagem.returncode != 0:
        detalhe = (checagem.stderr or checagem.stdout).strip()
        raise ErroRepositorio(detalhe or "git apply --check recusou o patch.")
    aplicado = git(repo, "apply", "--whitespace=nowarn", "-", entrada=patch)
    if aplicado.returncode != 0:
        detalhe = (aplicado.stderr or aplicado.stdout).strip()
        raise ErroRepositorio(detalhe or "git apply falhou.")
    return caminhos


def extrair_diff(texto: str) -> str:
    bruto = texto.strip()
    if bruto.startswith("SEM_PATCH"):
        return ""
    cerca = re.search(r"```(?:diff)?\s*\n(.*?)```", bruto, re.DOTALL)
    if cerca:
        bruto = cerca.group(1).strip()
    for marcador in ("diff --git ", "--- "):
        indice = bruto.find(marcador)
        if indice >= 0:
            bruto = bruto[indice:]
            break
    if bruto and not bruto.endswith("\n"):
        bruto += "\n"
    return bruto


def _tokens(pedido: str) -> list[str]:
    palavras = re.findall(r"[a-z0-9_./-]+", sem_acento(pedido.lower()))
    return [palavra for palavra in palavras if len(palavra) >= 4 and palavra not in _STOP]


def _resumo_arvore(arquivos: list[str]) -> str:
    if not arquivos:
        return "O git não listou arquivos."
    if len(arquivos) <= 80:
        return "\n".join(arquivos)
    contagem: dict[str, int] = {}
    for arquivo in arquivos:
        topo = arquivo.split("/", 1)[0] if "/" in arquivo else "(raiz)"
        contagem[topo] = contagem.get(topo, 0) + 1
    linhas = [f"{nome}: {quantidade}" for nome, quantidade in sorted(contagem.items())]
    return f"{len(arquivos)} arquivos.\n" + "\n".join(linhas)


def _ler_texto(repo: Path, relativo: str, limite: int = 60_000) -> str:
    raiz = repo.resolve()
    caminho = (repo / relativo).resolve()
    try:
        caminho.relative_to(raiz)
    except ValueError:
        return ""
    if not caminho.is_file():
        return ""
    dados = caminho.read_bytes()[: limite + 1]
    if b"\0" in dados[:4096]:
        return ""
    texto = dados.decode("utf-8", errors="replace")
    if len(texto) > limite:
        return texto[:limite] + "\n… [arquivo truncado na inspeção]\n"
    return texto


def escolher_arquivos(repo: Path, pedido: str, foco: str, arquivos: list[str], limite: int = 8) -> list[str]:
    mencionados: list[str] = []
    for achado in _CAMINHO_PEDIDO.findall(pedido):
        relativo = normalizar_caminho(achado)
        if relativo in arquivos and relativo not in mencionados:
            mencionados.append(relativo)
    termos = _tokens(pedido)
    pontos = {arquivo: 0 for arquivo in arquivos}
    for caminho in mencionados:
        pontos[caminho] += 50
    foco_prefixo = "" if foco in {"", "."} else foco.rstrip("/") + "/"
    for caminho in arquivos:
        nome = Path(caminho).name
        if nome in LOCKS or nome.endswith(".lock"):
            pontos[caminho] -= 20
        caminho_min = caminho.lower()
        for termo in termos:
            if termo in caminho_min:
                pontos[caminho] += 5
        if foco_prefixo and caminho.startswith(foco_prefixo):
            pontos[caminho] += 2
        if Path(caminho).suffix.lower() in EXTENSOES and nome not in LOCKS:
            pontos[caminho] += 1
    for termo in termos[:8]:
        proc = git(repo, "grep", "-l", "-I", "-i", "-F", "--", termo)
        if proc.returncode not in (0, 1):
            continue
        for linha in proc.stdout.splitlines():
            if linha in pontos:
                pontos[linha] += 3
    escolhidos = list(mencionados)
    ranqueados = sorted(arquivos, key=lambda item: (-pontos[item], item.count("/"), item))
    for caminho in ranqueados:
        if caminho in escolhidos:
            continue
        if pontos[caminho] <= 0:
            continue
        escolhidos.append(caminho)
        if len(escolhidos) >= limite:
            break
    if not escolhidos:
        fonte = [
            caminho
            for caminho in arquivos
            if Path(caminho).suffix.lower() in EXTENSOES and Path(caminho).name not in LOCKS
        ]
        fonte.sort(key=lambda item: (item.count("/"), len(item)))
        escolhidos = fonte[:limite]
    return escolhidos[:limite]


def protecoes_fixas(pedido: str, arquivos: list[str]) -> list[str]:
    pedido_norm = sem_acento(pedido.lower())
    saida: list[str] = []
    for arquivo in arquivos:
        nome = Path(arquivo).name
        if nome in LOCKS or nome.endswith(".lock"):
            if nome.lower() not in pedido_norm and arquivo.lower() not in pedido_norm:
                saida.append(arquivo)
    return saida


def caminhos_conhecidos(candidatos, arquivos: list[str]) -> list[str]:
    indice = set(arquivos)
    saida: list[str] = []
    for item in candidatos:
        caminho = normalizar_caminho(str(item))
        if not caminho or caminho_inseguro(caminho):
            continue
        prefixo = caminho.rstrip("/") + "/"
        if caminho in indice or any(arquivo.startswith(prefixo) for arquivo in indice):
            if caminho not in saida:
                saida.append(caminho)
    return saida


def formatar_conteudos(conteudos: dict) -> str:
    if not conteudos:
        return "(nenhum arquivo lido)\n"
    blocos = []
    for caminho, texto in conteudos.items():
        blocos.append(f"----- {caminho} -----\n{texto.rstrip()}\n")
    return "\n".join(blocos)


def coletar_fatos(repo: Path | str, pedido: str, foco: str = ".") -> dict:
    repo = Path(repo).resolve()
    ramo = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if ramo.returncode != 0:
        raise ErroRepositorio((ramo.stderr or "git rev-parse falhou.").strip())
    log = git(repo, "log", "-1", "--format=%H%n%s")
    commit = None
    assunto = None
    if log.returncode == 0 and log.stdout.strip():
        linhas = log.stdout.splitlines()
        commit = linhas[0].strip()
        assunto = linhas[1].strip() if len(linhas) > 1 else ""
    status = git(repo, "status", "--porcelain")
    if status.returncode != 0:
        raise ErroRepositorio((status.stderr or "git status falhou.").strip())
    linhas_status = [linha for linha in status.stdout.splitlines() if linha.strip()]
    arquivos = listar_arquivos(repo)
    relevantes = escolher_arquivos(repo, pedido, foco, arquivos)
    conteudos = {}
    for relativo in relevantes:
        texto = _ler_texto(repo, relativo)
        if texto:
            conteudos[relativo] = texto
    return {
        "topo": str(repo),
        "foco": foco,
        "ramo": (ramo.stdout or "").strip() or "HEAD",
        "commit": commit,
        "assunto": assunto,
        "sujo": bool(linhas_status),
        "status": linhas_status,
        "total_arquivos": len(arquivos),
        "arquivos": arquivos,
        "relevantes": [relativo for relativo in relevantes if relativo in conteudos],
        "conteudos": conteudos,
        "arvore_resumida": _resumo_arvore(arquivos),
    }

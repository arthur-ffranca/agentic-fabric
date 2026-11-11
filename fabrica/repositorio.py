"""Leitura real do repositório alvo. Escrita só em aplicar_patch e criar_commit."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
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
# Arquivos que podem guardar segredo. O conteúdo nunca vai ao modelo e o patch não toca neles.
_SENSIVEL_NOMES = {
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".htpasswd",
    ".git-credentials",
    "token.json",
    "tokens.json",
}
_SENSIVEL_SUFIXOS = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx"}
_SENSIVEL_PREFIXOS = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
_SENSIVEL_RADICAIS = ("credentials", "credenciais", "secrets", "secret", "segredos", "service-account", "service_account")
_SENSIVEL_DADOS = {"", ".json", ".yml", ".yaml", ".toml", ".txt", ".ini", ".cfg", ".conf", ".xml", ".csv", ".env"}
_SENSIVEL_PASTAS = {".ssh", ".aws", ".gnupg"}
_ENV_DE_EXEMPLO = (".example", ".sample", ".template", ".dist")
CONFIG_VERIFICACAO = ".fabrica.toml"

LIMITE_ARQUIVO = 200_000
ORCAMENTO_RECORTE = 480_000

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
    # Em bytes, e não em modo texto: no Windows o modo texto troca \n por \r\n na entrada e usa a
    # codificação do sistema, o que estraga o patch (fim de linha e acento) antes de chegar ao git.
    proc = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        input=None if entrada is None else entrada.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
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


def caminho_sensivel(caminho: str) -> bool:
    """Diz se o arquivo pode guardar segredo (.env, chave, credencial)."""
    partes = [parte for parte in normalizar_caminho(caminho).split("/") if parte]
    if not partes:
        return False
    if any(parte.lower() in _SENSIVEL_PASTAS for parte in partes[:-1]):
        return True
    nome = partes[-1].lower()
    if nome == ".env" or nome.startswith(".env."):
        return not nome.endswith(_ENV_DE_EXEMPLO)
    if nome.endswith(".env") and len(nome) > 4:
        return True
    if nome in _SENSIVEL_NOMES or nome.startswith(_SENSIVEL_PREFIXOS):
        return True
    sufixo = Path(nome).suffix
    if sufixo in _SENSIVEL_SUFIXOS:
        return True
    radical = nome[: -len(sufixo)] if sufixo else nome
    if sufixo in _SENSIVEL_DADOS and any(
        radical == base or radical.startswith((base + ".", base + "-", base + "_")) for base in _SENSIVEL_RADICAIS
    ):
        return True
    return False


def dentro_das_raizes(caminho: Path | str, raizes: list[Path] | None) -> bool:
    """Sem raízes configuradas, qualquer caminho vale (uso local)."""
    if not raizes:
        return True
    alvo = Path(caminho).expanduser().resolve()
    for raiz in raizes:
        base = Path(raiz).expanduser().resolve()
        if alvo == base or base in alvo.parents:
            return True
    return False


def raizes_do_ambiente() -> list[Path] | None:
    bruto = (os.environ.get("FABRICA_REPOS") or "").strip()
    if not bruto:
        return None
    return [Path(parte).expanduser() for parte in bruto.split(os.pathsep) if parte.strip()]


def listar_arquivos(repo: Path) -> list[str]:
    proc = git(repo, "ls-files", "-co", "--exclude-standard")
    if proc.returncode != 0:
        raise ErroRepositorio((proc.stderr or "git ls-files falhou.").strip())
    return [linha for linha in proc.stdout.splitlines() if linha and not linha.endswith("/")]


_ESCAPES_C = {"n": 10, "t": 9, "r": 13, "a": 7, "b": 8, "f": 12, "v": 11, '"': 34, "\\": 92}


def _ler_entre_aspas(texto: str) -> tuple[str, str]:
    """Lê um caminho no estilo C do git ("a/configura\\303\\247...") e devolve (caminho, resto)."""
    saida = bytearray()
    indice = 1
    while indice < len(texto):
        atual = texto[indice]
        if atual == '"':
            return saida.decode("utf-8", errors="replace"), texto[indice + 1 :]
        if atual == "\\" and indice + 1 < len(texto):
            seguinte = texto[indice + 1]
            if seguinte in "01234567":
                fim = indice + 1
                while fim < len(texto) and fim < indice + 4 and texto[fim] in "01234567":
                    fim += 1
                saida.append(int(texto[indice + 1 : fim], 8) & 0xFF)
                indice = fim
                continue
            if seguinte in _ESCAPES_C:
                saida.append(_ESCAPES_C[seguinte])
            else:
                saida.extend(seguinte.encode("utf-8"))
            indice += 2
            continue
        saida.extend(atual.encode("utf-8"))
        indice += 1
    return saida.decode("utf-8", errors="replace"), ""


def normalizar_caminho(caminho: str) -> str:
    texto = caminho.strip().strip("`")
    if texto.startswith('"'):
        texto, _ = _ler_entre_aspas(texto)
    elif "\t" in texto:
        texto = texto.split("\t", 1)[0].strip()
    texto = texto.replace("\\", "/")
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
    if token.strip().startswith('"'):
        texto = normalizar_caminho(token)
    else:
        texto = normalizar_caminho(token.split("\t", 1)[0])
    if texto in {"", "/dev/null"}:
        return None
    if texto.startswith("a/") or texto.startswith("b/"):
        texto = texto[2:]
    if texto in {"", "/dev/null"}:
        return None
    return texto


def _lados_do_diff_git(resto: str) -> list[str]:
    resto = resto.strip()
    if resto.startswith('"'):
        esquerda, sobra = _ler_entre_aspas(resto)
        sobra = sobra.strip()
        if sobra.startswith('"'):
            direita, _ = _ler_entre_aspas(sobra)
        else:
            direita = sobra
        return [esquerda, direita]
    if ' "b/' in resto:
        esquerda, sobra = resto.split(' "b/', 1)
        direita, _ = _ler_entre_aspas('"b/' + sobra)
        return [esquerda, direita]
    if resto.startswith("a/") and " b/" in resto:
        esquerda, direita = resto.split(" b/", 1)
        return [esquerda, "b/" + direita]
    return resto.split()


def caminhos_pelo_git(patch: str) -> list[str] | None:
    """Os caminhos que o próprio git diz que o patch toca. None quando o git não lê o patch."""
    if not patch or not patch.strip():
        return None
    try:
        proc = subprocess.run(
            ["git", "apply", "--numstat", "-z", "-"],
            input=patch.encode("utf-8"),
            capture_output=True,
            cwd=tempfile.gettempdir(),
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    campos = proc.stdout.decode("utf-8", errors="replace").split("\0")
    vistos: list[str] = []
    indice = 0
    while indice < len(campos):
        partes = campos[indice].split("\t", 2)
        indice += 1
        if len(partes) < 3:
            continue
        if partes[2]:
            nomes = [partes[2]]
        else:
            nomes = campos[indice : indice + 2]
            indice += 2
        for nome in nomes:
            if nome and nome not in vistos:
                vistos.append(nome)
    return vistos


def caminhos_do_patch(patch: str) -> list[str]:
    """Une o que os cabeçalhos dizem com o que o git diz. Na dúvida, o caminho entra na conferência."""
    vistos = _caminhos_dos_cabecalhos(patch)
    for caminho in caminhos_pelo_git(patch) or []:
        if caminho not in vistos:
            vistos.append(caminho)
    return vistos


def _caminhos_dos_cabecalhos(patch: str) -> list[str]:
    vistos: list[str] = []

    def adicionar(token: str | None) -> None:
        caminho = _token_caminho(token or "")
        if caminho and caminho not in vistos:
            vistos.append(caminho)

    for linha in patch.splitlines():
        if linha.startswith("diff --git "):
            for parte in _lados_do_diff_git(linha[len("diff --git ") :]):
                adicionar(parte)
            continue
        if linha.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            vistos_extra = normalizar_caminho(linha.split(" ", 2)[2])
            if vistos_extra and vistos_extra not in vistos:
                vistos.append(vistos_extra)
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


def checar_patch(repo: Path | str, patch: str, base: str = "") -> tuple[bool, str | None, list[str]]:
    """Confere se o patch aplica. Com base, confere em cima de um patch anterior ainda não aplicado."""
    if not patch or not patch.strip():
        return False, "O patch está vazio.", []
    try:
        caminhos = validar_caminhos_do_patch(patch)
    except ErroRepositorio as exc:
        return False, str(exc), []
    if not caminhos:
        return False, "Não encontrei arquivos no diff.", []
    if base and base.strip():
        try:
            with copia_descartavel(repo) as copia:
                antes = _git(copia, "apply", "--whitespace=nowarn", "-", entrada=base)
                if antes.returncode != 0:
                    detalhe = (antes.stderr or antes.stdout).strip()
                    return False, "O patch anterior não aplica mais: " + detalhe, caminhos
                proc = _git(copia, "apply", "--check", "--whitespace=nowarn", "-", entrada=patch)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"Não consegui conferir em cima do patch anterior: {exc}", caminhos
    else:
        proc = git(repo, "apply", "--check", "--whitespace=nowarn", "-", entrada=patch)
    if proc.returncode != 0:
        detalhe = (proc.stderr or proc.stdout).strip()
        return False, detalhe or "git apply --check recusou o patch.", caminhos
    return True, None, caminhos


def adaptar_fim_de_linha(repo: Path | str, patch: str) -> str:
    """Devolve o patch com CRLF nas seções de arquivos que usam CRLF no disco. O modelo escreve em LF."""
    raiz = Path(repo)
    saida: list[str] = []
    crlf = False
    for linha in patch.split("\n"):
        if linha.startswith("diff --git "):
            crlf = False
        elif linha.startswith("--- "):
            alvo = _token_caminho(linha[4:])
            crlf = False
            if alvo and not caminho_inseguro(alvo):
                arquivo = raiz / alvo
                try:
                    crlf = arquivo.is_file() and b"\r\n" in arquivo.read_bytes()[:65536]
                except OSError:
                    crlf = False
        elif crlf and linha[:1] in {" ", "+", "-"} and not linha.startswith(("+++ ", "--- ")) and not linha.endswith("\r"):
            linha += "\r"
        saida.append(linha)
    return "\n".join(saida)


@contextlib.contextmanager
def copia_descartavel(repo: Path | str):
    """Cópia do estado atual (rastreados e não rastreados fora do .gitignore) num diretório temporário.

    O repositório alvo não é tocado. A cópia vira um repositório próprio para o git apply não
    confundir a raiz quando o diretório temporário está dentro de outro repositório.
    """
    origem = Path(repo).resolve()
    arquivos = listar_arquivos(origem)
    pasta = Path(tempfile.mkdtemp(prefix="fabrica-copia-"))
    try:
        for relativo in arquivos:
            fonte = origem / relativo
            if not (fonte.is_file() or fonte.is_symlink()):
                continue
            destino = pasta / relativo
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fonte, destino, follow_symlinks=False)
        _git(pasta, "init", "-q")
        autocrlf = git(origem, "config", "--get", "core.autocrlf")
        _git(pasta, "config", "core.autocrlf", (autocrlf.stdout or "").strip() or "false")
        yield pasta
    finally:
        shutil.rmtree(pasta, ignore_errors=True)


def ler_com_patch(repo: Path | str, patch: str, caminhos: list[str]) -> dict[str, str] | None:
    """Conteúdo dos arquivos depois do patch, sem aplicar no repositório. None se o patch não aplica."""
    try:
        with copia_descartavel(repo) as copia:
            proc = _git(copia, "apply", "--whitespace=nowarn", "-", entrada=patch)
            if proc.returncode != 0:
                return None
            saida = {}
            for relativo in caminhos:
                texto = _ler_texto(copia, relativo)
                if texto:
                    saida[relativo] = texto
            return saida
    except (OSError, subprocess.SubprocessError, ErroRepositorio):
        return None


def impressao_dos_arquivos(repo: Path | str, caminhos: list[str]) -> dict[str, str | None]:
    """Hash do conteúdo de cada arquivo no disco. None para arquivo que não existe."""
    raiz = Path(repo)
    saida: dict[str, str | None] = {}
    for relativo in caminhos:
        if caminho_inseguro(relativo):
            continue
        arquivo = raiz / relativo
        try:
            saida[relativo] = hashlib.sha256(arquivo.read_bytes()).hexdigest() if arquivo.is_file() else None
        except OSError:
            saida[relativo] = None
    return saida


def commit_atual(repo: Path | str) -> str | None:
    proc = git(repo, "rev-parse", "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


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
    # Com os caminhos no fim, o commit leva só eles e deixa no index o que o usuário já tinha preparado.
    commit = git(repo, "commit", "-m", mensagem, "--", *caminhos)
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


def _ler_texto(repo: Path, relativo: str, limite: int = LIMITE_ARQUIVO, termos: list[str] | None = None) -> str:
    if caminho_sensivel(relativo):
        return ""
    raiz = repo.resolve()
    caminho = (repo / relativo).resolve()
    try:
        caminho.relative_to(raiz)
    except ValueError:
        return ""
    if not caminho.is_file():
        return ""
    dados = caminho.read_bytes()[:4_000_000]
    if b"\0" in dados[:4096]:
        return ""
    texto = dados.decode("utf-8", errors="replace")
    if len(texto) <= limite:
        return texto
    return _recorte_por_termos(texto, termos or [], limite)


def _recorte_por_termos(texto: str, termos: list[str], limite: int, margem: int = 40, cabeca: int = 60) -> str:
    """Arquivo grande: o começo e as janelas em volta do que o pedido cita, com as omissões marcadas."""
    linhas = texto.splitlines()
    alvos = [termo for termo in termos if termo]
    marcadas: list[int] = []
    if alvos:
        for numero, linha in enumerate(linhas):
            baixa = sem_acento(linha.lower())
            if any(termo in baixa for termo in alvos):
                marcadas.append(numero)
    if not marcadas:
        return texto[:limite] + "\n… [arquivo truncado na inspeção]\n"
    manter = set(range(min(cabeca, len(linhas))))
    gasto = sum(len(linhas[numero]) + 1 for numero in manter)
    for centro in marcadas:
        janela = range(max(0, centro - margem), min(len(linhas), centro + margem + 1))
        custo = sum(len(linhas[numero]) + 1 for numero in janela if numero not in manter)
        if gasto + custo > limite:
            continue
        manter.update(janela)
        gasto += custo
    saida: list[str] = []
    anterior = -1
    for numero in sorted(manter):
        if numero > anterior + 1:
            saida.append(f"… [linhas {anterior + 2}–{numero} omitidas na inspeção] …")
        saida.append(linhas[numero])
        anterior = numero
    if anterior < len(linhas) - 1:
        saida.append(f"… [linhas {anterior + 2}–{len(linhas)} omitidas na inspeção] …")
    return "\n".join(saida) + "\n"


_SIMBOLO = re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
_IMPORT_JS = re.compile(r"""(?:from|require\(|import\()\s*['"](\.{1,2}/[^'"]+)['"]""")
_EXT_JS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _simbolos(pedido: str) -> list[str]:
    achados = re.findall(r"`([^`]+)`", pedido) + _SIMBOLO.findall(pedido)
    vistos: list[str] = []
    for achado in achados:
        nome = achado.strip().split("(")[0].split(".")[-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{2,}", nome) and nome not in vistos:
            vistos.append(nome)
    return vistos[:6]


def _resolver_modulo(candidato: str, conjunto: set[str], arquivos: list[str]) -> str | None:
    if candidato in conjunto:
        return candidato
    sufixo = "/" + candidato
    for arquivo in arquivos:
        if arquivo.endswith(sufixo):
            return arquivo
    return None


def arquivos_importados(repo: Path, relativo: str, arquivos: list[str]) -> list[str]:
    """Arquivos do repositório que este arquivo importa. Segue import de Python e import relativo de JS/TS."""
    texto = _ler_texto(repo, relativo)
    if not texto:
        return []
    conjunto = set(arquivos)
    pasta = Path(relativo).parent
    candidatos: list[str] = []
    sufixo = Path(relativo).suffix.lower()
    if sufixo == ".py":
        try:
            arvore = ast.parse(texto)
        except (SyntaxError, ValueError):
            return []
        for no in ast.walk(arvore):
            if isinstance(no, ast.Import):
                modulos = [(apelido.name, 0) for apelido in no.names]
                nomes: list[str] = []
            elif isinstance(no, ast.ImportFrom):
                modulos = [(no.module or "", no.level)]
                nomes = [apelido.name for apelido in no.names if apelido.name != "*"]
            else:
                continue
            for modulo, nivel in modulos:
                base = modulo.replace(".", "/")
                if nivel:
                    raiz = pasta
                    for _ in range(nivel - 1):
                        raiz = raiz.parent
                    base = (raiz / base).as_posix() if base else raiz.as_posix()
                    base = "" if base == "." else base
                if base:
                    candidatos += [base + ".py", base + "/__init__.py"]
                for nome in nomes:
                    candidatos.append((base + "/" if base else "") + nome + ".py")
    elif sufixo in _EXT_JS:
        for alvo in _IMPORT_JS.findall(texto):
            base = os.path.normpath((pasta / alvo).as_posix()).replace("\\", "/")
            candidatos.append(base)
            candidatos += [base + ext for ext in _EXT_JS] + [base + "/index" + ext for ext in _EXT_JS]
    saida: list[str] = []
    for candidato in candidatos:
        achado = _resolver_modulo(candidato, conjunto, arquivos)
        if achado and achado != relativo and achado not in saida:
            saida.append(achado)
    return saida


def escolher_arquivos(repo: Path, pedido: str, foco: str, arquivos: list[str], limite: int = 8) -> list[str]:
    candidatos = [arquivo for arquivo in arquivos if not caminho_sensivel(arquivo)]
    conjunto = set(candidatos)
    mencionados: list[str] = []
    for achado in _CAMINHO_PEDIDO.findall(pedido):
        relativo = normalizar_caminho(achado)
        if relativo in conjunto and relativo not in mencionados:
            mencionados.append(relativo)
    termos = _tokens(pedido)
    pontos = {arquivo: 0 for arquivo in candidatos}
    for caminho in mencionados:
        pontos[caminho] += 50
    foco_prefixo = "" if foco in {"", "."} else foco.rstrip("/") + "/"
    for caminho in candidatos:
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
        proc = git(repo, "grep", "-l", "-I", "-i", "-F", "--untracked", "--", termo)
        if proc.returncode not in (0, 1):
            continue
        for linha in proc.stdout.splitlines():
            if linha in pontos:
                pontos[linha] += 3
    # Quem define o símbolo que o pedido cita pesa mais que quem só o menciona.
    for simbolo in _simbolos(pedido):
        padrao = rf"(def|class|function|func|fn|const|interface|type)[[:space:]]+{simbolo}([^A-Za-z0-9_]|$)"
        proc = git(repo, "grep", "-l", "-I", "-E", "--untracked", "-e", padrao)
        if proc.returncode not in (0, 1):
            continue
        for linha in proc.stdout.splitlines():
            if linha in pontos:
                pontos[linha] += 8
    # O que o arquivo citado importa entra junto: o código precisa ver o que ele usa.
    for caminho in mencionados:
        for importado in arquivos_importados(repo, caminho, candidatos):
            pontos[importado] += 30
    sementes = [
        caminho
        for caminho in sorted(candidatos, key=lambda item: (-pontos[item], item.count("/"), item))[:3]
        if pontos[caminho] > 0 and caminho not in mencionados
    ]
    for caminho in sementes:
        for importado in arquivos_importados(repo, caminho, candidatos):
            pontos[importado] += 4
    escolhidos = list(mencionados)
    ranqueados = sorted(candidatos, key=lambda item: (-pontos[item], item.count("/"), item))
    for caminho in ranqueados:
        if len(escolhidos) >= limite:
            break
        if caminho in escolhidos:
            continue
        if pontos[caminho] <= 0:
            continue
        escolhidos.append(caminho)
    if not escolhidos:
        fonte = [
            caminho
            for caminho in candidatos
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
        if caminho_sensivel(arquivo) or arquivo == CONFIG_VERIFICACAO:
            # O modelo não vê estes arquivos, então também não os altera.
            saida.append(arquivo)
            continue
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
    termos = _tokens(pedido) + [simbolo.lower() for simbolo in _simbolos(pedido)]
    conteudos = {}
    cortados: list[str] = []
    restante = ORCAMENTO_RECORTE
    for relativo in relevantes:
        if restante < 2_000:
            break
        texto = _ler_texto(repo, relativo, limite=min(LIMITE_ARQUIVO, restante), termos=termos)
        if texto:
            conteudos[relativo] = texto
            restante -= len(texto)
            if "omitidas na inspeção] …" in texto or "[arquivo truncado na inspeção]" in texto:
                cortados.append(relativo)
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
        "cortados": cortados,
        "arvore_resumida": _resumo_arvore(arquivos),
    }

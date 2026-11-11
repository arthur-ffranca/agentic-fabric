"""A triagem planeja o turno e despacha tarefas estreitas. O orquestrador não escolhe especialista."""

from __future__ import annotations

import re
import threading

from fabrica.erros import ErroFabrica, ErroLLM
from fabrica.fila import CaixaPatch
from fabrica.llm import solicitar_json
from fabrica.repositorio import ler_com_patch
from fabrica.tarefa import Tarefa, novo_id
from fabrica.uso import Medidor

_CAMINHO = re.compile(
    r"\b[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|php|cs|md|json|yml|yaml|toml|css|html|sql|txt)\b",
    re.IGNORECASE,
)
_ALVO = re.compile(
    r"\b[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|php|cs|md|json|yml|yaml|toml|css|html|sql|txt)\b"
    r"|`[^`]+`"
    r"|\b[\w]+_[\w]+\b",
    re.IGNORECASE,
)
_FORTE = re.compile(
    r"\b(adicion\w+|acrescent\w+|cri[aeo]\w*|remov\w+|apag\w+|exclu\w+|corrig\w+|consert\w+"
    r"|implement\w+|renome\w+|document\w+|escrev\w+|alter\w+|mud\w+|troc\w+|extra[ií]\w+"
    r"|mov\w+|substitu\w+|valid\w+|imped\w+|garant\w+)\b",
    re.IGNORECASE,
)
_FRACO = re.compile(
    r"\b(melhor\w+|otimiz\w+|ajust\w+|arrum\w+|refator\w+|mex\w+|olh\w+|vej\w+|revis\w+)\b",
    re.IGNORECASE,
)


def pedido_ambiguo(pedido: str) -> bool:
    texto = " ".join((pedido or "").split())
    palavras = texto.split()
    if len(palavras) < 4 or len(texto) < 12:
        return True
    tem_alvo = bool(_ALVO.search(texto))
    forte = bool(_FORTE.search(texto))
    fraco = bool(_FRACO.search(texto))
    if tem_alvo and (forte or fraco) and len(palavras) >= 4:
        return False
    if forte and len(palavras) >= 5:
        return False
    if (forte or fraco) and len(palavras) >= 8:
        return False
    if len(palavras) >= 12:
        return False
    return True


def planejar_heuristica(pedido: str) -> list[dict]:
    """Separa duas tarefas só quando o pedido nomeia arquivos diferentes em trechos distintos."""
    caminhos = _caminhos_no_texto(pedido)
    if len(caminhos) < 2:
        return [_spec(pedido, caminhos)]
    partes = [parte.strip() for parte in re.split(r"\s+\be\b\s+|;\s*|\n+", pedido) if parte.strip()]
    atribuidas = []
    for parte in partes:
        alvos = [caminho for caminho in caminhos if caminho in parte]
        if alvos:
            atribuidas.append((parte, alvos))
    if len(atribuidas) >= 2 and set(atribuidas[0][1]) != set(atribuidas[1][1]):
        return [_spec(parte, alvos) for parte, alvos in atribuidas[:2]]
    return [_spec(pedido, caminhos)]


class _Trilha(list):
    """A trilha do turno. Cada passo novo é avisado a quem acompanha (a tela, pelo servidor)."""

    def __init__(self, ao_passo=None) -> None:
        super().__init__()
        self._ao_passo = ao_passo

    def append(self, nome) -> None:
        super().append(nome)
        if self._ao_passo is not None:
            try:
                self._ao_passo(nome)
            except Exception:
                pass


class Triagem:
    def __init__(
        self,
        auditor,
        codigo,
        revisor,
        performance,
        fila,
        *,
        codigo_b=None,
        llm=None,
        planejar=None,
        verificador=None,
        max_ciclos: int = 3,
        timeout_performance: float = 180,
    ) -> None:
        self.auditor = auditor
        self.codigo = codigo
        self.codigo_b = codigo_b or codigo
        self.revisor = revisor
        self.performance = performance
        self.fila = fila
        self.llm = llm
        self.planejar = planejar
        self.verificador = verificador
        self.max_ciclos = max_ciclos
        self.timeout_performance = timeout_performance

    def receber(self, tarefa: Tarefa, ao_passo=None) -> Tarefa:
        if tarefa.tipo != "resolver" or tarefa.destinatario != "triagem":
            raise ErroFabrica("A triagem só recebe tarefa do tipo resolver.")
        pedido_cru = (tarefa.entrada.get("pedido") or "").strip()
        pendente = (tarefa.contexto or {}).get("pergunta_pendente") or None
        if isinstance(pendente, dict) and pendente.get("pedido_original"):
            pedido = pendente["pedido_original"].strip() + "\n\nEsclarecimento do usuário:\n" + pedido_cru
        else:
            pedido = pedido_cru
        if pedido_ambiguo(pedido):
            return self._pergunta(tarefa, pedido)
        return self._cadeia(tarefa, pedido, ao_passo)

    def _pergunta(self, tarefa: Tarefa, pedido: str) -> Tarefa:
        texto = f'Entendi assim: "{pedido.strip()}". O que deve mudar, e em qual arquivo ou módulo?'
        return Tarefa(
            id=novo_id(),
            destinatario="orquestrador",
            tipo="perguntar",
            o_que_fazer="Perguntar ao usuário antes de alterar o repositório",
            contexto={
                "pedido_original": pedido,
                "caso_id": (tarefa.contexto or {}).get("caso_id"),
            },
            entrada={"pergunta": texto},
            resultado_esperado="Resposta que torne o pedido executável",
        )

    def _cadeia(self, tarefa: Tarefa, pedido: str, ao_passo=None) -> Tarefa:
        repo = tarefa.entrada.get("repo")
        foco = tarefa.entrada.get("foco") or "."
        historico = (tarefa.contexto or {}).get("turnos_anteriores") or []
        caixa = CaixaPatch()
        medidor = Medidor()
        tarefa_perf = Tarefa(
            id=novo_id(),
            destinatario="performance",
            tipo="executar",
            o_que_fazer=(
                "Observar o recorte do repositório e o patch final e devolver propostas concretas, "
                "sem alterar o patch."
            ),
            contexto={"repo": repo, "caso_id": (tarefa.contexto or {}).get("caso_id"), "origem": "performance"},
            entrada={"repo": repo, "pedido": pedido, "foco": foco},
            resultado_esperado=(
                "Lista de tarefas executáveis pelo código, cada uma com o que fazer, onde e o que melhora."
            ),
        )

        def performance(corrente):
            with medidor.ativo():
                return self.performance.executar(corrente, caixa)

        # A fila só entra aqui, depois do despacho, porque a performance corre fora da cadeia.
        trabalho = self.fila.despachar(tarefa_perf, performance)
        trilha = _Trilha(ao_passo)
        passos: list[dict] = []
        rascunho: dict = {}
        erro_cadeia: Exception | None = None
        t_ini = None
        linhas: list[dict] = []
        patch = ""
        arquivos_patch: list[str] = []
        revisor = None
        rodadas: list[dict] = []
        t_fim = None
        paralelo = False
        verificacao = None
        try:
            with medidor.ativo():
                t_ini = self.auditor.executar(self._auditor("inicio", repo, foco, pedido))
                trilha.append("auditor:inicio")
                passos.append(_passo(t_ini))
                restricoes = t_ini.resultado or {}
                conteudos = restricoes.get("conteudos") or {}
                specs = self._planejar(pedido, restricoes)
                if len(specs) <= 1:
                    final = self._continuar_uma(
                        repo, pedido, restricoes, conteudos, historico, caixa, passos, trilha, rascunho
                    )
                else:
                    final = self._continuar_varias(
                        repo, specs, restricoes, conteudos, historico, caixa, passos, trilha, rascunho, medidor
                    )
                linhas = final["linhas"]
                patch = final["patch"]
                arquivos_patch = final["arquivos"]
                revisor = final["revisor"]
                rodadas = final["rodadas"]
                t_fim = final["auditor_fim"]
                paralelo = final["paralelo"]
                verificacao = final["verificacao"]
        except Exception as exc:
            erro_cadeia = exc
            caixa.fechar(erro=str(exc))
        aviso = None
        propostas: list[dict] = []
        try:
            concluida = trabalho.aguardar(self.timeout_performance)
            if concluida is not None:
                propostas = (concluida.resultado or {}).get("propostas") or []
                aviso = (concluida.resultado or {}).get("aviso")
                passos.append(_passo(concluida))
        except Exception as exc:
            aviso = str(exc)
        if erro_cadeia:
            if isinstance(erro_cadeia, ErroFabrica):
                # O que já estava pronto segue com o erro, para o caso não perder o patch.
                erro_cadeia.parcial = {
                    "pedido": pedido,
                    "auditor_inicio": None if t_ini is None else t_ini.resultado,
                    "patch": rascunho.get("patch") or "",
                    "revisor": rascunho.get("revisor"),
                    "trilha": list(trilha),
                    "passos": passos,
                    "uso": medidor.total(),
                }
            raise erro_cadeia
        tarefa.resultado = {
            "pedido": pedido,
            "auditor_inicio": None if t_ini is None else t_ini.resultado,
            "patch": patch,
            "arquivos_patch": arquivos_patch,
            "codigo_notas": linhas[0]["notas"] if linhas else "",
            "codigo_valido": bool(linhas) and all(linha.get("valido") for linha in linhas),
            "revisor": revisor,
            "rodadas_revisor": rodadas,
            "ciclos_revisor": len(rodadas),
            "auditor_fim": t_fim,
            "verificacao": verificacao,
            "propostas": propostas,
            "aviso_performance": aviso,
            "trilha": list(trilha),
            "passos": passos,
            "linhas": linhas,
            "paralelo": paralelo,
            "uso": medidor.total(),
        }
        tarefa.status = "concluida"
        return tarefa

    def _continuar_uma(self, repo, pedido, restricoes, conteudos, historico, caixa, passos, trilha, rascunho) -> dict:
        pronto = self._laco(
            self.codigo, repo, pedido, restricoes, conteudos, historico, passos, trilha, rascunho=rascunho
        )
        t_cod = pronto["codigo"]
        t_rev = pronto["revisor"]
        patch = (t_cod.resultado or {}).get("patch") or ""
        caixa.publicar(patch)
        t_fim = self.auditor.executar(
            self._auditor("fim", repo, ".", pedido, restricoes=restricoes, patch=patch)
        )
        trilha.append("auditor:fim")
        passos.append(_passo(t_fim))
        linha = _linha(
            nome="Código",
            titulo=_titulo(pedido),
            o_que_fazer=pedido,
            agente=t_cod,
            revisor=t_rev,
            auditor_fim=t_fim,
            rodadas=pronto["rodadas"],
            paralelo=False,
            verificacao=pronto["verificacao"],
        )
        return {
            "linhas": [linha],
            "patch": patch,
            "arquivos": (t_cod.resultado or {}).get("arquivos") or [],
            "revisor": t_rev.resultado,
            "rodadas": pronto["rodadas"],
            "auditor_fim": t_fim.resultado,
            "paralelo": False,
            "verificacao": pronto["verificacao"],
        }

    def _continuar_varias(
        self, repo, specs, restricoes, conteudos, historico, caixa, passos, trilha, rascunho, medidor
    ) -> dict:
        agentes = [self.codigo, self.codigo_b]
        ondas = _ondas(specs)
        parciais: list[dict | None] = [None] * len(specs)
        trava = threading.Lock()
        for onda in ondas:
            anteriores = _juntar([item["patch"] for item in parciais if item])
            conteudos_onda = conteudos
            ja_inclui = False
            if anteriores and conteudos:
                # Quem vem depois enxerga o arquivo como ele fica com o patch de quem veio antes.
                atualizados = ler_com_patch(repo, anteriores, list(conteudos))
                if atualizados is not None:
                    conteudos_onda = {**conteudos, **atualizados}
                    ja_inclui = True
            if len(onda) == 1:
                indice = onda[0]
                parciais[indice] = self._ate_revisor(
                    repo,
                    specs[indice],
                    agentes[indice],
                    restricoes,
                    conteudos_onda,
                    historico,
                    anteriores,
                    passos,
                    trilha,
                    trava=trava,
                    ja_inclui=ja_inclui,
                )
                rascunho["patch"] = _juntar([item["patch"] for item in parciais if item])
                continue
            erros: list[Exception] = []

            def correr(
                indice: int,
                spec: dict,
                anteriores=anteriores,
                conteudos_onda=conteudos_onda,
                ja_inclui=ja_inclui,
                erros=erros,
            ) -> None:
                try:
                    with medidor.ativo():
                        pronto = self._ate_revisor(
                            repo,
                            spec,
                            agentes[indice],
                            restricoes,
                            conteudos_onda,
                            historico,
                            anteriores,
                            passos,
                            trilha,
                            trava=trava,
                            ja_inclui=ja_inclui,
                        )
                except Exception as exc:
                    erros.append(exc)
                    return
                parciais[indice] = pronto

            threads = [
                threading.Thread(target=correr, args=(indice, specs[indice]), name=f"codigo-{indice + 1}")
                for indice in onda
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            rascunho["patch"] = _juntar([item["patch"] for item in parciais if item])
            if erros:
                raise erros[0]
        patch = _juntar([item["patch"] for item in parciais if item])
        rascunho["patch"] = patch
        # Cada patch passou sozinho. Aqui passa o conjunto, que é o que vai ser aplicado.
        verificacao = self._verificar(repo, patch, True, restricoes, "", trilha, passos, trava)
        caixa.publicar(patch)
        for parcial in parciais:
            if parcial is None:
                raise ErroFabrica("Uma tarefa de código não devolveu resultado.")
            t_fim = self.auditor.executar(
                self._auditor(
                    "fim",
                    repo,
                    ".",
                    parcial["o_que_fazer"],
                    restricoes=restricoes,
                    patch=parcial["patch"],
                )
            )
            trilha.append("auditor:fim")
            passos.append(_passo(t_fim))
            parcial["auditor_fim"] = t_fim
        linhas = []
        for indice, parcial in enumerate(parciais):
            assert parcial is not None
            linhas.append(
                _linha(
                    nome=f"Código {indice + 1}",
                    titulo=parcial["titulo"],
                    o_que_fazer=parcial["o_que_fazer"],
                    agente=parcial["codigo"],
                    revisor=parcial["revisor"],
                    auditor_fim=parcial["auditor_fim"],
                    rodadas=parcial["rodadas"],
                    paralelo=len(ondas) == 1,
                    verificacao=parcial["verificacao"],
                )
            )
        vereditos = [(linha["revisor"] or {}).get("veredito") for linha in linhas]
        veredito = "aprovar" if vereditos and all(item == "aprovar" for item in vereditos) else "pedir_correcoes"
        motivos = []
        for linha in linhas:
            motivos.extend((linha["revisor"] or {}).get("motivos") or [])
        comprometidos = [(linha["auditor_fim"] or {}).get("comprometido") for linha in linhas]
        if any(item is True for item in comprometidos):
            comprometido: bool | None = True
        elif any(item is None for item in comprometidos):
            comprometido = None
        else:
            comprometido = False
        relatos = [
            (linha["auditor_fim"] or {}).get("relato")
            for linha in linhas
            if (linha["auditor_fim"] or {}).get("relato")
        ]
        arquivos: list[str] = []
        for linha in linhas:
            for caminho in linha["arquivos"]:
                if caminho not in arquivos:
                    arquivos.append(caminho)
        return {
            "linhas": linhas,
            "patch": patch,
            "arquivos": arquivos,
            "revisor": {"veredito": veredito, "motivos": motivos, "correcoes": ""},
            "rodadas": [linha["revisor"] or {} for linha in linhas],
            "auditor_fim": {"comprometido": comprometido, "relato": " ".join(relatos)},
            "paralelo": len(ondas) == 1,
            "verificacao": verificacao,
        }

    def _ate_revisor(
        self,
        repo,
        spec: dict,
        agente,
        restricoes,
        conteudos,
        historico,
        anteriores: str,
        passos,
        trilha,
        trava: threading.Lock | None = None,
        ja_inclui: bool = False,
    ) -> dict:
        pedido = spec["o_que_fazer"]
        pronto = self._laco(
            agente,
            repo,
            pedido,
            restricoes,
            conteudos,
            historico,
            passos,
            trilha,
            trava=trava,
            arquivos=spec.get("arquivos") or [],
            anteriores=anteriores,
            ja_inclui=ja_inclui,
        )
        return {
            "titulo": spec.get("titulo") or _titulo(pedido),
            "o_que_fazer": pedido,
            "patch": (pronto["codigo"].resultado or {}).get("patch") or "",
            "codigo": pronto["codigo"],
            "revisor": pronto["revisor"],
            "rodadas": pronto["rodadas"],
            "verificacao": pronto["verificacao"],
        }

    def _laco(
        self,
        agente,
        repo,
        pedido,
        restricoes,
        conteudos,
        historico,
        passos,
        trilha,
        *,
        trava: threading.Lock | None = None,
        arquivos=None,
        anteriores: str = "",
        ja_inclui: bool = False,
        rascunho: dict | None = None,
    ) -> dict:
        """Código, verificação e revisor, até aprovar ou acabar o número de voltas.

        A verificação vem antes do revisor: patch que quebra volta ao código com o erro real,
        sem gastar uma chamada de revisão.
        """
        correcoes = None
        patch_anterior = None
        rodadas: list[dict] = []
        while True:
            t_cod = agente.executar(
                self._codigo(
                    repo,
                    pedido,
                    restricoes,
                    conteudos,
                    correcoes,
                    patch_anterior,
                    historico,
                    arquivos_desta_tarefa=arquivos,
                    patches_anteriores=anteriores,
                    ja_inclui=ja_inclui,
                )
            )
            self._anotar(trilha, passos, "codigo", t_cod, trava)
            resultado = t_cod.resultado or {}
            verificacao = self._verificar(
                repo,
                resultado.get("patch") or "",
                bool(resultado.get("valido")),
                restricoes,
                anteriores,
                trilha,
                passos,
                trava,
                erro_apply=resultado.get("erro_apply"),
            )
            if verificacao is not None and verificacao.get("ok") is False and resultado.get("valido"):
                t_rev = self._revisor(repo, pedido, resultado, restricoes, conteudos, anteriores)
                t_rev.resultado = {
                    "veredito": "pedir_correcoes",
                    "motivos": ["A verificação automática falhou: " + _titulo(verificacao.get("saida") or "")],
                    "correcoes": "A verificação automática falhou. Corrija exatamente isto:\n"
                    + (verificacao.get("saida") or ""),
                    "origem": "verificador",
                }
                t_rev.status = "concluida"
            else:
                t_rev = self.revisor.executar(
                    self._revisor(repo, pedido, resultado, restricoes, conteudos, anteriores)
                )
                self._anotar(trilha, passos, "revisor", t_rev, trava)
            rodadas.append(t_rev.resultado or {})
            if rascunho is not None:
                rascunho["patch"] = resultado.get("patch") or ""
                rascunho["revisor"] = t_rev.resultado
            if (t_rev.resultado or {}).get("veredito") == "aprovar" or len(rodadas) >= self.max_ciclos:
                break
            correcoes = (t_rev.resultado or {}).get("correcoes") or ""
            patch_anterior = resultado.get("patch") or ""
        return {"codigo": t_cod, "revisor": t_rev, "rodadas": rodadas, "verificacao": verificacao}

    def _verificar(
        self, repo, patch: str, valido: bool, restricoes, anteriores: str, trilha, passos, trava, erro_apply=None
    ) -> dict | None:
        """None quando não há verificador montado. Patch que nem aplica já sai reprovado, sem rodar nada."""
        if self.verificador is None:
            return None
        if not valido or not patch.strip():
            return {
                "ok": False,
                "etapas": [{"nome": "aplicar", "ok": False, "detalhe": erro_apply or "O patch não aplica."}],
                "saida": "aplicar: " + (erro_apply or "O patch não aplica."),
            }
        resultado = self.verificador.verificar(
            repo,
            _juntar([anteriores, patch]),
            proibidos=list((restricoes or {}).get("nao_tocar_caminhos") or []),
        )
        passo = {
            "id": novo_id(),
            "destinatario": "verificador",
            "tipo": "executar",
            "o_que_fazer": "Aplicar o patch numa cópia e conferir caminhos, sintaxe e o comando do repositório.",
            "status": "concluida" if resultado.get("ok") else "falhou",
        }
        if trava is None:
            trilha.append("verificador")
            passos.append(passo)
        else:
            with trava:
                trilha.append("verificador")
                passos.append(passo)
        return resultado

    def _anotar(self, trilha, passos, nome, tarefa, trava) -> None:
        if trava is None:
            trilha.append(nome)
            passos.append(_passo(tarefa))
            return
        with trava:
            trilha.append(nome)
            passos.append(_passo(tarefa))

    def _planejar(self, pedido: str, restricoes: dict) -> list[dict]:
        if self.planejar is not None:
            specs = self.planejar(pedido, restricoes)
            return _normalizar_specs(specs, pedido) or [_spec(pedido, _caminhos_no_texto(pedido))]
        if self.llm is None:
            return planejar_heuristica(pedido)
        try:
            dados = solicitar_json(
                self.llm,
                sistema=(
                    "PAPEL: TRIAGEM\n"
                    "Você divide um pedido já claro em 1 ou 2 tarefas de código.\n"
                    "Os dois agentes, quando existirem, são agentes de código. Não crie especialidade por camada.\n"
                    "Separe em duas tarefas só se cada uma puder mexer em arquivos diferentes.\n"
                    "Se for uma mudança só, devolva uma tarefa.\n"
                    "Não invente arquivo que não esteja no pedido nem na lista conhecida.\n"
                    'Responda só JSON: {"tarefas":[{"titulo":"curto","o_que_fazer":"...","arquivos":["caminho"]}]}'
                ),
                usuario=(
                    f"Pedido:\n{pedido}\n\n"
                    "Arquivos conhecidos:\n"
                    + "\n".join((restricoes.get("fatos") or {}).get("arquivos") or ["(nenhum)"])
                ),
            )
        except ErroLLM:
            raise
        specs = _normalizar_specs(dados.get("tarefas"), pedido)
        if not specs:
            return planejar_heuristica(pedido)
        conhecidos = set((restricoes.get("fatos") or {}).get("arquivos") or [])
        for spec in specs:
            spec["arquivos"] = [
                caminho
                for caminho in spec["arquivos"]
                if caminho in conhecidos or caminho in pedido
            ]
        if len(specs) == 1:
            specs[0]["o_que_fazer"] = pedido
        return specs[:2]

    def _auditor(self, fase: str, repo, foco: str, pedido: str, restricoes: dict | None = None, patch: str | None = None) -> Tarefa:
        entrada = {"fase": fase, "repo": repo, "foco": foco, "pedido": pedido}
        if fase == "fim":
            entrada["restricoes"] = restricoes or {}
            entrada["patch"] = patch or ""
            fazer = "Comparar o patch com o repositório e dizer se algo que já funciona foi comprometido."
            esperado = "Parecer de compromisso, sem escrever no repositório."
        else:
            fazer = "Inspecionar o repositório e devolver o estado, os arquivos relevantes e o que não pode ser tocado."
            esperado = "Restrições e fatos lidos do repositório, sem escrever nele."
        return Tarefa(
            id=novo_id(),
            destinatario="auditor",
            tipo="executar",
            o_que_fazer=fazer,
            contexto={"repo": repo, "fase": fase},
            entrada=entrada,
            resultado_esperado=esperado,
        )

    def _codigo(
        self,
        repo,
        pedido,
        restricoes,
        conteudos,
        correcoes,
        patch_anterior,
        historico,
        arquivos_desta_tarefa=None,
        patches_anteriores=None,
        ja_inclui: bool = False,
    ) -> Tarefa:
        entrada = {
            "repo": repo,
            "pedido": pedido,
            "restricoes": restricoes,
            "conteudos": conteudos,
            "correcoes": correcoes,
            "patch_anterior": patch_anterior,
            "historico": historico,
        }
        if arquivos_desta_tarefa:
            entrada["arquivos_desta_tarefa"] = list(arquivos_desta_tarefa)
        if patches_anteriores:
            entrada["patches_anteriores"] = patches_anteriores
            entrada["conteudos_com_anteriores"] = bool(ja_inclui)
        return Tarefa(
            id=novo_id(),
            destinatario="codigo",
            tipo="executar",
            o_que_fazer=pedido,
            contexto={"repo": repo},
            entrada=entrada,
            resultado_esperado="Patch unificado aplicável, ainda não aplicado no repositório.",
        )

    def _revisor(self, repo, pedido, resultado_codigo: dict, restricoes, conteudos, anteriores: str = "") -> Tarefa:
        return Tarefa(
            id=novo_id(),
            destinatario="revisor",
            tipo="executar",
            o_que_fazer="Revisar se o patch cumpre o pedido e respeita as restrições do auditor.",
            contexto={"repo": repo},
            entrada={
                "repo": repo,
                "pedido": pedido,
                "patch": resultado_codigo.get("patch") or "",
                "valido": resultado_codigo.get("valido"),
                "erro_apply": resultado_codigo.get("erro_apply"),
                "restricoes": restricoes,
                "conteudos": conteudos,
                "patches_anteriores": anteriores or "",
            },
            resultado_esperado="Veredito aprovar ou pedir_correcoes.",
        )


def _ondas(specs: list[dict]) -> list[list[int]]:
    ondas: list[list[int]] = []
    for indice, spec in enumerate(specs):
        encaixou = False
        for onda in ondas:
            if all(not _sobrepoem(spec.get("arquivos") or [], specs[outro].get("arquivos") or []) for outro in onda):
                onda.append(indice)
                encaixou = True
                break
        if not encaixou:
            ondas.append([indice])
    return ondas


def _sobrepoem(esquerda: list[str], direita: list[str]) -> bool:
    a = set(esquerda)
    b = set(direita)
    if not a or not b:
        return True
    return bool(a & b)


def _caminhos_no_texto(texto: str) -> list[str]:
    vistos: list[str] = []
    for achado in _CAMINHO.findall(texto or ""):
        caminho = achado.lstrip("./")
        if caminho not in vistos:
            vistos.append(caminho)
    return vistos


def _titulo(texto: str) -> str:
    linha = " ".join((texto or "").split())
    return (linha[:77] + "...") if len(linha) > 80 else linha


def _spec(pedido: str, arquivos: list[str]) -> dict:
    return {"titulo": _titulo(pedido), "o_que_fazer": pedido, "arquivos": list(arquivos)}


def _normalizar_specs(bruto, pedido: str) -> list[dict]:
    if not isinstance(bruto, list):
        return []
    specs = []
    for item in bruto[:2]:
        if not isinstance(item, dict):
            continue
        arquivos = []
        for caminho in item.get("arquivos") or []:
            texto = str(caminho).strip().lstrip("./")
            if texto and texto not in arquivos:
                arquivos.append(texto)
        fazer = str(item.get("o_que_fazer") or "").strip() or pedido
        titulo = str(item.get("titulo") or "").strip() or _titulo(fazer)
        specs.append({"titulo": titulo[:80], "o_que_fazer": fazer, "arquivos": arquivos})
    return specs


def _juntar(patches: list[str]) -> str:
    partes = []
    for patch in patches:
        texto = (patch or "").strip()
        if not texto:
            continue
        partes.append(texto if texto.endswith("\n") else texto + "\n")
    return "".join(partes)


def _linha(nome, titulo, o_que_fazer, agente, revisor, auditor_fim, rodadas, paralelo, verificacao=None) -> dict:
    resultado = agente.resultado or {}
    return {
        "nome": nome,
        "titulo": titulo,
        "o_que_fazer": o_que_fazer,
        "arquivos": resultado.get("arquivos") or [],
        "patch": resultado.get("patch") or "",
        "valido": bool(resultado.get("valido")),
        "notas": resultado.get("notas") or "",
        "revisor": revisor.resultado or {},
        "rodadas": rodadas,
        "auditor_fim": auditor_fim.resultado or {},
        "paralelo": paralelo,
        "verificacao": verificacao,
    }


def _passo(tarefa: Tarefa) -> dict:
    return {
        "id": tarefa.id,
        "destinatario": tarefa.destinatario,
        "tipo": tarefa.tipo,
        "o_que_fazer": tarefa.o_que_fazer,
        "status": tarefa.status,
    }

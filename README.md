# Fábrica de agentes

Ferramenta de uso diário. Você aponta para um repositório git local, descreve a mudança em português e acompanha o turno numa tela: pedido, cadeia, patches, performance, voltas, aplicar e commit.

O repositório alvo só é lido. Nada é aplicado sozinho, e aplicar não cria commit. O commit é outro passo, também explícito, e não empurra para o remoto.

## Rodar a interface

Python 3.11 ou mais novo. Não há dependência de execução.

```bash
cd /caminho/desta/fabrica
python3 -m fabrica
```

A interface sobe em `http://127.0.0.1:8765`. Outro host ou outra porta:

```bash
python3 -m fabrica --host 127.0.0.1 --porta 8791
```

As mesmas escolhas valem por ambiente: `FABRICA_HOST` e `FABRICA_PORT`. Um processo só serve a página e a API.

Em `127.0.0.1` a API só responde à própria página: ela confere `Host` e `Origin` e exige `application/json`, para um site aberto em outra aba não comandar a fábrica. Para escutar em outro endereço, veja [Fora do 127.0.0.1](#fora-do-127001).

Na tela, escreva o pedido e o caminho do repositório, por exemplo `~/projetos/app`, e aperte **Rodar turno**. O turno roda em segundo plano e a tela mostra cada passo conforme ele termina; se você recarregar a página, ela volta a acompanhar. O caso fica aberto. No dia seguinte a mesma tela mostra a última volta. **Novo caso** começa outro, sem apagar o anterior.

## Configurar o modelo

Os agentes usam um modelo de verdade. A inspeção do repositório (árvore, git, conteúdo dos arquivos) é local. Sem chave, a tela mostra o erro e não inventa patch.

```bash
export OPENAI_API_KEY="sk-..."
# opcional, se não for a API da OpenAI:
export OPENAI_BASE_URL="https://seu-servidor/v1"
```

O modelo fica fixo no código: `MODELO_PADRAO` em `fabrica/llm.py` (hoje `gpt-5.4`). `OPENAI_MODEL` no ambiente passa por cima e serve para um teste ou uma volta rápida. `FABRICA_LLM_API_KEY` e `FABRICA_LLM_MODELO` servem como alternativa aos nomes da OpenAI.

| Variável | Padrão | Para quê |
|---|---|---|
| `FABRICA_LLM_TIMEOUT` | 120 | Tempo de cada chamada, em segundos |
| `FABRICA_LLM_TENTATIVAS` | 3 | Tentativas em 429, 5xx e tempo esgotado, com espera entre elas (respeita `Retry-After`) |
| `FABRICA_TETO_TOKENS` | 2000000 | Teto de tokens por turno. Ao passar, o turno para. `0` desliga |

Se o modelo recusa `temperature`, o cliente repete a chamada sem ela e não manda mais. A tela mostra chamadas e tokens de cada volta.

A interface para trocar o fornecedor depois é uma só: `completar(sistema, usuario, temperatura) -> str`. O cliente incluso fala `POST /v1/chat/completions`.

## O que a tela faz

A ponte é a interface. Ela fala direto com o orquestrador. Quem fala com você é só ele. Ele não escolhe especialista e não aplica mudança por conta própria.

A triagem planeja. Se o pedido não dá para executar, devolve uma pergunta e para. Se dá, separa em uma ou duas tarefas de código. Os dois são agentes de código: não há um de aplicação e outro de banco. Se as tarefas não tocam os mesmos arquivos, os dois rodam no mesmo turno. Se tocam o mesmo arquivo, uma espera. Cada patch passa sozinho pelo revisor e pelo auditor do fim.

A cadeia de cada tarefa é fixa:

1. Auditor, no início, uma vez no turno. Só lê o repositório.
2. Código. Devolve um patch e não aplica. Se a verificação ou o revisor pedem correção, este código roda de novo.
3. Verificação. Código, sem modelo: confere o patch numa cópia. Se falha, o erro real volta ao código e o revisor não é chamado.
4. Revisor. Aprova ou pede correção.
5. Auditor, no fim. Diz se algo que já funcionava foi comprometido. Este passo não é pulado.

Com duas tarefas, cada patch é verificado sozinho e o conjunto é verificado de novo no fim. Quando as duas tocam o mesmo arquivo, a segunda recebe o arquivo já com o patch da primeira.

## Verificação

A verificação roda numa cópia descartável do estado atual (arquivos rastreados e não rastreados fora do `.gitignore`). O repositório alvo não é tocado. São quatro conferências, na ordem:

1. o patch do turno aplica inteiro;
2. os caminhos que o git diz que o patch toca são permitidos;
3. os arquivos tocados (`.py`, `.json`, `.toml`) continuam com sintaxe válida;
4. o comando do repositório passa, se houver um.

O comando é opcional e fica num `.fabrica.toml` na raiz do repositório alvo:

```toml
[verificar]
comando = "python -m pytest -q tests/unidade"
timeout = 300
```

Ele roda dentro da cópia, com `FABRICA_REPO_ORIGINAL` apontando para o repositório de verdade (útil para achar o venv). `FABRICA_VERIFICAR_COMANDO` e `FABRICA_VERIFICAR_TIMEOUT` no ambiente passam por cima do arquivo. Se o comando já falhava antes do patch, a falha não conta contra o patch e aparece como "não conta". O patch não pode alterar o `.fabrica.toml`.

O comando executa código do repositório, inclusive o que o patch acabou de escrever. Só configure em máquina em que isso é aceitável.

Performance corre em paralelo e não escreve no patch. Se você escolher uma proposta, ela vira a próxima volta, na mesma cadeia, uma por vez.

Os casos ficam em `~/.fabrica-agentes/casos/`. Outro diretório:

```bash
export FABRICA_HOME="$HOME/trabalho/fabrica"
```

## Aplicar e commit

O padrão é não aplicar.

**Aplicar** escreve o patch no worktree e não cria commit. Só fica disponível se, ao mesmo tempo:

- a verificação passou;
- o revisor aprovou cada patch do turno;
- o auditor final não apontou compromisso em nenhum deles;
- o patch não mexe em caminho protegido nem sai do repositório;
- o repositório está como estava na revisão: mesmo commit e os arquivos do patch sem mudança.

**Commit** é o passo seguinte, também na tela. Ele grava um commit local só com os arquivos do patch e não envia nada ao remoto. O que você já tinha preparado no index continua lá, fora do commit. Sem aplicar antes, o commit é recusado. O repositório precisa ter `user.name` e `user.email`; a fábrica não configura isso.

Lockfiles (`poetry.lock`, `package-lock.json`, `Cargo.lock` e equivalentes) entram na lista do que não tocar quando o pedido não os menciona.

Arquivos que podem guardar segredo (`.env` e variações, `*.pem`, `*.key`, `credentials.*`, `secrets.*`, `id_rsa`, `.npmrc` e parecidos) nunca têm o conteúdo enviado ao modelo e nunca entram num patch. `.env.example` é tratado como arquivo comum.

Um caso roda um passo por vez. Um segundo turno, aplicar ou commit no mesmo caso enquanto outro está rodando é recusado com aviso.

Se o modelo falha no meio do turno, o patch que já estava pronto fica guardado no caso, sem liberação para aplicar.

## Fora do 127.0.0.1

A fábrica só escuta em outro endereço com estas duas variáveis definidas. Sem elas, não sobe.

| Variável | Para quê |
|---|---|
| `FABRICA_TOKEN` | Token que a API exige em `Authorization: Bearer`. A tela pede o token e guarda no navegador |
| `FABRICA_REPOS` | Pasta (ou pastas, separadas por `:` no Linux e `;` no Windows) em que os repositórios alvo podem estar. Caminho fora dela é recusado |
| `FABRICA_HOSTS` | Opcional. Nomes de host aceitos no cabeçalho `Host`, separados por vírgula |

```bash
export FABRICA_TOKEN="um-token-longo"
export FABRICA_REPOS="/dados/repos"
python3 -m fabrica --host 0.0.0.0 --porta 8765
```

Ponha HTTPS na frente (proxy). O token trafega no cabeçalho.

## Linha de comando

O CLI continua disponível, com as mesmas travas. Não é o caminho do dia.

```bash
python3 -m fabrica turno --repo ~/projetos/app --pedido "Adiciona validação de tipo na função soma em app.py"
python3 -m fabrica casos
python3 -m fabrica continuar --caso CASO_ID --pedido "Na função soma de app.py, rejeite texto"
python3 -m fabrica patch --caso CASO_ID
python3 -m fabrica propostas --caso CASO_ID
python3 -m fabrica aceitar --caso CASO_ID --proposta PROPOSTA_ID
python3 -m fabrica aplicar --caso CASO_ID
python3 -m fabrica commit --caso CASO_ID
```

`--pedido -` lê o texto do stdin. Para ter o comando `fabrica` no shell: `python3 -m pip install -e .`

## Testes

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest
```

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

Na tela, escreva o pedido e o caminho do repositório, por exemplo `~/projetos/app`, e aperte **Rodar turno**. O caso fica aberto. No dia seguinte a mesma tela mostra a última volta. **Novo caso** começa outro, sem apagar o anterior.

## Configurar o modelo

Os agentes usam um modelo de verdade. A inspeção do repositório (árvore, git, conteúdo dos arquivos) é local. Sem chave, a tela mostra o erro e não inventa patch.

```bash
export OPENAI_API_KEY="sk-..."
# opcional, se não for a API da OpenAI:
export OPENAI_BASE_URL="https://seu-servidor/v1"
export OPENAI_MODEL="gpt-4o-mini"
```

`OPENAI_MODEL` padrão é `gpt-4o-mini`. Para mudança maior, use um modelo mais forte. `FABRICA_LLM_API_KEY` e `FABRICA_LLM_MODELO` servem como alternativa aos nomes da OpenAI. `FABRICA_LLM_TIMEOUT` é o tempo de cada chamada, em segundos (padrão 120).

A interface para trocar o fornecedor depois é uma só: `completar(sistema, usuario, temperatura) -> str`. O cliente incluso fala `POST /v1/chat/completions`.

## O que a tela faz

A ponte é a interface. Ela fala direto com o orquestrador. Quem fala com você é só ele. Ele não escolhe especialista e não aplica mudança por conta própria.

A triagem planeja. Se o pedido não dá para executar, devolve uma pergunta e para. Se dá, separa em uma ou duas tarefas de código. Os dois são agentes de código: não há um de aplicação e outro de banco. Se as tarefas não tocam os mesmos arquivos, os dois rodam no mesmo turno. Se tocam o mesmo arquivo, uma espera. Cada patch passa sozinho pelo revisor e pelo auditor do fim.

A cadeia de cada tarefa é fixa:

1. Auditor, no início, uma vez no turno. Só lê o repositório.
2. Código. Devolve um patch e não aplica. Se o revisor pede correção, este código roda de novo.
3. Revisor. Aprova ou pede correção.
4. Auditor, no fim. Diz se algo que já funcionava foi comprometido. Este passo não é pulado.

Performance corre em paralelo e não escreve no patch. Se você escolher uma proposta, ela vira a próxima volta, na mesma cadeia, uma por vez.

Os casos ficam em `~/.fabrica-agentes/casos/`. Outro diretório:

```bash
export FABRICA_HOME="$HOME/trabalho/fabrica"
```

## Aplicar e commit

O padrão é não aplicar.

**Aplicar** escreve o patch no worktree e não cria commit. Só fica disponível se, ao mesmo tempo:

- o revisor aprovou cada patch do turno;
- o auditor final não apontou compromisso em nenhum deles;
- o patch não mexe em caminho protegido nem sai do repositório.

**Commit** é o passo seguinte, também na tela. Ele grava um commit local só com os arquivos do patch e não envia nada ao remoto. Sem aplicar antes, o commit é recusado. O repositório precisa ter `user.name` e `user.email`; a fábrica não configura isso.

Lockfiles (`poetry.lock`, `package-lock.json`, `Cargo.lock` e equivalentes) entram na lista do que não tocar quando o pedido não os menciona.

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

"""Erros que a ponte pode mostrar, sem rastreio de implementação."""


class ErroFabrica(Exception):
    """Falha do fluxo que o usuário consegue corrigir ou repetir."""


class ErroLLM(ErroFabrica):
    """Chave ausente, servidor fora, ou resposta que não dá para usar."""


class ErroRepositorio(ErroFabrica):
    """O repositório alvo não pode ser lido, ou o patch não é seguro."""

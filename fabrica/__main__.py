import sys

from fabrica.cli import main as cli_main

_COMANDOS = {"turno", "continuar", "casos", "patch", "propostas", "aceitar", "aplicar", "commit"}


def main(argv: list[str] | None = None) -> int:
    argumentos = list(sys.argv[1:] if argv is None else argv)
    if not argumentos or argumentos[0] in {"ui", "servir"} or argumentos[0] in {"--host", "--porta"}:
        from fabrica.servidor import main as servir

        if argumentos and argumentos[0] in {"ui", "servir"}:
            argumentos = argumentos[1:]
        return servir(argumentos)
    return cli_main(argumentos)


if __name__ == "__main__":
    raise SystemExit(main())

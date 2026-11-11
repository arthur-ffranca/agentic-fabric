import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def isola_ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRICA_HOME", str(tmp_path / "memoria"))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FABRICA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("FABRICA_LLM_MODELO", raising=False)

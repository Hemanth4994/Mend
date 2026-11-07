"""The agent must not be able to see the answer key. Only tests and scripts may import mend.gold."""
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "mend"


def test_nothing_in_the_package_imports_the_gold_repairs():
    offenders = [p.name for p in SRC.glob("*.py") if p.name != "gold.py" and ("gold import" in p.read_text() or "import gold" in p.read_text()
                                                                          or "from .gold" in p.read_text() or "mend.gold" in p.read_text())]
    assert offenders == []

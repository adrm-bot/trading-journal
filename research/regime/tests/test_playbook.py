"""Playbook contract + import isolation (invariant 4)."""
from pathlib import Path

import classifier
import playbook


def test_playbook_covers_all_states_with_avoid():
    playbook.validate()
    for state in classifier.REGIMES:
        entry = playbook.REGIME_PLAYBOOK[state]
        assert entry["primary"]
        assert entry["avoid"], "the avoid field is load-bearing"


def test_classifier_never_imports_playbook():
    src = Path(classifier.__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "playbook" not in stripped, f"classifier imports playbook: {stripped!r}"

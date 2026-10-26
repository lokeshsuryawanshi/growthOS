"""The offline eval suite is part of CI: the gate must pass in mock mode."""
import pytest

from evals.run_evals import eval_guardrails, gate
import json
from pathlib import Path


@pytest.mark.eval
async def test_guardrail_eval_meets_baseline():
    g = await eval_guardrails()
    baseline = json.loads((Path(__file__).parents[1] / "evals/baseline.json").read_text())
    failures = gate({"guardrails": g, "pipeline": {}}, baseline)
    assert not failures, failures
    assert g["redteam_block_recall"] == 1.0 and g["benign_false_positive_rate"] == 0.0

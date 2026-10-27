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


async def test_rbac_eval_matrix_passes_with_no_violations(db):
    from app.llm.gateway import LLMGateway, set_gateway
    from evals.rbac import eval_rbac
    set_gateway(LLMGateway())
    r = await eval_rbac()
    assert r["rbac_checks"] >= 300
    assert r["rbac_matrix_pass_rate"] == 1.0, r["rbac_failures"]
    assert r["cross_tenant_access_violations"] == 0 and r["privilege_escalation_successes"] == 0

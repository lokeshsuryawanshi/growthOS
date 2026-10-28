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


async def test_isolation_eval_finds_no_violations_and_catches_every_mutation():
    from evals.isolation import eval_isolation
    r = await eval_isolation(mutations=True)
    assert r["isolation_routes"] >= 15 and r["isolation_checks"] >= 50
    assert r["cross_account_access_violations"] == r["session_escalations"] == r["cross_account_page_leaks"] == 0, \
        r["isolation_failures"]
    assert r["secret_exposure_findings"] == 0, r["secret_findings"]
    m = r["mutations"]
    assert m["mutations_caught"] == m["mutations_total"] >= 5, m["detail"]


def test_lead_screening_catches_every_labelled_adversarial_prospect():
    from evals.quality import lead_screening
    r = lead_screening("ledgerloop")
    assert r["lead_screen_recall_suppressed"] == r["lead_screen_recall_competitor"] == \
        r["lead_screen_recall_missing_contact"] == 1.0
    assert r["lead_screen_duplicates_kept"] == 0 and r["lead_screen_fit_false_rejections"] == 0


async def test_quality_report_on_delivered_drafts(db):
    from app.agents.orchestrator.engine import Orchestrator, set_orchestrator
    from app.fixtures.seed import seed
    from app.llm.gateway import LLMGateway, set_gateway
    from evals.quality import content_quality
    set_gateway(LLMGateway())
    orch = Orchestrator()
    set_orchestrator(orch)
    tid = (await seed(db))["ledgerloop"]
    await orch.execute(tid, await orch.create_run(tid, "discovery", {"start_date": "2026-11-02", "auto_approve_gates": True}))
    await orch.execute(tid, await orch.create_run(tid, "content", {}))
    q = await content_quality([tid])
    assert q["quality_delivered_posts"] >= 5
    assert q["quality_safety_violations_delivered"] == q["quality_ungrounded_figure_posts"] == 0
    assert q["quality_copied_sample_posts"] == q["quality_near_duplicate_pairs"] == 0

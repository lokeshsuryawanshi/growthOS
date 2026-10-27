"""Mutation test for the RBAC eval: break access control on purpose and check that the eval notices.

  python -m evals.rbac_mutations

A gate that has only ever passed proves little. Each mutation below introduces a real access-control bug; the eval
must report it (a matrix failure, a cross-tenant violation or a successful escalation). Exit code 1 if any mutation
goes unnoticed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile

os.environ["APP_ENV"] = "test"
os.environ["LLM_MODE"] = "mock"


def mutations():
    from app.core import security

    def viewers_may_approve():
        security.POLICY["content:approve"].add("client_viewer")

    def founders_grant_agency_roles():          # two independent checks guard this; both have to go
        security.GRANTABLE["client_owner"].add("account_manager")
        security.CLIENT_ROLES.add("account_manager")

    def tenant_check_skipped():
        security.Principal.can_access_tenant = lambda self, tenant_id: True

    def viewers_may_edit_the_profile():
        security.POLICY["onboarding:edit"].add("client_viewer")

    return [("viewers may approve posts", viewers_may_approve),
            ("founders may grant the account_manager role", founders_grant_agency_roles),
            ("workspace membership is not checked", tenant_check_skipped),
            ("viewers may edit the company profile", viewers_may_edit_the_profile)]


async def main() -> int:
    logging.disable(logging.WARNING)
    from app.core import security
    from app.db.session import Database, set_db
    from app.llm.gateway import LLMGateway, set_gateway
    from evals.rbac import eval_rbac

    set_db(Database(f"sqlite+aiosqlite:///{tempfile.mkdtemp(prefix='growthos-mut-')}/m.db"))
    set_gateway(LLMGateway())
    clean = await eval_rbac()
    print(f"unmutated: pass rate {clean['rbac_matrix_pass_rate']}, cross-tenant {clean['cross_tenant_access_violations']}, "
          f"escalations {clean['privilege_escalation_successes']}")
    missed = 0
    for name, mutate in mutations():
        saved = ({k: set(v) for k, v in security.POLICY.items()}, {k: set(v) for k, v in security.GRANTABLE.items()},
                 set(security.CLIENT_ROLES), security.Principal.can_access_tenant)
        mutate()
        try:
            r = await eval_rbac()
        finally:
            security.POLICY.clear(), security.POLICY.update(saved[0])
            security.GRANTABLE.clear(), security.GRANTABLE.update(saved[1])
            security.CLIENT_ROLES.clear(), security.CLIENT_ROLES.update(saved[2])
            security.Principal.can_access_tenant = saved[3]
        caught = len(r["rbac_failures"]) + r["cross_tenant_access_violations"] + r["privilege_escalation_successes"]
        missed += not caught
        print(f"{'CAUGHT' if caught else 'MISSED'}  {name}: {len(r['rbac_failures'])} matrix failures, "
              f"{r['cross_tenant_access_violations']} cross-tenant violations, "
              f"{r['privilege_escalation_successes']} escalations")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

# Eval report (mock)

Gate: **PASS**


## RBAC and tenant isolation

| Metric | Value |
|---|---|
| Matrix checks (role x action x endpoint, API and UI) | 315 |
| Matrix pass rate | 1.0 |
| Cross-tenant access violations | 0 |
| Privilege escalation attempts / successes | 14 / 0 |

Escalation attempts (all must fail):

- founder creates a platform_admin: HTTP 403
- founder promotes the viewer to platform_admin: HTTP 403
- founder creates a account_manager: HTTP 403
- founder promotes the viewer to account_manager: HTTP 403
- viewer promotes themself to owner: HTTP 403
- account manager creates an account manager: HTTP 403
- account manager assigns themself a client: HTTP 403
- founder adds a member to another workspace: HTTP 404
- last owner demotes themself: HTTP 409
- signup asks for the platform_admin role: HTTP 201
- token forged with a guessed secret: HTTP 401
- validly signed token for a user that does not exist: HTTP 401
- deactivated user's old token: HTTP 401
- removed member's old token: HTTP 401


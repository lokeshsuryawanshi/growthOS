-- Postgres Row-Level Security: the production second wall behind the ORM interceptor.
-- Generated from app.db.models.TENANT_TABLES. Apply after creating the schema.
--
-- The app sets the tenant per transaction:   SET LOCAL app.tenant_id = '<tenant id>';
-- Platform jobs that are allowed to cross tenants run as the growthos_platform role
-- (BYPASSRLS), never as the app role.

CREATE ROLE growthos_app NOLOGIN;
CREATE ROLE growthos_platform NOLOGIN BYPASSRLS;


ALTER TABLE client_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_profiles FORCE ROW LEVEL SECURITY;
CREATE POLICY client_profiles_tenant_isolation ON client_profiles
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE knowledge_docs ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_docs FORCE ROW LEVEL SECURITY;
CREATE POLICY knowledge_docs_tenant_isolation ON knowledge_docs
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifacts FORCE ROW LEVEL SECURITY;
CREATE POLICY artifacts_tenant_isolation ON artifacts
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs FORCE ROW LEVEL SECURITY;
CREATE POLICY runs_tenant_isolation ON runs
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE run_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_nodes FORCE ROW LEVEL SECURITY;
CREATE POLICY run_nodes_tenant_isolation ON run_nodes
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE llm_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_calls FORCE ROW LEVEL SECURITY;
CREATE POLICY llm_calls_tenant_isolation ON llm_calls
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE llm_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_cache FORCE ROW LEVEL SECURITY;
CREATE POLICY llm_cache_tenant_isolation ON llm_cache
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE trace_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE trace_events FORCE ROW LEVEL SECURITY;
CREATE POLICY trace_events_tenant_isolation ON trace_events
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE calendar_slots ENABLE ROW LEVEL SECURITY;
ALTER TABLE calendar_slots FORCE ROW LEVEL SECURITY;
CREATE POLICY calendar_slots_tenant_isolation ON calendar_slots
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE posts ENABLE ROW LEVEL SECURITY;
ALTER TABLE posts FORCE ROW LEVEL SECURITY;
CREATE POLICY posts_tenant_isolation ON posts
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE leads FORCE ROW LEVEL SECURITY;
CREATE POLICY leads_tenant_isolation ON leads
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE lead_rejections ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead_rejections FORCE ROW LEVEL SECURITY;
CREATE POLICY lead_rejections_tenant_isolation ON lead_rejections
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE outreach_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE outreach_messages FORCE ROW LEVEL SECURITY;
CREATE POLICY outreach_messages_tenant_isolation ON outreach_messages
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE suppression_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE suppression_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY suppression_entries_tenant_isolation ON suppression_entries
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback FORCE ROW LEVEL SECURITY;
CREATE POLICY feedback_tenant_isolation ON feedback
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE guardrail_incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardrail_incidents FORCE ROW LEVEL SECURITY;
CREATE POLICY guardrail_incidents_tenant_isolation ON guardrail_incidents
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_log_tenant_isolation ON audit_log
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

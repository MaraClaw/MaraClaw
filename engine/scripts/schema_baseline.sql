-- Baseline schema for fresh deploys (generated from residual ORM metadata).
-- Applied by app.scripts.bootstrap_db / entrypoint bootstrap role.
-- Do not rely on Alembic; this file is the schema source of truth for greenfield installs.
BEGIN;


DO $$ BEGIN CREATE TYPE im_provider_enum AS ENUM ('feishu', 'dingtalk', 'wecom', 'microsoft_teams', 'google_chat', 'web_only'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE user_role_enum AS ENUM ('platform_admin', 'org_admin', 'agent_admin', 'member'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE agent_status_enum AS ENUM ('creating', 'running', 'idle', 'stopped', 'error'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE activity_action_enum AS ENUM ('chat_reply', 'tool_call', 'feishu_msg_sent', 'agent_msg_sent', 'web_msg_sent', 'task_created', 'task_updated', 'file_written', 'error', 'schedule_run', 'heartbeat', 'plaza_post'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE permission_scope_enum AS ENUM ('company', 'department', 'user'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE approval_status_enum AS ENUM ('pending', 'approved', 'rejected'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE channel_type_enum AS ENUM ('feishu', 'wecom', 'wechat', 'whatsapp', 'dingtalk', 'slack', 'discord', 'atlassian', 'microsoft_teams', 'agentbay', 'google_chat'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE chat_role_enum AS ENUM ('user', 'assistant', 'system', 'tool_call'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE task_type_enum AS ENUM ('todo', 'supervision'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE task_status_enum AS ENUM ('pending', 'doing', 'done'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE task_priority_enum AS ENUM ('low', 'medium', 'high', 'urgent'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;


CREATE TABLE IF NOT EXISTS identities (
	id UUID NOT NULL, 
	email VARCHAR(255), 
	phone VARCHAR(50), 
	username VARCHAR(100), 
	password_hash VARCHAR(255), 
	is_active BOOLEAN NOT NULL DEFAULT true, 
	is_platform_admin BOOLEAN NOT NULL DEFAULT false, 
	email_verified BOOLEAN NOT NULL DEFAULT false, 
	must_change_password BOOLEAN NOT NULL DEFAULT false,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_identities_email ON identities (email);

CREATE UNIQUE INDEX IF NOT EXISTS ix_identities_username ON identities (username);

CREATE UNIQUE INDEX IF NOT EXISTS ix_identities_phone ON identities (phone);

CREATE TABLE IF NOT EXISTS identity_providers (
	id UUID NOT NULL, 
	provider_type VARCHAR(50) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	is_active BOOLEAN NOT NULL DEFAULT true, 
	sso_login_enabled BOOLEAN NOT NULL DEFAULT false, 
	config JSON NOT NULL DEFAULT '{}', 
	tenant_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS llm_models (
	id UUID NOT NULL, 
	tenant_id UUID, 
	provider VARCHAR(50) NOT NULL, 
	model VARCHAR(100) NOT NULL, 
	api_key_encrypted VARCHAR(1024) NOT NULL, 
	base_url VARCHAR(500), 
	label VARCHAR(200) NOT NULL, 
	max_tokens_per_day INTEGER, 
	enabled BOOLEAN NOT NULL DEFAULT true, 
	supports_vision BOOLEAN NOT NULL DEFAULT false, 
	temperature FLOAT, 
	request_timeout INTEGER, 
	max_output_tokens INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_llm_models_tenant_id ON llm_models (tenant_id);

CREATE TABLE IF NOT EXISTS okr_alignments (
	id UUID NOT NULL, 
	source_type VARCHAR(20) NOT NULL, 
	source_id UUID NOT NULL, 
	target_type VARCHAR(20) NOT NULL, 
	target_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_okr_alignment UNIQUE (source_type, source_id, target_type, target_id)
);

CREATE TABLE IF NOT EXISTS participants (
	id UUID NOT NULL, 
	type VARCHAR(10) NOT NULL, 
	ref_id UUID NOT NULL, 
	display_name VARCHAR(100) NOT NULL, 
	avatar_url VARCHAR(500), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_participants_type_ref UNIQUE (type, ref_id)
);

CREATE INDEX IF NOT EXISTS ix_participants_ref_id ON participants (ref_id);

CREATE TABLE IF NOT EXISTS plaza_posts (
	id UUID NOT NULL, 
	author_id UUID NOT NULL, 
	author_type VARCHAR(10) NOT NULL, 
	author_name VARCHAR(100) NOT NULL, 
	content TEXT NOT NULL, 
	tenant_id UUID, 
	likes_count INTEGER NOT NULL, 
	comments_count INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_plaza_posts_created_at ON plaza_posts (created_at);

CREATE INDEX IF NOT EXISTS ix_plaza_posts_author_id ON plaza_posts (author_id);

CREATE TABLE IF NOT EXISTS sso_scan_sessions (
	id UUID NOT NULL, 
	status VARCHAR(50) NOT NULL, 
	provider_type VARCHAR(50), 
	error_msg TEXT, 
	tenant_id UUID, 
	user_id UUID, 
	access_token TEXT, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS system_settings (
	key VARCHAR(100) NOT NULL, 
	value JSONB NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (key)
);

CREATE TABLE IF NOT EXISTS tenants (
	id UUID NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	slug VARCHAR(50) NOT NULL, 
	im_provider im_provider_enum NOT NULL DEFAULT 'web_only', 
	im_config JSON, 
	is_active BOOLEAN NOT NULL DEFAULT true, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	default_message_limit INTEGER NOT NULL DEFAULT 50, 
	default_message_period VARCHAR(20) NOT NULL DEFAULT 'permanent', 
	default_max_agents INTEGER NOT NULL DEFAULT 2, 
	default_agent_ttl_hours INTEGER NOT NULL DEFAULT 0, 
	default_max_llm_calls_per_day INTEGER NOT NULL DEFAULT 1000, 
	min_heartbeat_interval_minutes INTEGER NOT NULL DEFAULT 240, 
	timezone VARCHAR(50) NOT NULL DEFAULT 'UTC', 
	country_region VARCHAR(10) NOT NULL DEFAULT '001', 
	sso_enabled BOOLEAN NOT NULL DEFAULT false, 
	sso_domain VARCHAR(255), 
	default_max_triggers INTEGER NOT NULL DEFAULT 20, 
	min_poll_interval_floor INTEGER NOT NULL DEFAULT 5, 
	max_webhook_rate_ceiling INTEGER NOT NULL DEFAULT 5, 
	a2a_async_enabled BOOLEAN NOT NULL DEFAULT true, 
	default_model_id UUID, 
	PRIMARY KEY (id), 
	FOREIGN KEY(default_model_id) REFERENCES llm_models (id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_tenants_slug ON tenants (slug);

CREATE UNIQUE INDEX IF NOT EXISTS ix_tenants_sso_domain ON tenants (sso_domain);

DO $$ BEGIN
	ALTER TABLE llm_models ADD CONSTRAINT llm_models_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES tenants (id);
EXCEPTION
	WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS tools (
	id UUID NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	display_name VARCHAR(200) NOT NULL, 
	description TEXT NOT NULL, 
	type VARCHAR(20) NOT NULL, 
	category VARCHAR(50) NOT NULL, 
	icon VARCHAR(10) NOT NULL, 
	parameters_schema JSON NOT NULL, 
	config JSON NOT NULL DEFAULT '{}', 
	config_schema JSON NOT NULL, 
	mcp_server_url VARCHAR(500), 
	mcp_server_name VARCHAR(200), 
	mcp_tool_name VARCHAR(200), 
	enabled BOOLEAN NOT NULL DEFAULT true, 
	is_default BOOLEAN NOT NULL, 
	source VARCHAR(20) NOT NULL, 
	tenant_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE INDEX IF NOT EXISTS ix_tools_tenant_source ON tools (tenant_id, source, enabled);

CREATE TABLE IF NOT EXISTS company_reports (
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	report_type VARCHAR(10) NOT NULL, 
	period_start DATE NOT NULL, 
	period_end DATE NOT NULL, 
	period_label VARCHAR(100) NOT NULL, 
	content TEXT NOT NULL, 
	submitted_count INTEGER NOT NULL, 
	missing_count INTEGER NOT NULL, 
	needs_refresh BOOLEAN NOT NULL, 
	generated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_company_report_period UNIQUE (tenant_id, report_type, period_start, period_end), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_company_reports_tenant_id ON company_reports (tenant_id);

CREATE INDEX IF NOT EXISTS ix_company_reports_period_start ON company_reports (period_start);

CREATE TABLE IF NOT EXISTS member_daily_reports (
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	member_type VARCHAR(20) NOT NULL, 
	member_id UUID NOT NULL, 
	report_date DATE NOT NULL, 
	content TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	source VARCHAR(30) NOT NULL, 
	submitted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_member_daily_report UNIQUE (tenant_id, member_type, member_id, report_date), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_member_daily_reports_report_date ON member_daily_reports (report_date);

CREATE INDEX IF NOT EXISTS ix_member_daily_reports_member_id ON member_daily_reports (member_id);

CREATE INDEX IF NOT EXISTS ix_member_daily_reports_tenant_id ON member_daily_reports (tenant_id);

CREATE TABLE IF NOT EXISTS okr_objectives (
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	title VARCHAR(500) NOT NULL, 
	description TEXT, 
	owner_type VARCHAR(20) NOT NULL, 
	owner_id UUID, 
	period_start DATE NOT NULL, 
	period_end DATE NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_okr_objectives_tenant_id ON okr_objectives (tenant_id);

CREATE TABLE IF NOT EXISTS okr_settings (
	tenant_id UUID NOT NULL, 
	enabled BOOLEAN NOT NULL DEFAULT false, 
	first_enabled_at TIMESTAMP WITH TIME ZONE, 
	daily_report_enabled BOOLEAN NOT NULL DEFAULT false, 
	daily_report_time VARCHAR(5) NOT NULL DEFAULT '18:00', 
	daily_report_skip_non_workdays BOOLEAN NOT NULL DEFAULT true, 
	weekly_report_enabled BOOLEAN NOT NULL DEFAULT false, 
	weekly_report_day INTEGER NOT NULL DEFAULT 4, 
	period_frequency VARCHAR(20) NOT NULL DEFAULT 'quarterly', 
	period_length_days INTEGER, 
	okr_agent_id UUID, 
	PRIMARY KEY (tenant_id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS org_departments (
	id UUID NOT NULL, 
	external_id VARCHAR(100), 
	provider_id UUID, 
	name VARCHAR(200) NOT NULL, 
	parent_id UUID, 
	path VARCHAR(500) NOT NULL, 
	member_count INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	tenant_id UUID, 
	synced_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(parent_id) REFERENCES org_departments (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX IF NOT EXISTS ix_org_departments_external_id ON org_departments (external_id);

CREATE INDEX IF NOT EXISTS ix_org_departments_tenant_id ON org_departments (tenant_id);

CREATE TABLE IF NOT EXISTS plaza_comments (
	id UUID NOT NULL, 
	post_id UUID NOT NULL, 
	author_id UUID NOT NULL, 
	author_type VARCHAR(10) NOT NULL, 
	author_name VARCHAR(100) NOT NULL, 
	content TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(post_id) REFERENCES plaza_posts (id)
);

CREATE INDEX IF NOT EXISTS ix_plaza_comments_post_id ON plaza_comments (post_id);

CREATE TABLE IF NOT EXISTS plaza_likes (
	id UUID NOT NULL, 
	post_id UUID NOT NULL, 
	author_id UUID NOT NULL, 
	author_type VARCHAR(10) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(post_id) REFERENCES plaza_posts (id)
);

CREATE INDEX IF NOT EXISTS ix_plaza_likes_post_id ON plaza_likes (post_id);

CREATE TABLE IF NOT EXISTS skills (
	id UUID NOT NULL, 
	tenant_id UUID, 
	name VARCHAR(100) NOT NULL, 
	description TEXT NOT NULL, 
	category VARCHAR(50) NOT NULL, 
	icon VARCHAR(10) NOT NULL, 
	folder_name VARCHAR(100) NOT NULL, 
	is_builtin BOOLEAN NOT NULL, 
	is_default BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	UNIQUE (name), 
	UNIQUE (folder_name)
);

CREATE INDEX IF NOT EXISTS ix_skills_tenant_id ON skills (tenant_id);

CREATE TABLE IF NOT EXISTS tenant_settings (
	tenant_id UUID NOT NULL, 
	key VARCHAR(100) NOT NULL, 
	value JSONB NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (tenant_id, key), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS users (
	id UUID NOT NULL, 
	identity_id UUID, 
	tenant_id UUID, 
	display_name VARCHAR(100) NOT NULL, 
	avatar_url VARCHAR(500), 
	title VARCHAR(100), 
	role user_role_enum NOT NULL DEFAULT 'member', 
	is_active BOOLEAN NOT NULL DEFAULT true, 
	registration_source VARCHAR(50), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	quota_message_limit INTEGER NOT NULL DEFAULT 50, 
	quota_message_period VARCHAR(20) NOT NULL DEFAULT 'permanent', 
	quota_messages_used INTEGER NOT NULL DEFAULT 0, 
	quota_period_start TIMESTAMP WITH TIME ZONE, 
	quota_max_agents INTEGER NOT NULL DEFAULT 2, 
	quota_agent_ttl_hours INTEGER NOT NULL DEFAULT 0, 
	is_genesis BOOLEAN NOT NULL DEFAULT false, 
	PRIMARY KEY (id), 
	FOREIGN KEY(identity_id) REFERENCES identities (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX IF NOT EXISTS ix_users_identity_id ON users (identity_id);

CREATE INDEX IF NOT EXISTS ix_users_tenant_id ON users (tenant_id);

-- CREATE TABLE IF NOT EXISTS does not add columns to an existing users table.
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_genesis BOOLEAN NOT NULL DEFAULT false;

CREATE UNIQUE INDEX IF NOT EXISTS ux_users_genesis_platform_admin
	ON users (role) WHERE is_genesis IS TRUE AND role = 'platform_admin';

CREATE UNIQUE INDEX IF NOT EXISTS ux_users_genesis_org_admin
	ON users (tenant_id) WHERE is_genesis IS TRUE AND role = 'org_admin';

CREATE TABLE IF NOT EXISTS work_reports (
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	author_type VARCHAR(20) NOT NULL, 
	author_id UUID NOT NULL, 
	report_type VARCHAR(10) NOT NULL, 
	period_date DATE NOT NULL, 
	content TEXT NOT NULL, 
	source VARCHAR(30) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_work_reports_tenant_id ON work_reports (tenant_id);

CREATE TABLE IF NOT EXISTS agent_templates (
	id UUID NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	description TEXT NOT NULL, 
	icon VARCHAR(50) NOT NULL, 
	category VARCHAR(50) NOT NULL, 
	soul_template TEXT NOT NULL, 
	default_skills JSON NOT NULL, 
	default_mcp_servers JSON NOT NULL, 
	default_autonomy_policy JSON NOT NULL DEFAULT '{}', 
	capability_bullets JSON NOT NULL, 
	bootstrap_content TEXT, 
	is_builtin BOOLEAN NOT NULL, 
	created_by UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS enterprise_info (
	id UUID NOT NULL, 
	info_type VARCHAR(50) NOT NULL, 
	content JSON NOT NULL, 
	version INTEGER NOT NULL, 
	visible_roles JSON NOT NULL, 
	updated_by UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (info_type), 
	FOREIGN KEY(updated_by) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS invitation_codes (
	id UUID NOT NULL, 
	code VARCHAR(32) NOT NULL, 
	tenant_id UUID, 
	max_uses INTEGER NOT NULL, 
	used_count INTEGER NOT NULL, 
	is_active BOOLEAN NOT NULL DEFAULT true, 
	created_by UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_invitation_codes_code ON invitation_codes (code);

CREATE INDEX IF NOT EXISTS ix_invitation_codes_tenant_id ON invitation_codes (tenant_id);

CREATE TABLE IF NOT EXISTS okr_key_results (
	id UUID NOT NULL, 
	objective_id UUID NOT NULL, 
	title VARCHAR(500) NOT NULL, 
	target_value FLOAT NOT NULL, 
	current_value FLOAT NOT NULL, 
	unit VARCHAR(50), 
	focus_ref VARCHAR(200), 
	status VARCHAR(20) NOT NULL, 
	last_updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(objective_id) REFERENCES okr_objectives (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_okr_key_results_objective_id ON okr_key_results (objective_id);

CREATE TABLE IF NOT EXISTS org_members (
	id UUID NOT NULL, 
	open_id VARCHAR(100), 
	unionid VARCHAR(100), 
	external_id VARCHAR(100), 
	provider_id UUID, 
	name VARCHAR(100) NOT NULL, 
	name_translit_full VARCHAR(255), 
	name_translit_initial VARCHAR(50), 
	email VARCHAR(200), 
	avatar_url VARCHAR(500), 
	title VARCHAR(200) NOT NULL, 
	department_id UUID, 
	department_path VARCHAR(500) NOT NULL DEFAULT '', 
	phone VARCHAR(50), 
	status VARCHAR(20) NOT NULL, 
	tenant_id UUID, 
	user_id UUID, 
	synced_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(department_id) REFERENCES org_departments (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX IF NOT EXISTS ix_org_members_external_id ON org_members (external_id);

CREATE INDEX IF NOT EXISTS ix_org_members_open_id ON org_members (open_id);

CREATE INDEX IF NOT EXISTS ix_org_members_name_translit_full ON org_members (name_translit_full);

CREATE INDEX IF NOT EXISTS ix_org_members_tenant_id ON org_members (tenant_id);

CREATE INDEX IF NOT EXISTS ix_org_members_unionid ON org_members (unionid);

CREATE INDEX IF NOT EXISTS ix_org_members_name_translit_initial ON org_members (name_translit_initial);

CREATE TABLE IF NOT EXISTS skill_files (
	id UUID NOT NULL, 
	skill_id UUID NOT NULL, 
	path VARCHAR(500) NOT NULL, 
	content TEXT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(skill_id) REFERENCES skills (id)
);

CREATE TABLE IF NOT EXISTS agents (
	id UUID NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	avatar_url VARCHAR(500), 
	role_description VARCHAR(500) NOT NULL, 
	bio TEXT, 
	welcome_message TEXT, 
	creator_id UUID NOT NULL, 
	tenant_id UUID, 
	agent_type VARCHAR(20) NOT NULL, 
	gogcli_enabled BOOLEAN DEFAULT 'false' NOT NULL, 
	api_key_hash VARCHAR(128), 
	openclaw_last_seen TIMESTAMP WITH TIME ZONE, 
	status agent_status_enum NOT NULL DEFAULT 'creating', 
	container_id VARCHAR(100), 
	container_port INTEGER, 
	primary_model_id UUID, 
	fallback_model_id UUID, 
	autonomy_policy JSON NOT NULL DEFAULT '{}', 
	max_tokens_per_day INTEGER, 
	max_tokens_per_month INTEGER, 
	tokens_used_today INTEGER NOT NULL DEFAULT 0, 
	tokens_used_month INTEGER NOT NULL DEFAULT 0, 
	last_daily_reset TIMESTAMP WITH TIME ZONE, 
	last_monthly_reset TIMESTAMP WITH TIME ZONE, 
	tokens_used_total INTEGER NOT NULL DEFAULT 0, 
	cache_read_tokens_today INTEGER NOT NULL DEFAULT 0, 
	cache_read_tokens_month INTEGER NOT NULL DEFAULT 0, 
	cache_read_tokens_total INTEGER NOT NULL DEFAULT 0, 
	cache_creation_tokens_today INTEGER NOT NULL DEFAULT 0, 
	cache_creation_tokens_month INTEGER NOT NULL DEFAULT 0, 
	cache_creation_tokens_total INTEGER NOT NULL DEFAULT 0, 
	context_window_size INTEGER NOT NULL DEFAULT 100, 
	max_tool_rounds INTEGER NOT NULL DEFAULT 50, 
	max_triggers INTEGER NOT NULL, 
	min_poll_interval_min INTEGER NOT NULL, 
	webhook_rate_limit INTEGER NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	is_expired BOOLEAN NOT NULL DEFAULT false, 
	is_system BOOLEAN NOT NULL DEFAULT false, 
	access_mode VARCHAR(20) NOT NULL, 
	company_access_level VARCHAR(20) NOT NULL, 
	llm_calls_today INTEGER NOT NULL DEFAULT 0, 
	max_llm_calls_per_day INTEGER NOT NULL, 
	llm_calls_reset_at TIMESTAMP WITH TIME ZONE, 
	template_id UUID, 
	heartbeat_enabled BOOLEAN NOT NULL DEFAULT true, 
	heartbeat_interval_minutes INTEGER NOT NULL, 
	heartbeat_active_hours VARCHAR(20) NOT NULL DEFAULT '09:00-18:00', 
	last_heartbeat_at TIMESTAMP WITH TIME ZONE, 
	timezone VARCHAR(50), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	last_active_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(creator_id) REFERENCES users (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(primary_model_id) REFERENCES llm_models (id), 
	FOREIGN KEY(fallback_model_id) REFERENCES llm_models (id), 
	FOREIGN KEY(template_id) REFERENCES agent_templates (id)
);

CREATE INDEX IF NOT EXISTS ix_agents_tenant_id ON agents (tenant_id);

CREATE INDEX IF NOT EXISTS ix_agents_creator_id ON agents (creator_id);

CREATE INDEX IF NOT EXISTS ix_agents_api_key_hash ON agents (api_key_hash) WHERE api_key_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_agents_heartbeat ON agents (heartbeat_enabled, status) WHERE heartbeat_enabled IS TRUE;

CREATE TABLE IF NOT EXISTS okr_progress_logs (
	id UUID NOT NULL, 
	kr_id UUID NOT NULL, 
	previous_value FLOAT NOT NULL, 
	new_value FLOAT NOT NULL, 
	source VARCHAR(30) NOT NULL, 
	note TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(kr_id) REFERENCES okr_key_results (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_okr_progress_logs_kr_id ON okr_progress_logs (kr_id);

CREATE TABLE IF NOT EXISTS agent_activity_logs (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	action_type activity_action_enum NOT NULL, 
	summary VARCHAR(500) NOT NULL, 
	detail_json JSON, 
	related_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id)
);

CREATE INDEX IF NOT EXISTS ix_agent_activity_logs_agent_id ON agent_activity_logs (agent_id);

CREATE INDEX IF NOT EXISTS ix_agent_activity_logs_created_at ON agent_activity_logs (created_at);

CREATE TABLE IF NOT EXISTS agent_agent_relationships (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	target_agent_id UUID NOT NULL, 
	relation VARCHAR(50) NOT NULL, 
	description TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE, 
	created_by_user_id UUID, 
	updated_by_user_id UUID, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE, 
	FOREIGN KEY(target_agent_id) REFERENCES agents (id) ON DELETE CASCADE, 
	FOREIGN KEY(created_by_user_id) REFERENCES users (id), 
	FOREIGN KEY(updated_by_user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS agent_credentials (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	credential_type VARCHAR(20) NOT NULL, 
	platform VARCHAR(100) NOT NULL, 
	display_name VARCHAR(200) NOT NULL, 
	cookies_json TEXT, 
	cookies_updated_at TIMESTAMP WITH TIME ZONE, 
	status VARCHAR(20) NOT NULL, 
	last_login_at TIMESTAMP WITH TIME ZONE, 
	last_injected_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_agent_credentials_agent_id ON agent_credentials (agent_id);

CREATE TABLE IF NOT EXISTS agent_focus_items (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	key VARCHAR(200) NOT NULL, 
	title VARCHAR(200), 
	description TEXT NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	kind VARCHAR(24) NOT NULL, 
	source VARCHAR(40) NOT NULL, 
	metadata JSONB NOT NULL, 
	sort_order INTEGER NOT NULL, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_agent_focus_items_agent_key UNIQUE (agent_id, key), 
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_agent_focus_items_agent_id ON agent_focus_items (agent_id);

CREATE INDEX IF NOT EXISTS ix_agent_focus_items_created_at ON agent_focus_items (created_at);

CREATE INDEX IF NOT EXISTS ix_agent_focus_items_kind ON agent_focus_items (kind);

CREATE INDEX IF NOT EXISTS ix_agent_focus_items_source ON agent_focus_items (source);

CREATE INDEX IF NOT EXISTS ix_agent_focus_items_status ON agent_focus_items (status);

CREATE INDEX IF NOT EXISTS ix_agent_focus_items_key ON agent_focus_items (key);

CREATE TABLE IF NOT EXISTS agent_permissions (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	scope_type permission_scope_enum NOT NULL, 
	scope_id UUID, 
	access_level VARCHAR(20) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id)
);

CREATE INDEX IF NOT EXISTS ix_agent_permissions_agent_id ON agent_permissions (agent_id);

CREATE INDEX IF NOT EXISTS ix_agent_permissions_user_scope ON agent_permissions (scope_type, scope_id);

CREATE TABLE IF NOT EXISTS agent_relationships (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	member_id UUID NOT NULL, 
	relation VARCHAR(50) NOT NULL, 
	description TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE, 
	created_by_user_id UUID, 
	updated_by_user_id UUID, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE, 
	FOREIGN KEY(member_id) REFERENCES org_members (id), 
	FOREIGN KEY(created_by_user_id) REFERENCES users (id), 
	FOREIGN KEY(updated_by_user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS agent_schedules (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	instruction TEXT NOT NULL, 
	cron_expr VARCHAR(100) NOT NULL, 
	is_enabled BOOLEAN NOT NULL DEFAULT true, 
	last_run_at TIMESTAMP WITH TIME ZONE, 
	next_run_at TIMESTAMP WITH TIME ZONE, 
	run_count INTEGER NOT NULL, 
	created_by UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE, 
	FOREIGN KEY(created_by) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS ix_agent_schedules_agent_id ON agent_schedules (agent_id);

CREATE INDEX IF NOT EXISTS ix_agent_schedules_next_run_at ON agent_schedules (next_run_at);

CREATE TABLE IF NOT EXISTS agent_tools (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	tool_id UUID NOT NULL, 
	enabled BOOLEAN NOT NULL DEFAULT true, 
	config JSON NOT NULL DEFAULT '{}', 
	source VARCHAR(20) NOT NULL, 
	installed_by_agent_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE, 
	FOREIGN KEY(tool_id) REFERENCES tools (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_agent_tools_agent_id ON agent_tools (agent_id);

CREATE INDEX IF NOT EXISTS ix_agent_tools_agent_tool ON agent_tools (agent_id, tool_id);

CREATE TABLE IF NOT EXISTS agent_triggers (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	type VARCHAR(20) NOT NULL, 
	config JSONB NOT NULL, 
	reason TEXT NOT NULL, 
	focus_ref VARCHAR(200), 
	is_enabled BOOLEAN NOT NULL DEFAULT true, 
	last_fired_at TIMESTAMP WITH TIME ZONE, 
	fire_count INTEGER NOT NULL, 
	max_fires INTEGER, 
	cooldown_seconds INTEGER NOT NULL, 
	is_system BOOLEAN NOT NULL DEFAULT false, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_agent_trigger_name UNIQUE (agent_id, name), 
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_agent_triggers_agent_id ON agent_triggers (agent_id);

CREATE TABLE IF NOT EXISTS agent_user_onboardings (
	agent_id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	onboarded_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	phase VARCHAR(32) NOT NULL, 
	PRIMARY KEY (agent_id, user_id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS approval_requests (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	action_type VARCHAR(100) NOT NULL, 
	details JSON NOT NULL, 
	status approval_status_enum NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	resolved_at TIMESTAMP WITH TIME ZONE, 
	resolved_by UUID, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id), 
	FOREIGN KEY(resolved_by) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
	id UUID NOT NULL, 
	user_id UUID, 
	agent_id UUID, 
	action VARCHAR(100) NOT NULL, 
	details JSON NOT NULL, 
	ip_address VARCHAR(50), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id)
);

CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at);

CREATE TABLE IF NOT EXISTS admin_audit_logs (
	id UUID NOT NULL,
	actor_id UUID,
	actor_role VARCHAR(32) NOT NULL,
	actor_email VARCHAR(255),
	action VARCHAR(100) NOT NULL,
	target_type VARCHAR(50) NOT NULL,
	target_id UUID,
	tenant_id UUID,
	changes JSON NOT NULL DEFAULT '{}',
	details JSON NOT NULL DEFAULT '{}',
	ip_address VARCHAR(50),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(actor_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_created_at ON admin_audit_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_actor_id ON admin_audit_logs (actor_id);
CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_tenant_id ON admin_audit_logs (tenant_id);
CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_target ON admin_audit_logs (target_type, target_id);

CREATE TABLE IF NOT EXISTS channel_configs (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	channel_type channel_type_enum NOT NULL, 
	app_id VARCHAR(255), 
	app_secret VARCHAR(512), 
	encrypt_key VARCHAR(255), 
	verification_token VARCHAR(255), 
	is_configured BOOLEAN NOT NULL, 
	is_connected BOOLEAN NOT NULL, 
	last_tested_at TIMESTAMP WITH TIME ZONE, 
	extra_config JSON NOT NULL DEFAULT '{}', 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_channel_configs_agent_channel UNIQUE (agent_id, channel_type), 
	FOREIGN KEY(agent_id) REFERENCES agents (id)
);

CREATE INDEX IF NOT EXISTS ix_channel_configs_agent_id ON channel_configs (agent_id);

CREATE TABLE IF NOT EXISTS chat_messages (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	role chat_role_enum NOT NULL, 
	content TEXT NOT NULL, 
	conversation_id VARCHAR(200) NOT NULL, 
	participant_id UUID, 
	thinking TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(participant_id) REFERENCES participants (id)
);

CREATE INDEX IF NOT EXISTS ix_chat_messages_conversation_id ON chat_messages (conversation_id);

CREATE INDEX IF NOT EXISTS ix_chat_messages_created_at ON chat_messages (created_at);

CREATE INDEX IF NOT EXISTS ix_chat_messages_agent_id ON chat_messages (agent_id);

CREATE INDEX IF NOT EXISTS ix_chat_messages_conv_created ON chat_messages (conversation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS chat_sessions (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	source_channel VARCHAR(20) NOT NULL, 
	external_conv_id VARCHAR(200), 
	is_group BOOLEAN DEFAULT 'false' NOT NULL, 
	group_name VARCHAR(200), 
	participant_id UUID, 
	peer_agent_id UUID, 
	is_primary BOOLEAN DEFAULT 'false' NOT NULL, 
	last_read_at_by_user TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	last_message_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_sessions_agent_ext_conv UNIQUE (agent_id, external_conv_id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(participant_id) REFERENCES participants (id), 
	FOREIGN KEY(peer_agent_id) REFERENCES agents (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_sessions_primary_platform ON chat_sessions (agent_id, user_id) WHERE is_primary = true AND source_channel = 'web' AND is_group = false;

CREATE INDEX IF NOT EXISTS ix_chat_sessions_agent_id ON chat_sessions (agent_id);

CREATE INDEX IF NOT EXISTS ix_chat_sessions_user_id ON chat_sessions (user_id);

CREATE INDEX IF NOT EXISTS ix_chat_sessions_is_primary ON chat_sessions (is_primary);

CREATE INDEX IF NOT EXISTS ix_chat_sessions_created_at ON chat_sessions (created_at);

CREATE INDEX IF NOT EXISTS ix_chat_sessions_last_message ON chat_sessions (agent_id, last_message_at DESC);

CREATE TABLE IF NOT EXISTS daily_token_usage (
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	date TIMESTAMP WITH TIME ZONE NOT NULL, 
	tokens_used INTEGER NOT NULL, 
	input_tokens INTEGER NOT NULL, 
	output_tokens INTEGER NOT NULL, 
	cache_read_tokens INTEGER NOT NULL, 
	cache_creation_tokens INTEGER NOT NULL, 
	estimated_tokens INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_daily_token_usage_agent_date UNIQUE (agent_id, date), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_daily_token_usage_tenant_id ON daily_token_usage (tenant_id);

CREATE INDEX IF NOT EXISTS ix_daily_token_usage_date ON daily_token_usage (date);

CREATE INDEX IF NOT EXISTS ix_daily_token_usage_agent_id ON daily_token_usage (agent_id);

CREATE TABLE IF NOT EXISTS gateway_messages (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	sender_agent_id UUID, 
	sender_user_id UUID, 
	conversation_id VARCHAR(100), 
	content TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	result TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	delivered_at TIMESTAMP WITH TIME ZONE, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id), 
	FOREIGN KEY(sender_agent_id) REFERENCES agents (id), 
	FOREIGN KEY(sender_user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS gogcli_credential_states (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	agent_id UUID NOT NULL, 
	encrypted_keyring_password TEXT, 
	encrypted_gog_data_archive TEXT, 
	account_hint VARCHAR(320), 
	status VARCHAR(32) DEFAULT 'unauthenticated' NOT NULL, 
	keyring_password_updated_at TIMESTAMP WITH TIME ZONE, 
	credential_snapshot_updated_at TIMESTAMP WITH TIME ZONE, 
	last_authenticated_at TIMESTAMP WITH TIME ZONE, 
	last_status_checked_at TIMESTAMP WITH TIME ZONE, 
	last_restored_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_gogcli_credential_states_agent_id ON gogcli_credential_states (agent_id);

CREATE TABLE IF NOT EXISTS notifications (
	id UUID NOT NULL, 
	user_id UUID, 
	agent_id UUID, 
	type VARCHAR(50) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	body TEXT NOT NULL, 
	link VARCHAR(500), 
	ref_id UUID, 
	sender_name VARCHAR(100), 
	is_read BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id)
);

CREATE INDEX IF NOT EXISTS ix_notifications_agent_id ON notifications (agent_id);

CREATE INDEX IF NOT EXISTS ix_notifications_created_at ON notifications (created_at);

CREATE INDEX IF NOT EXISTS ix_notifications_user_id ON notifications (user_id);

CREATE TABLE IF NOT EXISTS published_pages (
	id UUID NOT NULL, 
	short_id VARCHAR(16) NOT NULL, 
	agent_id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	tenant_id UUID, 
	source_path VARCHAR(500) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	view_count INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX IF NOT EXISTS ix_published_pages_agent_id ON published_pages (agent_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_published_pages_short_id ON published_pages (short_id);

CREATE TABLE IF NOT EXISTS tasks (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	title VARCHAR(500) NOT NULL, 
	description TEXT, 
	type task_type_enum NOT NULL, 
	status task_status_enum NOT NULL, 
	priority task_priority_enum NOT NULL, 
	assignee VARCHAR(50) NOT NULL, 
	created_by UUID NOT NULL, 
	due_date TIMESTAMP WITH TIME ZONE, 
	supervision_target_user_id UUID, 
	supervision_target_name VARCHAR(100), 
	supervision_channel VARCHAR(50), 
	remind_schedule VARCHAR(100), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id), 
	FOREIGN KEY(created_by) REFERENCES users (id), 
	FOREIGN KEY(supervision_target_user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS user_tenant_onboardings (
	id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	current_step VARCHAR(32) NOT NULL, 
	entry_mode VARCHAR(32) NOT NULL, 
	personal_assistant_agent_id UUID, 
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_user_tenant_onboarding UNIQUE (user_id, tenant_id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE, 
	FOREIGN KEY(personal_assistant_agent_id) REFERENCES agents (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_user_tenant_onboardings_user_id ON user_tenant_onboardings (user_id);

CREATE INDEX IF NOT EXISTS ix_user_tenant_onboardings_tenant_id ON user_tenant_onboardings (tenant_id);

CREATE TABLE IF NOT EXISTS workspace_edit_locks (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	path VARCHAR(500) NOT NULL, 
	user_id UUID NOT NULL, 
	session_id VARCHAR(200), 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	heartbeat_count INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_workspace_edit_locks_agent_path UNIQUE (agent_id, path), 
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS ix_workspace_edit_locks_agent_id ON workspace_edit_locks (agent_id);

CREATE INDEX IF NOT EXISTS ix_workspace_edit_locks_user_id ON workspace_edit_locks (user_id);

CREATE INDEX IF NOT EXISTS ix_workspace_edit_locks_expires_at ON workspace_edit_locks (expires_at);

CREATE INDEX IF NOT EXISTS ix_workspace_edit_locks_path ON workspace_edit_locks (path);

CREATE TABLE IF NOT EXISTS workspace_file_revisions (
	id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	path VARCHAR(500) NOT NULL, 
	operation VARCHAR(40) NOT NULL, 
	actor_type VARCHAR(20) NOT NULL, 
	actor_id UUID, 
	session_id VARCHAR(200), 
	before_content TEXT, 
	after_content TEXT, 
	content_hash VARCHAR(64) NOT NULL, 
	group_key VARCHAR(200), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_workspace_file_revisions_agent_id ON workspace_file_revisions (agent_id);

CREATE INDEX IF NOT EXISTS ix_workspace_file_revisions_path ON workspace_file_revisions (path);

CREATE INDEX IF NOT EXISTS ix_workspace_file_revisions_created_at ON workspace_file_revisions (created_at);

CREATE INDEX IF NOT EXISTS ix_workspace_file_revisions_group_key ON workspace_file_revisions (group_key);

CREATE TABLE IF NOT EXISTS task_logs (
	id UUID NOT NULL, 
	task_id UUID NOT NULL, 
	content TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(task_id) REFERENCES tasks (id)
);

CREATE TABLE IF NOT EXISTS trigger_executions (
	id UUID NOT NULL, 
	trigger_id UUID NOT NULL, 
	agent_id UUID NOT NULL, 
	source VARCHAR(32) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	idempotency_key VARCHAR(255) NOT NULL, 
	payload JSONB NOT NULL, 
	payload_text TEXT NOT NULL, 
	lease_owner VARCHAR(128), 
	lease_expires_at TIMESTAMP WITH TIME ZONE, 
	scheduled_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	last_error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_trigger_execution_idempotency UNIQUE (trigger_id, idempotency_key), 
	FOREIGN KEY(trigger_id) REFERENCES agent_triggers (id) ON DELETE CASCADE, 
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_trigger_executions_status_scheduled ON trigger_executions (status, scheduled_at);

CREATE INDEX IF NOT EXISTS ix_trigger_executions_agent_id ON trigger_executions (agent_id);

CREATE INDEX IF NOT EXISTS ix_trigger_executions_trigger_id ON trigger_executions (trigger_id);

COMMIT;

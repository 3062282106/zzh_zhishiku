CREATE TABLE IF NOT EXISTS managed_version (
    id uuid PRIMARY KEY,
    platform text NOT NULL,
    document_id text NOT NULL,
    title text NOT NULL,
    filename text NOT NULL,
    original text NOT NULL,
    chunks jsonb NOT NULL,
    keyword_index text,
    vector_index text,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (platform, document_id) REFERENCES source_document(platform, document_id)
);
CREATE INDEX IF NOT EXISTS managed_version_by_document
    ON managed_version(platform, document_id, created_at DESC);
CREATE TABLE IF NOT EXISTS managed_publish_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id text NOT NULL,
    previous_version text NOT NULL,
    version_id uuid NOT NULL REFERENCES managed_version(id),
    action text NOT NULL CHECK (action IN ('publish', 'rollback')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS managed_source (
    id uuid PRIMARY KEY,
    library text NOT NULL REFERENCES knowledge_base(id),
    document_platform text NOT NULL DEFAULT 'upload',
    document_id text NOT NULL UNIQUE,
    provider text NOT NULL,
    external_id text NOT NULL,
    source_url text NOT NULL,
    title text NOT NULL,
    last_digest text,
    last_version_id uuid REFERENCES managed_version(id),
    checked_at timestamptz,
    last_error text,
    UNIQUE (library, provider, external_id),
    FOREIGN KEY (document_platform, document_id) REFERENCES source_document(platform, document_id)
);
ALTER TABLE managed_source ADD COLUMN IF NOT EXISTS section_heading text;
ALTER TABLE managed_source ADD COLUMN IF NOT EXISTS user_imported boolean NOT NULL DEFAULT false;
ALTER TABLE managed_source ADD COLUMN IF NOT EXISTS auto_sync boolean;
ALTER TABLE managed_source ADD COLUMN IF NOT EXISTS sync_requested boolean NOT NULL DEFAULT false;
ALTER TABLE managed_source ADD COLUMN IF NOT EXISTS sync_next_at timestamptz;
ALTER TABLE managed_source ADD COLUMN IF NOT EXISTS sync_pending_version uuid;
ALTER TABLE managed_source ADD COLUMN IF NOT EXISTS feishu_doc_token text;
ALTER TABLE managed_source ADD COLUMN IF NOT EXISTS feishu_revision bigint;
CREATE INDEX IF NOT EXISTS managed_feishu_sync_due ON managed_source(sync_next_at, id) WHERE provider='feishu';
UPDATE managed_source SET last_error='飞书授权已失效，请在知识库网页重新授权飞书。'
WHERE provider='feishu' AND last_error IN (
    '飞书授权已失效，请重新授权自动同步。',
    '请在知识库网页重新授权飞书自动同步。'
);
CREATE TABLE IF NOT EXISTS managed_availability_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id text NOT NULL,
    version_id uuid NOT NULL REFERENCES managed_version(id),
    was_published boolean NOT NULL,
    published boolean NOT NULL,
    previous_valid_until timestamptz,
    valid_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE managed_availability_event ADD COLUMN IF NOT EXISTS previous_valid_from timestamptz;
ALTER TABLE managed_availability_event ADD COLUMN IF NOT EXISTS valid_from timestamptz;
CREATE TABLE IF NOT EXISTS managed_file (
    id uuid PRIMARY KEY,
    library text NOT NULL REFERENCES knowledge_base(id),
    document_id text NOT NULL,
    title text NOT NULL,
    filename text NOT NULL,
    content_type text NOT NULL,
    content bytea NOT NULL,
    digest text NOT NULL,
    parser_document jsonb,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','parsing','ready','failed')),
    provider_task text,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    lease uuid,
    lease_until timestamptz,
    next_check timestamptz NOT NULL DEFAULT now(),
    deadline timestamptz NOT NULL DEFAULT now() + interval '1 hour',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (library, document_id, digest, title)
);
CREATE INDEX IF NOT EXISTS managed_file_work ON managed_file(next_check)
    WHERE status IN ('queued','parsing');
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS requested_by text;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS auto_publish boolean NOT NULL DEFAULT false;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS requires_mcp_grant boolean NOT NULL DEFAULT false;
ALTER TABLE managed_source ADD COLUMN IF NOT EXISTS requested_by text;
ALTER TABLE managed_publish_event ADD COLUMN IF NOT EXISTS actor_id text;
ALTER TABLE managed_availability_event ADD COLUMN IF NOT EXISTS actor_id text;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_status text
    CHECK (publication_status IN ('queued','indexing','published','failed'));
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_expected text;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_previous_live boolean;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_previous_until timestamptz;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_requested_at timestamptz;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_error text;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_actor_id text;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_valid_from timestamptz;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_valid_until timestamptz;
CREATE UNIQUE INDEX IF NOT EXISTS managed_file_one_publication ON managed_file(document_id)
    WHERE publication_status IN ('queued','indexing');

-- Console additions: document owner, member directory fields, grant change time.
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS owner text;
ALTER TABLE member_account ADD COLUMN IF NOT EXISTS display_name text;
ALTER TABLE member_account ADD COLUMN IF NOT EXISTS email text;
ALTER TABLE member_account ADD COLUMN IF NOT EXISTS kind text;
ALTER TABLE member_account ADD COLUMN IF NOT EXISTS personal_upload boolean NOT NULL DEFAULT false;
ALTER TABLE knowledge_grant ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS knowledge_grant_by_library ON knowledge_grant(kb_id, member_id);

-- Member lifecycle. Grant changes were already visible in the audit feed, but
-- "who created this account / who disabled it / when was the token rotated" had
-- nowhere to live: member_account only keeps the current state, and the token is
-- stored as a hash, so the old token cannot be reconstructed from the row.
CREATE TABLE IF NOT EXISTS member_event (
    id uuid PRIMARY KEY,
    member text NOT NULL,
    action text NOT NULL,
    detail text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS member_event_recent ON member_event(created_at DESC);


-- Console operators. Until now the console was unlocked by one shared token in
-- api.env, so two people logging in were indistinguishable in the audit feed, and
-- revoking one person meant rotating the token for everybody.
CREATE TABLE IF NOT EXISTS admin_account (
    id text PRIMARY KEY,
    username text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    display_name text,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

-- Session tokens are stored hashed for the same reason member tokens are: a database
-- dump must not hand anyone a working credential. Expiry is checked on read, so a
-- stale row is harmless even before the next cleanup pass.
ALTER TABLE admin_account ADD COLUMN IF NOT EXISTS email text;
CREATE UNIQUE INDEX IF NOT EXISTS admin_account_email_unique ON admin_account(lower(email)) WHERE email IS NOT NULL;
CREATE TABLE IF NOT EXISTS admin_upload_grant (
    admin_id text NOT NULL REFERENCES admin_account(id) ON DELETE CASCADE,
    library text NOT NULL REFERENCES knowledge_base(id),
    PRIMARY KEY (admin_id, library)
);
CREATE TABLE IF NOT EXISTS admin_import_token (
    admin_id text PRIMARY KEY REFERENCES admin_account(id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS admin_session (
    token_hash text PRIMARY KEY,
    admin_id text NOT NULL REFERENCES admin_account(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS admin_session_expiry ON admin_session(expires_at);
CREATE TABLE IF NOT EXISTS retrieval_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    member_id text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('mcp', 'api')),
    status text NOT NULL CHECK (status IN ('evidence_found', 'insufficient_evidence', 'unavailable')),
    searched_library_ids text[] NOT NULL,
    evidence_library_ids text[] NOT NULL,
    evidence_count integer NOT NULL,
    duration_ms integer NOT NULL,
    stages_ms jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS retrieval_event_recorded_at ON retrieval_event(recorded_at);
CREATE TABLE IF NOT EXISTS member_web_session (
    token_hash text PRIMARY KEY,
    member_id text NOT NULL REFERENCES member_account(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS member_web_session_member ON member_web_session(member_id);
CREATE TABLE IF NOT EXISTS member_web_credential (
    member_id text PRIMARY KEY REFERENCES member_account(id) ON DELETE CASCADE,
    email text NOT NULL UNIQUE,
    password_hash text,
    setup_hash text,
    setup_expires_at timestamptz,
    CHECK ((password_hash IS NOT NULL AND setup_hash IS NULL AND setup_expires_at IS NULL)
        OR (password_hash IS NULL AND setup_hash IS NOT NULL AND setup_expires_at IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS managed_index_cleanup (
    index_name text PRIMARY KEY CHECK (index_name ~ '^kb-local-(chunks|hybrid)-managed-[a-f0-9]{16}$'),
    retry_after timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS managed_delete_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id text NOT NULL,
    library text NOT NULL REFERENCES knowledge_base(id),
    title text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE managed_delete_event ADD COLUMN IF NOT EXISTS actor_id text;
CREATE TABLE IF NOT EXISTS knowledge_ingest_event (
    kind text NOT NULL CHECK (kind IN ('file','feishu')),
    origin_id uuid NOT NULL,
    library text NOT NULL REFERENCES knowledge_base(id),
    actor_id text NOT NULL,
    title text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, origin_id)
);
CREATE INDEX IF NOT EXISTS knowledge_ingest_event_actor_recent ON knowledge_ingest_event(actor_id, created_at DESC);
INSERT INTO knowledge_ingest_event (kind, origin_id, library, actor_id, created_at)
SELECT 'file', f.id, f.library, k.owner_member_id, f.created_at
FROM managed_file f JOIN knowledge_base k ON k.id=f.library
WHERE k.owner_member_id IS NOT NULL
ON CONFLICT DO NOTHING;
INSERT INTO knowledge_ingest_event (kind, origin_id, library, actor_id, created_at)
SELECT 'feishu', v.id, s.library, k.owner_member_id, v.created_at
FROM managed_source s JOIN knowledge_base k ON k.id=s.library
JOIN managed_version v ON v.platform=s.document_platform AND v.document_id=s.document_id
WHERE s.user_imported AND k.owner_member_id IS NOT NULL
ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS feishu_user_authorization (
    role text NOT NULL CHECK (role IN ('member','admin')),
    account_id text NOT NULL,
    email text NOT NULL,
    app_id text NOT NULL,
    ciphertext bytea NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (role, account_id)
);
CREATE INDEX IF NOT EXISTS feishu_user_authorization_expiry ON feishu_user_authorization(expires_at);
ALTER TABLE feishu_user_authorization ADD COLUMN IF NOT EXISTS refresh_ciphertext bytea;
ALTER TABLE feishu_user_authorization ADD COLUMN IF NOT EXISTS refresh_expires_at timestamptz;
CREATE TABLE IF NOT EXISTS console_setting (
    key text PRIMARY KEY,
    value text NOT NULL
);

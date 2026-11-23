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
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_status text
    CHECK (publication_status IN ('queued','indexing','published','failed'));
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_expected text;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_previous_live boolean;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_previous_until timestamptz;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_requested_at timestamptz;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_error text;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_valid_from timestamptz;
ALTER TABLE managed_file ADD COLUMN IF NOT EXISTS publication_valid_until timestamptz;
CREATE UNIQUE INDEX IF NOT EXISTS managed_file_one_publication ON managed_file(document_id)
    WHERE publication_status IN ('queued','indexing');

-- Console additions: document owner, member directory fields, grant change time.
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS owner text;
ALTER TABLE member_account ADD COLUMN IF NOT EXISTS display_name text;
ALTER TABLE member_account ADD COLUMN IF NOT EXISTS email text;
ALTER TABLE member_account ADD COLUMN IF NOT EXISTS kind text;
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


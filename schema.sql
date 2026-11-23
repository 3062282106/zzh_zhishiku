CREATE TABLE IF NOT EXISTS knowledge_base (
    id text PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE IF NOT EXISTS member_account (
    id text PRIMARY KEY,
    token_hash text UNIQUE NOT NULL,
    enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS knowledge_grant (
    member_id text NOT NULL REFERENCES member_account(id),
    kb_id text NOT NULL REFERENCES knowledge_base(id),
    revoked_at timestamptz,
    expires_at timestamptz,
    PRIMARY KEY (member_id, kb_id)
);
CREATE TABLE IF NOT EXISTS source_document (
    platform text NOT NULL,
    document_id text NOT NULL,
    kb_id text NOT NULL REFERENCES knowledge_base(id),
    current_version text NOT NULL,
    published boolean NOT NULL DEFAULT true,
    valid_from timestamptz,
    valid_until timestamptz,
    PRIMARY KEY (platform, document_id)
);
CREATE TABLE IF NOT EXISTS source_chunk (
    chunk_id text PRIMARY KEY,
    platform text NOT NULL,
    document_id text NOT NULL,
    version text NOT NULL,
    FOREIGN KEY (platform, document_id) REFERENCES source_document(platform, document_id)
);

CREATE TABLE document (
    sha256        char(64) PRIMARY KEY,
    s3_bucket     text        NOT NULL,
    s3_key        text        NOT NULL,
    text_s3_key   text,
    filename      text        NOT NULL,
    content_type  text,
    size_bytes    bigint      NOT NULL,
    etag          text,
    tika_version  text,
    extracted_at  timestamptz,
    indexed_at    timestamptz,
    seen_at       timestamptz NOT NULL DEFAULT now(),
    status        text        NOT NULL DEFAULT 'pending',
    error         text,
    UNIQUE (s3_bucket, s3_key)
);
CREATE INDEX ON document (status);

-- Reference DDL for PostgreSQL (SQLAlchemy create_all produces the same schema).
-- Use it when you need to bootstrap a database by hand or review the model in SQL.

CREATE TABLE IF NOT EXISTS leads (
    id                   VARCHAR(36) PRIMARY KEY,
    phone_number         VARCHAR(20) NOT NULL UNIQUE,
    full_name            VARCHAR(200),
    source               VARCHAR(100),
    status               VARCHAR(20) NOT NULL DEFAULT 'NEW',
    asking_price_usd     INTEGER,
    property_condition   VARCHAR(20),
    condition_raw        VARCHAR(250),
    property_size_sqft   INTEGER,
    questions_asked      JSONB NOT NULL DEFAULT '{}'::jsonb,
    turn_count           INTEGER NOT NULL DEFAULT 0,
    last_question_slot   VARCHAR(20),
    opt_out              BOOLEAN NOT NULL DEFAULT FALSE,
    handoff_at           TIMESTAMPTZ,
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL,
    last_inbound_at      TIMESTAMPTZ,
    last_outbound_at     TIMESTAMPTZ,
    -- Data integrity: a qualified lead must carry all three slots.
    CONSTRAINT ck_leads_status CHECK (
        status IN ('NEW','CONTACTED','IN_PROGRESS','QUALIFIED','HANDED_OFF','OPTED_OUT','FAILED')
    ),
    CONSTRAINT ck_leads_condition CHECK (
        property_condition IS NULL
        OR property_condition IN ('TURNKEY','MINOR_REPAIRS','FULL_REHAB','UNKNOWN')
    ),
    CONSTRAINT ck_leads_qualified CHECK (
        status <> 'HANDED_OFF'
        OR (asking_price_usd IS NOT NULL
            AND property_condition IS NOT NULL
            AND property_size_sqft IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_leads_status ON leads (status);
CREATE INDEX IF NOT EXISTS ix_leads_phone ON leads (phone_number);

CREATE TABLE IF NOT EXISTS messages (
    id               SERIAL PRIMARY KEY,
    lead_id          VARCHAR(36) NOT NULL REFERENCES leads (id) ON DELETE CASCADE,
    direction        VARCHAR(10) NOT NULL,
    kind             VARCHAR(30) NOT NULL DEFAULT 'INBOUND',
    body             TEXT NOT NULL,
    provider_sid     VARCHAR(64) UNIQUE,
    delivery_status  VARCHAR(30),
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_messages_direction CHECK (direction IN ('INBOUND','OUTBOUND'))
);

CREATE INDEX IF NOT EXISTS ix_messages_lead_id ON messages (lead_id);
CREATE INDEX IF NOT EXISTS ix_messages_lead_created ON messages (lead_id, created_at);

CREATE TABLE IF NOT EXISTS handoffs (
    id             SERIAL PRIMARY KEY,
    lead_id        VARCHAR(36) NOT NULL REFERENCES leads (id) ON DELETE CASCADE,
    admin_phone    VARCHAR(20) NOT NULL,
    status         VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    summary_text   TEXT NOT NULL,
    payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
    provider_sid   VARCHAR(64),
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL,
    sent_at        TIMESTAMPTZ,
    CONSTRAINT ck_handoffs_status CHECK (status IN ('PENDING','SENT','FAILED'))
);

CREATE INDEX IF NOT EXISTS ix_handoffs_lead_id ON handoffs (lead_id);
CREATE INDEX IF NOT EXISTS ix_handoffs_status ON handoffs (status);

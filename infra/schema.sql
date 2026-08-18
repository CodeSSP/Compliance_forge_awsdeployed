-- ComplianceForge on CockroachDB (AWS ap-south-1 / ap-south-2).
-- Requires CockroachDB v25.2+ for the native VECTOR type and C-SPANN vector indexes.
--
--   cockroach sql --url "$CRDB_DSN" -f infra/schema.sql
--
-- Column names mirror the CSV headers exactly so the detectors need no field renaming.

-- ---------------------------------------------------------------------------
-- Multi-region. RBI payment data localisation becomes a schema property.
-- Leave commented out on a single-node local cluster.
-- ---------------------------------------------------------------------------
-- ALTER DATABASE complianceforge SET PRIMARY REGION "aws-ap-south-1";
-- ALTER DATABASE complianceforge ADD REGION "aws-ap-south-2";
-- ALTER DATABASE complianceforge ADD SUPER REGION "india"
--   VALUES "aws-ap-south-1", "aws-ap-south-2";
-- ALTER DATABASE complianceforge SURVIVE REGION FAILURE;


-- ---------------------------------------------------------------------------
-- L0 / L2 reference data. Replaces the four CSVs in L2_transaction_monitor/data.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS transactions (
  tx_id                STRING PRIMARY KEY,
  timestamp            STRING,
  channel              STRING,
  amount_inr           DECIMAL(18,2),
  sender_account_id    STRING,
  sender_name          STRING,
  sender_pan           STRING,
  sender_dob           STRING,
  sender_bank          STRING,
  sender_ifsc          STRING,
  sender_vpa           STRING,
  receiver_account_id  STRING,
  receiver_name        STRING,
  receiver_pan         STRING,
  receiver_dob         STRING,
  receiver_bank        STRING,
  receiver_vpa         STRING,
  receiver_state       STRING,
  receiver_city        STRING,
  tx_location_city     STRING,
  tx_location_state    STRING,
  tx_location_country  STRING,
  tx_location_lat      STRING,
  tx_location_lon      STRING,
  device_id            STRING,
  purpose_code         STRING,
  is_cross_border      STRING,
  fx_usd_inr           STRING,
  usd_equiv            STRING,
  beneficiary_id       STRING,
  tx_status            STRING,
  source               STRING NOT NULL DEFAULT 'seed',   -- 'seed' | 'ui' (replaces ui_transactions.csv)
  ingested_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tx_by_sender ON transactions (sender_account_id, timestamp);
CREATE INDEX IF NOT EXISTS tx_by_receiver ON transactions (receiver_account_id, timestamp);
CREATE INDEX IF NOT EXISTS tx_by_pan ON transactions (sender_pan);

CREATE TABLE IF NOT EXISTS account_details (
  account_id                 STRING PRIMARY KEY,
  pan                        STRING,
  holder_name                STRING,
  dob                        STRING,
  account_age_days           STRING,
  account_type               STRING,
  kyc_status                 STRING,
  home_state                 STRING,
  home_city                  STRING,
  typical_device_id          STRING,
  avg_monthly_txn_count      STRING,
  avg_monthly_txn_value_inr  STRING,
  avg_tx_amount_inr          STRING,
  balance_inr                STRING,
  previous_flags             STRING,
  previous_strs              STRING,
  linked_accounts_count      STRING,
  occupation_category        STRING,
  is_pep                     STRING,
  negative_news_flag         STRING,
  account_dormancy_days      STRING,
  onboarding_channel         STRING,
  is_registered_merchant     STRING,
  travel_profile             STRING,
  home_country               STRING
);

CREATE TABLE IF NOT EXISTS case_history (
  leg_id              INT PRIMARY KEY DEFAULT unique_rowid(),
  account_id          STRING NOT NULL,
  timestamp           STRING,
  amount_inr          DECIMAL(18,2),
  channel             STRING,
  counterparty_id     STRING,
  direction           STRING,
  tx_location_lat     STRING,
  tx_location_lon     STRING,
  tx_location_city    STRING,
  tx_location_state   STRING,
  tx_location_country STRING
);

CREATE INDEX IF NOT EXISTS hist_by_account ON case_history (account_id, timestamp);

CREATE TABLE IF NOT EXISTS watchlist (
  watchlist_id            STRING PRIMARY KEY,
  primary_name            STRING,
  aliases                 STRING,
  entity_type             STRING,
  dob_or_incorp           STRING,
  nationality_or_country  STRING,
  pan                     STRING,
  passport                STRING,
  cin_or_din              STRING,
  national_id_last4       STRING,
  last_known_address      STRING,
  phone                   STRING,
  listing_source          STRING,
  reference_number        STRING,
  reason_narrative        STRING,
  listed_date             STRING,
  risk_tier               STRING
);

-- C1 per-account 90-day baseline. Replaces baseline_fixture.json.
CREATE TABLE IF NOT EXISTS account_baselines (
  account_id  STRING PRIMARY KEY,
  baseline    JSONB NOT NULL
);


-- ---------------------------------------------------------------------------
-- L1 case memory. Replaces data/case_memory.json.
-- MinHash feature sets are stored verbatim; the Jaccard comparison still runs
-- in Python, so the short-circuit logic is byte-for-byte the same.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS case_memory (
  case_id                 STRING PRIMARY KEY,
  tx_id                   STRING NOT NULL,
  feature_set             STRING[] NOT NULL,
  regulation_version_hash STRING,
  final_status            STRING,
  confidence              FLOAT8,
  str_pdf_url             STRING,
  stale                   BOOL NOT NULL DEFAULT false,
  decided_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS case_memory_by_hash ON case_memory (regulation_version_hash);

-- L1/L7 regulation freshness. Replaces data/regulation_meta.json.
CREATE TABLE IF NOT EXISTS regulation_meta (
  id              INT PRIMARY KEY DEFAULT 1,
  composite_hash  STRING NOT NULL,
  sources         JSONB NOT NULL DEFAULT '{}',
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT single_row CHECK (id = 1)
);


-- ---------------------------------------------------------------------------
-- L3 corpus. Replaces ChromaDB AND Azure AI Search in one table.
-- The vector arm reads `embedding`; the keyword arm reads `searchable_text`.
-- Row, embedding and index commit in the same transaction, so a regulation
-- update can never be half-applied.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS regulatory_chunks (
  chunk_id         STRING PRIMARY KEY,
  document_id      STRING NOT NULL,
  title            STRING,
  regulator        STRING NOT NULL DEFAULT 'RBI',
  document_type    STRING,
  effective_date   STRING,
  url              STRING,
  tags             STRING[],
  section_id       STRING,
  section_heading  STRING,
  content          STRING NOT NULL,
  searchable_text  STRING NOT NULL,
  key_phrases      STRING[],
  content_sha256   STRING,
  superseded_by    STRING,
  embedding        VECTOR(768),                -- nomic-embed-text
  VECTOR INDEX (regulator, embedding)
);

CREATE INDEX IF NOT EXISTS chunks_by_document ON regulatory_chunks (document_id);


-- ---------------------------------------------------------------------------
-- L3/L4 verdicts and L5 review queue.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS verdicts (
  tx_id        STRING PRIMARY KEY,
  case_id      STRING,
  confidence   FLOAT8,
  band         STRING,
  verdict      STRING,
  sub_scores   JSONB NOT NULL DEFAULT '{}',
  citations    JSONB NOT NULL DEFAULT '[]',
  str_pdf_url  STRING,
  str_s3_key   STRING,
  decided_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS review_queue (
  tx_id         STRING PRIMARY KEY,
  case_id       STRING,
  band          STRING NOT NULL,
  state         STRING NOT NULL DEFAULT 'open',
  fiu_deadline  TIMESTAMPTZ,
  maker         STRING,
  checker       STRING,
  notes         JSONB NOT NULL DEFAULT '{}',
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS review_open ON review_queue (state, fiu_deadline);


-- ---------------------------------------------------------------------------
-- L6 audit chain. Sharded so appends do not serialise on one range.
-- Serializable isolation is what makes this safe: two concurrent appends cannot
-- both read the same head and fork the chain. One retries instead.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_chain (
  shard        STRING NOT NULL,
  seq          INT NOT NULL,
  event_type   STRING NOT NULL,
  tx_id        STRING,
  case_id      STRING,
  payload      JSONB NOT NULL,
  prev_hash    STRING,
  entry_hash   STRING NOT NULL,
  committed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (shard, seq)
);

CREATE INDEX IF NOT EXISTS audit_by_tx ON audit_chain (tx_id);

-- Periodic global anchor over every shard head. Gives one provable root without
-- forcing every append through a single writer.
CREATE TABLE IF NOT EXISTS audit_anchors (
  anchor_id    INT PRIMARY KEY DEFAULT unique_rowid(),
  heads        JSONB NOT NULL,
  anchor_hash  STRING NOT NULL,
  prev_anchor  STRING,
  anchored_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- L7 regulatory watch. Replaces l7_state.json and processed_blobs*.json.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS watch_state (
  source_key         STRING PRIMARY KEY,
  last_processed_url STRING,
  content_sha256     STRING,
  last_checked       TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_changed       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS processed_documents (
  document_key  STRING PRIMARY KEY,
  processed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- Retention. Regulator replay needs the GC window open far wider than default
-- (which is a few hours). 90 days here; RBI wants 7 years in production.
-- ---------------------------------------------------------------------------

ALTER TABLE verdicts CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE audit_chain CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE case_memory CONFIGURE ZONE USING gc.ttlseconds = 7776000;

-- Changefeeds replace Service Bus. A verdict cannot commit without its
-- downstream event firing, which is stronger than a separate message broker.
-- CREATE CHANGEFEED FOR TABLE verdicts, review_queue
--   INTO 'kafka://<msk-bootstrap>' WITH updated, resolved = '10s';

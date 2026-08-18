# ComplianceForge — AWS + CockroachDB

Every Azure dependency is gone. Detection logic, scoring maths, routing bands,
goAML output and the React UI are unchanged.

## What did not change

| Component | Why it survived untouched |
|---|---|
| C1–C6 detectors | They take a transaction dict and return a score. The dict now comes from CockroachDB instead of pandas. Signatures identical. |
| `L2_transaction_monitor/orchestrator.py` | The `asyncio.gather` fan-out is untouched. |
| L3 four weighted sub-scores, five routing bands | Pure arithmetic over retrieval output. |
| MinHash LSH short-circuit | Feature sets and Jaccard comparison are byte-for-byte the same; only storage moved. |
| goAML builder + self-healing XSD loop | Writes the same XML. |
| `regulatory-ui-react/` | Zero changes. The SSE event shapes and `/api/*` contract are preserved exactly. |

## Service mapping

| Azure | AWS / CockroachDB |
|---|---|
| Azure Queue Storage / Service Bus | Amazon SQS (`aws/sqs_queue.py`) |
| Cosmos DB | CockroachDB (`aws/db.py`, `aws/store.py`) |
| Azure Blob Storage | Amazon S3 (`aws/s3_store.py`) |
| Azure AI Search | `regulatory_chunks` + BM25 (`aws/vectors.py`) |
| ChromaDB | `regulatory_chunks` VECTOR INDEX (same table) |
| Azure AI Document Intelligence | Amazon Textract (`L7_regulatory_watch/ocr.py`) |
| Azure AI Foundry / Ollama | Bedrock or SageMaker (`aws/model_gateway.py`) |
| Azure Functions | Lambda on EventBridge schedules |
| Azure Container Apps | ECS Fargate |
| Key Vault | Secrets Manager + KMS |
| `infra/main.bicep` | `infra/main.tf` |
| Local JSON state files | CockroachDB tables |

## File-by-file

### New

| File | Purpose |
|---|---|
| `aws/db.py` | Lazy CRDB pool, `@retry()` for 40001, `as_of()` time travel, vector literals |
| `aws/store.py` | Every read/write: L2 reference data, case memory, regulation meta, verdicts, review queue, watch state |
| `aws/vectors.py` | Vector arm + BM25 keyword arm, atomic chunk upsert, RAG-hunter delete |
| `aws/model_gateway.py` | One model entry point: `MODEL_BACKEND=bedrock\|sagemaker\|ollama` |
| `aws/s3_store.py` | S3 for raw circulars, STR PDFs, goAML XML |
| `aws/sqs_queue.py` | SQS with in-process fallback |
| `infra/schema.sql` | Full schema, columns matching the CSV headers exactly |
| `infra/main.tf` | S3 + KMS, SQS + DLQ, Secrets Manager, ECS, IAM, EventBridge |
| `scripts/seed_crdb.py` | CSVs + baseline fixture + case memory → CockroachDB |
| `scripts/migrate_chroma_to_crdb.py` | Existing 1445 nomic vectors → `regulatory_chunks`, no re-embedding |
| `scripts/verify_parity.py` | Regression gate: L2 over CSV vs CRDB, diffs scores and triggers |
| `L3_regulation_interpreter/crdb_ingestion.py` | S3 → chunk → embed → CRDB |
| `L7_regulatory_watch/s3_storage.py` | Replaces `blob_storage.py` |

### Rewritten

| File | Change |
|---|---|
| `config.py` | All Azure config removed; CRDB, S3, SQS, model backend, Textract |
| `requirements.txt` | Seven `azure-*` packages and `chromadb` out; `psycopg`, `boto3`, `rank-bm25` in |
| `api.py` | SQS publish, real CRDB audit chain, verdict + review persistence, richer `/api/health`. Response shapes unchanged. |
| `L0_event_ingestion/event_receiver.py` | SQS transport; `get_queue_client` / `publish_transactions` / `receive_message` / `delete_message` / `get_queue_length` all kept |
| `L1_orchestrator/minhash_lsh.py` | `case_memory` table; MinHash logic untouched |
| `L1_orchestrator/regulation_hash.py` | `regulation_meta` table; `update_hash` now also invalidates stale cached verdicts |
| `L2_transaction_monitor/data_layer.py` | CRDB-backed; `history_for` / `account_for` / `add_to_history` signatures preserved |
| `.../c1_.../cosmos_client.py` → `crdb_client.py` | Baseline and rolling-window reads from CRDB |
| C1/C2/C3/C5/C6 SLM calls | Route through `model_gateway`; function names kept for call sites |
| `L3_.../hybrid_retrieval.py` | Dual retrieval, both arms from CRDB |
| `L3_.../llm_client.py` | Gateway-backed; `generate_ollama_embedding` name kept (4 importers) |
| `L3_.../legal_reasoning.py` | `azure_analysis` → `keyword_analysis`; `backend_used` now `crdb_keyword` / `crdb_vector` |
| `L4/l4_report_generator.py` | `_slm_map_ollama` uses the gateway |
| `L6_audit_logger/*` | Was `TODO` stubs. Now a real sharded chain with anchors and `verify()` |
| `L7_regulatory_watch/{main,scraper,indexer,ocr}.py` | CRDB state, CRDB corpus, S3 archive, Textract, and the hash-update loop closed |
| `infra/setup-env.sh` | AWS CLI instead of `az` |

### Deleted

`infra/main.bicep`, `L0_event_ingestion/function.json`,
`L3_regulation_interpreter/azure_ingestion.py`, `filter_azure_corpus.py`,
`chroma_ingestion.py`, `reindex_chroma.py`, `processed_blobs*.json`,
`L7_regulatory_watch/blob_storage.py`.

## Runbook

```bash
# 1. Cluster. MUST be v25.2+ — no VECTOR type below that.
cockroach sql --url "$CRDB_DSN" -e "CREATE DATABASE complianceforge"
cockroach sql --url "$CRDB_DSN" -f infra/schema.sql

# 2. Config
cp .env.example .env      # set CRDB_DSN at minimum

# 3. Data
pip install -r requirements.txt
python scripts/seed_crdb.py                  # 2000 txns, 1662 accounts, 76k history legs, 104 watchlist
python scripts/migrate_chroma_to_crdb.py     # 1445 chunks, vectors carried over as-is

# 4. Regression gate — run this before touching L3
python scripts/verify_parity.py --limit 200

# 5. Run
uvicorn api:app --reload --port 8000
cd regulatory-ui-react && npm install && npm run dev
```

`chroma_db/` is kept in the repo only as the migration source. Delete it once
`verify_parity.py` passes and the L3 citations match.

## Order of work, and why

Step 4 is the gate. Run L2 over the same transactions in CSV mode and CRDB mode
and diff. If the scores move, the port broke data loading, not detection — and
you want to catch that before L3 and the model are in the picture confusing the
signal. Only after parity is confirmed should you compare L3 citations.

## Known gotchas

- **CockroachDB must be v25.2+.** The `VECTOR` type and C-SPANN indexes arrived
  in 25.2. Check the version before anything else; on an older cluster the L3
  story collapses and `schema.sql` will fail on `regulatory_chunks`.
- **Embeddings must stay nomic-embed-text.** The corpus is 768-dim nomic. Any
  Bedrock embedding model changes the dimension, which means editing
  `VECTOR(768)` in `schema.sql` and re-embedding all 1445 chunks. Generation is
  free to move to Bedrock; embeddings are not.
- **`langchain-postgres` PGVector will not work.** CockroachDB's `VECTOR` is
  native, not the pgvector extension. That is why `aws/vectors.py` talks SQL
  directly instead of using a LangChain retriever.
- **Embeddings are interpolated, not bound.** CockroachDB will not infer
  `VECTOR` for an untyped placeholder, so `db.vec()` output goes into the SQL
  string. It emits digits and commas only, so there is nothing injectable.
- **Every read-modify-write needs `@retry()`.** SERIALIZABLE raises 40001 under
  contention. All existing paths are wrapped; new ones must be too.
- **LangGraph checkpointing.** The Postgres saver mostly works over CRDB's
  pg-wire but expect friction on DDL and upsert patterns. Fallback is a
  hand-rolled checkpoint table, roughly 40 lines. Nothing in the current code
  path depends on it.
- **`case_history` seeding is insert-only.** It has a generated `leg_id`, so
  `seed_crdb.py` skips if the table is already populated. Truncate to reload.

## The four pitch angles

1. **RBI data localisation is a schema property.** `REGIONAL BY ROW` with a
   super region across `ap-south-1` and `ap-south-2`. Indian rows physically
   cannot leave India, and the cluster survives a full region loss. Enforced by
   the database, not by a deployment document. (Commented out in `schema.sql`
   for single-node dev — uncomment on a multi-region cluster.)
2. **Serializable isolation protects the hash chain.** Two concurrent appends
   cannot both read the same head and link to the same `prev_hash`. Under
   read-committed that race silently forks the chain, in the one component whose
   entire purpose is being tamper-evident.
3. **One table killed the L3 staleness window.** Chroma and Azure Search were
   updated separately, so there was a window where L3 could reason against a
   circular one arm had already superseded. Now the chunk row, its embedding and
   the index commit in one transaction.
4. **`AS OF SYSTEM TIME` gives regulator replay.** `store.verdict_as_of()`
   reconstructs exactly what the system believed at a past instant; the hash
   chain proves nothing was altered since.

## The demo moment

Kill a node mid-detection. The verdict still commits,
`L6_audit_logger.hash_chain.verify()` returns `None`, then
`store.verdict_as_of(tx_id, '<pre-failure timestamp>')` reconstructs the
pre-failure state and it matches. Time travel plus tamper evidence, on one
screen, in under a minute.

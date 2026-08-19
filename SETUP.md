# SETUP — from zip to running demo

Written for the hackathon deadline. Follow in order; each step tells you what
"working" looks like so you don't discover a problem three steps later.

Minimum viable path is **Steps 1–5** (~25 min). That gets CockroachDB + vector
search + the memory demo running locally. Steps 6–7 add the AWS services.

---

## Step 0 — Unzip and open

```bash
unzip ComplianceForge-hackathon-submission.zip
cd aws-port
python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Everything below is editing `.env` and running commands from this folder.

---

## Step 1 — CockroachDB Cloud cluster (required)

Sign up at https://cockroachlabs.cloud — free tier, no card needed.

**Option A — ccloud CLI (do this one; it's a required hackathon feature)**

```bash
brew install cockroachdb/tap/ccloud      # macOS
# Linux: download from https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-get-started

./scripts/provision_ccloud.sh
```

That authenticates, creates the cluster/database/user, checks the version, and
writes `CRDB_DSN` into `.env` for you.

**Option B — Cloud Console** (if ccloud gives you trouble and time is short)

Create Cluster → **AWS** → **ap-south-1 (Mumbai)** → Basic/free. Then Connect →
General connection string. Copy it into `.env`:

```
CRDB_DSN=postgresql://user:pass@host:26257/complianceforge?sslmode=verify-full
```

> **Check the version before going further.** The `VECTOR` type needs v25.2+.
> `./scripts/provision_ccloud.sh version` — or the console shows it on the
> cluster page. On an older version, `schema.sql` fails on `regulatory_chunks`
> and the whole L3 story breaks. Create a new cluster rather than working around it.

**No API key needed** — CockroachDB auth is the DSN's username/password.

---

## Step 2 — Schema

```bash
cockroach sql --url "$CRDB_DSN" -e "CREATE DATABASE IF NOT EXISTS complianceforge"
cockroach sql --url "$CRDB_DSN" -f infra/schema.sql
```

No `cockroach` binary? The Cloud Console has a SQL shell — paste `schema.sql`
into it.

**Working looks like:** `SHOW TABLES;` lists ~12 tables including
`regulatory_chunks` and `audit_chain`.

---

## Step 3 — Ollama (local model, no API key, no cost)

```bash
# https://ollama.com/download
ollama pull phi4-mini
ollama pull nomic-embed-text     # REQUIRED — the corpus is 768-dim nomic
ollama serve
```

`.env` already defaults to this, so nothing to change:

```
MODEL_BACKEND=ollama
EMBED_BACKEND=ollama
```

> `nomic-embed-text` is not optional. The stored vectors are 768-dim nomic; a
> different embedding model returns garbage matches rather than an error, which
> is worse.

---

## Step 4 — Load the data

```bash
python scripts/seed_crdb.py                  # ~2 min
python scripts/migrate_chroma_to_crdb.py     # ~1 min, needs: pip install chromadb
```

**Working looks like:** seed reports roughly 2000 transactions, 1662 accounts,
76k history legs, 104 watchlist rows. Migration reports **1445 chunks**. If the
chunk count is 0, the `chroma_db/` folder didn't come across in the unzip.

---

## Step 5 — Verify, then run

```bash
python scripts/verify_parity.py --limit 200
```

This runs the detectors against the CSVs and against CockroachDB and diffs
them. They must match. **If they don't, the bug is data loading, not detection
— don't touch detector logic to make it pass.**

```bash
uvicorn api:app --reload --port 8000
# separate terminal:
cd regulatory-ui-react && npm install && npm run dev
```

Open http://localhost:8000/api/health — `cockroachdb` should read `connected`.

**You can film the demo from here.** Steps 6–7 add AWS services but the memory
story is already fully working.

---

## Step 6 — AWS (free-tier services only)

Console → IAM → Users → create user → **Access keys** → download the CSV.

```
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=ap-south-1
```

Put these in `.env`, or run `aws configure` and leave them out of the file —
`boto3` picks up `~/.aws/credentials` automatically, which is safer.

Then create the two resources:

```bash
aws s3 mb s3://complianceforge-<something-unique> --region ap-south-1
aws sqs create-queue --queue-name tx-events --region ap-south-1
```

Add to `.env`:

```
S3_BUCKET=complianceforge-<something-unique>
SQS_QUEUE_URL=https://sqs.ap-south-1.amazonaws.com/<account-id>/tx-events
TEXTRACT_ENABLED=true
```

Restart uvicorn. **Working looks like:** `/api/health` now shows a real bucket
and queue URL instead of "not configured". That endpoint is your cheapest proof
on camera.

> S3 (5GB), SQS (1M requests/month) and Lambda (1M requests/month) are free
> tier. **Do not create a SageMaker endpoint** — it bills per hour while it
> exists, not while you use it, and will quietly eat your credits overnight.

---

## Step 7 — Managed MCP Server (required hackathon feature)

The per-cluster **Connect** dialog only offers SQL-user credentials — there is
no MCP tab there. MCP access is org-level, via a service account:

```bash
ccloud service-account create mcp-agent --description "Claude Code MCP access"
ccloud role add <service-account-id> CLUSTER_ADMIN CLUSTER <cluster-id>
ccloud service-account api-key create <service-account-id> mcp-key
```

The last command prints a secret **once** — it won't be shown again. Register
it as a local (not project-scoped, so it never touches `.mcp.json` or git):

```bash
claude mcp add --transport http cockroachdb-cloud https://cockroachlabs.cloud/mcp \
  --header "Authorization: Bearer <the-secret>" -s local
```

`.mcp.json`'s checked-in `cockroachdb-cloud` entry has no credentials by
design — it's the public placeholder every clone starts from. Note the host:
it's `cockroachlabs.cloud/mcp`, **not** `mcp.cockroachlabs.cloud` (that
subdomain doesn't resolve).

Verify with `claude mcp get cockroachdb-cloud` (expect `✓ Connected`), then:

```bash
claude
```

Ask it something like *"what tables are in this database and how many rows are
in regulatory_chunks?"* — that's a clean 15-second clip for the video and it
makes the MCP claim true rather than aspirational.

---

## Before you push to GitHub

```bash
grep -rn "AKIA\|password\|secret" .env       # confirm .env is the only place
cat .gitignore | grep .env                    # confirm it's ignored
```

`.env` is gitignored already. Just don't force-add it.

---

## Troubleshooting

**`no pq wrapper available`** → `pip install "psycopg[binary]"`

**`type VECTOR does not exist`** → cluster is below v25.2. New cluster required.

**Vector search returns nothing** → `SELECT count(*) FROM regulatory_chunks WHERE embedding IS NOT NULL;` should be ~1445. If 0, the migration didn't carry embeddings.

**Nothing ever short-circuits** → `case_memory` is empty. Run 20–30 transactions through the UI first, then submit one similar to an earlier flagged case. Similarity threshold is 0.80.

**Ollama timeouts** → first call loads the model into RAM and is slow. Warm it before recording: `ollama run phi4-mini "hi"`.

**`40001` serialization errors** → expected under load; the retry decorator handles them. Only worry if one escapes.

---

## Demo checklist

Before recording, confirm each of these actually happens:

- [ ] `/api/health` shows cockroachdb connected, real S3 bucket, real SQS URL
- [ ] A flagged transaction runs the full pipeline with citations
- [ ] A similar transaction **short-circuits** at L1
- [ ] After changing the regulation hash, that same transaction runs fully again
- [ ] `verify()` on the audit chain returns `None` (intact)
- [ ] Claude Code can query the cluster over MCP

#!/usr/bin/env bash
# Provision the ComplianceForge memory layer with the agent-ready ccloud CLI.
#
# Every command uses -o json so an agent (Claude Code, CI) can parse the result
# rather than screen-scrape. Run end to end, or let an agent run it step by step.
#
#   ./scripts/provision_ccloud.sh                  # create + configure + emit DSN
#   ./scripts/provision_ccloud.sh status           # cluster info as JSON
#   ./scripts/provision_ccloud.sh dsn              # print the connection string
#
# Requires: ccloud (brew install cockroachdb/tap/ccloud), jq

set -euo pipefail

CLUSTER="${CRDB_CLUSTER_NAME:-complianceforge}"
DATABASE="${CRDB_DATABASE:-complianceforge}"
SQL_USER="${CRDB_SQL_USER:-complianceforge_app}"
CLOUD="AWS"
# RBI payment data localisation: the cluster must sit in an Indian region.
REGION="${CRDB_REGION:-ap-south-1}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing $1"; exit 1; }; }
need ccloud
need jq

cmd_create() {
  echo "==> Authenticating"
  ccloud auth login --no-redirect || true

  echo "==> Creating cluster '$CLUSTER' on $CLOUD/$REGION"
  # Basic (serverless) scales to zero and is the right shape for a hackathon
  # demo. Swap to 'advanced' for the multi-region REGIONAL BY ROW story.
  ccloud cluster create basic "$CLUSTER" "$REGION" --cloud "$CLOUD" -o json \
    | tee /tmp/cf_cluster.json | jq '{name, id, state, cloud_provider}'

  echo "==> Creating database '$DATABASE'"
  ccloud cluster database create "$CLUSTER" "$DATABASE" -o json | jq '.'

  echo "==> Creating SQL user '$SQL_USER'"
  ccloud cluster user create "$CLUSTER" "$SQL_USER" -o json | jq '{name}' || \
    echo "    (user may already exist — continuing)"

  echo "==> Verifying the cluster version supports VECTOR"
  cmd_version_check

  cmd_dsn
}

cmd_version_check() {
  # The native VECTOR type and C-SPANN vector indexes require v25.2+.
  # Anything older and infra/schema.sql fails on regulatory_chunks.
  local version
  version=$(ccloud cluster info "$CLUSTER" -o json | jq -r '.cockroach_version // empty')
  echo "    cluster version: ${version:-unknown}"

  local major minor
  major=$(echo "$version" | sed 's/^v//' | cut -d. -f1)
  minor=$(echo "$version" | sed 's/^v//' | cut -d. -f2)

  if [ -n "$major" ] && { [ "$major" -gt 25 ] || { [ "$major" -eq 25 ] && [ "$minor" -ge 2 ]; }; }; then
    echo "    OK — vector indexing available"
  else
    echo "    WARNING: v25.2+ is required for the VECTOR type used by L3."
    echo "    Upgrade before applying infra/schema.sql."
  fi
}

cmd_dsn() {
  echo "==> Connection string"
  local dsn
  dsn=$(ccloud cluster connection-string "$CLUSTER" \
          --database "$DATABASE" --sql-user "$SQL_USER" -o json \
        | jq -r '.connection_url')

  echo "$dsn"

  if grep -q "^CRDB_DSN=" .env 2>/dev/null; then
    sed -i.bak "s|^CRDB_DSN=.*|CRDB_DSN=$dsn|" .env && rm -f .env.bak
  else
    echo "CRDB_DSN=$dsn" >> .env
  fi
  echo "==> Written to .env"

  cat <<EOT

Next:
  cockroach sql --url "\$CRDB_DSN" -f infra/schema.sql
  python scripts/seed_crdb.py
  python scripts/migrate_chroma_to_crdb.py
  python scripts/verify_parity.py --limit 200
EOT
}

cmd_status() {
  ccloud cluster info "$CLUSTER" -o json | jq '{name, state, cloud_provider, regions, cockroach_version}'
}

cmd_backup() {
  echo "==> Backup configuration (RBI expects recoverable audit history)"
  ccloud cluster backup config update "$CLUSTER" --frequency 60 --retention 60 -o json | jq '.'
}

cmd_audit() {
  # The control-plane audit log complements the in-database L6 hash chain:
  # one records who touched the cluster, the other records what the pipeline decided.
  ccloud audit list --limit 20 -o json | jq '.[] | {timestamp, action, user}'
}

case "${1:-create}" in
  create)  cmd_create ;;
  dsn)     cmd_dsn ;;
  status)  cmd_status ;;
  version) cmd_version_check ;;
  backup)  cmd_backup ;;
  audit)   cmd_audit ;;
  *)       echo "Usage: $0 {create|dsn|status|version|backup|audit}"; exit 1 ;;
esac

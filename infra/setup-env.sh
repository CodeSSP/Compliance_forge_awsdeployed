#!/usr/bin/env bash
# Populates .env from provisioned AWS resources.
# Usage: ./infra/setup-env.sh <stack-or-prefix>
set -euo pipefail

PREFIX="${1:-complianceforge}"
REGION="${AWS_REGION:-ap-south-1}"

echo "Looking up AWS resources with prefix '$PREFIX' in $REGION..."

BUCKET=$(aws s3api list-buckets --query "Buckets[?starts_with(Name, '$PREFIX')].Name | [0]" --output text)
QUEUE_URL=$(aws sqs list-queues --region "$REGION" --queue-name-prefix "$PREFIX" \
  --query "QueueUrls[0]" --output text 2>/dev/null || echo "None")

[ "$BUCKET" = "None" ] && { echo "No S3 bucket found with prefix $PREFIX"; exit 1; }
echo "  S3 bucket: $BUCKET"
[ "$QUEUE_URL" != "None" ] && echo "  SQS queue: $QUEUE_URL"

touch .env
upsert() {
  local key="$1" val="$2"
  if grep -q "^${key}=" .env 2>/dev/null; then
    sed -i.bak "s|^${key}=.*|${key}=${val}|" .env && rm -f .env.bak
  else
    echo "${key}=${val}" >> .env
  fi
}

upsert AWS_REGION "$REGION"
upsert S3_BUCKET "$BUCKET"
[ "$QUEUE_URL" != "None" ] && upsert SQS_QUEUE_URL "$QUEUE_URL"

echo
echo "Written to .env. Still set by hand:"
echo "  CRDB_DSN            (from the CockroachDB Cloud console)"
echo "  MODEL_BACKEND       (bedrock | sagemaker | ollama)"
echo "  BEDROCK_MODEL_ID    or SAGEMAKER_GEN_ENDPOINT"

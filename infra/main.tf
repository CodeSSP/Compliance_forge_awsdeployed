terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  description = "RBI data localisation: keep this in India."
  type        = string
  default     = "ap-south-1"
}

variable "prefix" {
  type    = string
  default = "complianceforge"
}

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# S3 — raw circulars, STR PDFs, goAML XML. Replaces Azure Blob Storage.
# ---------------------------------------------------------------------------

resource "aws_kms_key" "data" {
  description             = "ComplianceForge data at rest"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

resource "aws_s3_bucket" "data" {
  bucket = "${var.prefix}-${data.aws_caller_identity.current.account_id}-${var.region}"
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.data.arn
      sse_algorithm     = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Object Lock equivalent for the 7-year RBI retention on filed reports.
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    id     = "retain-reports"
    status = "Enabled"
    filter { prefix = "reports/" }
    noncurrent_version_expiration { noncurrent_days = 2555 }
  }
}

# ---------------------------------------------------------------------------
# SQS — L0 transport. Replaces Azure Queue Storage / Service Bus.
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.prefix}-tx-events-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "tx_events" {
  name                       = "${var.prefix}-tx-events"
  visibility_timeout_seconds = 300
  kms_master_key_id          = aws_kms_key.data.id

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 5
  })
}

# ---------------------------------------------------------------------------
# Secrets — CRDB DSN lives here, never in the task definition.
# ---------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "crdb" {
  name       = "${var.prefix}/crdb-dsn"
  kms_key_id = aws_kms_key.data.id
}

# ---------------------------------------------------------------------------
# ECS Fargate — api.py. Replaces Azure Container Apps.
# ---------------------------------------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = var.prefix
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${var.prefix}-api"
  retention_in_days = 30
}

resource "aws_iam_role" "task" {
  name = "${var.prefix}-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "task" {
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*"]
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage", "sqs:ReceiveMessage",
          "sqs:DeleteMessage", "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.tx_events.arn
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.crdb.arn
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.data.arn
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:Converse"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["sagemaker:InvokeEndpoint"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "textract:StartDocumentTextDetection",
          "textract:GetDocumentTextDetection"
        ]
        Resource = "*"
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# EventBridge — L7 regulatory watch every 6 hours, L6 anchor every 5 minutes.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "regulatory_watch" {
  name                = "${var.prefix}-l7-watch"
  schedule_expression = "rate(6 hours)"
}

resource "aws_cloudwatch_event_rule" "audit_anchor" {
  name                = "${var.prefix}-l6-anchor"
  schedule_expression = "rate(5 minutes)"
}

# ---------------------------------------------------------------------------

output "s3_bucket" {
  value = aws_s3_bucket.data.id
}

output "sqs_queue_url" {
  value = aws_sqs_queue.tx_events.url
}

output "crdb_secret_arn" {
  value = aws_secretsmanager_secret.crdb.arn
}

output "next_steps" {
  value = <<-EOT
    1. Create the CockroachDB cluster (v25.2+) in ${var.region} and store the DSN:
         aws secretsmanager put-secret-value --secret-id ${aws_secretsmanager_secret.crdb.name} --secret-string '<dsn>'
    2. ./infra/setup-env.sh ${var.prefix}
    3. cockroach sql --url "$CRDB_DSN" -f infra/schema.sql
    4. python scripts/seed_crdb.py && python scripts/migrate_chroma_to_crdb.py
  EOT
}

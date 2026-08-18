import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    """Base configuration. AWS + CockroachDB."""

    # ---- CockroachDB (single store: transactions, cases, verdicts, chunks, audit) ----
    CRDB_DSN = os.environ.get("CRDB_DSN")
    CRDB_POOL_MAX = int(os.environ.get("CRDB_POOL_MAX", "16"))

    # ---- Amazon SQS (L0 transport, replaces Azure Queue Storage / Service Bus) ----
    SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL")
    SQS_QUEUE_NAME = os.environ.get("SQS_QUEUE_NAME", "tx-events")

    # ---- Amazon S3 (raw circulars, STR PDFs, goAML XML) ----
    S3_BUCKET = os.environ.get("S3_BUCKET")
    S3_REGULATION_PREFIX = os.environ.get("S3_REGULATION_PREFIX", "regulations/pdf/")
    S3_RAW_PREFIX = "regulations/raw/"
    S3_REPORTS_PREFIX = "reports/str/"
    S3_GOAML_PREFIX = "reports/goaml/"
    S3_KMS_KEY_ID = os.environ.get("S3_KMS_KEY_ID")

    # ---- Models (Bedrock / SageMaker / Ollama), see aws/model_gateway.py ----
    AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
    MODEL_BACKEND = os.environ.get("MODEL_BACKEND", "ollama")
    BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID")
    SAGEMAKER_GEN_ENDPOINT = os.environ.get("SAGEMAKER_GEN_ENDPOINT")

    # Embeddings stay on nomic-embed-text at 768 dims to match regulatory_chunks.
    EMBED_BACKEND = os.environ.get("EMBED_BACKEND", "ollama")
    SAGEMAKER_EMBED_ENDPOINT = os.environ.get("SAGEMAKER_EMBED_ENDPOINT")
    EMBED_DIM = 768

    # ---- Local model gateway (dev / offline demo) ----
    OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "phi4-mini:latest")
    OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

    # ---- Amazon Textract (L7 OCR, replaces Document Intelligence) ----
    TEXTRACT_ENABLED = os.environ.get("TEXTRACT_ENABLED", "false").lower() == "true"

    # ---- L6 audit chain ----
    AUDIT_SHARD = os.environ.get("AUDIT_SHARD", os.environ.get("AWS_REGION", "ap-south-1"))

    # ---- Pipeline settings ----
    FIU_IND_DEADLINE_DAYS = 7
    L2_ENDPOINT = os.environ.get("L2_ENDPOINT", "http://localhost:8002/process")
    L6_ENDPOINT = os.environ.get("L6_ENDPOINT", "http://localhost:8006/log")

    # ---- Seed dataset paths (used only by scripts/seed_crdb.py) ----
    TRANSACTIONS_CSV   = "data/transactions.csv"
    ACCOUNTS_CSV       = "data/account_details.csv"
    WATCHLIST_CSV      = "data/watchlist.csv"
    CASE_HISTORY_CSV   = "data/case_history.csv"
    GROUND_TRUTH_CSV   = "data/ground_truth.csv"

    VALID_CHANNELS = {"UPI", "NEFT", "RTGS", "IMPS", "SWIFT"}


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig
}


def get_config():
    config_name = os.environ.get("APP_ENV", os.environ.get("FLASK_ENV", "default"))
    return config.get(config_name, DevelopmentConfig)()

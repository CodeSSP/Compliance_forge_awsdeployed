"""
C4: Account Risk and Dormancy Analysis
- Account age assessment
- KYC level evaluation
- Historical activity patterns
- Dormancy detection
- Risk tier calculation
- Runs local data-quality and risk heuristics
- Optionally asks Gemini/Phi-4 for a structured risk verdict
"""
import asyncio
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib import error

# L3 llm_client and pandas are only needed by the legacy LLM-based risk scorer.
# The unified pipeline uses evaluate_row() (deterministic), so these are optional.
sys.path.append(str(Path(__file__).resolve().parent.parent))
try:
    from L3_regulation_interpreter.llm_client import chat_json
except Exception:  # pragma: no cover - L3 not present in L2-only deliverable
    def chat_json(*_a, **_k):
        raise RuntimeError("L3 llm_client unavailable in L2-only mode")

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None


OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "phi4-mini:latest")
DEFAULT_SHEET_NAME = os.environ.get("RISK_SCORE_SHEET_NAME")

FIELD_ALIASES = {
    "tx_id": ["tx_id", "transaction_id", "txn_id", "id", "reference_id", "row_id", "account_id"],
    "amount": [
        "amount",
        "transaction_amount",
        "tx_amount",
        "value",
        "txn_amount",
        "avg_monthly_txn_value_inr",
    ],
    "account_age_days": [
        "account_age_days",
        "acct_age_days",
        "days_since_account_open",
        "account_tenure_days",
    ],
    "account_age_months": ["account_age_months", "acct_age_months", "tenure_months"],
    "past_flags": ["past_flags", "previous_flags", "historical_flags", "flag_count"],
    "previous_strs": ["previous_strs", "historical_strs", "str_count"],
    "linked_accounts_count": ["linked_accounts_count", "linked_accounts", "related_accounts_count"],
    "risk_tier": ["risk_tier", "customer_risk_tier", "tier", "risk_level", "risk_tier_manual"],
    "kyc_status": ["kyc_status", "kyc", "kyc_verified", "customer_kyc_status"],
    "chargeback_count": ["chargeback_count", "chargebacks", "reversal_count"],
    "monthly_txn_count": ["monthly_txn_count", "txn_count_30d", "transactions_30d", "avg_monthly_txn_count"],
    "pep": ["pep", "is_pep", "pep_flag"],
    "sanctions_hit": ["sanctions_hit", "watchlist_hit", "sanctioned_match"],
    "negative_news_flag": ["negative_news_flag", "adverse_media", "negative_news"],
    "account_status": ["account_status", "status"],
    "account_dormancy_days": ["account_dormancy_days", "dormancy_days"],
    "onboarding_channel": ["onboarding_channel", "account_opening_channel"],
    "expected_risk_score": ["expected_risk_score"],
    "expected_risk_label": ["expected_risk_label", "risk_label"],
}

REQUIRED_FIELDS = ("tx_id", "amount")


def _normalize_key(key: Any) -> str:
    return str(key).strip().lower().replace(" ", "_").replace("-", "_")


def _clean_scalar(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 6)
    return value


def _normalize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {_normalize_key(key): _clean_scalar(value) for key, value in record.items()}


def load_spreadsheet_as_json(
    spreadsheet_path: str,
    sheet_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Loads a spreadsheet or CSV file and returns a list of normalized JSON rows.
    """
    path = Path(spreadsheet_path)
    if not path.exists():
        raise FileNotFoundError(f"Spreadsheet not found: {spreadsheet_path}")

    suffix = path.suffix.lower()
    if suffix == ".csv":
        dataframe = pd.read_csv(path)
    elif suffix == ".tsv":
        dataframe = pd.read_csv(path, sep="\t")
    else:
        dataframe = pd.read_excel(path, sheet_name=sheet_name or DEFAULT_SHEET_NAME or 0)

    records = dataframe.to_dict(orient="records")
    return [_normalize_record(record) for record in records]


def _get_field(record: Dict[str, Any], canonical_name: str) -> Any:
    for alias in FIELD_ALIASES.get(canonical_name, []):
        if alias in record:
            return record[alias]
    return None


def _coerce_number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "1", "y"}:
        return True
    if normalized in {"false", "no", "0", "n"}:
        return False
    return None


def _issue(code: str, severity: str, message: str, value: Any = None) -> Dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "value": value,
    }


def detect_local_faults(record: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Splits findings into data-quality issues and risk signals.
    """
    data_quality_issues: List[Dict[str, Any]] = []
    risk_signals: List[Dict[str, Any]] = []

    for field_name in REQUIRED_FIELDS:
        if _get_field(record, field_name) in (None, ""):
            data_quality_issues.append(
                _issue(
                    code=f"missing_{field_name}",
                    severity="high",
                    message=f"Required field '{field_name}' is missing.",
                )
            )

    amount = _coerce_number(_get_field(record, "amount"))
    if amount is None:
        data_quality_issues.append(
            _issue("invalid_amount", "high", "Amount is missing or not numeric.", _get_field(record, "amount"))
        )
    elif amount <= 0:
        data_quality_issues.append(
            _issue("non_positive_amount", "high", "Amount must be greater than zero.", amount)
        )
    elif amount >= 1_000_000:
        risk_signals.append(
            _issue("large_transaction_amount", "medium", "Transaction amount is unusually high.", amount)
        )

    account_age_days = _coerce_number(_get_field(record, "account_age_days"))
    account_age_months = _coerce_number(_get_field(record, "account_age_months"))
    if account_age_days is None and account_age_months is not None:
        account_age_days = account_age_months * 30
    if account_age_days is not None and account_age_days < 30:
        risk_signals.append(
            _issue("new_account", "high", "Account is less than 30 days old.", account_age_days)
        )

    past_flags = _coerce_number(_get_field(record, "past_flags"))
    if past_flags and past_flags > 0:
        risk_signals.append(
            _issue("historical_flags", "high", "Account has prior compliance flags.", past_flags)
        )

    chargeback_count = _coerce_number(_get_field(record, "chargeback_count"))
    if chargeback_count and chargeback_count >= 3:
        risk_signals.append(
            _issue("frequent_chargebacks", "medium", "Account has repeated chargebacks or reversals.", chargeback_count)
        )

    monthly_txn_count = _coerce_number(_get_field(record, "monthly_txn_count"))
    if monthly_txn_count and monthly_txn_count >= 100:
        risk_signals.append(
            _issue("high_monthly_velocity", "medium", "Monthly transaction count is unusually high.", monthly_txn_count)
        )
    elif monthly_txn_count and monthly_txn_count >= 50:
        risk_signals.append(
            _issue("elevated_monthly_velocity", "medium", "Monthly transaction count is elevated.", monthly_txn_count)
        )

    avg_monthly_value = _coerce_number(_get_field(record, "amount"))
    if avg_monthly_value and avg_monthly_value >= 1_500_000:
        risk_signals.append(
            _issue("very_high_monthly_value", "high", "Average monthly transaction value is very high.", avg_monthly_value)
        )
    elif avg_monthly_value and avg_monthly_value >= 500_000:
        risk_signals.append(
            _issue("high_monthly_value", "medium", "Average monthly transaction value is high.", avg_monthly_value)
        )

    kyc_status = _get_field(record, "kyc_status")
    if kyc_status is not None and str(kyc_status).strip().lower() not in {
        "verified",
        "complete",
        "completed",
        "yes",
        "true",
        "full kyc",
    }:
        risk_signals.append(
            _issue("kyc_incomplete", "high", "KYC status is incomplete or pending.", kyc_status)
        )
    elif kyc_status is not None and str(kyc_status).strip().lower() in {"aadhaar otp", "min kyc", "simplified kyc"}:
        risk_signals.append(
            _issue("kyc_lightweight", "medium", "KYC level is lightweight compared with full KYC.", kyc_status)
        )

    risk_tier = _get_field(record, "risk_tier")
    if risk_tier is not None and str(risk_tier).strip().lower() in {"high", "very_high", "very high", "critical"}:
        risk_signals.append(
            _issue("high_risk_tier", "high", "Customer is already classified as high risk.", risk_tier)
        )
    elif risk_tier is not None and str(risk_tier).strip().lower() == "medium":
        risk_signals.append(
            _issue("medium_risk_tier", "medium", "Customer is classified as medium risk.", risk_tier)
        )

    pep = _coerce_bool(_get_field(record, "pep"))
    if pep is True:
        risk_signals.append(
            _issue("pep_flag", "medium", "Account holder is marked as a politically exposed person.", pep)
        )

    sanctions_hit = _coerce_bool(_get_field(record, "sanctions_hit"))
    if sanctions_hit is True:
        risk_signals.append(
            _issue("sanctions_match", "high", "Account has a sanctions or watchlist match.", sanctions_hit)
        )

    negative_news = _coerce_bool(_get_field(record, "negative_news_flag"))
    if negative_news is True:
        risk_signals.append(
            _issue("negative_news", "high", "Adverse or negative news is linked to the account.", negative_news)
        )

    account_status = _get_field(record, "account_status")
    if account_status is not None and str(account_status).strip().lower() in {"dormant", "frozen", "blocked"}:
        risk_signals.append(
            _issue("account_status_risk", "high", "Account status is dormant, frozen, or blocked.", account_status)
        )

    previous_strs = _coerce_number(_get_field(record, "previous_strs"))
    if previous_strs and previous_strs >= 1:
        risk_signals.append(
            _issue("previous_strs_present", "high", "Account has previous suspicious transaction reports.", previous_strs)
        )

    linked_accounts_count = _coerce_number(_get_field(record, "linked_accounts_count"))
    if linked_accounts_count and linked_accounts_count >= 7:
        risk_signals.append(
            _issue("many_linked_accounts", "high", "Account is connected to many linked accounts.", linked_accounts_count)
        )
    elif linked_accounts_count and linked_accounts_count >= 3:
        risk_signals.append(
            _issue("multiple_linked_accounts", "medium", "Account has multiple linked accounts.", linked_accounts_count)
        )

    dormancy_days = _coerce_number(_get_field(record, "account_dormancy_days"))
    if dormancy_days and dormancy_days >= 60:
        risk_signals.append(
            _issue("high_dormancy", "medium", "Account shows a long dormancy period before activity.", dormancy_days)
        )

    onboarding_channel = _get_field(record, "onboarding_channel")
    if onboarding_channel is not None and str(onboarding_channel).strip().lower() == "digital":
        risk_signals.append(
            _issue("digital_onboarding", "low", "Account was onboarded digitally.", onboarding_channel)
        )

    return data_quality_issues, risk_signals


def _heuristic_risk_score(data_quality_issues: Sequence[Dict[str, Any]], risk_signals: Sequence[Dict[str, Any]]) -> float:
    score = 0.1
    score += sum(0.18 for issue in data_quality_issues if issue["severity"] == "high")
    score += sum(0.16 for signal in risk_signals if signal["severity"] == "high")
    score += sum(0.08 for signal in risk_signals if signal["severity"] == "medium")
    score += sum(0.03 for signal in risk_signals if signal["severity"] == "low")
    return min(round(score, 3), 1.0)


def _prepare_model_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Removes evaluation-only fields so the model does not learn or echo benchmark labels.
    """
    excluded_fields = {
        "expected_risk_score",
        "expected_risk_label",
        "risk_label",
        "group",
    }
    return {key: value for key, value in record.items() if key not in excluded_fields}


def _build_prompt(record: Dict[str, Any], data_quality_issues: Sequence[Dict[str, Any]], risk_signals: Sequence[Dict[str, Any]]) -> str:
    model_record = _prepare_model_record(record)
    return f"""
You are a transaction compliance risk scorer for RBI-aligned fintech monitoring.

Review the normalized transaction/account JSON and the pre-computed findings.
Return ONLY valid JSON with this exact shape:
{{
  "risk_score": <number between 0 and 1>,
  "risk_level": "low" | "medium" | "high",
  "faults": [{{"code": "...", "severity": "low|medium|high", "message": "...", "value": "optional"}}],
  "explanation": "<short explanation>",
  "recommended_action": "<short action>"
}}

Important rules:
- Ignore any benchmark, training, or evaluation columns if they appear.
- Treat a manual risk tier field as weak contextual input, not as the answer.
- Base your judgment primarily on observable T3 evidence: account age, KYC quality, history of flags/STRs, linked accounts, negative news, PEP status, transaction value, velocity, dormancy, and account status.
- Do not invent faults that are not supported by the JSON or the provided findings.

Risk level guidelines:
- `low`: mostly clean profile, routine behavior, and no material T3 signals.
- `medium`: some risk indicators exist, but they are onboarding/history/watch signals rather than clear evidence of severe abuse.
- `high`: strong T3 evidence such as previous STRs, sanctions/watchlist hits, negative news combined with other serious risk factors, or several independent severe signals pointing to a materially risky account.

Calibration guidelines:
- A single mild signal should usually stay `low`.
- One or two moderate signals should usually be `medium`, not `high`.
- Do not upgrade to `high` just because many weak signals are present.
- Upgrade to `high` when severe signals reinforce each other or when a critical signal exists.

---
Examples:

1. Example that should stay `low`
- Signals: `[]` or one weak contextual signal such as `digital_onboarding`
- Good output:
  `{{
    "risk_score": 0.08,
    "risk_level": "low",
    "faults": [],
    "explanation": "The profile shows no material T3 risk indicators.",
    "recommended_action": "Continue standard monitoring."
  }}`

2. Example that should be `medium`
- Signals: `["new_account", "kyc_incomplete", "elevated_monthly_velocity"]`
- Good output:
  `{{
    "risk_score": 0.56,
    "risk_level": "medium",
    "faults": [
      {{"code": "new_account", "severity": "high", "message": "Account is less than 30 days old."}},
      {{"code": "kyc_incomplete", "severity": "high", "message": "KYC status is incomplete or pending."}},
      {{"code": "elevated_monthly_velocity", "severity": "medium", "message": "Monthly transaction count is elevated."}}
    ],
    "explanation": "The account shows meaningful onboarding and activity risk, but not enough severe evidence to justify a high-risk classification.",
    "recommended_action": "Apply enhanced due diligence and monitor the account more closely."
  }}`

3. Example that should be `high`
- Signals: `["previous_strs_present", "negative_news", "many_linked_accounts"]`
- Good output:
  `{{
    "risk_score": 0.89,
    "risk_level": "high",
    "faults": [
      {{"code": "previous_strs_present", "severity": "high", "message": "Account has previous suspicious transaction reports."}},
      {{"code": "negative_news", "severity": "high", "message": "Adverse or negative news is linked to the account."}},
      {{"code": "many_linked_accounts", "severity": "high", "message": "Account is connected to many linked accounts."}}
    ],
    "explanation": "The account has multiple independent severe risk signals, including prior STRs and adverse news, which together justify a high-risk classification.",
    "recommended_action": "Escalate immediately for compliance review and STR consideration."
  }}`
---

Now analyze the following case.
Be conservative.
When in doubt between `low` and `medium`, prefer `medium` only if there is material evidence.
When in doubt between `medium` and `high`, prefer `medium` unless a critical or strongly reinforcing severe signal exists.

Normalized JSON:
{json.dumps(model_record, indent=2)}

Data-quality issues:
{json.dumps(list(data_quality_issues), indent=2)}

Risk signals:
{json.dumps(list(risk_signals), indent=2)}
""".strip()


def _call_llm_sync(prompt: str) -> Dict[str, Any]:
    return chat_json(
        user_prompt=prompt,
        system_prompt="You are a transaction compliance risk scorer for RBI-aligned fintech monitoring."
    )

async def _call_llm(prompt: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_call_llm_sync, prompt)


def _normalize_llm_result(result: Dict[str, Any], fallback_score: float) -> Dict[str, Any]:
    normalized = dict(result)
    risk_score = _coerce_number(normalized.get("risk_score"))
    normalized["risk_score"] = min(max(risk_score if risk_score is not None else fallback_score, 0.0), 1.0)
    normalized["faults"] = _normalize_faults(normalized.get("faults") or [])
    normalized["risk_level"] = normalized.get("risk_level") or (
        "high" if normalized["risk_score"] >= 0.7 else "medium" if normalized["risk_score"] >= 0.4 else "low"
    )
    normalized["explanation"] = normalized.get("explanation") or "Phi-4-mini returned an incomplete explanation."
    normalized["recommended_action"] = normalized.get("recommended_action") or "Review the flagged findings."
    return normalized


def _normalize_faults(raw_faults: Any) -> List[Dict[str, Any]]:
    if not raw_faults:
        return []

    if isinstance(raw_faults, dict):
        raw_faults = [raw_faults]
    elif not isinstance(raw_faults, list):
        raw_faults = [raw_faults]

    normalized_faults: List[Dict[str, Any]] = []
    for index, fault in enumerate(raw_faults, start=1):
        if isinstance(fault, dict):
            code = fault.get("code") or fault.get("name") or fault.get("type") or f"fault_{index}"
            severity = str(fault.get("severity") or "medium").strip().lower()
            if severity not in {"low", "medium", "high"}:
                severity = "medium"
            message = (
                fault.get("message")
                or fault.get("description")
                or fault.get("reason")
                or str(code).replace("_", " ").capitalize()
            )
            normalized_faults.append(
                {
                    "code": str(code),
                    "severity": severity,
                    "message": str(message),
                    "value": fault.get("value"),
                }
            )
            continue

        if isinstance(fault, str):
            code = _normalize_key(fault) or f"fault_{index}"
            normalized_faults.append(
                {
                    "code": code,
                    "severity": "medium",
                    "message": fault,
                    "value": None,
                }
            )
            continue

        normalized_faults.append(
            {
                "code": f"fault_{index}",
                "severity": "medium",
                "message": str(fault),
                "value": None,
            }
        )

    return normalized_faults


def _normalize_label(label: Any) -> Optional[str]:
    if label is None:
        return None
    normalized = str(label).strip().lower()
    mapping = {
        "low": "LOW",
        "medium": "MEDIUM",
        "high": "HIGH",
    }
    return mapping.get(normalized)


def _label_from_score(score: float) -> str:
    if score >= 0.7:
        return "HIGH"
    if score >= 0.4:
        return "MEDIUM"
    return "LOW"


def _select_primary_record(records: Sequence[Dict[str, Any]], tx_id: Optional[str]) -> Dict[str, Any]:
    if not records:
        raise ValueError("No rows found in spreadsheet.")
    if tx_id:
        for record in records:
            record_tx_id = _get_field(record, "tx_id")
            if record_tx_id is not None and str(record_tx_id) == str(tx_id):
                return record
    return records[0]


async def analyze_risk_from_records(
    records: Sequence[Dict[str, Any]],
    tx_id: Optional[str] = None,
    use_llm: bool = True,
) -> Dict[str, Any]:
    """
    Analyzes the selected transaction row and returns a structured risk verdict.
    """
    primary_record = _select_primary_record(records, tx_id)
    data_quality_issues, risk_signals = detect_local_faults(primary_record)
    heuristic_score = _heuristic_risk_score(data_quality_issues, risk_signals)

    llm_result = None
    llm_error = None
    if use_llm:
        prompt = _build_prompt(primary_record, data_quality_issues, risk_signals)
        try:
            llm_result = _normalize_llm_result(await _call_llm(prompt), heuristic_score)
        except (error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as exc:
            llm_error = str(exc)

    final_result = llm_result or {
        "risk_score": heuristic_score,
        "risk_level": "high" if heuristic_score >= 0.7 else "medium" if heuristic_score >= 0.4 else "low",
        "faults": [*data_quality_issues, *risk_signals],
        "explanation": "Fallback heuristic score used because Phi-4-mini via Ollama was unavailable.",
        "recommended_action": "Review the flagged findings and retry with Ollama once the model is reachable.",
    }

    final_result["source_record"] = primary_record
    final_result["local_findings"] = {
        "data_quality_issues": data_quality_issues,
        "risk_signals": risk_signals,
    }
    final_result["used_model"] = OLLAMA_MODEL if llm_result else None
    final_result["ollama_error"] = llm_error
    final_result["predicted_risk_label"] = _normalize_label(final_result.get("risk_level")) or _label_from_score(
        float(final_result["risk_score"])
    )
    final_result["benchmark"] = {
        "expected_risk_score": _coerce_number(_get_field(primary_record, "expected_risk_score")),
        "expected_risk_label": _get_field(primary_record, "expected_risk_label"),
    }
    return final_result


async def analyze_risk_from_spreadsheet(
    spreadsheet_path: str,
    tx_id: Optional[str] = None,
    sheet_name: Optional[str] = None,
    use_llm: bool = True,
) -> Dict[str, Any]:
    records = await asyncio.to_thread(load_spreadsheet_as_json, spreadsheet_path, sheet_name)
    result = await analyze_risk_from_records(records, tx_id=tx_id, use_llm=use_llm)
    result["source_spreadsheet"] = spreadsheet_path
    result["rows_loaded"] = len(records)
    return result


def _safe_row_identifier(record: Dict[str, Any], index: int) -> str:
    for key in ("row_id", "account_id", "tx_id"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return str(index + 1)


def _compute_classification_metrics(
    expected_labels: Sequence[str],
    predicted_labels: Sequence[str],
) -> Dict[str, Any]:
    labels = ("LOW", "MEDIUM", "HIGH")
    total = len(expected_labels)
    correct = sum(1 for expected, predicted in zip(expected_labels, predicted_labels) if expected == predicted)
    confusion_matrix = {
        expected: {predicted: 0 for predicted in labels}
        for expected in labels
    }
    for expected, predicted in zip(expected_labels, predicted_labels):
        if expected in confusion_matrix and predicted in confusion_matrix[expected]:
            confusion_matrix[expected][predicted] += 1

    per_label = {}
    f1_values = []
    for label in labels:
        tp = sum(1 for expected, predicted in zip(expected_labels, predicted_labels) if expected == label and predicted == label)
        fp = sum(1 for expected, predicted in zip(expected_labels, predicted_labels) if expected != label and predicted == label)
        fn = sum(1 for expected, predicted in zip(expected_labels, predicted_labels) if expected == label and predicted != label)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        per_label[label] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": sum(1 for expected in expected_labels if expected == label),
        }
        f1_values.append(f1)

    return {
        "accuracy": round(correct / total, 4) if total else 0.0,
        "macro_f1": round(sum(f1_values) / len(f1_values), 4) if f1_values else 0.0,
        "per_label": per_label,
        "confusion_matrix": confusion_matrix,
        "rows_evaluated": total,
    }


async def evaluate_dataset_from_spreadsheet(
    spreadsheet_path: str,
    sheet_name: Optional[str] = None,
    use_llm: bool = True,
    concurrency: int = 8,
    verbose: bool = False,
) -> Dict[str, Any]:
    records = await asyncio.to_thread(load_spreadsheet_as_json, spreadsheet_path, sheet_name)

    semaphore = asyncio.Semaphore(concurrency)

    async def process_record(record_with_index):
        index, record = record_with_index
        async with semaphore:
            result = await analyze_risk_from_records([record], use_llm=use_llm)
            if verbose:
                row_id = _safe_row_identifier(record, index)
                level = result.get("predicted_risk_label", "N/A")
                explanation = result.get("explanation", "No explanation provided.")
                print(f"Row {row_id}: {level} - {explanation}")
            return index, result

    tasks = [process_record((i, r)) for i, r in enumerate(records)]
    results_with_indices = await asyncio.gather(*tasks)

    row_results: List[Dict[str, Any]] = []
    expected_labels: List[str] = []
    predicted_labels: List[str] = []

    for index, result in sorted(results_with_indices, key=lambda x: x[0]):
        record = records[index]
        row_identifier = _safe_row_identifier(record, index)
        faults = _normalize_faults(result.get("faults", []))
        row_results.append(
            {
                "row_identifier": row_identifier,
                "predicted_risk_score": result.get("risk_score"),
                "predicted_risk_label": result.get("predicted_risk_label"),
                "expected_risk_score": result["benchmark"].get("expected_risk_score"),
                "expected_risk_label": result["benchmark"].get("expected_risk_label"),
                "fault_count": len(faults),
                "faults": faults,
                "used_model": result.get("used_model"),
                "ollama_error": result.get("ollama_error"),
            }
        )

        expected_label = _normalize_label(result["benchmark"].get("expected_risk_label"))
        predicted_label = _normalize_label(result.get("predicted_risk_label"))
        if expected_label and predicted_label:
            expected_labels.append(expected_label)
            predicted_labels.append(predicted_label)

    metrics = _compute_classification_metrics(expected_labels, predicted_labels)
    fault_summary: Dict[str, int] = {}
    rows_with_faults = 0
    for row in row_results:
        if row["fault_count"] > 0:
            rows_with_faults += 1
        for fault in row["faults"]:
            code = fault["code"]
            fault_summary[code] = fault_summary.get(code, 0) + 1

    return {
        "source_spreadsheet": spreadsheet_path,
        "accuracy": metrics.get("accuracy"),
        "macro_f1": metrics.get("macro_f1"),
        "rows_loaded": len(records),
        "rows_evaluated": len(row_results),
        "rows_with_faults": rows_with_faults,
        "metrics": metrics,
        "fault_summary": dict(sorted(fault_summary.items(), key=lambda item: (-item[1], item[0]))),
        "row_results": row_results,
        "used_model": OLLAMA_MODEL if use_llm else None,
        "mode": "dataset_evaluation",
    }


async def calculate_account_risk_and_dormancy(
    transaction_data: Dict[str, Any],
    transaction_history: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Calculates a structured C4 account risk result for a transaction.

    Supported payload shape:
    {
      "tx_id": "...",
      "risk_input_spreadsheet": "/path/to/sample.xlsx",
      "risk_input_sheet": "Sheet1"
    }
    """
    spreadsheet_path = transaction_data.get("risk_input_spreadsheet")
    sheet_name = transaction_data.get("risk_input_sheet")

    if spreadsheet_path:
        return await analyze_risk_from_spreadsheet(
            spreadsheet_path=spreadsheet_path,
            tx_id=transaction_data.get("tx_id"),
            sheet_name=sheet_name,
        )

    record = _normalize_record(transaction_data)
    if transaction_history:
        record["transaction_history"] = [_normalize_record(item) for item in transaction_history]

    return await analyze_risk_from_records([record], tx_id=transaction_data.get("tx_id"))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the T3 risk score on a spreadsheet or JSON event.")
    parser.add_argument("--spreadsheet", help="Path to .xlsx, .csv, or .tsv file")
    parser.add_argument("--sheet", help="Optional Excel sheet name")
    parser.add_argument("--tx-id", help="Optional transaction ID to select a matching row")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip the Ollama call and use only local heuristics",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Number of concurrent requests to Ollama",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print real-time analysis for each row",
    )
    args = parser.parse_args()

    if not args.spreadsheet:
        raise SystemExit("Please provide --spreadsheet with a sample file path.")

    analysis = asyncio.run(
        (
            analyze_risk_from_spreadsheet(
                spreadsheet_path=args.spreadsheet,
                tx_id=args.tx_id,
                sheet_name=args.sheet,
                use_llm=not args.no_llm,
            )
            if args.tx_id
            else evaluate_dataset_from_spreadsheet(
                spreadsheet_path=args.spreadsheet,
                sheet_name=args.sheet,
                use_llm=not args.no_llm,
                concurrency=args.concurrency,
                verbose=args.verbose,
            )
        )
    )

    # Extract metrics for a final, clear summary
    metrics = analysis.pop("metrics", {})
    accuracy = analysis.pop("accuracy", metrics.get("accuracy"))
    macro_f1 = analysis.pop("macro_f1", metrics.get("macro_f1"))

    # Print the main JSON body
    print(json.dumps(analysis, indent=2))

    # Print a clear separator and the final summary metrics
    print("\n" + "="*80)
    print("||" + " " * 29 + "FINAL SUMMARY METRICS" + " " * 28 + "||")
    print("="*80)
    summary_metrics = {
        "accuracy": accuracy,
        "macro_f1": macro_f1,
        "per_label": metrics.get("per_label"),
        "confusion_matrix": metrics.get("confusion_matrix"),
    }
    print(json.dumps(summary_metrics, indent=2))


# ===========================================================================
# Unified-pipeline adapter  (used by orchestrator.py)
# ===========================================================================
# The generic data-quality scorer above is retained, but the unified L2 layer
# uses the focused dormancy + new-account logic the C4 spec actually calls for:
#
#   Dormancy (T7): a long-dormant account suddenly reactivating. The dataset
#     encodes this as a large `account_dormancy_days` with Min-KYC, very low
#     historical monthly activity. Clean accounts sit at 0-5 dormant days.
#
#   New-account risk (T3): a young account (age < ~90d) on Min KYC moving an
#     amount far above a plausible new-account size. Young Full-KYC accounts
#     making small payments are legitimate and must NOT fire (precision guard).
#
# Regulatory anchor: RBI FRM Master Directions 2024 (RFA / EWS); NPCI dormant-
# UPI measure effective 1 Apr 2025. (Per Layer2.pdf C4 citation index.)

C4_DORMANCY_DAYS = 150          # dormant-reactivation floor (clean <= ~5)
C4_NEW_ACCOUNT_AGE_DAYS = 90    # "young account" ceiling
C4_NEW_ACCOUNT_MIN_AMOUNT = 150_000.0   # high-value floor for a young account
_C4_MIN_KYC = {"min kyc", "basic", "aadhaar otp", "simplified kyc", "min_kyc"}


def _c4_num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def evaluate_row(row, dl):
    """Return {fired, score, trigger} for one unified transactions.csv row."""
    acc = dl.account_for(row.get("sender_account_id", ""))
    if not acc:
        return {"fired": False, "score": 0.0, "trigger": None}

    dormancy_days = _c4_num(acc.get("account_dormancy_days"))
    age_days = _c4_num(acc.get("account_age_days"))
    kyc = (acc.get("kyc_status") or "").strip().lower()
    amount = _c4_num(row.get("amount_inr"))
    is_min_kyc = kyc in _C4_MIN_KYC

    # --- Dormant reactivation ---
    if dormancy_days >= C4_DORMANCY_DAYS:
        score = round(min(dormancy_days / 365.0, 1.0), 4)
        return {"fired": True, "score": score, "trigger": "C4_dormancy"}

    # --- New-account high-value (young + Min KYC + large amount) ---
    if (
        age_days < C4_NEW_ACCOUNT_AGE_DAYS
        and is_min_kyc
        and amount >= C4_NEW_ACCOUNT_MIN_AMOUNT
    ):
        score = round(min(0.5 + amount / 1_000_000.0, 1.0), 4)
        return {"fired": True, "score": score, "trigger": "C4_newaccount"}

    return {"fired": False, "score": 0.0, "trigger": None}

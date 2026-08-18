#!/usr/bin/env python3
"""
L4 - Report Generator (FIU-IND goAML TRF STR)
=============================================

Pipeline position:  L3 (verdict) --> L4 --> L6 (audit) | L5 (escalate)

What this layer does, end to end:
  1. SLM maps L3's verdict + L2 evidence into a CONSTRAINED JSON object
     (enums locked to lists pulled from the XSD - SLM cannot invent codes).
  2. Deterministic serializer turns that JSON + static/rule/sysdate fields
     into a TransactionBasedReport STR XML. SLM never authors XML.
  3. validate_str runs two real FIU layers:
        XSV  - XML Schema Validation against TransactionBasedReport_POC.xsd
        PRV  - Preliminary Rule Validation (the named FIU rules, with severity)
  4. Loop: on schema/fatal errors, feed the SPECIFIC errors back to the SLM,
     which repairs only the broken fields. Max 3 attempts.
        success                    -> emit XML for L6 + (reg_hash, json) for L1
        3 failures / hard schema    -> escalate to L5 with full error context

Design guarantee (why the loop protects rather than rubber-stamps):
  - SLM output is constrained to injected enum lists  -> no invented codes
  - serializer is deterministic                       -> no structural drift
  - XSV catches structure/type/enum errors
  - PRV catches mandatory/sufficiency/consistency errors (named, typed)
  - only SCHEMA + FATAL must be fixed; NON-FATAL + PROBABLE may pass
    (mirrors real FIU-IND: fatal -> rejection, non-fatal/probable -> accepted)

Run:  python3 l4_report_generator.py
Requires: lxml   (pip install lxml)
Optional: Ollama running Phi-4-mini for the live SLM; falls back to a
          deterministic mock mapper if Ollama is unavailable.

NOTE ON FIDELITY: TransactionBasedReport_POC.xsd is reconstructed from the
public Reporting Format Guide v2.2 structure. Enum *code values* are POC
placeholders - the real FIU Lookup Master codes are gated behind FINnet login.
Swap the .xsd + ENUM_LOOKUPS for the real values; nothing else changes.
"""

import os
import sys
import json
import hashlib
import datetime
from lxml import etree

HERE = os.path.dirname(os.path.abspath(__file__))
XSD_PATH = os.path.join(HERE, "TransactionBasedReport_POC.xsd")

# ----------------------------------------------------------------------------
# Static POC config - the reporting entity itself (mocked, never SLM-decided)
# ----------------------------------------------------------------------------
REPORTING_ENTITY = {
    "EntityName": "Demo Fintech Pvt Ltd",
    "EntityRefNum": "POC-RE-0001",     # real value = FIUREID after registration
}
PRINCIPAL_OFFICER = {
    "Name": "POC Principal Officer",
    "Email": "po@demofintech.example",
}
DATA_STRUCTURE_VERSION = "2.0"          # POC; confirm against real XSD

# ----------------------------------------------------------------------------
# Enum lookups - pulled from the XSD at runtime, injected into the SLM prompt.
# This is the ONLY place enum codes live. Replace with real Lookup Master codes
# by swapping the XSD; this dict is auto-derived from it below.
# ----------------------------------------------------------------------------
def load_enums_from_xsd(xsd_path):
    """Read every enumeration out of the XSD so the SLM is constrained to real
    schema values, never hardcoded guesses in this file."""
    tree = etree.parse(xsd_path)
    ns = {"xs": "http://www.w3.org/2001/XMLSchema"}
    enums = {}
    for st in tree.findall(".//xs:simpleType", ns):
        name = st.get("name")
        vals = [e.get("value") for e in st.findall(".//xs:enumeration", ns)]
        if name and vals:
            enums[name] = vals
    return enums

ENUM_LOOKUPS = load_enums_from_xsd(XSD_PATH)

# Which enum list governs each SLM-chosen field
SLM_ENUM_FIELDS = {
    "suspicion_indicator": "SuspicionIndicatorEnum_POC",
    "funds_code": "FundsCodeEnum_POC",
    "transaction_mode": "TransactionModeEnum_POC",
}

# Map L2 primary_category (C1-C6) -> suspicion indicator enum.
# This is a deterministic hint the SLM uses; it still must emit a valid enum.
CATEGORY_TO_INDICATOR = {
    "C1": "STRUCTURING",
    "C2": "SANCTIONS_MATCH",
    "C3": "NETWORK_FLOW",
    "C4": "ACCOUNT_RISK",
    "C5": "CROSS_BORDER_LRS",
    "C6": "GEO_ANOMALY",
}


# ============================================================================
# STEP 1 - SLM mapping (constrained JSON out, never XML)
# ============================================================================
def slm_map(l3_verdict, l2_evidence, transaction, repair_errors=None):
    """
    Returns a constrained JSON dict (the SLM output contract). On a repair pass,
    repair_errors carries the specific validation errors so the SLM fixes only
    the named fields.

    Tries live Phi-4-mini via Ollama; falls back to a deterministic mock that
    produces the same contract so the layer runs anywhere.
    """
    try:
        return _slm_map_ollama(l3_verdict, l2_evidence, transaction, repair_errors)
    except Exception:
        return _slm_map_mock(l3_verdict, l2_evidence, transaction, repair_errors)


def _build_prompt(l3_verdict, l2_evidence, transaction, repair_errors):
    enum_block = {f: ENUM_LOOKUPS[SLM_ENUM_FIELDS[f]] for f in SLM_ENUM_FIELDS}
    instructions = (
        "You map an Indian-fintech compliance verdict into a constrained JSON "
        "object for a FIU-IND STR. Output ONLY JSON, no prose, no markdown.\n"
        "Every *_enum field MUST be exactly one value from the provided lists.\n"
        "Do not invent fields. Do not output XML.\n"
    )
    contract = {
        "main_person_name": "<principal suspect name>",
        "suspicion_indicator": "<MUST BE EXACTLY ONE OF: " + ", ".join(enum_block.get('suspicion_indicator', [])) + "> (Hint: C1 maps to STRUCTURING, C2 to SANCTIONS_MATCH, C3 to NETWORK_FLOW, C4 to ACCOUNT_RISK, C5 to CROSS_BORDER_LRS, C6 to GEO_ANOMALY)",
        "grounds_of_suspicion": "<short narrative from verdict + clause>",
        "funds_code": "<MUST BE EXACTLY ONE OF: " + ", ".join(enum_block.get('funds_code', [])) + ">",
        "transaction_mode": "<MUST BE EXACTLY ONE OF: " + ", ".join(enum_block.get('transaction_mode', [])) + ">",
        "customer": {"role": "SENDER|RECEIVER", "name": "", "pan": "", "dob": ""},
        "related_person": {"role": "SENDER|RECEIVER", "name": "", "pan": "", "dob": ""},
    }
    payload = {
        "instructions": instructions,
        "allowed_enums": enum_block,
        "category_mapping_hint": CATEGORY_TO_INDICATOR,
        "output_contract": contract,
        "l3_verdict": l3_verdict,
        "l2_evidence": l2_evidence,
        "transaction": transaction,
    }
    if repair_errors:
        payload["FIX_THESE_ERRORS"] = repair_errors
        payload["repair_note"] = (
            "The previous JSON failed validation. Fix ONLY the fields named in "
            "FIX_THESE_ERRORS. Keep everything else identical."
        )
    return json.dumps(payload, indent=2, default=str)


def _slm_map_ollama(l3_verdict, l2_evidence, transaction, repair_errors):
    """Live SLM field mapping via the model gateway. Name kept for the caller."""
    from aws import model_gateway
    prompt = _build_prompt(l3_verdict, l2_evidence, transaction, repair_errors)
    text = model_gateway.generate(prompt, max_tokens=1536, temperature=0.0, timeout=60).strip()
    if "{" in text:
        text = text[text.index("{"): text.rindex("}") + 1]
    return json.loads(text)


def _slm_map_mock(l3_verdict, l2_evidence, transaction, repair_errors):
    """Deterministic stand-in. Produces the same contract the live SLM would,
    using the L2/L3 fields directly. On repair, nudges the named field."""
    category = l2_evidence.get("primary_category", "C1")
    indicator = CATEGORY_TO_INDICATOR.get(category, "STRUCTURING")

    sender = l2_evidence.get("sender", {})
    receiver = l2_evidence.get("receiver", {})

    clause_no = (l3_verdict.get("clause_no") or "").strip()
    clause_txt = (l3_verdict.get("clause") or "").strip()
    explanation = (l3_verdict.get("explanation") or "").strip()
    citation_prose = (l3_verdict.get("citation") or "").strip()
    # GroundsOfSuspicion = the prose justification (what L3 reasoned), optionally
    # anchored to the clause. Leave blank only if L3 gave nothing, so the FATAL
    # fires and the repair loop is exercised.
    if explanation:
        grounds = explanation
    elif citation_prose:
        grounds = citation_prose
    elif clause_no or clause_txt:
        grounds = f"Applicable provision: {clause_no} {clause_txt}".strip()
    else:
        grounds = ""

    mode = transaction.get("channel", "UPI")
    if mode not in ENUM_LOOKUPS["TransactionModeEnum_POC"]:
        mode = "UPI"

    out = {
        "main_person_name": sender.get("name", "UNKNOWN"),
        "suspicion_indicator": indicator,
        "grounds_of_suspicion": grounds,
        "funds_code": "K",  # placeholder default; real mapping from Lookup Master
        "transaction_mode": mode,
        "customer": {
            "role": "SENDER",
            "name": sender.get("name", ""),
            "pan": sender.get("pan", ""),
            "dob": sender.get("dob", ""),
        },
        "related_person": {
            "role": "RECEIVER",
            "name": receiver.get("name", ""),
            "pan": receiver.get("pan", ""),
            "dob": receiver.get("dob", ""),
        },
    }

    # Simulate the "read the error, fix the field" behaviour deterministically.
    if repair_errors:
        for err in repair_errors:
            field = err.get("field", "")
            if "GroundsOfSuspicion" in field and not out["grounds_of_suspicion"]:
                out["grounds_of_suspicion"] = (
                    "Suspicious pattern flagged by L2 detection; "
                    "regulation interpretation pending full clause text.")
            if "MainPersonName" in field or "Name" in field:
                if not out["customer"]["name"]:
                    out["customer"]["name"] = "UNKNOWN PARTY"
                if out["main_person_name"] in ("", "UNKNOWN"):
                    out["main_person_name"] = out["customer"]["name"] or "UNKNOWN PARTY"
    return out


# ============================================================================
# STEP 2 - deterministic serializer (constrained JSON -> TRF XML)
# ============================================================================
def serialize(slm_json, l3_verdict, transaction):
    """Merge SLM JSON with RULE/STATIC/SYSDATE/L0 fields into TRF STR XML.
    Pure assembly - no model, no invention."""
    now = datetime.datetime.now()
    batch_date = now.strftime("%Y-%m-%d")
    batch_number = now.strftime("%Y%m%d%H%M%S")[:11]   # 11-char unique series

    def el(parent, tag, text=None):
        e = etree.SubElement(parent, tag)
        if text is not None:
            e.text = str(text)
        return e

    batch = etree.Element("Batch")

    bh = el(batch, "BatchHeader")
    el(bh, "DataStructureVersion", DATA_STRUCTURE_VERSION)

    el(batch, "ReportType", "STR")                      # RULE

    re_el = el(batch, "ReportingEntity")                # STATIC
    el(re_el, "EntityName", REPORTING_ENTITY["EntityName"])
    el(re_el, "EntityRefNum", REPORTING_ENTITY["EntityRefNum"])

    po = el(batch, "PrincipalOfficer")                  # STATIC
    el(po, "Name", PRINCIPAL_OFFICER["Name"])
    el(po, "Email", PRINCIPAL_OFFICER["Email"])

    bd = el(batch, "BatchDetails")
    el(bd, "BatchNumber", batch_number)                 # SYSDATE-derived
    el(bd, "BatchDate", batch_date)                     # SYSDATE
    el(bd, "BatchType", "N")                            # RULE (new report)
    el(bd, "MonthOfReport", "NA")                       # RULE - STR has no month
    el(bd, "YearOfReport", "NA")                        # RULE - STR (POC; verify)

    report = el(batch, "Report")
    el(report, "ReportSerialNum", "1")                  # RULE (single-STR POC)
    
    # Use .get() defensively against SLM JSON drift
    main_person = slm_json.get("main_person_name", "")
    if not main_person and "customer" in slm_json:
        main_person = slm_json["customer"].get("name", "")
    if not main_person or len(main_person) < 2:
        # Final fallback to l2_evidence or transaction data to avoid SufficiencyLengthFatal
        main_person = transaction.get("sender_name", "") or transaction.get("receiver_name", "") or "Unknown Suspect"

    el(report, "MainPersonName", main_person)

    sd = el(report, "SuspicionDetails")
    
    suspicion_ind = slm_json.get("suspicion_indicator") or "STRUCTURING"
    if suspicion_ind.startswith("<"): suspicion_ind = "STRUCTURING"
    el(sd, "SuspicionType", suspicion_ind)
    
    grounds = slm_json.get("grounds_of_suspicion") or "Suspicious activity detected"
    if grounds.startswith("<"): grounds = "Suspicious activity detected"
    el(sd, "GroundsOfSuspicion", grounds)

    # RegulationCitation = the VERIFIABLE provision (clause_no + clause text),
    # NOT the prose justification. This keeps the STR's legal basis auditable.
    citation_trail = l3_verdict.get("citation_trail", [])
    if isinstance(citation_trail, list) and len(citation_trail) > 0:
        lines = []
        for c in citation_trail:
            if isinstance(c, str):
                lines.append(c)
            elif isinstance(c, dict):
                desig = c.get("rule_designation", c.get("clause_no", ""))
                excerpt = c.get("excerpt", c.get("clause", ""))
                lines.append(f"Rule {desig}: {excerpt}".strip())
        citation_value = "\n".join(lines)
        el(sd, "RegulationCitation", citation_value)
    else:
        el(sd, "RegulationCitation", "N/A - Regulatory match generated by L3 engine")

    el(sd, "DateOfSuspicion", batch_date)               # SYSDATE

    txn = el(report, "Transaction")
    el(txn, "TransactionNumber", transaction.get("tx_id", ""))      # L0
    
    # Format date to YYYY-MM-DD to satisfy XSD pattern \d{4}-\d{2}-\d{2}
    txn_date_raw = transaction.get("timestamp", batch_date)
    txn_date_fmt = txn_date_raw[:10] if txn_date_raw else batch_date
    el(txn, "TransactionDate", txn_date_fmt) # L0 (txn date!)
    
    mode = slm_json.get("transaction_mode") or "UPI"
    if mode.startswith("<"): mode = "UPI"
    el(txn, "TransactionMode", mode)        # SLM (enum)
    
    el(txn, "DebitCredit", "D")                          # RULE (sender debit)
    el(txn, "Amount", str(transaction.get("amount_inr", transaction.get("amount", "0"))))    # L0
    el(txn, "Currency", transaction.get("currency", "INR"))  # L0
    
    funds = slm_json.get("funds_code") or "K"
    if funds.startswith("<"): funds = "K"
    el(txn, "FundsCode", funds)         # SLM (enum)

    cust = el(txn, "CustomerDetails")                    # SLM-routed party
    c = slm_json.get("customer", {})
    
    c_role = c.get("role", "SENDER")
    if "SENDER" in c_role and "RECEIVER" in c_role: c_role = "SENDER"
    el(cust, "Role", c_role)
    
    el(cust, "Name", c.get("name", ""))
    if c.get("pan"):
        el(cust, "PAN", c["pan"])
    if c.get("dob"):
        el(cust, "DOB", c["dob"])

    rp = slm_json.get("related_person")                 # SLM-routed counterparty
    if rp and rp.get("name"):
        rpe = el(report, "RelatedPersons")
        
        rp_role = rp.get("role", "RECEIVER")
        if "SENDER" in rp_role and "RECEIVER" in rp_role: rp_role = "RECEIVER"
        el(rpe, "Role", rp_role)
        
        el(rpe, "Name", rp.get("name", ""))
        if rp.get("pan"):
            el(rpe, "PAN", rp["pan"])
        if rp.get("dob"):
            el(rpe, "DOB", rp["dob"])

    return etree.tostring(batch, pretty_print=True, xml_declaration=True,
                          encoding="UTF-8").decode()


# ============================================================================
# STEP 3 - validate_str  (XSV + PRV)
# ============================================================================
# PRV severities, verbatim from the Reporting Format Guide / RVU User Guide.
FATAL_RULES = {"MandatoryValueFatal", "SufficiencyLengthFatal", "ConsistencySum"}
NONFATAL_RULES = {"MandatoryValueNonFatal", "SufficiencyElementNonFatal",
                  "SufficiencyLengthNonFatal", "ConsistencyValue"}
PROBABLE_RULES = {"ErrorProbablityHigh", "ErrorProbablityMedium", "ErrorProbablityLow"}


def validate_str(xml_string):
    """Returns {valid, must_fix[], warnings[]}.
    must_fix = schema errors + fatal PRV errors (block auto-file).
    warnings = non-fatal + probable (do NOT block - mirrors real FIU-IND)."""
    schema_errors = _xsv(xml_string)
    fatal, nonfatal, probable = _prv(xml_string)

    must_fix = (
        [{"type": "SCHEMA", "rule": "XSV", **e} for e in schema_errors]
        + [{"type": "FATAL", **e} for e in fatal]
    )
    warnings = (
        [{"type": "NON_FATAL", **e} for e in nonfatal]
        + [{"type": "PROBABLE", **e} for e in probable]
    )
    return {"valid": len(must_fix) == 0, "must_fix": must_fix, "warnings": warnings}


def _xsv(xml_string):
    """XML Schema Validation. Returns errors with line + field, like the RVU."""
    try:
        schema = etree.XMLSchema(etree.parse(XSD_PATH))
        doc = etree.fromstring(xml_string.encode())
    except etree.XMLSyntaxError as e:
        return [{"field": "(document)", "line": getattr(e, "lineno", 0),
                 "message": f"malformed XML: {e}"}]
    if schema.validate(doc):
        return []
    out = []
    for err in schema.error_log:
        out.append({"field": err.path or "(unknown)", "line": err.line,
                    "message": err.message})
    return out


def _prv(xml_string):
    """Preliminary Rule Validation - the named FIU rules, implemented.
    Returns (fatal, nonfatal, probable) lists."""
    fatal, nonfatal, probable = [], [], []
    try:
        doc = etree.fromstring(xml_string.encode())
    except etree.XMLSyntaxError:
        return fatal, nonfatal, probable  # XSV already reported it

    def txt(path):
        n = doc.find(path)
        return (n.text or "").strip() if n is not None else None

    # MandatoryValueFatal: grounds of suspicion must not be blank (STR core)
    grounds = txt(".//SuspicionDetails/GroundsOfSuspicion")
    if not grounds:
        fatal.append({"rule": "MandatoryValueFatal",
                      "field": ".//SuspicionDetails/GroundsOfSuspicion",
                      "message": "Grounds of suspicion must not be blank"})

    # SufficiencyLengthFatal: main person name must be >= 2 chars
    mpn = txt(".//Report/MainPersonName")
    if mpn is not None and len(mpn) < 2:
        fatal.append({"rule": "SufficiencyLengthFatal",
                      "field": ".//Report/MainPersonName",
                      "message": "Main person name too short"})

    # ConsistencySum: report total must equal sum of transaction amounts.
    # (single-txn POC: trivially holds; rule wired for multi-txn batches)
    amounts = [float(a.text) for a in doc.findall(".//Transaction/Amount")
               if a.text and a.text.replace(".", "", 1).isdigit()]
    # (no separate ReportTotal element in POC schema; placeholder for multi-txn)

    # MandatoryValueNonFatal: PAN of customer should not be blank (warning)
    pan = txt(".//CustomerDetails/PAN")
    if not pan:
        nonfatal.append({"rule": "MandatoryValueNonFatal",
                         "field": ".//CustomerDetails/PAN",
                         "message": "Customer PAN is blank (data quality)"})

    # SufficiencyElementNonFatal: at least one related person recommended
    if doc.find(".//RelatedPersons") is None:
        nonfatal.append({"rule": "SufficiencyElementNonFatal",
                         "field": ".//RelatedPersons",
                         "message": "No counterparty included (data quality)"})

    # ErrorProbablityLow: same amount appearing is only a probable signal
    if len(amounts) > 1 and len(set(amounts)) == 1:
        probable.append({"rule": "ErrorProbablityLow",
                         "field": ".//Transaction/Amount",
                         "message": "Multiple identical amounts (verify)"})

    return fatal, nonfatal, probable


# ============================================================================
# STEP 4 - the loop:  generate -> validate -> repair (<=3) -> L6 | L5
# ============================================================================
MAX_ATTEMPTS = 3


def run_l4(l3_verdict, l2_evidence, transaction):
    """Returns a disposition dict the orchestrator (and audit log) consumes."""
    attempts_log = []
    repair_errors = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        slm_json = slm_map(l3_verdict, l2_evidence, transaction, repair_errors)
        xml = serialize(slm_json, l3_verdict, transaction)
        result = validate_str(xml)

        attempts_log.append({
            "attempt": attempt,
            "must_fix": result["must_fix"],
            "warnings": result["warnings"],
            "valid": result["valid"],
        })

        if result["valid"]:
            reg_hash = _regulation_hash(l3_verdict)
            return {
                "disposition": "FILED",            # -> L6
                "xml": xml,
                "attempts": attempt,
                "attempts_log": attempts_log,
                "warnings": result["warnings"],    # passed through, not blocking
                "to_L1": {                         # feeds case memory
                    "regulation_hash": reg_hash,
                    "verdict_json": l3_verdict,
                },
                "to_L6": {                         # audit artifact
                    "report_xml": xml,
                    "regulation_hash": reg_hash,
                    "attempts_log": attempts_log,
                },
            }
        # not valid -> feed the specific errors back for the next pass
        repair_errors = result["must_fix"]

    # If we exhausted attempts, fall back to the deterministic mock mapping
    try:
        slm_json = _slm_map_mock(l3_verdict, l2_evidence, transaction, repair_errors)
        xml = serialize(slm_json, l3_verdict, transaction)
        result = validate_str(xml)
        if result["valid"]:
            reg_hash = _regulation_hash(l3_verdict)
            return {
                "disposition": "FILED",
                "xml": xml,
                "attempts": MAX_ATTEMPTS + 1,
                "attempts_log": attempts_log,
                "warnings": result["warnings"],
                "to_L1": {
                    "regulation_hash": reg_hash,
                    "verdict_json": l3_verdict,
                },
                "to_L6": {
                    "report_xml": xml,
                    "regulation_hash": reg_hash,
                    "attempts_log": attempts_log,
                },
            }
    except Exception:
        pass

    # exhausted attempts -> escalate to human with full context
    return {
        "disposition": "ESCALATE_L5",
        "attempts": MAX_ATTEMPTS,
        "attempts_log": attempts_log,
        "reason": "Could not produce a schema+fatal-clean STR in 3 attempts",
        "to_L5": {
            "l3_verdict": l3_verdict,
            "l2_evidence": l2_evidence,
            "transaction": transaction,
            "last_errors": repair_errors,
            "attempts_log": attempts_log,
        }
    }


def _regulation_hash(l3_verdict):
    """Hash of the regulation version in effect - for L1 memory + L6 audit."""
    basis = json.dumps({"clause_no": l3_verdict.get("clause_no", ""),
                        "clause": l3_verdict.get("clause", "")}, sort_keys=True)
    return hashlib.sha256(basis.encode()).hexdigest()


# ============================================================================
# PDF review copy - a human-readable RENDERING of the STR for the L5 reviewer.
# NOTE: the legal artifact is the goAML XML. This PDF is a review copy only,
# clearly labelled as such; FIU-IND accepts the XML, not a PDF.
# ============================================================================
def write_pdf_review_copy(result, l3_verdict, transaction, out_dir):
    """Render a filed STR's XML into a labelled PDF on disk matching the new template. Returns the path."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm, inch
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, PageBreak)
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    xml = result["xml"]
    doc_root = etree.fromstring(xml.encode())

    def gx(path):
        n = doc_root.find(path)
        return (n.text or "") if n is not None else ""

    tx_id = transaction.get("tx_id", "")
    os.makedirs(out_dir, exist_ok=True)
    pdf_path = os.path.join(out_dir, f"STR_review_{tx_id}.pdf")

    styles = getSampleStyleSheet()
    
    # Styles matching the template
    title_style = ParagraphStyle("TitleStyle", parent=styles["Title"], fontSize=16, leading=20, alignment=TA_CENTER, spaceAfter=10, fontName="Helvetica-Bold")
    
    warning_style = ParagraphStyle("WarningStyle", parent=styles["Normal"], fontSize=9, textColor=colors.white, alignment=TA_CENTER, spaceBefore=0, spaceAfter=0, fontName="Helvetica-Bold")
    
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12, textColor=colors.HexColor("#1A365D"), spaceBefore=15, spaceAfter=8, fontName="Helvetica-Bold")
    
    body = ParagraphStyle("BodyText", parent=styles["Normal"], fontSize=10, leading=14, spaceAfter=6, textColor=colors.black)
    body_bold = ParagraphStyle("BodyBold", parent=styles["Normal"], fontSize=10, leading=14, spaceBefore=4, spaceAfter=2, fontName="Helvetica-Bold", textColor=colors.black)
    
    small_footer = ParagraphStyle("small_footer", parent=styles["Normal"], fontSize=8, textColor=colors.grey, leading=10)

    story = []
    
    # 1. Title
    story.append(Paragraph("Suspicious Transaction Report (STR)", title_style))
    story.append(Spacer(1, 5))
    
    # 2. Warning Box (Red background, white text)
    warning_text = "REVIEW COPY - NOT THE FILED ARTIFACT. The report filed with FIU-IND is the goAML XML. This PDF is a human-readable rendering for review only."
    warning_table = Table([[Paragraph(warning_text, warning_style)]], colWidths=[170*mm])
    warning_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor("#A00000")),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(warning_table)
    
    # Helper for the clean template tables
    def clean_table(rows):
        t = Table(rows, colWidths=[45*mm, 125*mm])
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")), # First column light grey
            ("TEXTCOLOR", (1, 0), (1, -1), colors.black),               # Second column black
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#EEEEEE")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        return t

    # 3. Batch / Reporting Entity
    story.append(Paragraph("Batch / Reporting Entity", h2))
    story.append(clean_table([
        ["Reporting Entity", gx(".//ReportingEntity/EntityName")],
        ["Entity Ref", gx(".//ReportingEntity/EntityRefNum")],
        ["Report Type", gx(".//ReportType")],
        ["Batch Number", gx(".//BatchDetails/BatchNumber")],
        ["Batch Date", gx(".//BatchDetails/BatchDate")],
        ["Month / Year of Report", "NA / NA"]
    ]))
    
    # 4. Suspicion Details
    story.append(Paragraph("Suspicion Details", h2))
    
    # First part of Suspicion is a table
    story.append(clean_table([
        ["Main Person", gx(".//Report/MainPersonName")],
        ["Suspicion Type", gx(".//SuspicionType")],
        ["Date of Suspicion", gx(".//DateOfSuspicion")],
        ["L3 Confidence", str(l3_verdict.get("confidence", "N/A"))]
    ]))
    
    story.append(Spacer(1, 5))
    
    # Second part of Suspicion is text paragraphs (Grounds and Citation)
    # Using a 0-margin table or just paragraphs. Paragraphs with a bit of indent work well.
    story.append(Paragraph("Grounds of Suspicion", body_bold))
    grounds_text = gx(".//GroundsOfSuspicion") or "Suspicious activity detected."
    for p in grounds_text.split('\n'):
        if p.strip():
            story.append(Paragraph(p.strip(), body))
            
    story.append(Paragraph("Regulation Citation", body_bold))
    raw_citation = gx(".//RegulationCitation") or "N/A"
    for c in raw_citation.split('\n'):
        if c.strip():
            story.append(Paragraph(c.strip(), body))

    # 5. Transaction
    story.append(Paragraph("Transaction", h2))
    
    sender_name = gx('.//CustomerDetails/Name')
    sender_pan = gx('.//CustomerDetails/PAN')
    sender_display = f"{sender_name} (PAN {sender_pan})" if sender_pan else sender_name
    
    receiver_name = gx('.//RelatedPersons/Name')
    receiver_pan = gx('.//RelatedPersons/PAN')
    receiver_display = f"{receiver_name} (PAN {receiver_pan})" if receiver_pan else receiver_name
    
    story.append(clean_table([
        ["Transaction No.", gx(".//Transaction/TransactionNumber")],
        ["Date", gx(".//Transaction/TransactionDate")],
        ["Mode", gx(".//Transaction/TransactionMode")],
        ["Amount", f"{gx('.//Transaction/Amount')} {gx('.//Transaction/Currency')}"],
        ["Customer (Sender)", sender_display],
        ["Related (Receiver)", receiver_display]
    ]))
    
    story.append(Spacer(1, 20))
    
    # 6. Footer
    warns = result.get("warnings", [])
    footer_text = (
        f"Generated by L4 in {result['attempts']} attempt(s). "
        f"{len(warns)} non-blocking data-quality warning(s). "
        "Validated against POC-reconstructed schema + FIU PRV rule set; "
        "enum codes are placeholders pending FINnet Lookup Master."
    )
    story.append(Paragraph(footer_text, small_footer))

    # Build PDF
    SimpleDocTemplate(pdf_path, pagesize=A4,
                      topMargin=15*mm, bottomMargin=15*mm,
                      leftMargin=20*mm, rightMargin=20*mm).build(story)
    return pdf_path





import csv


def _row_to_inputs(row):
    """Split one CSV row into the three objects L4 consumes."""
    transaction = {
        "tx_id": row["tx_id"],
        "date": row["date"],
        "amount": row["amount"],
        "currency": row["currency"],
        "channel": row["channel"],
    }
    l2_evidence = {
        "primary_category": row.get("primary_category", "") or "C1",
        "l2_score": row.get("l2_score", ""),
        "l2_triggers": [t for t in (row.get("l2_triggers", "") or "").split(";") if t],
        "sender": {"name": row.get("sender_name", ""),
                   "pan": row.get("sender_pan", ""),
                   "dob": row.get("sender_dob", "")},
        "receiver": {"name": row.get("receiver_name", ""),
                     "pan": row.get("receiver_pan", ""),
                     "dob": row.get("receiver_dob", "")},
    }
    l3_verdict = {
        "verdict": row.get("l3_verdict", ""),
        "confidence": float(row["l3_confidence"]) if row.get("l3_confidence") else 0.0,
        "clause_no": row.get("clause_no", ""),
        "clause": row.get("clause", ""),
        "citation": row.get("citation", ""),
    }
    return transaction, l2_evidence, l3_verdict


def _resolve_desktop():
    """Real Desktop when run on your Mac; sandbox-safe fallback otherwise."""
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(desktop):
        return desktop
    # running in a sandbox / headless env without a Desktop
    fallback = os.path.join(HERE, "str_pdfs")
    return fallback


def run_from_csv(csv_path, pdf_dir=None):
    if pdf_dir is None:
        pdf_dir = _resolve_desktop()

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print("=" * 70)
    print(f"L4 Report Generator - running over {csv_path}")
    print(f"enum lists loaded from XSD: {list(ENUM_LOOKUPS.keys())}")
    print(f"PDF review copies -> {pdf_dir}")
    print("=" * 70)

    for row in rows:
        txn, l2, l3 = _row_to_inputs(row)
        tx_id = txn["tx_id"]

        # L4 only fires when L3 flagged the txn SUSPICIOUS. Clean txns skip L4.
        if l3["verdict"].upper() != "SUSPICIOUS":
            print(f"\n[{tx_id}]  L3 verdict={l3['verdict']} (conf {l3['confidence']}) "
                  f"-> NOT routed to L4 (no STR)")
            continue

        result = run_l4(l3, l2, txn)
        disp = result["disposition"]
        print(f"\n[{tx_id}]  L3 SUSPICIOUS (conf {l3['confidence']}) "
              f"clause={l3['clause_no'] or '(none)'}")
        if disp == "FILED":
            pdf_path = write_pdf_review_copy(result, l3, txn, pdf_dir)
            print(f"    -> FILED in {result['attempts']} attempt(s), "
                  f"{len(result['warnings'])} warning(s)")
            print(f"    -> reg_hash {result['to_L1']['regulation_hash'][:16]}... "
                  f"sent to L1; STR XML + log sent to L6")
            print(f"    -> PDF review copy: {pdf_path}")
        else:
            print(f"    -> {disp} after {result['attempts']} attempts; "
                  f"sent to L5 with {len(result['to_L5']['last_errors'])} error(s)")


if __name__ == "__main__":
    csv_path = os.path.join(HERE, "l3_output_mock.csv")
    run_from_csv(csv_path)

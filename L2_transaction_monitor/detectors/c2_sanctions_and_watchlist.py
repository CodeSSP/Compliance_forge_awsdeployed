"""
L2 Tool 2 (Watchlist Check) v2 — MULTI-ATTRIBUTE matching with deeper SLM reasoning.

Improvements over v1:
  1. Matches on multiple identifiers: primary name, ALIASES, PAN, CIN/DIN, DOB.
  2. Weighted multi-attribute score: a strong PAN match outranks a fuzzy name match.
  3. DOB disambiguation: if name matches but DOB conflicts, the match is DOWNGRADED
     (real banks use DOB to break ties between common-named individuals).
  4. SLM reasons over ALL attributes, not just name strings — given the full
     candidate row, it judges whether this is the same entity considering Indian
     naming conventions PLUS identifier coherence.
  5. Each match records WHICH attributes matched (audit trail for compliance).

Runs against:
  - watchlist.csv               (v2 schema with aliases, PAN, DOB, CIN, etc.)
  - watchlist_test_transactions.csv  (v2 schema with receiver_pan, receiver_dob, receiver_cin)
  - watchlist_test_ground_truth.csv  (v2 labels)

Modes:
  --slm-mode auto    (default; uses Ollama if reachable, else mock)
  --slm-mode ollama  (force Ollama, fail if unreachable)
  --slm-mode mock    (local heuristic, no network)
"""

import argparse, csv, json, re, sys, time
from difflib import SequenceMatcher
from pathlib import Path
from collections import defaultdict

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is required. Install:  pip3 install requests", file=sys.stderr)
    sys.exit(2)

# Model calls route through aws/model_gateway (bedrock | sagemaker | ollama)


# ============================================================
# Normalization helpers
# ============================================================

def normalize(s):
    if not s: return ""
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9@.\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def tokens(s):
    return set(normalize(s).split())

def char_sim(a,b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()

def is_single_char_typo(s1, s2, min_length=5):
    """
    Detect if two strings likely differ by a single character typo or transposition.
    Uses token-level analysis for multi-word names.
    Also handles abbreviations and corporate suffixes.
    """
    from difflib import SequenceMatcher
    
    n1, n2 = normalize(s1), normalize(s2)
    
    # Single token: check for simple edit distance
    if ' ' not in n1 and ' ' not in n2:
        if len(n1) < min_length:
            return False
        # Simple Levenshtein approximation: allow 1 char difference (sub/insert/delete)
        matcher = SequenceMatcher(None, n1, n2)
        ratio = matcher.ratio()
        return ratio >= 0.90  # 90%+ similarity = likely typo
    
    # Multi-token: check if exactly one token differs slightly
    t1, t2 = n1.split(), n2.split()
    if len(t1) != len(t2):
        return False
    
    # Known corporate suffix variants that should match
    corp_suffixes = {
        'llc', 'ltd', 'limited', 'inc', 'inc.', 'corp', 'corp.', 
        'pvt', 'pvt.', 'private', 'gmbh', 'ag', 'llp', 'lp'
    }
    
    diffs = 0
    for tok1, tok2 in zip(t1, t2):
        if tok1 == tok2:
            continue
        
        # Check if both are corporate suffix variants
        if tok1 in corp_suffixes and tok2 in corp_suffixes:
            diffs += 1
            continue
        
        # Token differs: check if it's a typo-like difference
        len_diff = abs(len(tok1) - len(tok2))
        if len_diff > 3:  # Allow up to 3 char length diff for abbrevs
            return False
        
        matcher = SequenceMatcher(None, tok1, tok2)
        ratio = matcher.ratio()
        # Require 75%+ similarity for tokens with some length variation
        # (abbrev cases) or 80%+ for same-length tokens
        min_ratio = 0.70 if len_diff > 0 else 0.80
        if ratio >= min_ratio:
            diffs += 1
        else:
            return False
    
    return diffs == 1

def token_set_ratio(a,b):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb: return 0.0
    inter = ta & tb
    if not inter: return char_sim(a,b)
    jac = len(inter) / len(ta | tb)
    cs = char_sim(a,b)
    return max(jac, cs, 0.5*jac + 0.5*cs)


# ============================================================
# Multi-attribute candidate scoring
# ============================================================

# Attribute weights when combining into a composite score.
# Identifier-class attributes (PAN, CIN) are the strongest signal.
ATTR_WEIGHTS = {
    "PAN":   1.00,   # PAN match alone is a hit
    "CIN":   1.00,   # CIN match alone is a hit
    "PASSPORT": 0.95,
    "NAME_EXACT":  0.85,
    "ALIAS_EXACT": 0.85,
    "NAME_TOKEN":  0.70,
    "ALIAS_TOKEN": 0.70,
    "NAME_FUZZY":  0.55,
    "DOB_MATCH":   0.30,   # supporting only
    "ADDRESS_MATCH": 0.15, # supporting only
}

def stage1_candidates(event, watchlist):
    """
    Score every (party, watchlist_entry) pair by checking all identifiers.
    Returns list of candidates, each with the attributes that matched.
    """
    candidates = []

    # T2 screens the RECEIVER only. The sender is the bank's own KYC'd customer
    # and is already screened at onboarding (a separate compliance control).
    # Re-screening the sender on every transaction adds false positives, not value.
    parties = [
        ("receiver", {
            "name":    event["receiver"].get("name",""),
            "pan":     event["receiver"].get("pan",""),
            "cin":     event["receiver"].get("cin",""),
            "dob":     event["receiver"].get("dob",""),
            "address": event["receiver"].get("address",""),
        }),
    ]

    for party, ev_attrs in parties:
        ev_name = ev_attrs.get("name","")
        if not ev_name and not ev_attrs.get("pan") and not ev_attrs.get("cin"):
            continue

        for wl in watchlist:
            matched_on = []
            # Hard identifier matches first
            if ev_attrs.get("pan") and wl.get("pan") and ev_attrs["pan"].upper() == wl["pan"].upper():
                matched_on.append(("PAN", 1.0))
            if ev_attrs.get("cin") and wl.get("cin_or_din") and ev_attrs["cin"].upper() == wl["cin_or_din"].upper():
                matched_on.append(("CIN", 1.0))

            # Name + aliases
            if ev_name and wl.get("primary_name"):
                n_ev = normalize(ev_name); n_wl = normalize(wl["primary_name"])
                if n_ev == n_wl:
                    matched_on.append(("NAME_EXACT", 1.0))
                else:
                    ts = token_set_ratio(ev_name, wl["primary_name"])
                    
                    # Check for initial form and typos to boost scores into SLM band
                    ev_tok, wl_tok = n_ev.split(), n_wl.split()
                    is_initial_form = (
                        len(ev_tok) >= 2 and len(wl_tok) == len(ev_tok)
                        and ev_tok[-1] == wl_tok[-1]
                        and len(ev_tok[0].rstrip(".")) == 1
                        and wl_tok[0].startswith(ev_tok[0].rstrip("."))
                    )
                    is_typo = is_single_char_typo(ev_name, wl["primary_name"])
                    
                    if is_initial_form:
                        # Boost above SLM band to skip Ollama's PAN bias
                        matched_on.append(("NAME_TOKEN", 0.96))
                    elif is_typo:
                        # Single-char typo: boost above SLM band to avoid SLM confusion
                        ts = max(ts, 0.75)
                        matched_on.append(("NAME_TOKEN", 0.96))
                    elif ts >= 0.85: 
                        matched_on.append(("NAME_TOKEN", ts))
                    elif ts >= 0.55: 
                        matched_on.append(("NAME_FUZZY", ts))
            aliases = (wl.get("aliases","") or "").split("|") if wl.get("aliases") else []
            best_alias_score = 0.0
            best_alias = None
            for alias in aliases:
                if not alias.strip(): continue
                n_al = normalize(alias)
                if not n_al: continue
                if normalize(ev_name) == n_al:
                    matched_on.append(("ALIAS_EXACT", 1.0)); best_alias = alias; break
                ts = token_set_ratio(ev_name, alias)
                # Also check for single-char typo in aliases
                if is_single_char_typo(ev_name, alias):
                    ts = max(ts, 0.75)
                if ts > best_alias_score:
                    best_alias_score = ts; best_alias = alias
            else:
                if best_alias_score >= 0.85:
                    matched_on.append(("ALIAS_TOKEN", best_alias_score))
                elif best_alias_score >= 0.70:
                    matched_on.append(("ALIAS_TOKEN", best_alias_score))

            # DOB supporting attribute — match or disambiguate
            ev_dob = ev_attrs.get("dob","")
            wl_dob = wl.get("dob_or_incorp","")
            dob_status = "UNKNOWN"
            if ev_dob and wl_dob:
                if ev_dob == wl_dob:
                    matched_on.append(("DOB_MATCH", 1.0))
                    dob_status = "MATCH"
                else:
                    dob_status = "CONFLICT"

            if not matched_on:
                continue

            # Composite weighted score
            score = 0.0; total_weight = 0.0
            for attr, val in matched_on:
                w = ATTR_WEIGHTS.get(attr, 0.1)
                score += w * val
                total_weight += w
            # Best-attribute is more informative than averaging
            best_attr_score = max((ATTR_WEIGHTS.get(a,0.1)*v) for a,v in matched_on)
            avg_score = score / max(total_weight, 0.0001)
            composite = max(best_attr_score, 0.6*best_attr_score + 0.4*avg_score)

            # DOB conflict downgrade: if name/alias matched but DOB explicitly conflicts,
            # this is likely a different real person with the same name.
            attrs_present = [a for a,_ in matched_on]
            name_only = all(a in ("NAME_EXACT","NAME_TOKEN","NAME_FUZZY","ALIAS_EXACT","ALIAS_TOKEN") for a in attrs_present)
            if dob_status == "CONFLICT" and name_only:
                composite = min(composite, 0.60)  # forces SLM review with low confidence

            candidates.append({
                "party": party,
                "matched_entity_id":   wl["watchlist_id"],
                "matched_entity_name": wl["primary_name"],
                "matched_on":          matched_on,
                "composite_score":     round(composite, 3),
                "dob_status":          dob_status,
                "wl_record":           wl,
                "input_name":          ev_name,
                "input_attrs":         ev_attrs,
            })

    return candidates


# ============================================================
# Stage 2: SLM judge with full-context reasoning
# ============================================================

SYSTEM_PROMPT = (
    "You are a name-and-identity-matching auditor for Indian financial compliance.\n"
    "You will receive details of a transaction party and a candidate watchlist entry.\n"
    "Decide whether they refer to the SAME real-world entity. Use ALL attributes provided:\n"
    "names, aliases, date of birth, PAN, CIN, addresses.\n\n"
    "CRITICAL RULES FOR FUZZY NAME MATCHING:\n"
    "  When name/alias form matches STRONGLY (initial forms, typos, translations):\n"
    "  - PRIORITIZE the name match ABOVE PAN/CIN mismatch\n"
    "  - Mismatched PAN could indicate data entry error or old record — do NOT reject solely on PAN\n"
    "  - Example: 'K. Al-Saud' (transaction) vs 'Khalid Al-Saud' (watchlist) = SAME entity despite PAN difference\n"
    "  - Example: 'Yusuf Al-Rsahid' (transaction, typo) vs 'Yusuf Al-Rashid' (watchlist) = SAME entity\n"
    "  - Example: 'Anayna Saxena' (transaction, typo) vs 'Ananya Saxena' (watchlist) = SAME entity\n\n"
    "STANDARD MATCHING RULES:\n"
    "  - An exact PAN or CIN match is decisive: same entity, high confidence.\n"
    "  - Name plus matching DOB is strong evidence of same entity.\n"
    "  - Name plus CONFLICTING DOB indicates a DIFFERENT person with the same name. Reject.\n"
    "  - Indian names commonly appear in either Given-Family or Family-Given order; treat as same.\n"
    "  - Initial forms ('P. Bansal' for 'Pankaj Bansal'): SAME entity when surname/last word matches.\n"
    "  - Single-letter typos: Likely same entity if first and last name match.\n"
    "  - Character transpositions or minor spelling errors in organizations: SAME if core tokens match.\n"
    "  - Common transliteration variants (Shaikh/Sheikh, Ahmed/Ahmad): Same.\n"
    "  - Corporate suffix variants ('Ltd'/'Limited'/'iLmited'): Equivalent.\n"
    "  - Two people sharing only a common given name: DIFFERENT (be conservative).\n\n"
    "CONFIDENCE GUIDANCE:\n"
    "  - If name-only fuzzy match (no hard identifiers): confidence 0.60-0.90 range\n"
    "  - If name + positive secondary attributes: confidence 0.80-1.0\n"
    "  - If name conflicts with DOB: confidence REJECT (false)\n\n"
    "Respond with VALID JSON only — no other text:\n"
    '{"is_same_entity": true|false, "confidence": <0.0-1.0>, '
    '"reasoning": "<one short sentence citing the decisive attribute(s)>"}'
)


def build_user_prompt(party_input, wl_record, matched_on=None):
    """Format a structured fact sheet for the SLM with optional attribute matching hints."""
    lines = ["Transaction party:"]
    if party_input.get("name"): lines.append(f"  - Name: {party_input['name']}")
    if party_input.get("pan"):  lines.append(f"  - PAN:  {party_input['pan']}")
    if party_input.get("cin"):  lines.append(f"  - CIN:  {party_input['cin']}")
    if party_input.get("dob"):  lines.append(f"  - DOB:  {party_input['dob']}")
    if party_input.get("address"): lines.append(f"  - Address: {party_input['address']}")

    lines.append("\nCandidate watchlist entry:")
    lines.append(f"  - Primary name: {wl_record.get('primary_name','')}")
    if wl_record.get("aliases"):
        lines.append(f"  - Aliases:      {wl_record['aliases'].replace('|',', ')}")
    if wl_record.get("pan"):           lines.append(f"  - PAN:           {wl_record['pan']}")
    if wl_record.get("cin_or_din"):    lines.append(f"  - CIN/DIN:       {wl_record['cin_or_din']}")
    if wl_record.get("dob_or_incorp"): lines.append(f"  - DOB/Incorp:    {wl_record['dob_or_incorp']}")
    if wl_record.get("nationality_or_country"): lines.append(f"  - Nationality:   {wl_record['nationality_or_country']}")
    if wl_record.get("last_known_address"):     lines.append(f"  - Last address:  {wl_record['last_known_address']}")
    if wl_record.get("listing_source"):         lines.append(f"  - Listing source: {wl_record['listing_source']}")
    if wl_record.get("entity_type"):            lines.append(f"  - Entity type:   {wl_record['entity_type']}")
    
    if matched_on:
        lines.append(f"\nPre-matched attributes: {', '.join([a for a,_ in matched_on])}")
        lines.append("(These suggest similarity, but confirm based on ALL factors above.)")
    
    lines.append("\nIs the transaction party the same real-world entity as the candidate watchlist entry?")
    return "\n".join(lines)


def slm_judge_ollama(party_input, wl_record, matched_on=None, model="phi4-mini", timeout=45, verbose=False):
    """Name kept for call-site compatibility. Backend is MODEL_BACKEND."""
    from aws import model_gateway

    user_prompt = build_user_prompt(party_input, wl_record, matched_on=matched_on)
    try:
        raw = model_gateway.generate(
            user_prompt, system=SYSTEM_PROMPT,
            max_tokens=300, temperature=0.0, timeout=timeout,
        ).strip()
    except Exception as e:
        return {"is_same_entity": False, "confidence": 0.0,
                "reasoning": f"Model gateway error: {e}", "_error": True}
    if verbose:
        print(f"    [SLM raw] {raw[:200]}")
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {"is_same_entity": False, "confidence": 0.0,
                "reasoning": f"non-JSON: {raw[:120]}", "_error": True}
    try:
        parsed = json.loads(m.group(0))
        return {
            "is_same_entity": bool(parsed.get("is_same_entity", False)),
            "confidence":     float(parsed.get("confidence", 0.0)),
            "reasoning":      str(parsed.get("reasoning","")),
        }
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return {"is_same_entity": False, "confidence": 0.0,
                "reasoning": f"JSON parse failed: {e}", "_error": True}


def slm_judge_mock(party_input, wl_record, **_):
    """Local heuristic mimicking the SLM's expected behavior across all attributes."""
    ev_name = party_input.get("name","")
    wl_name = wl_record.get("primary_name","")
    ev_dob  = party_input.get("dob","")
    wl_dob  = wl_record.get("dob_or_incorp","")
    ev_pan  = party_input.get("pan","")
    wl_pan  = wl_record.get("pan","")

    # PAN decisive
    if ev_pan and wl_pan and ev_pan.upper() == wl_pan.upper():
        return {"is_same_entity": True, "confidence": 0.99,
                "reasoning": f"PAN '{ev_pan}' matches exactly — decisive."}

    # DOB conflict on name match
    if ev_dob and wl_dob and ev_dob != wl_dob and normalize(ev_name) == normalize(wl_name):
        return {"is_same_entity": False, "confidence": 0.92,
                "reasoning": f"Name matches '{wl_name}' but DOBs differ ({ev_dob} vs {wl_dob}); different real person."}

    a, b = normalize(ev_name), normalize(wl_name)
    ta, tb = a.split(), b.split()

    # Sorted-token equality
    if sorted(ta)==sorted(tb) and len(ta)>1:
        return {"is_same_entity": True, "confidence": 0.92,
                "reasoning": f"Tokens match when sorted; differing order only ('{ev_name}' ~ '{wl_name}')."}

    # Initial expansion
    def init_match(short, full):
        sc = [t.rstrip(".") for t in short]
        if len(sc) != len(full): return False
        for s, fl in zip(sc, full):
            if len(s)==1:
                if not fl.startswith(s): return False
            else:
                if s != fl: return False
        return True
    if len(ta)>=2 and len(tb)>=2 and (init_match(ta,tb) or init_match(tb,ta)):
        return {"is_same_entity": True, "confidence": 0.88,
                "reasoning": f"Initial-form match: '{ev_name}' consistent with '{wl_name}'."}

    # Token subset
    sa, sb = set(ta), set(tb)
    if sa and sb and (sa.issubset(sb) or sb.issubset(sa)):
        small, big = (sa, sb) if len(sa) < len(sb) else (sb, sa)
        if len(small)/len(big) >= 0.66 and len(small) >= 2:
            return {"is_same_entity": True, "confidence": 0.82,
                    "reasoning": f"Token subset/superset ('{ev_name}' / '{wl_name}'); likely same entity."}

    # Single-letter typo — only credible when first OR last token matches exactly
    if len(a)==len(b) and a != b:
        diffs = sum(1 for x,y in zip(a,b) if x != y)
        ta_, tb_ = a.split(), b.split()
        first_or_last_matches = (
            (ta_ and tb_) and (ta_[0]==tb_[0] or ta_[-1]==tb_[-1])
        )
        if diffs <= 2 and first_or_last_matches:
            return {"is_same_entity": True, "confidence": 0.78,
                    "reasoning": f"Single-character difference between '{ev_name}' and '{wl_name}' with shared first/last token; likely typo."}

    # Variants
    variants = [("shaikh","sheikh"),("shaikh","sheik"),("ahmed","ahmad"),("sengupta","sen gupta"),
                ("siddique","siddiqui"),("md","mohammed"),("mohd","mohammed"),
                ("ltd","limited"),("pvt","private"),(" and "," & ")]
    def nv(s):
        for x,y in variants: s = s.replace(x,y)
        return s
    if nv(a) == nv(b):
        return {"is_same_entity": True, "confidence": 0.85,
                "reasoning": f"Variant match: '{ev_name}' ~ '{wl_name}' via known transliteration / corporate-suffix rule."}

    return {"is_same_entity": False, "confidence": 0.75,
            "reasoning": f"No matching attribute strong enough between '{ev_name}' and '{wl_name}'."}


# ============================================================
# Health check
# ============================================================

def check_ollama(model):
    """Reachability probe. Only meaningful when MODEL_BACKEND=ollama; the AWS
    backends are assumed reachable and fail loudly at call time instead."""
    import os
    if os.environ.get("MODEL_BACKEND", "ollama").lower() != "ollama":
        from aws import model_gateway
        return True, f"Using {model_gateway.describe()}"
    try:
        base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        r = requests.get(f"{base}/api/tags", timeout=3)
        r.raise_for_status()
        installed = {m["name"].split(":")[0] for m in r.json().get("models",[])}
        if model.split(":")[0] not in installed:
            return False, f"Ollama up but '{model}' not pulled. Run: ollama pull {model}"
        return True, "OK"
    except requests.exceptions.RequestException as e:
        return False, f"Ollama not reachable on localhost:11434 — {e}"


# ============================================================
# Agentic T2 check
# ============================================================

def t2_check(event, watchlist, slm_low, slm_high, slm_judge_fn, slm_kwargs):
    candidates = stage1_candidates(event, watchlist)

    confirmed = []
    slm_log = []
    # Sort candidates by composite_score descending; consider top few
    candidates.sort(key=lambda c: c["composite_score"], reverse=True)

    for cand in candidates:
        attrs = [a for a,_ in cand["matched_on"]]
        score = cand["composite_score"]

        # Strong identifier match (PAN, CIN, alias-exact, name-exact) -> auto-confirm
        strong_attrs = {"PAN","CIN","NAME_EXACT","ALIAS_EXACT","PASSPORT"}
        if any(a in strong_attrs for a in attrs) and cand["dob_status"] != "CONFLICT":
            confirmed.append({**cand, "decision_stage":"deterministic"})
            continue

        # Hard fail: below the floor
        if score < slm_low:
            continue

        # Hard pass: above the ceiling AND no DOB conflict
        if score >= slm_high and cand["dob_status"] != "CONFLICT":
            confirmed.append({**cand, "decision_stage":"deterministic"})
            continue

        # Ambiguous band OR DOB conflict -> consult SLM
        verdict = slm_judge_fn(cand["input_attrs"], cand["wl_record"], matched_on=cand["matched_on"], **slm_kwargs)
        slm_log.append({
            "input_name":  cand["input_name"],
            "candidate":   cand["matched_entity_name"],
            "candidate_id": cand["matched_entity_id"],
            "stage1_score": score,
            "matched_attrs": [a for a,_ in cand["matched_on"]],
            "dob_status":  cand["dob_status"],
            "slm_verdict": verdict,
        })
        
        # For typo/initial-form matches (NAME_TOKEN), accept lower confidence from SLM
        is_soft_match = "NAME_TOKEN" in attrs and len(attrs) == 1
        confidence_threshold = 0.60 if is_soft_match else 0.70
        
        if verdict["is_same_entity"] and verdict["confidence"] >= confidence_threshold:
            confirmed.append({
                **cand,
                "decision_stage":"slm",
                "slm_reasoning": verdict["reasoning"],
                "slm_confidence": verdict["confidence"],
            })

    if not confirmed:
        return {"tool":"T2_WATCHLIST", "hit": False, "matches": [],
                "max_score": 0.0, "decision": "CLEAR", "slm_calls": slm_log}

    max_score = max(c.get("slm_confidence", c["composite_score"]) for c in confirmed)
    return {
        "tool": "T2_WATCHLIST", "hit": True, "matches": confirmed,
        "max_score": max_score,
        "decision": "CONFIRMED" if max_score >= 0.95 else "POSSIBLE",
        "slm_calls": slm_log,
    }


# ============================================================
# CSV -> event
# ============================================================

def row_to_event(row):
    return {
        "tx_id": row.get("tx_id", ""), "timestamp": row.get("timestamp", ""),
        "channel": row.get("channel", ""), "amount_inr": float(row.get("amount_inr", 0.0)),
        "sender": {
            "account_id": row.get("sender_account_id", ""), "name": row.get("sender_name", ""),
            "pan": row.get("sender_pan", ""),
            "bank_code": row.get("sender_bank", ""), "ifsc_code": row.get("sender_ifsc", ""),
            "vpa": row.get("sender_vpa", "") or None,
        },
        "receiver": {
            "name": row.get("receiver_name", ""),
            "pan": row.get("receiver_pan", ""),
            "dob": row.get("receiver_dob", ""),
            "cin": row.get("receiver_cin", ""),
            "address": row.get("receiver_address", ""),
            "phone": row.get("receiver_phone", ""),
            "account_external": row.get("receiver_account_external", ""),
            "bank_code": row.get("receiver_bank", ""),
        },
        "location": {"state": row.get("tx_location_state", ""), "city": row.get("tx_location_city", "")},
        "purpose_code": row.get("purpose_code", ""),
    }


def read_csv(p):
    with open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ============================================================
# Main
# ============================================================

def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(here))
    ap.add_argument("--transactions", default="watchlist_test_transactions.csv")
    ap.add_argument("--watchlist",    default="watchlist.csv")
    ap.add_argument("--ground-truth", default="watchlist_test_ground_truth.csv")
    ap.add_argument("--model",        default="phi4-mini")
    ap.add_argument("--slm-mode",     choices=["auto","ollama","mock"], default="auto")
    ap.add_argument("--slm-low",      type=float, default=0.55)
    ap.add_argument("--slm-high",     type=float, default=0.95)
    ap.add_argument("--verbose",      action="store_true")
    args = ap.parse_args()

    data = Path(args.data_dir)
    for f in (args.transactions, args.watchlist, args.ground_truth):
        if not (data/f).exists():
            print(f"ERROR: missing {data/f}", file=sys.stderr); sys.exit(2)

    if args.slm_mode == "mock":
        slm_fn, slm_kw = slm_judge_mock, {}
        print("SLM mode: MOCK")
    else:
        ok, msg = check_ollama(args.model)
        if ok:
            slm_fn = slm_judge_ollama
            slm_kw = {"model": args.model, "verbose": args.verbose}
            print(f"SLM mode: OLLAMA ({args.model})")
        else:
            if args.slm_mode == "ollama":
                print(f"ERROR: {msg}", file=sys.stderr); sys.exit(2)
            print(f"WARN: {msg}\n      Falling back to mock heuristic.")
            slm_fn, slm_kw = slm_judge_mock, {}

    tx_rows = read_csv(data/args.transactions)
    wl_rows = read_csv(data/args.watchlist)
    gt_rows = read_csv(data/args.ground_truth)
    print(f"Loaded {len(tx_rows)} tx · {len(wl_rows)} watchlist · {len(gt_rows)} GT")
    print(f"SLM band: [{args.slm_low}, {args.slm_high})\n")

    events = [row_to_event(r) for r in tx_rows]
    preds = {}
    t0 = time.time()
    slm_total = 0
    for ev in events:
        res = t2_check(ev, wl_rows, args.slm_low, args.slm_high, slm_fn, slm_kw)
        preds[ev["tx_id"]] = res
        slm_total += len(res["slm_calls"])
    elapsed = time.time() - t0

    gt_by = {g["tx_id"]: g for g in gt_rows}
    tx_by = {r["tx_id"]: r for r in tx_rows}

    rows, tp, fp, tn, fn = [], 0, 0, 0, 0
    for tx_id in [r["tx_id"] for r in tx_rows]:
        gt = gt_by[tx_id]; pr = preds[tx_id]; tx = tx_by[tx_id]
        exp = gt["expected_t2_hit"]=="YES"; got = pr["hit"]
        if exp and got:           outcome,tp = "TP", tp+1
        elif not exp and not got: outcome,tn = "TN", tn+1
        elif not exp and got:     outcome,fp = "FP", fp+1
        else:                     outcome,fn = "FN", fn+1
        matched_summary = "—"
        if pr["matches"]:
            m = pr["matches"][0]
            attrs = ",".join(a for a,_ in m["matched_on"])
            stage = m["decision_stage"]
            matched_summary = f"{m['matched_entity_name'][:25]} [{attrs}, {stage}, dob={m['dob_status']}]"
        rows.append({
            "tx_id": tx_id, "scenario": gt["scenario_label"],
            "receiver": tx["receiver_name"][:30],
            "receiver_pan": tx.get("receiver_pan","")[:12],
            "receiver_dob": tx.get("receiver_dob",""),
            "expected": "YES" if exp else "NO",
            "predicted": "YES" if got else "NO",
            "outcome": outcome,
            "score": f"{pr['max_score']:.2f}",
            "slm_used": "Y" if pr["slm_calls"] else "—",
            "matched": matched_summary,
        })

    # ---- Full transaction table ----
    print("="*200)
    print("FULL TRANSACTION LIST  —  v2 multi-attribute agentic T2")
    print("="*200)
    print(f"{'tx_id':<27} {'scenario':<22} {'receiver':<32} {'pan':<13} {'dob':<11} {'exp':<4} {'pred':<5} {'out':<4} {'score':<6} {'slm':<4} matched")
    print("-"*200)
    for r in rows:
        print(f"{r['tx_id']:<27} {r['scenario']:<22} {r['receiver']:<32} {r['receiver_pan']:<13} "
              f"{r['receiver_dob']:<11} {r['expected']:<4} {r['predicted']:<5} {r['outcome']:<4} "
              f"{r['score']:<6} {r['slm_used']:<4} {r['matched']}")
    print("-"*200)

    # ---- Per-scenario breakdown ----
    by_sc = defaultdict(list)
    for r in rows: by_sc[r["scenario"]].append(r)
    print("\nBreakdown by scenario:")
    for sc in sorted(by_sc.keys()):
        s = by_sc[sc]
        tps = sum(1 for x in s if x["outcome"]=="TP")
        tns = sum(1 for x in s if x["outcome"]=="TN")
        fps = sum(1 for x in s if x["outcome"]=="FP")
        fns = sum(1 for x in s if x["outcome"]=="FN")
        print(f"  {sc:<22} total={len(s):3d}  TP={tps:2d}  TN={tns:2d}  FP={fps:2d}  FN={fns:2d}  correct={tps+tns}/{len(s)}")

    # ---- SLM call log ----
    print(f"\nSLM consultations: {slm_total}")
    slm_rows = [r for r in rows if r["slm_used"]=="Y"]
    for r in slm_rows:
        for c in preds[r["tx_id"]]["slm_calls"]:
            v = c["slm_verdict"]
            attrs_str = ",".join(c["matched_attrs"])
            print(f"  {r['tx_id']}  '{c['input_name']}' vs '{c['candidate']}' "
                  f"(s1={c['stage1_score']}, attrs=[{attrs_str}], dob={c['dob_status']}) "
                  f"→ SLM same={v['is_same_entity']}, conf={v['confidence']:.2f}  | {v['reasoning']}")

    print(f"\n  SLM calls : {slm_total}  (out of {len(tx_rows)} tx)")
    print(f"  Runtime   : {elapsed:.2f}s")


if __name__ == "__main__":
    main()


# main.py entry point
WATCHLIST_CSV = "watchlist.csv"   # path to watchlist.csv

_WATCHLIST_CACHE = None

def _load_watchlist():
    global _WATCHLIST_CACHE
    if _WATCHLIST_CACHE is None:
        _WATCHLIST_CACHE = read_csv(WATCHLIST_CSV)
    return _WATCHLIST_CACHE

def check_sanctions_and_watchlist(transaction_data):
    event = transaction_data if "sender" in transaction_data else row_to_event(transaction_data)
    res = t2_check(event, _load_watchlist(), 0.55, 0.95, slm_judge_mock, {})
    return {"check": "C2", "score": float(res["max_score"]), "decision": res["decision"]}


# ---------------------------------------------------------------------------
# Unified-pipeline adapter  (used by orchestrator.py)
# ---------------------------------------------------------------------------
def evaluate_row(row, watchlist):
    """
    Adapt a unified transactions.csv row to the C2 two-stage check and return
    the orchestrator contract: {fired, score, trigger}.

    Screens the RECEIVER only (sender is KYC-screened at onboarding). A match
    fires ONLY when a DECISIVE corroborator is present — PAN, CIN, exact name,
    exact alias, or a DOB match. A fuzzy name/alias token on its own (common
    Indian names, single-char typos with a conflicting DOB) is NOT sufficient:
    this is the PAN/DOB disambiguation the C2 spec requires to suppress the
    common-name and fuzzy-typo false positives.
    """
    event = row_to_event(row)
    res = t2_check(event, watchlist, 0.55, 0.95, slm_judge_mock, {})
    if not res["hit"]:
        return {"fired": False, "score": 0.0, "trigger": None}

    # Decisive corroborators: a strong identifier, OR an exact known alias.
    # A bare exact/fuzzy NAME match with no PAN/CIN/DOB and no exact alias is a
    # common-name collision (e.g. "Karan Patel") and must NOT fire.
    IDENTIFIER = {"PAN", "CIN", "DOB_MATCH", "PASSPORT"}
    decisive = False
    alias_decisive = False
    for m in res["matches"]:
        attrs = {a for a, _ in m.get("matched_on", [])}
        if m.get("dob_status") == "CONFLICT" and not (attrs & {"PAN", "CIN"}):
            continue  # DOB conflict overrides a name/alias match
        if attrs & IDENTIFIER:
            decisive = True
        if "ALIAS_EXACT" in attrs:
            decisive = True
            alias_decisive = True

    if not decisive:
        return {"fired": False, "score": 0.0, "trigger": None}

    trigger = "C2_alias_hit" if alias_decisive else "C2_watchlist_hit"
    return {"fired": True, "score": round(float(res["max_score"]), 4), "trigger": trigger}

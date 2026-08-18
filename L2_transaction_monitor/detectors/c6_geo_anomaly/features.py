"""
features.py  (C6 — Geo-Anomaly)
-------------------------------
Pure feature extraction. Turns a raw transaction + account history into a flat,
self-describing feature dict. This is the SINGLE source of truth consumed by
BOTH the deterministic detector and the SLM classifier — so the two are always
compared on identical inputs (a fair F1 contest).

No scoring or decisions happen here — only measurement.
"""

from datetime import datetime
from math import asin, cos, radians, sin, sqrt

from .thresholds import (
    BALANCE_DRAIN_SHARE,
    DEFAULT_HOME_COUNTRY,
    FATF_HIGH_RISK_COUNTRIES,
    MAX_PLAUSIBLE_SPEED_KMH,
    ODD_HOUR_END,
    ODD_HOUR_START,
    RARE_LOCATION_SHARE,
)


def _norm(value: str) -> str:
    return (value or "").strip().lower()


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance between two lat/lon points, in kilometres."""
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def _parse_location(raw) -> dict:
    """Normalise a `location` field (string "Mumbai, IN" or dict) to a uniform shape."""
    city = country = None
    lat = lon = None

    if isinstance(raw, dict):
        city = raw.get("city") or raw.get("name")
        country = raw.get("country") or raw.get("country_code")
        lat = raw.get("lat", raw.get("latitude"))
        lon = raw.get("lon", raw.get("longitude"))
    elif isinstance(raw, str) and raw.strip():
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if parts:
            city = parts[0]
        if len(parts) >= 2 and len(parts[-1]) == 2:
            country = parts[-1]

    key = _norm(city) or _norm(country) or _norm(str(raw) if raw is not None else "")
    return {
        "city": city,
        "country": country.upper() if isinstance(country, str) else country,
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "key": key,
    }


def _location_frequency(account_history: dict) -> dict:
    """Build a {normalised_location_key: count} map. Accepts dict or list."""
    known = (account_history or {}).get("known_locations")
    freq: dict[str, int] = {}
    if isinstance(known, dict):
        for loc, count in known.items():
            freq[_norm(loc)] = freq.get(_norm(loc), 0) + int(count)
    elif isinstance(known, (list, tuple)):
        for loc in known:
            freq[_norm(loc)] = freq.get(_norm(loc), 0) + 1
    return freq


def extract_features(transaction: dict, account_history: dict | None = None) -> dict:
    """
    Extract the full C6 feature set for one transaction.

    Returns a flat dict of measurements (booleans / numbers / labels). Downstream
    consumers decide what to do with them; this function never fires or scores.
    """
    account_history = account_history or {}
    loc = _parse_location(transaction.get("location"))
    freq = _location_frequency(account_history)
    total_seen = sum(freq.values())
    home_country = (account_history.get("home_country") or DEFAULT_HOME_COUNTRY).upper()

    # --- Location novelty / rarity ---
    location_count = freq.get(loc["key"], 0)
    share = (location_count / total_seen) if total_seen > 0 else None
    is_new_location = bool(total_seen > 0 and location_count == 0)
    is_rare_location = bool(share is not None and 0 < share < RARE_LOCATION_SHARE)

    # --- Foreign / FATF ---
    cur_country = loc["country"]
    is_foreign = bool(cur_country and cur_country != home_country)
    is_fatf = bool(cur_country in FATF_HIGH_RISK_COUNTRIES)

    # --- Impossible travel ---
    last = account_history.get("last_location") or {}
    distance_km = hours_elapsed = implied_speed = None
    impossible_travel = False
    if (
        loc["lat"] is not None and loc["lon"] is not None
        and last.get("lat") is not None and last.get("lon") is not None
        and last.get("timestamp") and transaction.get("timestamp")
    ):
        try:
            t_now = datetime.fromisoformat(transaction["timestamp"])
            t_prev = datetime.fromisoformat(last["timestamp"])
            hours_elapsed = (t_now - t_prev).total_seconds() / 3600.0
            distance_km = _haversine_km(last["lat"], last["lon"], loc["lat"], loc["lon"])
            if hours_elapsed > 0:
                implied_speed = distance_km / hours_elapsed
                impossible_travel = implied_speed > MAX_PLAUSIBLE_SPEED_KMH
            elif distance_km > 0:
                impossible_travel = True
        except (ValueError, TypeError):
            pass

    # --- Device novelty ---
    device_id = transaction.get("device_id")
    typical_devices = {
        _norm(d) for d in (account_history.get("typical_devices") or [])
    }
    is_new_device = bool(device_id and _norm(device_id) not in typical_devices and typical_devices)

    # --- Balance drain ---
    amount = float(transaction.get("amount_inr", 0) or 0)
    balance = account_history.get("balance_inr")
    drain_share = (amount / balance) if balance else None
    is_balance_drain = bool(drain_share is not None and drain_share >= BALANCE_DRAIN_SHARE)

    # --- Odd hour ---
    is_odd_hour = False
    try:
        hr = datetime.fromisoformat(transaction["timestamp"]).hour
        is_odd_hour = ODD_HOUR_START <= hr < ODD_HOUR_END
    except (ValueError, TypeError, KeyError):
        pass

    # --- High value ---
    avg_amount = account_history.get("avg_tx_amount") or 0
    amount_vs_avg = (amount / avg_amount) if avg_amount else None

    return {
        # identity / context (passed through for the SLM prompt + audit)
        "tx_id": transaction.get("tx_id"),
        "account_id": transaction.get("sender_account_id"),
        "amount_inr": amount,
        "channel": transaction.get("channel"),
        "purpose_code": transaction.get("purpose_code"),
        "account_type": account_history.get("account_type", "SAVINGS"),
        "travel_profile": account_history.get("travel_profile", "DOMESTIC_STATIC"),
        "home_country": home_country,
        "current_city": loc["city"] or loc["key"],
        "current_country": cur_country,
        # measured signals
        "is_new_location": is_new_location,
        "is_rare_location": is_rare_location,
        "location_share": round(share, 4) if share is not None else None,
        "history_total_seen": total_seen,
        "is_foreign": is_foreign,
        "is_fatf_high_risk": is_fatf,
        "impossible_travel": impossible_travel,
        "distance_from_last_km": round(distance_km, 1) if distance_km is not None else None,
        "hours_since_last": round(hours_elapsed, 2) if hours_elapsed is not None else None,
        "implied_speed_kmh": round(implied_speed, 1) if implied_speed is not None else None,
        "is_new_device": is_new_device,
        "is_balance_drain": is_balance_drain,
        "drain_share": round(drain_share, 3) if drain_share is not None else None,
        "is_odd_hour": is_odd_hour,
        "amount_vs_avg": round(amount_vs_avg, 2) if amount_vs_avg is not None else None,
    }

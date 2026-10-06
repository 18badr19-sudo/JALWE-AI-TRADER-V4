"""Safe diagnostics for research data; never infer prices for missing minutes."""
from __future__ import annotations

import json
from collections import Counter

import pandas as pd


SIP_RESEARCH_DELAY_MINUTES = 16  # Alpaca requires historical end >= 15 minutes old.


def error_diagnostics(exc: Exception) -> dict:
    """Retain the provider's error category across wrappers, without secrets/URLs."""
    chain, current = [], exc
    while current is not None and current not in chain and len(chain) < 8:
        chain.append(current)
        current = current.__cause__ or current.__context__
    status = None
    messages = []
    for item in chain:
        messages.append(str(item).lower())
        candidate = getattr(getattr(item, "response", None), "status_code", None)
        if candidate is None:
            try:
                candidate = getattr(item, "status_code", None)
            except Exception:
                candidate = None
        if isinstance(candidate, int) and 100 <= candidate <= 599:
            status = candidate
    text = " ".join(messages)
    if "subscription does not permit" in text:
        reason = "SUBSCRIPTION_REQUIRED"
    elif status == 429:
        reason = "RATE_LIMITED"
    elif status == 401:
        reason = "AUTHENTICATION_FAILED"
    elif status == 403:
        reason = "FORBIDDEN"
    elif status is not None and status >= 500:
        reason = "PROVIDER_SERVER_ERROR"
    elif any(isinstance(item, TimeoutError) or "timeout" in type(item).__name__.lower()
             for item in chain):
        reason = "PROVIDER_TIMEOUT"
    elif "feed differs" in text or "feed mismatch" in text:
        reason = "OBSERVATION_FEED_MISMATCH"
    else:
        reason = "PROVIDER_REQUEST_FAILED"
    return {"reason": reason, "error_type": type(exc).__name__,
            "provider_error_type": type(chain[-1]).__name__, "http_status": status}


def coverage_diagnostics(frame, valid, start, end) -> dict:
    expected = pd.date_range(start, end, freq="min", inclusive="left")
    stamps = (pd.DatetimeIndex([], tz="UTC") if frame is None else
              pd.to_datetime(frame.index, utc=True, errors="coerce"))
    in_window = stamps[(stamps >= start) & (stamps < end)]
    valid_stamps = pd.DatetimeIndex(valid.index)
    missing = expected.difference(valid_stamps)
    columns = {"open", "high", "low", "close", "volume"}
    if frame is None or frame.empty:
        reason = "EMPTY_PROVIDER_WINDOW"
    elif not columns.issubset(frame.columns):
        reason = "MISSING_OHLCV_COLUMNS"
    elif len(missing) == 0:
        reason = "COMPLETE"
    elif len(in_window.unique()) > len(valid_stamps):
        reason = "INVALID_OR_DUPLICATE_BARS"
    else:
        reason = "MISSING_PROVIDER_MINUTES"
    return {"reason": reason, "expected_minutes": len(expected),
            "raw_rows": 0 if frame is None else len(frame),
            "observed_minutes": len(valid_stamps), "missing_minutes": len(missing),
            "duplicate_rows": int(in_window.duplicated().sum()),
            "first_missing_minutes": [stamp.isoformat() for stamp in missing[:5]]}


def diagnostic_counts(rows, field="metadata_json") -> dict:
    counts = Counter()
    for row in rows:
        if row.get("status") not in {"PARTIAL_DATA", "DATA_UNAVAILABLE"}:
            continue
        try:
            metadata = json.loads(row.get(field) or "{}")
            reason = (metadata.get("data_diagnostics") or {}).get("reason", "LEGACY_UNCLASSIFIED")
        except (ValueError, TypeError):
            reason = "LEGACY_UNCLASSIFIED"
        counts[reason] += 1
    return dict(counts)


def diagnostic_text(counts: dict) -> str:
    labels = {"EMPTY_PROVIDER_WINDOW": "نافذة فارغة",
              "MISSING_PROVIDER_MINUTES": "دقائق لم يرجعها المزود",
              "INVALID_OR_DUPLICATE_BARS": "شموع مرفوضة أو مكررة",
              "MISSING_OHLCV_COLUMNS": "حقول شموع ناقصة",
              "SUBSCRIPTION_REQUIRED": "صلاحية اشتراك",
              "RATE_LIMITED": "حد الطلبات", "AUTHENTICATION_FAILED": "رفض المصادقة",
              "FORBIDDEN": "رفض الصلاحية", "PROVIDER_TIMEOUT": "انتهاء مهلة المزود",
              "PROVIDER_SERVER_ERROR": "خطأ خادم المزود",
              "OBSERVATION_FEED_MISMATCH": "مصدر مختلف عن المطلوب",
              "PROVIDER_REQUEST_FAILED": "فشل طلب المزود",
              "LEGACY_UNCLASSIFIED": "سجل سابق دون سبب مفصل"}
    return " | ".join(f"{labels.get(reason, reason)}: {count}"
                      for reason, count in sorted(counts.items()))

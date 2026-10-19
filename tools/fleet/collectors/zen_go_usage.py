"""OpenCode Go account usage via the official `GET /zen/go/v1/usage` endpoint.

Why this exists (2026-09-04): the Go plan's rolling-5h/weekly/monthly limits were
console-only ("no usage api — plan quota is console-only") until upstream shipped the
public usage endpoint (anomalyco/opencode #31084, PR #2879; #16017 tracker). Probed live
2026-09-04: the public endpoint accepts the active OpenCode OAuth or the OpenCode Go
API key. V2 reads the active opencode integration first, uses opencode-go only when
OAuth is absent, and keeps auth.json as a compatibility source. Same observer posture
as usage_api.py: bounded READ-ONLY GET, TTL-cached, no token refresh or credential writes.

Response shape (probed 2026-09-04): {"usage": {"rolling" | "weekly" | "monthly":
{"status", "percent", "resetsAt"(ISO-8601)}}}. rolling maps to the legacy 5h slot;
weekly is a Mon 00:00 window, NOT rolling 7d, so it must not claim the rl_7d slot —
it rides rl_windows with its true label.
"""
import json
import hashlib
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

_TTL = 180.0
_STALE_MAX = 900.0
_cache = {"ts": 0.0, "ok_ts": 0.0, "data": None, "scope": None}

_ENDPOINT = "https://opencode.ai/zen/go/v1/usage"
_USER_AGENT = "opencode-fleet-usage/1"


def _runtime():
    utilities = Path(__file__).resolve().parents[3] / "utilities"
    if str(utilities) not in sys.path:
        sys.path.insert(0, str(utilities))
    import opencode_runtime
    return opencode_runtime


def _auth_path():
    if os.environ.get("OPENCODE_AUTH"):
        return os.environ["OPENCODE_AUTH"]
    return str(_runtime().opencode_data_dir() / "auth.json")


def _token():
    runtime = _runtime()
    if runtime.is_v2():
        credential = _v2_usage_credential(runtime)
        return credential.get("token") if credential else None
    credential = _legacy_go_credential(runtime)
    return credential.get("token") if credential else None


def _native_credential(integration_id):
    return _native_credential_row(integration_id, _runtime().read_credential(integration_id))


def _native_credential_row(integration_id, row):
    if not isinstance(row, dict):
        return None
    value = row.get("value") or {}
    token = (value.get("access") if value.get("type") == "oauth" else
             value.get("key") if value.get("type") in {"key", "api"} else None)
    return {"token": token if isinstance(token, str) and token else None,
            "integration_id": integration_id,
            "credential_type": value.get("type")}


def _legacy_go_credential(runtime=None):
    runtime = runtime or _runtime()
    row = runtime.read_legacy_credential("opencode-go", auth_path=_auth_path())
    if not isinstance(row, dict):
        return None
    value = row.get("value") or {}
    token = value.get("key") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token:
        return {"token": None, "scope": "unscoped", "integration_id": "opencode-go",
                "credential_type": "legacy-invalid"}
    identity = row.get("label") or token
    scope = hashlib.sha256(("opencode-go\0" + str(identity)).encode()).hexdigest()[:20]
    return {"token": token, "scope": scope, "integration_id": "opencode-go",
            "credential_type": "legacy-key"}


def _v2_usage_credential(runtime=None):
    """OpenCode's active OAuth owns Go first; never bypass a present rejection."""
    runtime = runtime or _runtime()
    selected = runtime.selected_go_usage_credential()
    if selected is None:
        return None
    return _native_credential_row(selected["integration_id"], selected["credential"])


def _record_status(provider, scope, state, reason=None, source="zen-go-usage"):
    try:
        from . import usage_cache
        usage_cache.record_provider("opencode", provider, "*", scope, state,
                                    reason=reason, source=source)
    except Exception:
        pass


def _epoch(v):
    """resetsAt → epoch seconds; accepts epoch numbers or ISO-8601 strings, else None."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None


def _request_usage(tok):
    if not tok:
        return None, None
    # Cloudflare in front of opencode.ai bans the default `Python-urllib/3.x`
    # fingerprint with 403 "error code: 1010" (measured 2026-09-04: same key,
    # same machine — 403 with the default UA, 200 with any explicit UA). An
    # explicit User-Agent is therefore load-bearing here, not cosmetic.
    req = urllib.request.Request(
        _ENDPOINT, headers={"Authorization": "Bearer " + tok,
                            "Content-Type": "application/json",
                            "User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(64 * 1024)
        except Exception:
            raw = b""
        return None, (e.code, raw)
    except Exception:
        return None, None
    return d, None


def _usage_payload(d):
    windows_in = (d if isinstance(d, dict) else {}).get("usage")
    if not isinstance(windows_in, dict):
        return None
    out = {"rl_5h": None, "rl_7d": None, "rl_ms": [], "rs_5h": None, "rs_7d": None,
           "rl_windows": []}
    for key, label in (("rolling", "5h"), ("weekly", "wk"), ("monthly", "mo")):
        w = windows_in.get(key)
        if not isinstance(w, dict) or not isinstance(w.get("percent"), (int, float)):
            continue
        pct = round(w["percent"])
        reset = _epoch(w.get("resetsAt"))
        out["rl_windows"].append([label, pct, reset])
        if key == "rolling":
            out["rl_5h"], out["rs_5h"] = pct, reset
    if not out["rl_windows"]:
        return None
    return out


def _fetch_v1():
    tok = _token()
    if not tok:
        return None
    body, rejection = _request_usage(tok)
    if rejection:
        code, raw = rejection
        status = _runtime().classify_usage_rejection(code, raw)
        if status:
            reason = status["reason"]
            return {"error": "auth", "detail": code, "reason": reason}
        return None
    return _usage_payload(body)


def _fetch_v2():
    runtime = _runtime()
    credential = _v2_usage_credential(runtime)
    if credential is None:
        return {"error": "missing-credential", "reason": "credential-not-found",
                "provider_availability": {}}
    if not credential.get("token"):
        return {"error": "unknown", "reason": "credential-invalid",
                "provider_availability": {}}

    integration = credential["integration_id"]
    oauth = integration == "opencode"
    scope = runtime.provider_account_scope("opencode-go")
    entitlement_scope = runtime.account_scope("opencode") if oauth else None
    observed_at = time.time()

    def provider_state(state, reason, *, account_scope=scope, http_status=None):
        result = {"state": state, "reason": reason, "account_scope": account_scope,
                  "observed_at": observed_at}
        if http_status is not None:
            result["http_status"] = http_status
        return result

    body, rejection = _request_usage(credential["token"])
    if rejection:
        code, raw = rejection
        classified = runtime.classify_usage_rejection(code, raw)
        if not classified:
            return {"error": "unknown", "reason": "usage-observation-unavailable",
                    "provider_availability": {"opencode-go": provider_state(
                        "unknown", "unclassified-http", http_status=code)}}
        reason = classified["reason"]
        _record_status("opencode-go", scope, "unavailable", reason,
                       source="active-opencode-oauth" if oauth else "opencode-go-api-key")
        states = {"opencode-go": provider_state(
            "unavailable", reason, http_status=code)}
        if oauth:
            entitlement = provider_state("unavailable", reason,
                                         account_scope=entitlement_scope, http_status=code)
            states["opencode-go-entitlement"] = entitlement
            _record_status("opencode-go-entitlement", entitlement_scope, "unavailable",
                           reason, source="active-opencode-oauth")
        error = "subscription-missing" if reason == "subscription-missing" else "auth"
        return {"error": error, "detail": code, "reason": reason,
                "provider_availability": states}

    if body is None:
        return {"error": "unknown", "reason": "usage-observation-unavailable",
                "provider_availability": {"opencode-go": provider_state(
                    "unknown", "usage-observation-unavailable")}}
    usage = _usage_payload(body)
    if usage is None:
        return {"error": "unknown", "reason": "usage-response-invalid",
                "provider_availability": {"opencode-go": provider_state(
                    "unknown", "usage-response-invalid")}}
    _record_status("opencode-go", scope, "usable",
                   source="active-opencode-oauth" if oauth else "opencode-go-api-key")
    states = {"opencode-go": provider_state("usable", None)}
    if oauth:
        states["opencode-go-entitlement"] = provider_state(
            "usable", None, account_scope=entitlement_scope)
        _record_status("opencode-go-entitlement", entitlement_scope, "usable",
                       source="active-opencode-oauth")
    usage["provider_availability"] = states
    return usage


def _fetch():
    return _fetch_v2() if _runtime().is_v2() else _fetch_v1()


def _usage_account_scope():
    runtime = _runtime()
    if runtime.is_v2():
        credential = _v2_usage_credential(runtime)
        return runtime.provider_account_scope("opencode-go") if credential else "unscoped"
    return runtime.account_scope("opencode-go")


def account_usage():
    """TTL-cached Go plan usage {rl_5h, rl_7d, rl_windows} for the opencode-go plan, or None.

    Serve stale quota windows on transient failure. A confirmed credential or entitlement
    rejection is returned immediately, while the usage cache keeps its original quota
    observation timestamp. A rejected account never inherits a prior success as eligibility."""
    now = time.time()
    scope = _usage_account_scope()
    if _cache.get("scope") != scope:
        _cache.update(ts=0.0, ok_ts=0.0, data=None, scope=scope)
    if now - _cache["ts"] > _TTL:
        d = _fetch()
        _cache["ts"] = now              # failures throttle too — no retry storm inside the TTL
        if isinstance(d, dict) and d.get("error") in ("auth", "subscription-missing"):
            previous = _cache.get("data")
            if isinstance(previous, dict):
                quota = {key: value for key, value in previous.items()
                         if key in {"rl_5h", "rl_7d", "rl_ms", "rs_5h", "rs_7d", "rl_windows"}}
                _cache["data"] = {**quota, **d}
            else:
                _cache["data"] = d
        elif isinstance(d, dict) and "error" not in d:
            _cache["data"] = d
            _cache["ok_ts"] = now
        elif now - _cache["ok_ts"] > _STALE_MAX:
            _cache["data"] = d           # None (no key / transient) or the error marker
    return _cache["data"]

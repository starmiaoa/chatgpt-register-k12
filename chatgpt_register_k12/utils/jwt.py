"""JWT decoding helpers — no dependencies beyond Python stdlib.

Used by the sub2api exporter to extract claims from OpenAI access tokens
and id_tokens (email, chatgpt_account_id, plan_type, exp, etc.).
"""

from __future__ import annotations

import base64
import json


def _base64url_decode(data: str) -> bytes:
    """Decode a base64url-encoded string (with padding restored)."""
    padded = data + "=" * (4 - len(data) % 4) if len(data) % 4 else data
    return base64.urlsafe_b64decode(padded)


def decode_jwt_payload(token: str) -> dict:
    """Decode the payload (middle segment) of a JWT without verification.

    Returns an empty dict on any decode error — the caller should handle
    missing claims gracefully.
    """
    if not token:
        return {}
    try:
        parts = str(token).split(".")
        if len(parts) < 2:
            return {}
        payload_bytes = _base64url_decode(parts[1].replace("-", "+").replace("_", "/"))
        payload = json.loads(payload_bytes)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def extract_account_info(token: str) -> dict:
    """Extract account info from an OpenAI access token or id_token JWT.

    Returns a flat dict with keys matching sub2api credentials schema:
      email, chatgpt_account_id, chatgpt_user_id, plan_type, exp, organization_id

    All values are strings or empty strings if not found.
    """
    payload = decode_jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth", {})
    if not isinstance(auth, dict):
        auth = {}
    profile = payload.get("https://api.openai.com/profile", {})
    if not isinstance(profile, dict):
        profile = {}

    exp = payload.get("exp", 0)
    try:
        exp_int = int(exp)
    except (TypeError, ValueError):
        exp_int = 0

    return {
        "email": str(profile.get("email") or ""),
        "chatgpt_account_id": str(auth.get("chatgpt_account_id") or ""),
        "chatgpt_user_id": str(auth.get("chatgpt_user_id") or ""),
        "plan_type": str(auth.get("chatgpt_plan_type") or "free"),
        "exp": exp_int,
        "organization_id": str(auth.get("organization_id") or ""),
    }


def backfill_id_token_claims(
    id_token: str,
    chatgpt_account_id: str = "",
    account_id: str = "",
) -> str:
    """Ensure an id_token JWT contains chatgpt_account_id and account_id in
    its https://api.openai.com/auth claim.

    If either is missing, a compatibility id_token is synthesized.
    Returns the (possibly modified) id_token string.
    """
    if not id_token:
        return id_token

    payload = decode_jwt_payload(id_token)
    auth = payload.get("https://api.openai.com/auth", {})
    if not isinstance(auth, dict):
        auth = {}

    existing_account = str(auth.get("chatgpt_account_id") or "")
    existing_user = str(auth.get("account_id") or "")

    need_account = not existing_account and chatgpt_account_id
    need_user = not existing_user and account_id

    if not need_account and not need_user:
        return id_token

    # Rebuild the auth claim
    new_auth = dict(auth)
    if need_account:
        new_auth["chatgpt_account_id"] = chatgpt_account_id
    if need_user:
        new_auth["account_id"] = account_id

    new_payload = {**payload, "https://api.openai.com/auth": new_auth}

    # Encode new header + payload, keep original signature
    header_bytes = _base64url_decode(
        id_token.split(".")[0].replace("-", "+").replace("_", "/")
    )
    new_payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(new_payload).encode())
        .rstrip(b"=")
        .decode()
    )
    signature = id_token.split(".")[2] if len(id_token.split(".")) > 2 else ""
    header_b64 = (
        base64.urlsafe_b64encode(header_bytes).rstrip(b"=").decode()
    )
    return f"{header_b64}.{new_payload_b64}.{signature}"

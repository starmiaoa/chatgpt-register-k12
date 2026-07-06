"""PKCE (Proof Key for Code Exchange) generation for OAuth 2.0 + PKCE flow.

Used by both the registration and login flows to generate code_verifier
and code_challenge for the OpenAI OAuth authorize endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import secrets


def generate_pkce() -> tuple[str, str]:
    """Generate a PKCE code_verifier and its S256 code_challenge.

    Returns:
        (code_verifier, code_challenge) — both base64url-encoded
        with padding stripped, as required by Auth0 / OpenAI OAuth.
    """
    code_verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(64))
        .rstrip(b"=")
        .decode("ascii")
    )
    code_challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    return code_verifier, code_challenge

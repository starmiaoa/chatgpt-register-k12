"""Mail provider for ChatGPT registration — OAuth mailbox pools.

Supports:
  - outlook_token: email----password----client_id----refresh_token
  - gmail_oauth:  email----client_id----client_secret----refresh_token
"""

from __future__ import annotations

import hashlib
import imaplib
import json
import re
import time
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from curl_cffi import requests

from chatgpt_register_sub2api.workspace_state import (
    claim_workspace_email,
    set_workspace_email_state,
    workspace_email_available,
)
from chatgpt_register_sub2api.utils.proxy import normalize_proxy_url

# ── Data directory (stores pool state) ─────────────────────────────

DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "outlook_token_state.json"

# ── Outlook constants ───────────────────────────────────────────────

OUTLOOK_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
OUTLOOK_GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
OUTLOOK_GRAPH_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"
OUTLOOK_IMAP_SCOPE = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
OUTLOOK_DEFAULT_IMAP_HOST = "outlook.office365.com"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_IMAP_SCOPE = "https://mail.google.com/"
GMAIL_DEFAULT_IMAP_HOST = "imap.gmail.com"

# ── Pool state tracking ─────────────────────────────────────────────

_mail_provider_state_lock = Lock()
_outlook_token_state_lock = _mail_provider_state_lock
OUTLOOK_IN_USE_STALE_SECONDS = 3600  # 1 hour stale timeout
OUTLOOK_UNAVAILABLE_STATES = {"used", "token_invalid", "failed"}


def _split_email(address: str) -> tuple[str, str]:
    text = str(address or "").strip()
    if "@" not in text:
        return text, ""
    local, domain = text.rsplit("@", 1)
    return local, domain


def _base_plus_address(address: str) -> str:
    local, domain = _split_email(address)
    if not domain:
        return str(address or "").strip().lower()
    return f"{local.split('+', 1)[0]}@{domain}".lower()


def _plus_alias_address(base_address: str, alias_index: int) -> str:
    local, domain = _split_email(base_address)
    if not domain:
        return str(base_address or "").strip()
    if alias_index <= 0:
        return f"{local}@{domain}"
    return f"{local}+{alias_index}@{domain}"


def _plus_aliases(base_address: str, limit: int) -> list[tuple[int, str]]:
    return [
        (index, _plus_alias_address(base_address, index))
        for index in range(max(1, int(limit)))
    ]


def _credential_state_key(address: str, provider: str) -> str:
    return _state_key(f"credential:{_base_plus_address(address)}", provider)


def _credential_unavailable(
    store: dict[str, dict[str, Any]],
    address: str,
    provider: str,
) -> bool:
    entry = store.get(_credential_state_key(address, provider))
    if isinstance(entry, dict) and str(entry.get("state") or "") in {
        "token_invalid",
        "failed",
    }:
        return True
    base_entry = _state_entry(store, _base_plus_address(address), provider)
    if isinstance(base_entry, dict) and str(base_entry.get("state") or "") == "token_invalid":
        return True
    return False


def _bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _positive_int(value: Any, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def _state_key(address: str, provider: str = "") -> str:
    target = str(address or "").strip().lower()
    provider_name = str(provider or "").strip().lower()
    if not target:
        return ""
    return f"{provider_name}:{target}" if provider_name else target


def _state_candidates(address: str, provider: str = "") -> list[str]:
    target = str(address or "").strip().lower()
    if not target:
        return []
    keys: list[str] = []
    provider_key = _state_key(target, provider)
    if provider_key:
        keys.append(provider_key)
    # Backward compatibility with the original email-only state keys.
    if target not in keys:
        keys.append(target)
    return keys


def _state_entry(
    store: dict[str, dict[str, Any]],
    address: str,
    provider: str = "",
) -> dict[str, Any] | None:
    for key in _state_candidates(address, provider):
        entry = store.get(key)
        if isinstance(entry, dict):
            return entry
    return None


def _load_state(state_file: Path = STATE_FILE) -> dict[str, dict[str, Any]]:
    """Load pool state from disk (email_lower → {state, reason, updated_at})."""
    path = Path(state_file)
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    state: dict[str, dict[str, Any]] = {}
    if isinstance(data, list):
        for item in data:
            key = str(item).strip().lower()
            if key:
                state[key] = {"state": "used", "reason": "", "updated_at": ""}
    elif isinstance(data, dict):
        for key, value in data.items():
            email = str(key).strip().lower()
            if not email:
                continue
            if isinstance(value, dict):
                entry = dict(value)
                entry["state"] = str(entry.get("state") or "used").strip() or "used"
                entry["reason"] = str(entry.get("reason") or "")
                entry["updated_at"] = str(entry.get("updated_at") or "")
                state[email] = entry
            else:
                state[email] = {
                    "state": str(value or "used").strip() or "used",
                    "reason": "",
                    "updated_at": "",
                }
    return state


def _save_state(
    state: dict[str, dict[str, Any]],
    state_file: Path = STATE_FILE,
) -> None:
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {key: state[key] for key in sorted(state)}
    path.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _entry_available(entry: dict[str, Any] | None) -> bool:
    """Check if this email is available for use."""
    if not isinstance(entry, dict):
        return True
    current = str(entry.get("state") or "")
    if current in OUTLOOK_UNAVAILABLE_STATES:
        return False
    if current == "in_use":
        updated_at = str(entry.get("updated_at") or "")
        try:
            ts = datetime.fromisoformat(updated_at)
            age = (
                datetime.now(timezone.utc)
                - (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))
            ).total_seconds()
            return age >= OUTLOOK_IN_USE_STALE_SECONDS
        except Exception:
            return True
    return True


def _entry_available_for_workspace(
    entry: dict[str, Any] | None,
    *,
    workspace_scoped: bool,
) -> bool:
    if not workspace_scoped:
        return _entry_available(entry)
    if not isinstance(entry, dict):
        return True
    current = str(entry.get("state") or "")
    if current == "token_invalid":
        return False
    if current == "in_use":
        return _entry_available(entry)
    return True


def _set_state(
    address: str,
    state: str,
    reason: str = "",
    state_file: Path = STATE_FILE,
    provider: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    target = str(address or "").strip().lower()
    if not target:
        return
    with _mail_provider_state_lock:
        store = _load_state(state_file)
        entry = {
            "state": str(state),
            "reason": str(reason or ""),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            entry.update(extra)
        store[_state_key(target, provider)] = entry
        _save_state(store, state_file)


def _release_state(
    address: str,
    state_file: Path = STATE_FILE,
    provider: str = "",
) -> None:
    """Release in_use state back to unused."""
    target = str(address or "").strip().lower()
    if not target:
        return
    with _mail_provider_state_lock:
        store = _load_state(state_file)
        changed = False
        for key in _state_candidates(target, provider):
            entry = store.get(key)
            if isinstance(entry, dict) and str(entry.get("state") or "") == "in_use":
                store.pop(key, None)
                changed = True
        if changed:
            _save_state(store, state_file)


# ── Credential parsing ──────────────────────────────────────────────


def parse_outlook_credentials(text: str) -> list[dict[str, str]]:
    """Parse outlook token pool text.

    Format: email----password----client_id----refresh_token (one per line)
    """
    credentials: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = str(raw_line or "").strip()
        if not line or "----" not in line:
            continue
        parts = [str(p).strip() for p in line.split("----", 3)]
        if len(parts) != 4:
            continue
        email, password, client_id, refresh_token = parts
        if "@" not in email or not client_id or not refresh_token:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        credentials.append(
            {
                "email": email,
                "password": password,
                "client_id": client_id,
                "refresh_token": refresh_token,
            }
        )
    return credentials


def parse_gmail_credentials(text: str) -> list[dict[str, str]]:
    """Parse Gmail OAuth pool text.

    Format: email----client_id----client_secret----refresh_token
    """
    credentials: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = str(raw_line or "").strip()
        if not line or "----" not in line:
            continue
        parts = [str(p).strip() for p in line.split("----", 3)]
        if len(parts) != 4:
            continue
        email, client_id, client_secret, refresh_token = parts
        if "@" not in email or not client_id or not client_secret or not refresh_token:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        credentials.append(
            {
                "email": email,
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            }
        )
    return credentials


def configured_mailboxes(mail_config: dict) -> list[dict[str, Any]]:
    """Return configured mailbox candidates without mutating provider state."""
    candidates: list[dict[str, Any]] = []
    conf = _make_config(mail_config)
    provider_entries = _provider_entries(mail_config)
    for entry in provider_entries:
        provider_type = str(entry.get("type") or "").strip()
        provider_ref = str(entry.get("provider_ref") or "").strip()
        label = str(entry.get("label") or provider_ref or provider_type)
        if provider_type == OutlookTokenProvider.name:
            pool = parse_outlook_credentials(
                str(entry.get("mailboxes") or entry.get("pool") or "")
            )
            alias_enabled = _bool_value(
                entry.get("alias_enabled"),
                _bool_value(conf.get("alias_enabled"), False),
            )
            alias_limit = _positive_int(
                entry.get("alias_limit_per_mailbox")
                or conf.get("alias_limit_per_mailbox")
                or 6,
                6,
            )
            for credential in pool:
                base_address = _base_plus_address(credential["email"])
                aliases = _plus_aliases(base_address, alias_limit if alias_enabled else 1)
                for alias_index, alias_address in aliases:
                    candidates.append(
                        {
                            "provider": provider_type,
                            "provider_ref": provider_ref,
                            "label": label,
                            "address": alias_address,
                            "base_address": base_address,
                            "login_address": base_address,
                            "alias_index": alias_index,
                            "alias_limit_per_mailbox": alias_limit,
                            "password": credential["password"],
                            "client_id": credential["client_id"],
                            "refresh_token": credential["refresh_token"],
                        }
                    )
        elif provider_type == GmailOAuthProvider.name:
            for credential in parse_gmail_credentials(
                str(entry.get("mailboxes") or entry.get("pool") or "")
            ):
                candidates.append(
                    {
                        "provider": provider_type,
                        "provider_ref": provider_ref,
                        "label": label,
                        "address": credential["email"],
                        "client_id": credential["client_id"],
                        "client_secret": credential["client_secret"],
                        "refresh_token": credential["refresh_token"],
                    }
                )
    return candidates


# ── Code extraction ─────────────────────────────────────────────────


def _extract_code(message: dict[str, Any]) -> str | None:
    """Extract 6-digit verification code from email content."""
    content = (
        f"{message.get('subject', '')}\n"
        f"{message.get('text_content', '')}\n"
        f"{message.get('html_content', '')}"
    ).strip()
    if not content:
        return None

    # OpenAI styled <p> with background-color: #F3F3F3
    match = re.search(
        r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?(\d{6})[\s\S]*?</p>",
        content,
        re.I,
    )
    if match:
        return match.group(1)

    # Text patterns
    match = re.search(
        r"(?:Verification code|code is|代码为|验证码)[:\s]*(\d{6})",
        content,
        re.I,
    )
    if match and match.group(1) != "177010":
        return match.group(1)

    # Generic 6-digit codes (excluding known false positive 177010)
    for code in re.findall(r">\s*(\d{6})\s*<|(?<![#&])\b(\d{6})\b", content):
        value = code[0] or code[1]
        if value and value != "177010":
            return value

    return None


def _message_tracking_ref(message: dict[str, Any]) -> str:
    """Create a content-based tracking reference for deduplication."""
    provider = str(message.get("provider") or "").strip()
    mailbox = str(message.get("mailbox") or "").strip()
    message_id = str(message.get("message_id") or "").strip()
    if message_id:
        return f"id:{provider}:{mailbox}:{message_id}"

    received_at = message.get("received_at")
    received_value = (
        received_at.isoformat()
        if isinstance(received_at, datetime)
        else str(received_at or "")
    )
    content = "\n".join(
        str(message.get(key) or "")
        for key in ("subject", "sender", "text_content", "html_content")
    )
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    return f"content:{provider}:{mailbox}:{received_value}:{digest}"


def _message_before_code_boundary(
    mailbox: dict[str, Any], message: dict[str, Any]
) -> bool:
    """Check if message arrived before the code boundary timestamp."""
    boundary = mailbox.get("_code_not_before")
    received_at = message.get("received_at")
    if not isinstance(boundary, datetime) or not isinstance(received_at, datetime):
        return False
    if not received_at.tzinfo:
        received_at = received_at.replace(tzinfo=timezone.utc)
    return received_at < boundary


def _message_matches_mailbox(mailbox: dict[str, Any], message: dict[str, Any]) -> bool:
    """When recipient data is available, only accept OTPs sent to this alias."""
    target = str(mailbox.get("address") or "").strip().lower()
    recipients = message.get("recipients")
    if not target or not isinstance(recipients, list) or not recipients:
        return True
    return target in {str(item or "").strip().lower() for item in recipients}


def _message_code_priority(message: dict[str, Any], hint: str = "") -> tuple[int, float]:
    """Rank OTP messages so registration doesn't pick a login code first."""
    subject = str(message.get("subject") or "").lower()
    hint = str(hint or "").strip().lower()
    received_at = message.get("received_at")
    timestamp = 0.0
    if isinstance(received_at, datetime):
        value = received_at if received_at.tzinfo else received_at.replace(tzinfo=timezone.utc)
        timestamp = value.timestamp()

    is_login = any(
        marker in subject
        for marker in ("login code", "sign-in code", "sign in code", "登录")
    )
    is_verification = any(
        marker in subject
        for marker in ("verification code", "验证码", "验证")
    )

    if hint == "login":
        priority = 0 if is_login else 2 if is_verification else 1
    elif hint == "verification":
        priority = 0 if is_verification else 2 if is_login else 1
    else:
        priority = 0 if is_verification else 1 if is_login else 2

    return priority, -timestamp


def _parse_received_at(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        date = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        date = parsedate_to_datetime(text)
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ── Provider classes ────────────────────────────────────────────────


class MailProviderTokenError(RuntimeError):
    """refresh_token exchange failed (invalid/expired credentials)."""


class OutlookTokenError(MailProviderTokenError):
    """refresh_token exchange failed (invalid/expired credentials)."""


class BaseMailProvider:
    """Abstract base for mail providers."""

    name = "unknown"

    def __init__(self, conf: dict, provider_ref: str = ""):
        self.conf = conf
        self.provider_ref = provider_ref

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}

        deadline = time.monotonic() + self.conf["wait_timeout"]
        while time.monotonic() < deadline:
            message = self.fetch_latest_message(mailbox)
            if message:
                ref = _message_tracking_ref(message)
                if ref not in seen_refs and not _message_matches_mailbox(mailbox, message):
                    seen_refs.add(ref)
                    seen_value.append(ref)
                elif ref not in seen_refs:
                    code = _extract_code(message)
                    if code:
                        seen_value.append(ref)
                        return code
                    seen_refs.add(ref)
            time.sleep(max(0.2, self.conf["wait_interval"]))
        return None

    def close(self) -> None:
        pass

    def _make_session(self):
        proxy = normalize_proxy_url(str(self.conf.get("proxy") or "").strip())
        kwargs = {"impersonate": "chrome", "verify": True}
        if proxy:
            kwargs["proxy"] = proxy
        return requests.Session(**kwargs)

    def _imap_messages(
        self,
        mailbox: dict[str, Any],
        access_token: str,
        imap_host: str,
        label: str,
        message_limit: int,
    ) -> list[dict[str, Any]]:
        login_address = (
            str(mailbox.get("login_address") or mailbox.get("base_address") or "").strip()
            or str(mailbox["address"])
        )
        auth_string = (
            f"user={login_address}\x01auth=Bearer {access_token}\x01\x01"
        )
        imap = imaplib.IMAP4_SSL(imap_host)
        try:
            imap.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
            status, _ = imap.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError(f"{label} IMAP select INBOX failed")
            status, data = imap.uid("search", None, "ALL")
            if status != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()[-message_limit:]
            messages: list[dict[str, Any]] = []
            for uid in reversed(uids):
                status, fetched = imap.uid("fetch", uid, "(RFC822)")
                if status != "OK":
                    continue
                raw_payload = next(
                    (
                        part[1]
                        for part in fetched
                        if isinstance(part, tuple) and isinstance(part[1], bytes)
                    ),
                    b"",
                )
                if raw_payload:
                    messages.append(
                        self._parse_imap_message(mailbox, raw_payload)
                    )
            return messages
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def _parse_imap_message(
        self,
        mailbox: dict[str, Any],
        raw: bytes,
    ) -> dict[str, Any]:
        login_address = (
            str(mailbox.get("login_address") or mailbox.get("base_address") or "").strip()
            or str(mailbox.get("address") or "").strip()
        )
        message = message_from_bytes(raw, policy=policy.default)
        try:
            received = _parse_received_at(
                parsedate_to_datetime(str(message.get("Date") or ""))
            )
        except Exception:
            received = None
        plain: list[str] = []
        html: list[str] = []
        for part in message.walk() if message.is_multipart() else [message]:
            if part.get_content_maintype() == "multipart":
                continue
            try:
                payload = part.get_content()
            except Exception:
                continue
            if not payload:
                continue
            if part.get_content_type() == "text/html":
                html.append(str(payload))
            else:
                plain.append(str(payload))

        def _decode(value: str | None) -> str:
            if not value:
                return ""
            try:
                return str(make_header(decode_header(value)))
            except Exception:
                return value

        recipients = [
            address.lower()
            for _, address in getaddresses(
                [
                    str(message.get("To") or ""),
                    str(message.get("Cc") or ""),
                    str(message.get("Delivered-To") or ""),
                    str(message.get("X-Original-To") or ""),
                ]
            )
            if address
        ]

        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "login_mailbox": login_address,
            "recipients": recipients,
            "message_id": _decode(str(message.get("Message-ID") or "")),
            "subject": _decode(str(message.get("Subject") or "")),
            "sender": _decode(str(message.get("From") or "")),
            "text_content": "\n".join(plain).strip(),
            "html_content": "\n".join(html).strip(),
            "received_at": received,
            "raw": None,
        }


class OutlookTokenProvider(BaseMailProvider):
    """Use Outlook/Hotmail refresh_token to read verification codes.

    Pool entries: email----password----client_id----refresh_token
    Supports Graph API and IMAP modes for reading mail.
    """

    name = "outlook_token"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.label = str(entry.get("label") or self.provider_ref)
        self.pool = parse_outlook_credentials(
            str(entry.get("mailboxes") or entry.get("pool") or "")
        )
        self.mode = str(entry.get("mode") or "graph").strip().lower() or "graph"
        if self.mode not in {"graph", "imap", "auto"}:
            self.mode = "graph"
        self.imap_host = (
            str(entry.get("imap_host") or OUTLOOK_DEFAULT_IMAP_HOST).strip()
            or OUTLOOK_DEFAULT_IMAP_HOST
        )
        self.message_limit = max(1, int(entry.get("message_limit") or 10))
        self.alias_enabled = _bool_value(
            entry.get("alias_enabled"),
            _bool_value(conf.get("alias_enabled"), False),
        )
        self.alias_limit_per_mailbox = _positive_int(
            entry.get("alias_limit_per_mailbox")
            or conf.get("alias_limit_per_mailbox")
            or 6,
            6,
        )
        self.state_file = Path(conf.get("state_file") or STATE_FILE)
        self.workspace_id = str(conf.get("workspace_id") or "").strip()
        self.workspace_state_file = Path(
            conf.get("workspace_state_file") or DATA_DIR / "workspace_account_state.json"
        )
        self.session = self._make_session()

    def _make_session(self):
        return super()._make_session()

    def close(self) -> None:
        self.session.close()

    # ── Token exchange ──────────────────────────────────────────

    def _exchange_refresh_token(
        self, client_id: str, refresh_token: str, scope: str
    ) -> str:
        resp = self.session.post(
            OUTLOOK_TOKEN_URL,
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": scope,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self.conf["user_agent"],
            },
            timeout=self.conf["request_timeout"],
            verify=True,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200:
            detail = (
                data.get("error_description")
                or data.get("error")
                or resp.text[:300]
            )
            raise OutlookTokenError(
                f"OutlookToken refresh failed: HTTP {resp.status_code}, {detail}"
            )
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise OutlookTokenError(
                "OutlookToken refresh response missing access_token"
            )
        return access_token

    def _cached_access_token(
        self, mailbox: dict[str, Any], client_id: str, refresh_token: str, scope: str
    ) -> str:
        """Cache access_token for 10 min to avoid rate limits during polling."""
        cache = mailbox.get("_outlook_token_cache")
        if not isinstance(cache, dict):
            cache = {}
            mailbox["_outlook_token_cache"] = cache
        cached = cache.get(scope)
        if (
            isinstance(cached, tuple)
            and len(cached) == 2
            and time.monotonic() < cached[1]
        ):
            return str(cached[0])
        token = self._exchange_refresh_token(client_id, refresh_token, scope)
        cache[scope] = (token, time.monotonic() + 600)
        return token

    # ── Mailbox creation ─────────────────────────────────────────

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.pool:
            raise RuntimeError(
                "OutlookToken pool is empty. "
                "Import email----password----client_id----refresh_token lines."
        )
        with _mail_provider_state_lock:
            store = _load_state(self.state_file)
            selected: tuple[dict[str, str], int, str] | None = None
            for item in self.pool:
                base_address = _base_plus_address(item["email"])
                if _credential_unavailable(store, base_address, self.name):
                    continue
                aliases = _plus_aliases(
                    base_address,
                    self.alias_limit_per_mailbox if self.alias_enabled else 1,
                )
                for alias_index, alias_address in aliases:
                    if not _entry_available_for_workspace(
                        _state_entry(store, alias_address, self.name),
                        workspace_scoped=bool(self.workspace_id),
                    ):
                        continue
                    if self.workspace_id and not workspace_email_available(
                        self.workspace_state_file,
                        self.workspace_id,
                        alias_address,
                    ):
                        continue
                    if self.workspace_id and not claim_workspace_email(
                        self.workspace_state_file,
                        self.workspace_id,
                        alias_address,
                        mode="register",
                        extra={
                            "provider": self.name,
                            "base_address": base_address,
                            "alias_index": alias_index,
                        },
                    ):
                        continue
                    selected = (item, alias_index, alias_address)
                    break
                if selected is not None:
                    break
            if selected is None:
                raise RuntimeError(
                    f"[{self.label}] OutlookToken pool exhausted "
                    f"({len(self.pool)} total). "
                    "All emails/aliases used/failed. Import new emails, "
                    "increase alias_limit_per_mailbox, or reset pool state."
                )
            credential, alias_index, alias_address = selected
            base_address = _base_plus_address(credential["email"])
            store[_state_key(alias_address, self.name)] = {
                "state": "in_use",
                "reason": "",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "base_address": base_address,
                "alias_index": alias_index,
            }
            _save_state(store, self.state_file)

        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": alias_address,
            "base_address": base_address,
            "login_address": base_address,
            "alias_index": alias_index,
            "alias_limit_per_mailbox": self.alias_limit_per_mailbox,
            "password": credential["password"],
            "label": self.label,
            "client_id": credential["client_id"],
            "refresh_token": credential["refresh_token"],
            "_state_file": str(self.state_file),
            "_workspace_id": self.workspace_id,
            "_workspace_state_file": str(self.workspace_state_file),
        }

    # ── Graph API mail reading ───────────────────────────────────

    def _read_graph(self, access_token: str) -> list[dict[str, Any]]:
        resp = self.session.get(
            OUTLOOK_GRAPH_MESSAGES_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "User-Agent": self.conf["user_agent"],
            },
            params={
                "$top": self.message_limit,
                "$orderby": "receivedDateTime desc",
                "$select": (
                    "subject,receivedDateTime,from,toRecipients,ccRecipients,"
                    "body,bodyPreview"
                ),
            },
            timeout=self.conf["request_timeout"],
            verify=True,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200:
            detail = (
                data.get("error", {}).get("message")
                if isinstance(data.get("error"), dict)
                else resp.text[:300]
            )
            raise RuntimeError(
                f"OutlookToken Graph failed: HTTP {resp.status_code}, {detail}"
            )
        items = data.get("value") if isinstance(data, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    @staticmethod
    def _graph_sender(message: dict[str, Any]) -> str:
        sender = message.get("from") or {}
        if isinstance(sender, dict):
            address = sender.get("emailAddress") or {}
            if isinstance(address, dict):
                return str(address.get("address") or address.get("name") or "")
        return ""

    @staticmethod
    def _graph_recipients(message: dict[str, Any]) -> list[str]:
        recipients: list[str] = []
        for key in ("toRecipients", "ccRecipients"):
            values = message.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict):
                    continue
                email = item.get("emailAddress") or {}
                if not isinstance(email, dict):
                    continue
                address = str(email.get("address") or "").strip().lower()
                if address:
                    recipients.append(address)
        return recipients

    def _normalize_graph_item(
        self, mailbox: dict[str, Any], item: dict[str, Any]
    ) -> dict[str, Any]:
        body = item.get("body") if isinstance(item.get("body"), dict) else {}
        content_type = str(body.get("contentType") or "").lower()
        content = str(body.get("content") or "")
        text_content = (
            content if content_type != "html" else str(item.get("bodyPreview") or "")
        )
        html_content = content if content_type == "html" else ""
        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "login_mailbox": (
                str(mailbox.get("login_address") or mailbox.get("base_address") or "").strip()
                or str(mailbox.get("address") or "").strip()
            ),
            "recipients": self._graph_recipients(item),
            "message_id": str(item.get("id") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": self._graph_sender(item),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(item.get("receivedDateTime")),
            "raw": item,
        }

    def _graph_messages(
        self, mailbox: dict[str, Any], access_token: str
    ) -> list[dict[str, Any]]:
        return [
            self._normalize_graph_item(mailbox, item)
            for item in self._read_graph(access_token)
        ]

    # ── IMAP mail reading ────────────────────────────────────────

    def _imap_messages(
        self, mailbox: dict[str, Any], access_token: str
    ) -> list[dict[str, Any]]:
        return super()._imap_messages(
            mailbox,
            access_token,
            self.imap_host,
            "OutlookToken",
            self.message_limit,
        )

    # ── Message fetching ─────────────────────────────────────────

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        client_id = str(mailbox.get("client_id") or "").strip()
        refresh_token = str(mailbox.get("refresh_token") or "").strip()
        if not client_id or not refresh_token:
            raise RuntimeError(
                "OutlookToken mailbox missing client_id or refresh_token"
            )
        errors: list[str] = []
        if self.mode in {"graph", "auto"}:
            try:
                access_token = self._cached_access_token(
                    mailbox, client_id, refresh_token, OUTLOOK_GRAPH_SCOPE
                )
                return self._graph_messages(mailbox, access_token)
            except Exception as error:
                if self.mode == "graph":
                    raise
                errors.append(f"graph: {error}")
        if self.mode in {"imap", "auto"}:
            try:
                access_token = self._cached_access_token(
                    mailbox, client_id, refresh_token, OUTLOOK_IMAP_SCOPE
                )
                return self._imap_messages(mailbox, access_token)
            except Exception as error:
                if self.mode == "imap":
                    raise
                errors.append(f"imap: {error}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return []

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        """Scan recent N messages for verification code, not just the latest."""
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}
        hint = str(mailbox.get("_code_subject_hint") or "")

        deadline = time.monotonic() + self.conf["wait_timeout"]
        while time.monotonic() < deadline:
            candidates: list[tuple[tuple[int, float], str, str]] = []
            for message in self.fetch_recent_messages(mailbox):
                # Skip messages from before the code boundary
                if _message_before_code_boundary(mailbox, message):
                    continue
                ref = _message_tracking_ref(message)
                if ref in seen_refs:
                    continue
                if not _message_matches_mailbox(mailbox, message):
                    seen_refs.add(ref)
                    seen_value.append(ref)
                    continue
                code = _extract_code(message)
                if code:
                    candidates.append((_message_code_priority(message, hint), ref, code))
                    continue
                seen_refs.add(ref)
            if candidates:
                candidates.sort(key=lambda item: item[0])
                _, ref, code = candidates[0]
                seen_value.append(ref)
                return code
            time.sleep(max(0.2, self.conf["wait_interval"]))
        return None


class GmailOAuthError(MailProviderTokenError):
    """Google refresh_token exchange failed (invalid/expired credentials)."""


class GmailOAuthProvider(BaseMailProvider):
    """Use Gmail refresh_token to read verification codes over IMAP XOAUTH2."""

    name = "gmail_oauth"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.label = str(entry.get("label") or self.provider_ref)
        self.pool = parse_gmail_credentials(
            str(entry.get("mailboxes") or entry.get("pool") or "")
        )
        self.imap_host = (
            str(entry.get("imap_host") or GMAIL_DEFAULT_IMAP_HOST).strip()
            or GMAIL_DEFAULT_IMAP_HOST
        )
        self.message_limit = max(1, int(entry.get("message_limit") or 10))
        self.state_file = Path(conf.get("state_file") or STATE_FILE)
        self.workspace_id = str(conf.get("workspace_id") or "").strip()
        self.workspace_state_file = Path(
            conf.get("workspace_state_file") or DATA_DIR / "workspace_account_state.json"
        )
        self.session = self._make_session()

    def _make_session(self):
        return super()._make_session()

    def close(self) -> None:
        self.session.close()

    def _exchange_refresh_token(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> str:
        resp = self.session.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self.conf["user_agent"],
            },
            timeout=self.conf["request_timeout"],
            verify=True,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200:
            detail = (
                data.get("error_description")
                or data.get("error")
                or resp.text[:300]
            )
            raise GmailOAuthError(
                f"GmailOAuth refresh failed: HTTP {resp.status_code}, {detail}"
            )
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise GmailOAuthError(
                "GmailOAuth refresh response missing access_token"
            )
        return access_token

    def _cached_access_token(
        self,
        mailbox: dict[str, Any],
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> str:
        cache = mailbox.get("_gmail_oauth_cache")
        if not isinstance(cache, dict):
            cache = {}
            mailbox["_gmail_oauth_cache"] = cache
        cached = cache.get("imap")
        if (
            isinstance(cached, tuple)
            and len(cached) == 2
            and time.monotonic() < cached[1]
        ):
            return str(cached[0])
        token = self._exchange_refresh_token(
            client_id,
            client_secret,
            refresh_token,
        )
        cache["imap"] = (token, time.monotonic() + 600)
        return token

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.pool:
            raise RuntimeError(
                "Gmail OAuth pool is empty. "
                "Import email----client_id----client_secret----refresh_token lines."
            )
        with _mail_provider_state_lock:
            store = _load_state(self.state_file)
            credential = next(
                (
                    item
                    for item in self.pool
                    if _entry_available_for_workspace(
                        _state_entry(store, item["email"], self.name),
                        workspace_scoped=bool(self.workspace_id),
                    )
                    and (
                        not self.workspace_id
                        or workspace_email_available(
                            self.workspace_state_file,
                            self.workspace_id,
                            item["email"],
                        )
                    )
                    and (
                        not self.workspace_id
                        or claim_workspace_email(
                            self.workspace_state_file,
                            self.workspace_id,
                            item["email"],
                            mode="register",
                            extra={"provider": self.name},
                        )
                    )
                ),
                None,
            )
            if credential is None:
                raise RuntimeError(
                    f"[{self.label}] Gmail OAuth pool exhausted "
                    f"({len(self.pool)} total). "
                    f"All emails used/failed. Import new emails or reset pool state."
                )
            store[_state_key(credential["email"], self.name)] = {
                "state": "in_use",
                "reason": "",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_state(store, self.state_file)

        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": credential["email"],
            "label": self.label,
            "client_id": credential["client_id"],
            "client_secret": credential["client_secret"],
            "refresh_token": credential["refresh_token"],
            "_state_file": str(self.state_file),
            "_workspace_id": self.workspace_id,
            "_workspace_state_file": str(self.workspace_state_file),
        }

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        client_id = str(mailbox.get("client_id") or "").strip()
        client_secret = str(mailbox.get("client_secret") or "").strip()
        refresh_token = str(mailbox.get("refresh_token") or "").strip()
        if not client_id or not client_secret or not refresh_token:
            raise RuntimeError(
                "GmailOAuth mailbox missing client_id, client_secret, or refresh_token"
            )
        access_token = self._cached_access_token(
            mailbox,
            client_id,
            client_secret,
            refresh_token,
        )
        return super()._imap_messages(
            mailbox,
            access_token,
            self.imap_host,
            "GmailOAuth",
            self.message_limit,
        )

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None


# ── Public API ─────────────────────────────────────────────────────


def _make_config(mail_config: dict) -> dict:
    """Normalize mail config for provider construction."""
    config_dir = str(mail_config.get("_config_dir") or "").strip()
    state_file_value = str(mail_config.get("state_file") or "").strip()
    if state_file_value:
        state_file = Path(state_file_value)
        if not state_file.is_absolute() and config_dir:
            state_file = Path(config_dir) / state_file
    elif config_dir:
        state_file = Path(config_dir) / "data" / "outlook_token_state.json"
    else:
        state_file = STATE_FILE

    return {
        "request_timeout": float(mail_config.get("request_timeout") or 30),
        "wait_timeout": float(mail_config.get("wait_timeout") or 30),
        "wait_interval": float(mail_config.get("wait_interval") or 2),
        "user_agent": str(
            mail_config.get("user_agent")
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ),
        "proxy": str(mail_config.get("proxy") or "").strip(),
        "state_file": str(state_file),
        "alias_enabled": _bool_value(mail_config.get("alias_enabled"), False),
        "alias_limit_per_mailbox": _positive_int(
            mail_config.get("alias_limit_per_mailbox") or 5,
            6,
        ),
    }


def _outlook_entries(mail_config: dict) -> list[dict[str, Any]]:
    providers = (
        mail_config.get("providers")
        if isinstance(mail_config.get("providers"), list)
        else []
    )
    return [
        dict(item, provider_ref=f"outlook_token#{i+1}")
        for i, item in enumerate(providers)
        if isinstance(item, dict)
        and item.get("type") == "outlook_token"
        and item.get("enable", True)
    ]


def _gmail_entries(mail_config: dict) -> list[dict[str, Any]]:
    providers = (
        mail_config.get("providers")
        if isinstance(mail_config.get("providers"), list)
        else []
    )
    return [
        dict(item, provider_ref=f"gmail_oauth#{i+1}")
        for i, item in enumerate(providers)
        if isinstance(item, dict)
        and item.get("type") == "gmail_oauth"
        and item.get("enable", True)
    ]


def _provider_entries(mail_config: dict) -> list[dict[str, Any]]:
    providers = (
        mail_config.get("providers")
        if isinstance(mail_config.get("providers"), list)
        else []
    )
    refs: dict[str, int] = {}
    entries: list[dict[str, Any]] = []
    for item in providers:
        if not isinstance(item, dict) or not item.get("enable", True):
            continue
        provider_type = str(item.get("type") or "").strip()
        if provider_type not in {
            OutlookTokenProvider.name,
            GmailOAuthProvider.name,
        }:
            continue
        refs[provider_type] = refs.get(provider_type, 0) + 1
        entries.append(
            dict(item, provider_ref=f"{provider_type}#{refs[provider_type]}")
        )
    return entries


def _provider_class(provider_type: str):
    return {
        OutlookTokenProvider.name: OutlookTokenProvider,
        GmailOAuthProvider.name: GmailOAuthProvider,
    }.get(str(provider_type or "").strip())


def _entry_has_address(entry: dict[str, Any], address: str) -> bool:
    target = str(address or "").strip().lower()
    if not target:
        return False
    provider_type = str(entry.get("type") or "").strip()
    if provider_type == OutlookTokenProvider.name:
        target = _base_plus_address(target)
        credentials = parse_outlook_credentials(
            str(entry.get("mailboxes") or entry.get("pool") or "")
        )
    elif provider_type == GmailOAuthProvider.name:
        credentials = parse_gmail_credentials(
            str(entry.get("mailboxes") or entry.get("pool") or "")
        )
    else:
        credentials = []
    if provider_type == OutlookTokenProvider.name:
        return any(
            _base_plus_address(str(item.get("email") or "")) == target
            for item in credentials
        )
    return any(str(item.get("email") or "").strip().lower() == target for item in credentials)


def _fill_mailbox_credentials(
    mailbox: dict[str, Any],
    entry: dict[str, Any],
) -> dict[str, Any]:
    if mailbox.get("client_id") and mailbox.get("refresh_token"):
        return mailbox

    address = str(mailbox.get("address") or "").strip().lower()
    if not address:
        return mailbox

    provider_type = str(entry.get("type") or "").strip()
    if provider_type == OutlookTokenProvider.name:
        target = str(mailbox.get("base_address") or _base_plus_address(address)).strip().lower()
        credentials = parse_outlook_credentials(
            str(entry.get("mailboxes") or entry.get("pool") or "")
        )
    elif provider_type == GmailOAuthProvider.name:
        target = address
        credentials = parse_gmail_credentials(
            str(entry.get("mailboxes") or entry.get("pool") or "")
        )
    else:
        target = address
        credentials = []

    credential = next(
        (
            item
            for item in credentials
            if (
                _base_plus_address(str(item.get("email") or ""))
                if provider_type == OutlookTokenProvider.name
                else str(item.get("email") or "").strip().lower()
            ) == target
        ),
        None,
    )
    if credential:
        credential_address = str(credential.get("email") or "").strip()
        mailbox["client_id"] = credential["client_id"]
        mailbox["refresh_token"] = credential["refresh_token"]
        if credential.get("client_secret"):
            mailbox["client_secret"] = credential["client_secret"]
        if credential.get("password"):
            mailbox["password"] = credential["password"]
        if provider_type == OutlookTokenProvider.name:
            if not mailbox.get("base_address"):
                mailbox["base_address"] = _base_plus_address(credential_address)
            if not mailbox.get("login_address"):
                mailbox["login_address"] = credential_address
    return mailbox


def create_mailbox(mail_config: dict, username: str | None = None) -> dict:
    """Create a mailbox from the configured OAuth mailbox pools."""
    provider_entries = _provider_entries(mail_config)
    if not provider_entries:
        raise RuntimeError(
            "No supported mail provider found in mail.providers config"
        )

    conf = _make_config(mail_config)
    last_error = ""
    for entry in provider_entries:
        provider_cls = _provider_class(entry.get("type"))
        if provider_cls is None:
            continue
        provider = provider_cls(entry, conf)
        try:
            mailbox = provider.create_mailbox(username)
            mailbox["_code_not_before"] = datetime.now(timezone.utc)
            return mailbox
        except RuntimeError as error:
            last_error = str(error)
        finally:
            provider.close()
    raise RuntimeError(last_error or "All Outlook providers exhausted")


def wait_for_code(mail_config: dict, mailbox: dict) -> str | None:
    """Wait for verification code from the configured mailbox provider."""
    provider_entries = _provider_entries(mail_config)
    provider_name = str(mailbox.get("provider") or "")
    provider_ref = str(mailbox.get("provider_ref") or "")
    address = str(mailbox.get("address") or "").strip()

    # Try matching by provider_ref, then by provider+address, then by address.
    entry = next(
        (item for item in provider_entries if item.get("provider_ref") == provider_ref),
        None,
    )
    if entry is None:
        entry = next(
            (
                item for item in provider_entries
                if item.get("type") == provider_name and _entry_has_address(item, address)
            ),
            None,
        )
    if entry is None:
        entry = next(
            (item for item in provider_entries if _entry_has_address(item, address)),
            None,
        )
    if entry is None and provider_entries:
        entry = provider_entries[0]
    if entry is None:
        raise RuntimeError(
            f"No mail provider found (ref={provider_ref}, address={address})"
        )

    mailbox = dict(mailbox)
    _fill_mailbox_credentials(mailbox, entry)
    conf = _make_config(mail_config)
    provider_cls = _provider_class(entry.get("type"))
    if provider_cls is None:
        raise RuntimeError(f"Unsupported mail provider type: {entry.get('type')}")
    provider = provider_cls(entry, conf)
    try:
        return provider.wait_for_code(mailbox)
    finally:
        provider.close()


def mark_mailbox_result(
    mailbox: dict,
    *,
    success: bool,
    error: Exception | str | None = None,
) -> None:
    """Update pool state after registration attempt.

    - Success → mark as 'used'
    - Token invalid → mark as 'token_invalid'
    - Other failure → mark as 'failed'
    """
    provider_name = str(mailbox.get("provider") or "").strip()
    if provider_name not in {
        OutlookTokenProvider.name,
        GmailOAuthProvider.name,
    }:
        return
    address = str(mailbox.get("address") or "").strip()
    if not address:
        return
    base_address = str(
        mailbox.get("base_address") or _base_plus_address(address)
    ).strip().lower()
    extra: dict[str, Any] = {}
    if provider_name == OutlookTokenProvider.name and base_address:
        extra["base_address"] = base_address
        if mailbox.get("alias_index") is not None:
            extra["alias_index"] = mailbox.get("alias_index")
    if success:
        _set_state(
            address,
            "used",
            state_file=Path(mailbox.get("_state_file") or STATE_FILE),
            provider=provider_name,
            extra=extra,
        )
        workspace_id = str(mailbox.get("_workspace_id") or "").strip()
        workspace_state_file = str(mailbox.get("_workspace_state_file") or "").strip()
        if workspace_id and workspace_state_file:
            set_workspace_email_state(
                workspace_state_file,
                workspace_id,
                address,
                "registered",
                mode="register",
                extra={
                    "provider": provider_name,
                    **extra,
                },
            )
        return
    reason = str(error or "").strip()
    if (
        isinstance(error, MailProviderTokenError)
        or "OutlookToken" in reason
        or "GmailOAuth" in reason
        or "access_token" in reason
    ):
        _set_state(
            address,
            "token_invalid",
            reason[:300],
            state_file=Path(mailbox.get("_state_file") or STATE_FILE),
            provider=provider_name,
            extra=extra,
        )
        if provider_name == OutlookTokenProvider.name and base_address:
            _set_state(
                f"credential:{base_address}",
                "token_invalid",
                reason[:300],
                state_file=Path(mailbox.get("_state_file") or STATE_FILE),
                provider=provider_name,
                extra={"base_address": base_address},
            )
    else:
        _set_state(
            address,
            "failed",
            reason[:300],
            state_file=Path(mailbox.get("_state_file") or STATE_FILE),
            provider=provider_name,
            extra=extra,
        )
    workspace_id = str(mailbox.get("_workspace_id") or "").strip()
    workspace_state_file = str(mailbox.get("_workspace_state_file") or "").strip()
    if workspace_id and workspace_state_file:
        state_name = (
            "token_invalid"
            if isinstance(error, MailProviderTokenError)
            or "OutlookToken" in reason
            or "GmailOAuth" in reason
            or "access_token" in reason
            else "failed"
        )
        set_workspace_email_state(
            workspace_state_file,
            workspace_id,
            address,
            state_name,
            mode="register",
            reason=reason[:300],
            extra={
                "provider": provider_name,
                **extra,
            },
        )


def release_mailbox(mailbox: dict) -> None:
    """Release in_use state back to unused (if registration is abandoned)."""
    provider_name = str(mailbox.get("provider") or "").strip()
    if provider_name not in {
        OutlookTokenProvider.name,
        GmailOAuthProvider.name,
    }:
        return
    _release_state(
        str(mailbox.get("address") or ""),
        state_file=Path(mailbox.get("_state_file") or STATE_FILE),
        provider=provider_name,
    )
    workspace_id = str(mailbox.get("_workspace_id") or "").strip()
    workspace_state_file = str(mailbox.get("_workspace_state_file") or "").strip()
    if workspace_id and workspace_state_file:
        set_workspace_email_state(
            workspace_state_file,
            workspace_id,
            str(mailbox.get("address") or ""),
            "failed",
            mode="register",
            reason="released",
            extra={"provider": provider_name},
        )

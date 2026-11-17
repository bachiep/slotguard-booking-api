"""HMAC-SHA256 webhook signature computation and constant-time replay-safe verification."""

import hashlib
import hmac

from slotguard.core.clock import Clock
from slotguard.core.config import Settings
from slotguard.core.exceptions import UnauthorizedError


def compute_webhook_signature(secret: str, timestamp: int, raw_body: bytes) -> str:
    """Compute canonical sha256=<hex> HMAC over '<timestamp>.<raw_body>'."""
    signed_payload = f"{timestamp}.".encode() + raw_body
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_webhook_headers(
    *,
    timestamp_header: str | None,
    signature_header: str | None,
    raw_body: bytes,
    settings: Settings,
    clock: Clock,
) -> int:
    """Verify X-Webhook-Timestamp replay window and constant-time X-Webhook-Signature."""
    if timestamp_header is None:
        raise UnauthorizedError("Missing X-Webhook-Timestamp header.")
    try:
        timestamp = int(timestamp_header.strip())
    except ValueError as exc:
        raise UnauthorizedError("Invalid X-Webhook-Timestamp header.") from exc

    now_ts = int(clock.now().timestamp())
    if abs(now_ts - timestamp) > settings.webhook_replay_window_seconds:
        raise UnauthorizedError("Webhook timestamp is outside the allowed replay window.")

    if not signature_header or not signature_header.strip().startswith("sha256="):
        raise UnauthorizedError("Missing or malformed X-Webhook-Signature header.")

    expected = compute_webhook_signature(settings.webhook_secret, timestamp, raw_body)
    if not hmac.compare_digest(expected, signature_header.strip()):
        raise UnauthorizedError("Invalid webhook signature.")
    return timestamp

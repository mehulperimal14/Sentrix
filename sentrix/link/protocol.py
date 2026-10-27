"""Device link protocol v1: message types, envelopes, binary frames (docs/7 §5).

One outbound WebSocket per device: ``wss://<portal>/device/v1/link`` (``ws://``
only against a plain-http development Portal). Text messages are JSON objects
with a common envelope; binary messages carry video frames and large tunnel
bodies behind a 16-byte header. Stdlib only: the device (``sentrix.link``) and
the Portal (``sentrix_portal``) import the same definitions.

Envelope (every text message)::

    {"v": 1, "type": "<type>", "id": "<uuid4 of this message>",
     "ts": <sender wall clock, ms since epoch>, ...type fields}

Unknown fields are ignored (forward compatible); an unknown ``type`` is
answered with ``error`` code ``unknown_type``; a different ``v`` closes the
socket with ``CLOSE_UNSUPPORTED``.
"""

from __future__ import annotations

import json
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Any

VERSION = 1
PATH = "/device/v1/link"

#: Device -> cloud
HELLO = "hello"
HEARTBEAT = "heartbeat"
EVENT = "event"                    # outbox event (at-least-once, deduped by seq)
STATE = "state"                    # live state (ws_payload) while subscribed
TUNNEL_RESPONSE = "tunnel.response"
COMMAND_RESULT = "command.result"
PROFILE_ACK = "profile.ack"
#: Cloud -> device
WELCOME = "welcome"
ACK = "ack"
PROFILE_PUSH = "profile.push"
COMMAND = "command"
TUNNEL_REQUEST = "tunnel.request"
TUNNEL_CANCEL = "tunnel.cancel"
VIDEO_SUBSCRIBE = "video.subscribe"
VIDEO_UNSUBSCRIBE = "video.unsubscribe"
STATE_SUBSCRIBE = "state.subscribe"
STATE_UNSUBSCRIBE = "state.unsubscribe"
VIEWERS = "viewers"
TOKEN_ISSUE = "token.issue"
SOS_SYNC = "sos.sync"
#: Both directions
ERROR = "error"

DEVICE_TO_CLOUD = frozenset({HELLO, HEARTBEAT, EVENT, STATE, TUNNEL_RESPONSE,
                             COMMAND_RESULT, PROFILE_ACK, ERROR})
CLOUD_TO_DEVICE = frozenset({WELCOME, ACK, PROFILE_PUSH, COMMAND, TUNNEL_REQUEST,
                             TUNNEL_CANCEL, VIDEO_SUBSCRIBE, VIDEO_UNSUBSCRIBE,
                             STATE_SUBSCRIBE, STATE_UNSUBSCRIBE, VIEWERS,
                             TOKEN_ISSUE, SOS_SYNC, ERROR})

#: ``event`` kinds (outbox). SOS kinds travel at priority 0.
EVENT_KINDS = (
    "incident.upsert", "dispatch.upsert", "verdict.set",
    "sos.created", "sos.transition", "sos.event", "sos.snapshot",
    "evidence.meta", "snapshot.meta", "gallery.audit", "mode.changed",
    "remote.audit", "notification.status", "node.alert",
)
SOS_EVENT_KINDS = frozenset(k for k in EVENT_KINDS if k.startswith("sos."))

#: ``command`` names the device accepts (allowlist; anything else is refused
#: with ``unknown_command``). No shell, no code execution, nothing that turns
#: off the siren or the evidence vault. Rules the device enforces: docs/7 §5.6.
COMMANDS = (
    "sos.respond",      # approve | decline | checking | safe | help_now (PIN on device)
    "sos.update",       # cloud-side SOS events: acknowledge | resolve | delivered | ...
    "verdict.set",      # queued Real/False verdict from an offline mirror page
    "mode.set",         # HOME/ARMED; disarm needs the SOS PIN
    "siren.test",       # 1 s, at most once a minute, owner + step-up
    "sms.send",         # modem SMS, only to numbers in the current profile, 30/day
    "token.rotate",     # device token rotation (answered by token.issue)
    "unpair",           # wipe profile + token (owner + step-up)
    "diag.ping",        # round-trip check
    "demo.close_incident",  # test mode only: close the open incident now
    "demo.trigger",     # test mode only: disclosed scripted L3 (SIMULATED)
)

#: ``sos.respond`` actions (owner, member, device admin; a contact may only
#: ``approve``, and only under the request's frozen safety defaults).
SOS_RESPOND_ACTIONS = ("approve", "decline", "checking", "safe", "help_now")
#: ``sos.update`` events: cloud-side facts the device applies through
#: ``sentrix.sos.states.next_state`` (``notification``: a Portal channel's
#: delivery status for one of this request's messages).
SOS_UPDATE_EVENTS = ("acknowledge", "resolve", "delivered", "delivery_failed",
                     "contacts_notified", "device_offline", "device_online", "seen",
                     "notification")

#: Command / tunnel error codes and their HTTP-like status.
ERROR_CODES = {
    "invalid_args": 400, "unauthorised": 401, "forbidden_role": 403,
    "consent_required": 403, "step_up_required": 403, "test_mode_only": 403,
    "not_allowed_recipient": 403, "pin_invalid": 403, "not_found": 404,
    "conflict": 409, "too_large": 413, "unprocessable": 422, "pin_locked": 429,
    "rate_limited": 429, "daily_cap": 429, "unknown_command": 400,
    "unknown_type": 400, "unsupported": 400, "internal": 500, "device_busy": 503, "timeout": 504,
}

#: WebSocket close codes the Portal uses.
CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_SERVER_ERROR = 1011
CLOSE_UNAUTHORISED = 4001     # bad token/bootstrap: back off to the maximum
CLOSE_REVOKED = 4003          # unpaired or token revoked: wipe token + profile
CLOSE_UNSUPPORTED = 4008      # protocol version not supported
CLOSE_REPLACED = 4009         # a newer connection of the same device took over

#: Upgrade authentication (``Authorization`` header on the WSS handshake).
AUTH_BEARER = "Bearer"        # Bearer <device token>           (claimed device)
AUTH_BOOTSTRAP = "Bootstrap"  # Bootstrap <device_id>:<secret>  (before the claim)

#: Size limits (bytes). websockets' default max_size is 1 MiB; keep it.
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_FRAME_JPEG_BYTES = 512 * 1024
INLINE_BODY_BYTES = 256 * 1024          # larger bodies go as BODY chunks
CHUNK_BYTES = 256 * 1024

# ── binary messages ───────────────────────────────────────────────────────────
#: 16-byte header, big-endian: kind u8 | version u8 | flags u16 | a u32 | b u64.
#:   FRAME: a = frame seq (u32, wraps), b = capture wall time in ms
#:   BODY:  a = tunnel stream id (u32), b = chunk index; flags bit 0 = last chunk
_HEADER = struct.Struct(">BBHIQ")
HEADER_BYTES = _HEADER.size              # 16
KIND_FRAME = 0x01
KIND_BODY = 0x02
FLAG_LAST = 0x0001


@dataclass(frozen=True)
class BinaryHeader:
    kind: int
    flags: int
    a: int
    b: int


def pack_frame(seq: int, ts_ms: int, jpeg: bytes) -> bytes:
    if len(jpeg) > MAX_FRAME_JPEG_BYTES:
        raise ValueError("frame larger than MAX_FRAME_JPEG_BYTES")
    return _HEADER.pack(KIND_FRAME, VERSION, 0, seq & 0xFFFFFFFF, ts_ms) + jpeg


def pack_body_chunk(stream: int, index: int, data: bytes, last: bool) -> bytes:
    if len(data) > CHUNK_BYTES:
        raise ValueError("chunk larger than CHUNK_BYTES")
    return _HEADER.pack(KIND_BODY, VERSION, FLAG_LAST if last else 0,
                        stream & 0xFFFFFFFF, index) + data


def unpack_binary(message: bytes) -> tuple[BinaryHeader, bytes]:
    if len(message) < HEADER_BYTES:
        raise ValueError("binary message shorter than its header")
    kind, version, flags, a, b = _HEADER.unpack_from(message)
    if version != VERSION:
        raise ValueError(f"unsupported binary version {version}")
    if kind not in (KIND_FRAME, KIND_BODY):
        raise ValueError(f"unknown binary kind {kind}")
    return BinaryHeader(kind, flags, a, b), message[HEADER_BYTES:]


# ── canonical JSON (hashes both sides recompute) ─────────────────────────────
def canonical_json(obj: Any) -> bytes:
    """The one byte encoding hashed on both sides: the profile ``sha256``, the
    request-event chains and the Portal audit chain.

    Sorted keys, ``(",", ":")`` separators, UTF-8 without ASCII escaping, and
    **no** ``default=str``: a value JSON cannot represent raises ``TypeError``
    instead of hashing a ``repr`` the other side cannot reproduce. Timestamps
    travel as ISO strings and are hashed (and stored by the Portal) verbatim.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(obj: Any) -> str:
    import hashlib
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def sos_dedupe_key(request_id: str, origin: str, seq: int) -> str:
    """Outbox dedupe key of one SOS event: ``sos:<request>:<origin>:<seq>``."""
    return f"sos:{request_id}:{origin}:{int(seq)}"


# ── text messages ─────────────────────────────────────────────────────────────
def now_ms() -> int:
    return int(time.time() * 1000)


def envelope(msg_type: str, **fields: Any) -> dict[str, Any]:
    """A new message dict with the common envelope."""
    return {"v": VERSION, "type": msg_type, "id": str(uuid.uuid4()),
            "ts": now_ms(), **fields}


def encode(message: dict[str, Any]) -> str:
    text = json.dumps(message, separators=(",", ":"), default=str)
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("message larger than MAX_MESSAGE_BYTES")
    return text


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def decode(text: str | bytes) -> dict[str, Any]:
    """Parse and validate the envelope; raises :class:`ProtocolError`."""
    try:
        message = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("invalid_args", f"not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("invalid_args", "message is not an object")
    if message.get("v") != VERSION:
        raise ProtocolError("unsupported", f"protocol version {message.get('v')!r}")
    if not isinstance(message.get("type"), str) or not message.get("id"):
        raise ProtocolError("invalid_args", "missing type or id")
    return message


def error(code: str, message: str, ref: str | None = None) -> dict[str, Any]:
    return envelope(ERROR, code=code, status=ERROR_CODES.get(code, 400),
                    message=message, ref=ref)


__all__ = [name for name in dir() if name.isupper() and not name.startswith("_")] + [
    "BinaryHeader", "pack_frame", "pack_body_chunk", "unpack_binary", "now_ms",
    "envelope", "encode", "decode", "error", "ProtocolError", "canonical_json",
    "sha256_hex", "sos_dedupe_key"]

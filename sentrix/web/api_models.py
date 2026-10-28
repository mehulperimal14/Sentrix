"""Pydantic bodies of the device console's JSON API for the React apps (docs/9).

Request and response models of ``sentrix.web.api_routes``, so ``/openapi.json``
describes every new endpoint and ``openapi-typescript`` can generate the
apps' types. Response models list exactly what a route returns: FastAPI drops
any other key, so a field added to a database row never reaches a browser by
accident. :class:`LiveState` is the exception: it documents the live state the
websocket ``/ws/threat`` sends (and ``GET /api/state`` returns byte for byte),
whose blocks other tracks own; it allows extra keys and is never used to
filter that payload.

Times are ISO 8601 UTC strings ending in ``Z``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["device_admin", "owner", "member", "operator"]


# ── errors (docs/9 §4) ────────────────────────────────────────────────────────
class ApiErrorBody(BaseModel):
    code: str = Field(description="not_signed_in, forbidden, csrf, step_up_required, "
                                  "consent_required, not_found, conflict, validation, "
                                  "rate_limited, wrong_password, wrong_code, not_configured, "
                                  "unsupported, wifi_failed, internal")
    message: str = Field(description="Plain English for the user.")
    fields: dict[str, str] | None = Field(None, description="Per-field messages (validation).")
    retry_after: int | None = Field(None, description="Seconds (rate_limited).")
    reason: str | None = Field(None, description="What the device reported (wifi_failed).")


class ApiErrorEnvelope(BaseModel):
    error: ApiErrorBody


class OkResponse(BaseModel):
    ok: bool = True


# ── session ───────────────────────────────────────────────────────────────────
class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class SessionUser(BaseModel):
    id: str | None = Field(None, description="The Portal user id (tunnel); null on the LAN.")
    name: str | None = None


class DeviceInfo(BaseModel):
    id: str | None = Field(None, description="device_id from the device identity, if provisioned")
    serial: str | None = Field(None, description="The serial on the label, e.g. SX5-000001")
    name: str | None = Field(None, description="Not on the device yet (the Portal keeps it)")
    room: str | None = Field(None, description="Room label of the first camera (profile)")
    profile: str = Field(description="Hardware profile: laptop, pi5-clip, ...")


class LinkSummary(BaseModel):
    enabled: bool = False
    state: str | None = None
    connected: bool = False
    claimed: bool = False
    remote_viewers: int = 0


class CallInfo(BaseModel):
    """The "Call 112 now" button (docs/7 §7.10, D13): built on the device, so
    no app ever holds an emergency number. In test mode it calls the SENTRIX
    Desk, or is disabled when the device knows no Desk number."""

    enabled: bool
    href: str | None = Field(None, description="tel: link, or null when disabled")
    number: str | None = None
    desk: bool = Field(description="True when the button calls the SENTRIX Desk (test mode)")
    note: str = ""


class MediaAccess(BaseModel):
    """Will a media fetch pass the path table? LAN: always. Through the
    Portal: the household's consents (remote_view; faces also face_recognition)."""

    snapshots: bool
    evidence: bool
    faces: bool


class SosFeatures(BaseModel):
    enabled: bool = Field(description="The SOS service runs on this unit")
    intrusion_requests: bool = Field(description="An armed intrusion may open a request")
    demo_close_incident: bool = Field(description="Test mode: POST /api/sos/demo/close-incident")
    demo_trigger: bool = Field(description="Test mode, device admin: POST /api/sos/demo/trigger")


class SessionInfo(BaseModel):
    signed_in: bool
    via: Literal["lan", "portal"]
    role: Role | None = None
    user: SessionUser | None = None
    capabilities: list[str] = Field(default_factory=list,
                                    description="ROLE_CAPABILITIES of the role")
    lan_admin: bool = Field(False, description="The device admin on the LAN: diagnostics, "
                                               "activity lists, faces, network, recording files")
    csrf_token: str | None = Field(None, description="Send as csrf_header on every POST, PUT, "
                                                     "PATCH, DELETE (LAN session only)")
    csrf_header: str
    step_up_at: str | None = Field(None, description="Last Portal step-up (tunnel)")
    base: str = Field(description="URL prefix of this console: '' on the LAN, /d/<id> "
                                  "through the Portal")
    test_mode: bool
    setup_mode: bool
    password_configured: bool
    time_zone: str
    server_time: str
    device: DeviceInfo | None = None
    claimed: bool | None = None
    link: LinkSummary | None = None
    call: CallInfo | None = None
    media: MediaAccess | None = None
    sos: SosFeatures | None = None


# ── live state (the /ws/threat message) ───────────────────────────────────────
class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class AlarmBlock(_Open):
    level: int = 1
    status: str = "SECURE"
    since_s: float | None = None
    incident_id: str | None = None
    incident_type: str | None = None
    headline: str | None = None
    peak_tci: float | None = None
    deescalate_in: float | None = None


class SosBlock(_Open):
    active: bool = False
    request_id: str | None = None
    short_id: str | None = None
    state: str | None = None
    label: str | None = None
    incident_id: str | None = None
    incident_type: str | None = None
    level: int | None = None
    urgency: str | None = None
    evidence_state: str | None = None
    created_at: str | None = None
    expires_at: str | None = None
    remaining_s: int | None = None
    seen: bool = False
    repeat: bool = False
    authority: str | None = None
    recommended_authority: str | None = None
    sos_text: str | None = None
    snapshot: str | None = Field(None, description="Base-relative: media/snapshot/<name>")
    actions: list[str] = Field(default_factory=list)
    last: dict[str, Any] | None = None
    flags: dict[str, Any] | None = None
    desk_number: str | None = None
    call_number: str | None = None
    test_mode: bool | None = None


class LinkBlock(_Open):
    enabled: bool = False
    state: str | None = None
    connected: bool = False
    claimed: bool = False
    device_id: str | None = None
    portal: str | None = None
    since: Any = None
    last_message_at: Any = None
    outbox_depth: int | None = None
    profile_version: int | None = None


class ModeBlock(_Open):
    armed: bool = False
    source: str | None = None
    changed_by: str | None = None
    changed_at: str | None = None


class ChannelsBlock(_Open):
    enabled: bool = False
    outbound_ready: bool = False
    live: bool = False
    channels: dict[str, Any] = Field(default_factory=dict)
    outbox_depth: int | None = None
    last_error: Any = None
    modem: dict[str, Any] | None = None
    legacy_twilio: bool | None = None
    warn: bool = False


class TopFactor(_Open):
    name: str
    contribution: float


class Contribution(_Open):
    input: str
    effect: str | None = None
    value: Any = None
    detail: str | None = None


class LiveState(_Open):
    """What ``/ws/threat`` sends at telemetry rate and ``GET /api/state``
    returns once. ``alarm`` is the committed level (badge, headline); ``tci``,
    ``level`` and the scores are the live tick. Other blocks (``node``,
    ``clip``, ``fusion``, ``audio``, ``features``) are free-form here."""

    timestamp: str | None = None
    test_mode: bool | None = None
    tci: float = 0.0
    raw_tci: float | None = None
    level: int = 1
    status: str = "SECURE"
    reason: str | None = None
    incident_type: str | None = None
    incident_id: str | None = None
    evidence_state: str | None = None
    alarm: AlarmBlock = Field(default_factory=AlarmBlock)
    features: dict[str, Any] = Field(default_factory=dict)
    top_factors: list[TopFactor] = Field(default_factory=list)
    contributions: list[Contribution] = Field(default_factory=list)
    fusion: dict[str, Any] = Field(default_factory=dict)
    audio: dict[str, Any] = Field(default_factory=dict)
    audio_label: str | None = None
    behaviour_label: str | None = None
    person_count: int | None = None
    authorized: bool | None = None
    identity_degraded: bool | None = None
    camera_online: bool = False
    mic_available: bool = False
    mic_classifying: bool = False
    video_fps: float | None = None
    ai_hz: float | None = None
    frame_age_ms: float | None = None
    latency_ms: float | None = None
    latency_p95_ms: float | None = None
    queue_depth: int | None = None
    sos: SosBlock = Field(default_factory=SosBlock)
    link: LinkBlock = Field(default_factory=LinkBlock)
    mode: ModeBlock = Field(default_factory=ModeBlock)
    channels: ChannelsBlock = Field(default_factory=ChannelsBlock)
    node: dict[str, Any] = Field(default_factory=dict)
    clip: dict[str, Any] = Field(default_factory=dict)
    remote_viewers: int = 0
    dispatch_package: dict[str, Any] | None = None


# ── activity: incidents ───────────────────────────────────────────────────────
class RequestRef(BaseModel):
    """The assistance request an incident or package belongs to, as the
    household sees it (owner view: a duress decline reads as a false alarm)."""

    id: str
    short_id: str
    state: str
    label: str


class ActivityIncident(BaseModel):
    id: str
    started_at: str | None = None
    ended_at: str | None = None
    ongoing: bool
    duration_seconds: float | None = None
    peak_level: int | None = None
    peak_tci: float | None = None
    status: str | None = None
    incident_type: str | None = None
    reason: str | None = Field(None, description="The peak tick's evidence reason")
    authorized: bool | None = None
    camera_id: str | None = None
    room: str | None = None
    verdict: str | None = Field(None, description="TRUE (real), FALSE (false alarm) or null")
    verdict_note: str | None = None
    verdict_by: str | None = Field(None, description="Who: 'device admin', 'Portal user ...'")
    verdict_at: str | None = None
    request: RequestRef | None = None
    has_snapshot: bool = False
    snapshot_url: str | None = Field(None, description="Null when this caller may not fetch it")
    has_evidence: bool = False
    evidence_url: str | None = None


class IncidentStats(BaseModel):
    total: int = 0
    confirmed: int = 0
    false_alarms: int = 0
    unreviewed: int = 0
    false_alarm_rate: float | None = None


class ActivityPage(BaseModel):
    items: list[ActivityIncident]
    total: int = Field(description="Incidents matching the filters")
    limit: int
    cursor: str | None = None
    next_cursor: str | None = Field(None, description="Pass as ?cursor= for older rows")
    stats: IncidentStats
    filters: dict[str, Any] = Field(description="The filters applied (malformed ones dropped)")


# ── activity: snapshots, evidence, dispatch (LAN) ─────────────────────────────
class SnapshotIncident(BaseModel):
    id: str
    incident_type: str | None = None
    peak_level: int | None = None


class SnapshotItem(BaseModel):
    name: str
    url: str
    taken_at: str | None = None
    bytes: int | None = None
    incident: SnapshotIncident | None = None


class SnapshotPage(BaseModel):
    items: list[SnapshotItem]
    total: int
    limit: int
    cursor: str | None = None
    next_cursor: str | None = None


class Detection(BaseModel):
    label: str
    conf: float | None = None
    track_id: int | None = None


class ChainProblem(BaseModel):
    line: int | None = None
    bundle: str | None = None
    error: str


class ChainStatus(BaseModel):
    ok: bool
    entries: int = 0
    note: str | None = None
    problems: list[ChainProblem] = Field(default_factory=list,
                                         description="At most the first 20")
    problem_count: int = 0
    error: str | None = None


class EvidenceItem(BaseModel):
    bundle_id: str
    timestamp: str | None = None
    incident_id: str | None = None
    camera_id: str | None = None
    room: str | None = None
    level: int | None = None
    tci: float | None = None
    status: str | None = None
    incident_type: str | None = None
    reason: str | None = None
    detections: list[Detection] = Field(default_factory=list)
    sha256: str | None = None
    view_url: str


class EvidencePage(BaseModel):
    chain: ChainStatus
    items: list[EvidenceItem]
    total: int
    limit: int
    cursor: str | None = None
    next_cursor: str | None = None


class DispatchItem(BaseModel):
    id: str
    created_at: str | None = None
    incident_id: str | None = None
    incident_type: str | None = None
    level: int | None = None
    tci: float | None = None
    authority: str | None = Field(None, description="The recommended authority")
    chosen_authority: str | None = None
    status: str | None = None
    error: str | None = None
    location: str | None = Field(None, description="The household address on the package")
    camera_location: str | None = None
    sent_at: str | None = None
    sent_by: str | None = None
    sent_to: list[str] = Field(default_factory=list, description="Masked recipients")
    expires_at: str | None = None
    request: RequestRef | None = None
    sendable: bool = Field(description="PENDING or FAILED: POST /api/dispatch/{id}/send/{authority}")


class DispatchPage(BaseModel):
    items: list[DispatchItem]
    authorities: list[str]
    sendable_statuses: list[str]
    limit: int
    cursor: str | None = None
    next_cursor: str | None = None


# ── faces (LAN) ───────────────────────────────────────────────────────────────
class FaceItem(BaseModel):
    filename: str
    person: str
    enrolled: bool = Field(description="False: no usable face in this image")
    url: str


class FaceGallery(BaseModel):
    backend: str | None = Field(None, description="Face recognition backend, null when none "
                                                  "works (identity fails closed)")
    available: bool
    items: list[FaceItem]


# ── Wi-Fi setup mode (LAN, no session) ────────────────────────────────────────
class SetupWifiInfo(BaseModel):
    setup_mode: bool = True
    network_available: bool = Field(description="Wi-Fi control exists on this build")
    test_mode: bool


class SetupCodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=64, description="The setup code on the label")


class WifiConnectBody(SetupCodeBody):
    ssid: str = Field(min_length=1, max_length=64)
    psk: str | None = Field(None, max_length=63, description="Empty or 8 to 63 characters")

    @field_validator("ssid")
    @classmethod
    def _ssid(cls, value: str) -> str:
        value = value.strip()
        if (not value or len(value.encode("utf-8")) > 32
                or any(ord(c) < 32 for c in value)):
            raise ValueError("Choose a network name (at most 32 bytes)")
        return value

    @field_validator("psk")
    @classmethod
    def _psk(cls, value: str | None) -> str | None:
        if value and not 8 <= len(value) <= 63:
            raise ValueError("A Wi-Fi password has 8 to 63 characters")
        return value or None


class WifiNetwork(BaseModel):
    ssid: str
    signal: int | None = None
    security: str = ""


class NetworkStatus(BaseModel):
    connected: bool = False
    ssid: str | None = None
    ip: str | None = None
    via: str | None = None


class WifiScanResult(BaseModel):
    networks: list[WifiNetwork]
    status: NetworkStatus


class WifiConnectResult(BaseModel):
    ok: bool = True
    status: NetworkStatus

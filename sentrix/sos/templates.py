"""SOS texts: the one English template source (docs/7 §5.11, §7.10).

The device and the Portal render every SOS message from these templates, so
an offline SMS from the device's modem says the same thing as the Portal's
Telegram message or the Desk console. The Portal imports this module (it is
stdlib plus Jinja2 only and reads no configuration).

What is fixed (decisions, docs/7 §7.10):

* the buttons: "Alert my contacts and the SENTRIX desk" (with Police / Fire /
  Medical context), "Call 112 now" (shown first for weapon and fire; the Desk
  number in test mode), "I'm checking", "False alarm (PIN)";
* every SOS text ends "SENTRIX does not contact 112 itself.";
* corroborated evidence is "confirmed by two different models" (never
  "independent camera checks"); a single-source L3 is "possible (one camera
  check)"; a dark camera adds "camera view degraded (dark), detection
  unreliable";
* test messages start with the send guard's banner (the GSM-7 variant on SMS);
* the duress banner to the Desk and contacts says "Do not go to the home.
  Call 112.";
* Telegram gets a short text and a Portal link only: no photo, no address, no
  medical notes. SMS can approve or raise, never decline.

Rendering never trusts the profile: every value is cleaned (control
characters removed, whitespace collapsed, length capped) and inserted as data
into a sandboxed Jinja environment, so a contact named ``{{ 7*7 }}`` stays a
name. Missing fields are left out, never invented, so fictional demo profiles
and half-finished ones render alike. Medical notes appear only when the
profile's ``medical_notes`` consent is on (the Portal also omits them from the
profile otherwise), and only for the Desk and contacts' full text.

Times are India Standard Time (UTC+05:30, no daylight saving).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from jinja2 import ChainableUndefined
from jinja2.sandbox import SandboxedEnvironment

from sentrix.alerting.guard import TEST_PREFIX, TEST_PREFIX_GSM

TEMPLATE_VERSION = "en-1"
IST = timezone(timedelta(hours=5, minutes=30), "IST")

DISCLAIMER = "SENTRIX does not contact 112 itself."
WORDING_CORROBORATED = "confirmed by two different models"
WORDING_SINGLE_SOURCE = "possible (one camera check)"
WORDING_DEGRADED = "camera view degraded (dark), detection unreliable"
WORDING_SIMULATED = "SIMULATED DETECTION"
DURESS_BANNER = ("DURESS: the resident may be under threat. "
                 "Do not go to the home. Call 112.")

#: Button labels the LC and the Portal show (names: ``sentrix.sos.SOS_ACTIONS``).
BUTTONS = {
    "approve": "Alert my contacts and the SENTRIX desk",
    "call_112": "Call 112 now",
    "checking": "I'm checking",
    "decline": "False alarm (PIN)",
    "safe": "I'm safe now (PIN)",
    "help_now": "Request help now",
}
AUTHORITY_LABELS = {"POLICE": "Police", "FIRE": "Fire", "MEDICAL": "Medical"}
EVENT_NAMES = {"weapon": "WEAPON", "fire": "FIRE", "intrusion": "INTRUDER",
               "duress": "DURESS ALERT"}
LEVEL_NAMES = {1: "SECURE", 2: "SUSPICIOUS", 3: "ELEVATED", 4: "HIGH", 5: "CRITICAL"}

#: Message templates (``channels.base.Message.template``) this module renders.
MESSAGE_TEMPLATES = ("sos.prompt", "sos.reminder", "sos.lapse_warning", "sos.lapsed",
                     "sos.full", "sos.sms", "sos.offline_prompt", "sos.resolved",
                     "sos.duress", "sos.failed", "sos.unacknowledged",
                     "sos.acknowledged")

#: GSM 03.38 basic character set (one septet each) and its extension table
#: (two septets each). Anything else forces UCS-2 (70 characters a segment).
_GSM_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_GSM_EXTENDED = frozenset("^{}\\[~]|€")
#: Typography that has a plain GSM equivalent.
_GSM_MAP = str.maketrans({"–": "-", "—": "-", "·": "-", "‘": "'", "’": "'",
                          "“": '"', "”": '"', "…": "...", "₹": "Rs", "•": "-",
                          " ": " ", "\t": " "})
#: Two concatenated GSM-7 segments (153 septets each); UCS-2 text (a name in
#: Devanagari) may take three (67 characters each), as the design allows.
SMS_LIMIT_GSM = 306
SMS_LIMIT_UCS2 = 201

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏  ]")
_SPACES = re.compile(r"\s+")


# ── cleaning ──────────────────────────────────────────────────────────────────
def clean(value: Any, limit: int = 120) -> str:
    """A profile value as safe single-line text: '' for None, no control or
    zero-width characters, whitespace collapsed, at most ``limit`` chars."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    text = _SPACES.sub(" ", _CONTROL.sub("", str(value))).strip()
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _join(parts: Iterable[Any], sep: str = ", ", limit: int = 120) -> str:
    return sep.join(p for p in (clean(x, limit) for x in parts) if p)


def to_ist(value: Any) -> datetime | None:
    """An ISO string, epoch seconds or datetime -> aware IST datetime."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, datetime):
            moment = value
        elif isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(float(value), timezone.utc)
        else:
            moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(IST)


def clock_time(value: Any, seconds: bool = False) -> str:
    """``14:32`` (or ``14:32:07``) IST, '' when unknown."""
    moment = to_ist(value)
    if moment is None:
        return ""
    return moment.strftime("%H:%M:%S" if seconds else "%H:%M")


# ── SMS ───────────────────────────────────────────────────────────────────────
def gsm_text(text: str) -> str:
    """Typography mapped to its GSM-7 equivalent (an en dash alone would turn
    the whole SMS into UCS-2 and cut each segment from 153 to 67 chars)."""
    return text.translate(_GSM_MAP)


def is_gsm(text: str) -> bool:
    return all(ch in _GSM_BASIC or ch in _GSM_EXTENDED for ch in text)


def sms_length(text: str) -> int:
    """Characters as the network counts them for this text's encoding."""
    if is_gsm(text):
        return sum(2 if ch in _GSM_EXTENDED else 1 for ch in text)
    return len(text)


def sms_limit(text: str) -> int:
    return SMS_LIMIT_GSM if is_gsm(text) else SMS_LIMIT_UCS2


def fits_sms(text: str) -> bool:
    return sms_length(text) <= sms_limit(text)


# ── context ───────────────────────────────────────────────────────────────────
def _event_phrase(incident_type: str, evidence_state: str, level: int) -> str:
    """"HANDGUN confirmed by two different models" style wording."""
    name = EVENT_NAMES.get(incident_type, clean(incident_type, 24).upper() or "ALERT")
    if incident_type == "duress":
        return "duress PIN entered"
    if incident_type == "intrusion":
        return "unrecognised person in the home while armed"
    if evidence_state == "corroborated" or level >= 4:
        return f"{name} {WORDING_CORROBORATED}"
    # "possible (one camera check)" with the class named inside it, as in
    # "possible WEAPON (one camera check)".
    return f"possible {name} (one camera check)"


def _room(profile: Mapping, camera_id: str | None) -> tuple[str, str]:
    cameras = [c for c in (profile.get("cameras") or []) if isinstance(c, Mapping)]
    chosen = next((c for c in cameras if c.get("camera_id") == camera_id), None)
    chosen = chosen or (cameras[0] if cameras else {})
    return clean(chosen.get("room_label"), 40) or "Camera", clean(chosen.get("floor"), 30)


def _address(profile: Mapping) -> tuple[str, str]:
    """(full address, compressed address for SMS)."""
    addr = profile.get("address") or {}
    if not isinstance(addr, Mapping):
        return "", ""
    landmark = clean(addr.get("landmark"), 60)
    full = _join([addr.get("house"), addr.get("building"), addr.get("street"),
                  addr.get("locality"), f"near {landmark}" if landmark else None,
                  addr.get("city"), addr.get("district"),
                  " ".join(p for p in (clean(addr.get("state"), 30),
                                       clean(addr.get("pincode"), 10)) if p)],
                 limit=80)
    floor, note = clean(addr.get("floor"), 30), clean(addr.get("entry_note"), 80)
    if floor:
        full = f"{full} ({floor})" if full else floor
    if note:
        full = f"{full}. Entry: {note}" if full else f"Entry: {note}"
    short = _join([addr.get("house"), addr.get("street"), addr.get("locality"),
                   addr.get("city"), addr.get("pincode")], sep=" ", limit=40)
    return full, short


def _map_url(profile: Mapping) -> str:
    addr = profile.get("address") or {}
    if not isinstance(addr, Mapping):
        return ""
    url = clean(addr.get("map_url"), 120)
    if url.startswith(("https://", "http://")):
        return url
    try:
        lat, lon = float(addr["lat"]), float(addr["lon"])
    except (KeyError, TypeError, ValueError):
        return ""
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return f"https://maps.google.com/?q={lat:.5f},{lon:.5f}"
    return ""


def _home(profile: Mapping) -> str:
    household = profile.get("household") or {}
    if not isinstance(household, Mapping):
        return ""
    occupants = household.get("occupants") or {}
    parts = []
    if isinstance(occupants, Mapping):
        counts = []
        for key, singular in (("adults", "adult"), ("children", "child"),
                              ("seniors", "senior")):
            try:
                n = int(occupants.get(key) or 0)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                plural = {"child": "children"}.get(singular, singular + "s")
                counts.append(f"{n} {singular if n == 1 else plural}")
        if counts:
            parts.append(", ".join(counts))
    pets = clean(household.get("pets"), 60)
    if pets:
        parts.append(f"pets: {pets}")
    return "; ".join(parts)


def _medical(profile: Mapping) -> str:
    """Only with the ``medical_notes`` consent (the Portal also leaves the
    field out of the profile otherwise, docs/7 §5.11)."""
    consents = profile.get("consents") or {}
    if not (isinstance(consents, Mapping) and consents.get("medical_notes")):
        return ""
    household = profile.get("household") or {}
    return clean(household.get("medical_notes"), 200) if isinstance(household, Mapping) else ""


def _nearest(profile: Mapping) -> str:
    nearest = profile.get("nearest") or {}
    if not isinstance(nearest, Mapping):
        return ""
    parts = []
    for key, label in (("police", "Police"), ("fire", "Fire"), ("hospital", "Hospital")):
        entry = nearest.get(key) or {}
        if isinstance(entry, Mapping):
            text = _join([entry.get("name"), entry.get("phone")], limit=60)
            if text:
                parts.append(f"{label}: {text}")
    return " · ".join(parts)


def call_number(profile: Mapping | None, test_mode: bool) -> str:
    """112, or in test mode the Desk number (no 112 link ever in test mode)."""
    if not test_mode:
        return "112"
    desk = profile.get("desk") if isinstance(profile, Mapping) else None
    phone = clean(desk.get("phone"), 20) if isinstance(desk, Mapping) else ""
    return phone or "the SENTRIX desk"


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_context(profile: Mapping | None, request: Mapping, *, audience: str = "owner",
                  test_mode: bool | None = None, link: str | None = None,
                  approved_by: str | None = None, degraded: bool = False,
                  now: Any = None) -> dict[str, Any]:
    """Everything a template may print, as cleaned strings.

    ``request`` is a request dict (``storage.requests.request_dict`` or the
    Portal mirror); ``audience`` is owner | desk | contact (the duress banner
    is never shown to the owner); ``link`` is the per-recipient Portal link,
    or the literal ``{link}`` placeholder the Portal fills in.
    """
    profile = profile if isinstance(profile, Mapping) else {}
    request = request if isinstance(request, Mapping) else {}
    flags = request.get("flags") if isinstance(request.get("flags"), Mapping) else {}
    policy = request.get("policy") if isinstance(request.get("policy"), Mapping) else {}
    if test_mode is None:
        test_mode = bool(request.get("test_mode", True))
    level = _int(request.get("peak_level") or request.get("trigger_level"), 3)
    incident_type = clean(request.get("incident_type"), 24) or "alert"
    evidence = clean(request.get("evidence_state"), 16)
    room, floor = _room(profile, request.get("camera_id"))
    address, address_short = _address(profile)
    owner = profile.get("owner") or {}
    owner_name = clean(owner.get("name"), 60) if isinstance(owner, Mapping) else ""
    owner_phone = clean(owner.get("phone"), 20) if isinstance(owner, Mapping) else ""
    device = clean(profile.get("customer_code"), 20) or clean(profile.get("device_id"), 12)
    created = request.get("created_at")
    expires = request.get("expires_at")
    remaining = ""
    if expires and now is not None:
        end, start = to_ist(expires), to_ist(now)
        if end is not None and start is not None:
            remaining = str(max(0, int((end - start).total_seconds() // 60)))
    evidence_hash = clean(request.get("evidence_hash"), 64)
    return {
        "banner": TEST_PREFIX + " " if test_mode else "",
        "test_mode": test_mode,
        "short_id": clean(request.get("short_id"), 12)
        or f"SX-{clean(request.get('id'), 6).upper()}",
        "event": EVENT_NAMES.get(incident_type, incident_type.upper()),
        "event_phrase": _event_phrase(incident_type, evidence, level),
        "level": level, "level_name": LEVEL_NAMES.get(level, ""),
        "room": room, "floor": floor,
        "where": _join([f"{room} camera", floor, f"device {device}" if device else None],
                       sep=" · ", limit=60),
        "address": address or "address not set", "address_short": address_short,
        "map_url": _map_url(profile),
        "resident": owner_name or "the resident", "resident_phone": owner_phone,
        "home": _home(profile),
        "medical": _medical(profile) if audience in ("desk", "contact") else "",
        "nearest": _nearest(profile),
        "evidence": f"sha256 {evidence_hash[:12]}" if evidence_hash else "",
        "detected": clock_time(created, seconds=True),
        "time": clock_time(created),
        "expires_time": clock_time(expires),
        "remaining_min": remaining,
        "window_min": str(_int(policy.get("window_seconds"), 1800) // 60),
        "approved_by": clean(approved_by, 120),
        "authority": AUTHORITY_LABELS.get(str(request.get("authority") or ""), ""),
        "simulated": WORDING_SIMULATED if flags.get("simulated") else "",
        "degraded": WORDING_DEGRADED if degraded else "",
        "duress": DURESS_BANNER if (flags.get("duress") and audience != "owner") else "",
        "duress_banner": DURESS_BANNER,
        # Telegram drops the phone number, address and map (short text + link).
        "include_details": True,
        "resident_safe": bool(flags.get("resident_safe")),
        "help_open": request.get("state") in ("APPROVED", "DISPATCHED", "ACKNOWLEDGED",
                                              "FAILED"),
        "contacts_notified": bool(flags.get("contacts_notified")),
        "call_number": call_number(profile, test_mode),
        "link": clean(link, 200) if link and link != "{link}" else (link or ""),
        "disclaimer": DISCLAIMER,
    }


# ── templates ─────────────────────────────────────────────────────────────────
#: Full text: Portal, Desk console, e-mail, contact pages.
FULL = """\
{{ banner }}SENTRIX SOS · Request {{ short_id }}{% if simulated %} · {{ simulated }}{% endif %}
{% if duress %}{{ duress }}
{% endif %}EVENT: {{ event_phrase }} · Level {{ level }} {{ level_name }}\
{% if degraded %} · {{ degraded }}{% endif %}
WHERE: {{ where }}
ADDRESS: {{ address }}
{% if map_url %}MAP: {{ map_url }}
{% endif %}RESIDENT: {{ resident }}\
{% if resident_phone %}, {{ resident_phone }} (call back first){% endif %}
{% if home %}HOME: {{ home }}
{% endif %}{% if medical %}MEDICAL: {{ medical }}
{% endif %}{% if nearest %}NEAREST: {{ nearest }}
{% endif %}{% if evidence %}EVIDENCE: {{ evidence }}
{% endif %}TIMELINE: detected {{ detected }} IST{% if approved_by %} · {{ approved_by }}{% endif %}
{% if link %}DETAILS: {{ link }}
{% endif %}This alert comes from a home security system. Call the resident, and \
{{ call_number }} if needed.
{{ disclaimer }}"""

#: Short text: SMS to contacts and the Desk (with address), Telegram (without).
SHORT = """\
{{ banner }}SENTRIX SOS {{ short_id }}{% if simulated %} ({{ simulated }}){% endif %}: \
{% if duress %}DURESS. Do not go to the home. Call 112. {% endif %}\
{{ event }}, {{ room }} {{ time }}. {{ resident }}\
{% if include_details and resident_phone %} {{ resident_phone }}{% endif %}.\
{% if include_details and address_short %} {{ address_short }}.{% endif %}\
{% if include_details and map_url %} {{ map_url }}{% endif %}\
{% if link %} Details: {{ link }}{% endif %} Call {{ call_number }} if needed. {{ disclaimer }}"""

#: The owner's prompt (LC, Portal, Telegram, e-mail).
PROMPT = """\
{{ banner }}SENTRIX{% if simulated %} ({{ simulated }}){% endif %}: {{ event_phrase }} in \
{{ room }} at {{ time }} (Level {{ level }} {{ level_name }})\
{% if degraded %}, {{ degraded }}{% endif %}. \
Do you need assistance? Answer by {{ expires_time }} ({{ window_min }} min).\
{% if link %} {{ link }}{% endif %} {{ disclaimer }}"""

#: Owner SMS when the link is down or the prompt is unseen after 10 min.
#: SMS can approve or raise, never decline (inbound SMS is Tier 2: answers
#: happen on the console or the Portal).
OFFLINE_PROMPT = """\
{{ banner }}SENTRIX {{ short_id }}: {{ event_phrase }} in {{ room }} {{ time }}. \
Need help? Open the SENTRIX console or Portal and tap "Alert my contacts". \
Emergency: call {{ call_number }}. Lapses {{ expires_time }}. {{ disclaimer }}"""

REMINDER = """\
{{ banner }}SENTRIX {{ short_id }}: {% if urgent %}UPDATE - now {{ event_phrase }} \
(Level {{ level }} {{ level_name }}). {% else %}still waiting for your answer: \
{{ event_phrase }} in {{ room }} at {{ time }}. {% endif %}\
Do you need assistance? Answer by {{ expires_time }}\
{% if remaining_min %} ({{ remaining_min }} min left){% endif %}.\
{% if link %} {{ link }}{% endif %} {{ disclaimer }}"""

LAPSE_WARNING = """\
{{ banner }}SENTRIX {{ short_id }}: your request lapses in {{ remaining_min or '5' }} min \
(at {{ expires_time }}) unanswered. {{ event_phrase }} in {{ room }} at {{ time }}.\
{% if link %} {{ link }}{% endif %} {{ disclaimer }}"""

LAPSED = """\
{% if audience == 'owner' %}{{ banner }}SENTRIX {{ short_id }}: the request lapsed unanswered \
at {{ expires_time }}. It is recorded as unanswered, not as safe. Please review the event\
{% if contacts_notified %}; your confirmed contacts were alerted{% endif %}.\
{% else %}{{ banner }}SENTRIX {{ short_id }}: the resident did not answer and the request \
lapsed at {{ expires_time }}. If you have not reached {{ resident }}, call them, and \
{{ call_number }} if needed.{% endif %} {{ disclaimer }}"""

RESOLVED = """\
{{ banner }}SENTRIX {{ short_id }}: {% if help_open %}the resident reports being \
safe. Help stays open until the Desk or a contact closes it.{% else %}resolved by the \
resident. No action is needed now.{% endif %} {{ disclaimer }}"""

DURESS = """\
{{ banner }}SENTRIX SOS {{ short_id }}: {{ duress_banner }} {{ resident }}\
{% if include_details and resident_phone %} {{ resident_phone }}{% endif %}.\
{% if include_details and address_short %} {{ address_short }}.{% endif %}\
{% if link %} Details: {{ link }}{% endif %} \
{{ disclaimer }}"""

FAILED = """\
{{ banner }}SENTRIX {{ short_id }}: SOS NOT DELIVERED - call {{ call_number }} now. \
Nobody outside the home has confirmed your help request yet. {{ disclaimer }}"""

UNACKNOWLEDGED = """\
{{ banner }}SENTRIX {{ short_id }}: nobody has acknowledged your help request yet. \
Call {{ call_number }} now if you need help. {{ disclaimer }}"""

ACKNOWLEDGED = """\
{{ banner }}SENTRIX {{ short_id }}: acknowledged{% if acknowledged_by %} by \
{{ acknowledged_by }}{% endif %}. Help is being arranged. Call {{ call_number }} if \
the situation gets worse. {{ disclaimer }}"""

BUILTIN = {
    "full": FULL, "short": SHORT, "prompt": PROMPT, "offline_prompt": OFFLINE_PROMPT,
    "reminder": REMINDER, "lapse_warning": LAPSE_WARNING, "lapsed": LAPSED,
    "resolved": RESOLVED, "duress": DURESS, "failed": FAILED,
    "unacknowledged": UNACKNOWLEDGED, "acknowledged": ACKNOWLEDGED,
}
#: ``profile.sos.templates`` keys (Tier 2) and the built-in each replaces.
PROFILE_TEMPLATE_KEYS = {"full": "full", "sms": "short", "prompt_sms": "offline_prompt"}

_ENV = SandboxedEnvironment(autoescape=False, undefined=ChainableUndefined,
                            keep_trailing_newline=False)
_COMPILED: dict[str, Any] = {}


def _compiled(name: str, source: str | None = None):
    if source is not None:
        return _ENV.from_string(source)
    template = _COMPILED.get(name)
    if template is None:
        template = _COMPILED[name] = _ENV.from_string(BUILTIN[name])
    return template


def _custom_source(profile: Mapping | None, name: str) -> str | None:
    """A Tier 2 template pushed with the profile, if one replaces ``name``."""
    sos = (profile or {}).get("sos") if isinstance(profile, Mapping) else None
    templates = (sos or {}).get("templates") if isinstance(sos, Mapping) else None
    if not isinstance(templates, Mapping):
        return None
    for key, builtin in PROFILE_TEMPLATE_KEYS.items():
        if builtin == name and isinstance(templates.get(key), str) and templates[key].strip():
            return templates[key][:4000]
    return None


def _finish(text: str, *, sms: bool, test_mode: bool) -> str:
    """Tidy, enforce the banner and the closing sentence."""
    lines = [line.rstrip() for line in text.replace("\r", "").split("\n")]
    text = "\n".join(lines).strip()
    if not text.endswith(DISCLAIMER):
        text = f"{text} {DISCLAIMER}" if text else DISCLAIMER
    if test_mode and not (text.startswith(TEST_PREFIX) or text.startswith(TEST_PREFIX_GSM)):
        text = f"{TEST_PREFIX} {text}"
    if sms:
        # Also turns TEST_PREFIX (en dash) into TEST_PREFIX_GSM.
        text = gsm_text(text)
    return text


def render(name: str, context: Mapping[str, Any], *, sms: bool = False,
           profile: Mapping | None = None, **extra: Any) -> str:
    """Render one template (``BUILTIN`` key) with a :func:`build_context`
    dict. A profile-supplied template (Tier 2) replaces the built-in when
    present; a broken one falls back to the built-in."""
    values = {**context, **extra}
    source = _custom_source(profile, name)
    try:
        text = _compiled(name, source).render(**values)
    except Exception:
        if source is None:
            raise
        text = _compiled(name).render(**values)
    return _finish(text, sms=sms, test_mode=bool(values.get("test_mode")))


def render_full(profile: Mapping | None, request: Mapping, **kwargs: Any) -> str:
    """The full SOS text (Portal, Desk, e-mail, contact page)."""
    audience = kwargs.pop("audience", "desk")
    return render("full", build_context(profile, request, audience=audience, **kwargs),
                  profile=profile)


def render_short(profile: Mapping | None, request: Mapping, *, channel: str = "sms",
                 link: str | None = "{link}", **kwargs: Any) -> str:
    """The short text. ``channel="telegram"`` leaves out the address and map
    (Telegram gets a short text and a Portal link only); ``"sms"`` keeps a
    compressed address and is fitted into two SMS segments. ``link`` is the
    per-recipient Portal link, or the ``{link}`` placeholder."""
    audience = kwargs.pop("audience", "contact")
    context = build_context(profile, request, audience=audience, link=link, **kwargs)
    if channel != "sms":
        return render("short", context, profile=profile, include_details=False)
    return fit_sms("short", context, profile=profile, include_details=True)


def render_offline_prompt(profile: Mapping | None, request: Mapping, **kwargs: Any) -> str:
    """The owner's SMS while the link is down (or the prompt is unseen)."""
    context = build_context(profile, request, audience="owner", **kwargs)
    return fit_sms("offline_prompt", context, profile=profile)


def fit_sms(name: str, context: Mapping[str, Any], *, profile: Mapping | None = None,
            **extra: Any) -> str:
    """Render for SMS within two segments, dropping optional parts first
    (map link, address, room and resident detail), then truncating; the
    banner and the closing sentence always stay."""
    attempts = [dict(), {"map_url": ""}, {"map_url": "", "address_short": ""},
                {"map_url": "", "address_short": "", "resident_phone": "",
                 "room": clean(context.get("room"), 16)}]
    text = ""
    for overrides in attempts:
        text = render(name, {**context, **overrides}, sms=True, profile=profile, **extra)
        if fits_sms(text):
            return text
    # Still too long (very long names): keep the head and the closing sentence.
    budget = sms_limit(text) - len(DISCLAIMER) - 4          # "... " before it
    head = text[: -len(DISCLAIMER)].rstrip()
    while sms_length(head) > budget and head:
        head = head[:-1]
    return f"{head.rstrip()}... {DISCLAIMER}"


def message_text(template: str, profile: Mapping | None, request: Mapping, *,
                 audience: str, channel: str, test_mode: bool | None = None,
                 link: str | None = None, **kwargs: Any) -> tuple[str, str | None]:
    """(text, e-mail subject) for one outbound message.

    ``template`` is a ``channels.base.Message.template`` name (``sos.prompt``,
    ``sos.reminder`` ...); ``channel`` a guard channel name. SMS channels get
    the GSM-7, two-segment rendering; Telegram the short text without the
    address; e-mail the full text with a subject line.
    """
    extra = {k: kwargs.pop(k) for k in ("urgent", "acknowledged_by") if k in kwargs}
    context = build_context(profile, request, audience=audience, test_mode=test_mode,
                            link=link, **kwargs)
    sms = channel in ("modem_sms", "twilio_sms", "modem_call", "twilio_call")
    name = template.removeprefix("sos.")
    if name == "sms":
        name = "short"
    if name not in BUILTIN:
        raise ValueError(f"unknown SOS template {template!r}")
    if name == "duress" and audience == "owner":
        # Someone forcing the resident must never see that help is coming.
        raise ValueError("the duress text is for the Desk and contacts only")
    values = {**context, "audience": audience, **extra}
    if channel == "telegram":
        values["include_details"] = False
    if name == "full" and channel == "telegram":
        # Telegram never carries the address, photo or medical notes.
        name, values["include_details"] = "short", False
    if sms:
        text = fit_sms(name, values, profile=profile)
    else:
        text = render(name, values, profile=profile)
    subject = None
    if channel == "email":
        subject = gsm_text(f"{'[TEST] ' if context['test_mode'] else ''}SENTRIX "
                           f"{context['short_id']}: {context['event']} "
                           f"{_SUBJECTS.get(name, 'update')}")
    return text, subject


_SUBJECTS = {"full": "SOS", "short": "SOS", "prompt": "- do you need assistance?",
             "offline_prompt": "- do you need assistance?", "reminder": "reminder",
             "lapse_warning": "lapses soon", "lapsed": "lapsed unanswered",
             "resolved": "resolved", "duress": "DURESS", "failed": "NOT DELIVERED",
             "unacknowledged": "not acknowledged", "acknowledged": "acknowledged"}


def preview(profile: Mapping | None, *, incident_type: str = "weapon",
            evidence_state: str = "confirmed", level: int = 3,
            test_mode: bool = True, now: Any = None, window_seconds: int = 1800,
            camera_id: str = "CAM_0") -> dict[str, Any]:
    """Every text a request would produce right now, from ``profile``
    (``GET /api/sos/preview``; onboarding's "check your SOS text")."""
    moment = to_ist(now) or datetime.now(IST)
    request = {"id": "000000preview", "short_id": "SX-PREVIEW",
               "incident_type": incident_type, "evidence_state": evidence_state,
               "trigger_level": level, "peak_level": level, "camera_id": camera_id,
               "created_at": moment.isoformat(),
               "expires_at": (moment + timedelta(seconds=window_seconds)).isoformat(),
               "flags": {}, "test_mode": test_mode,
               "policy": {"window_seconds": window_seconds}}
    return {
        "template_version": TEMPLATE_VERSION, "test_mode": test_mode,
        "incident_type": incident_type, "level": level,
        "full": render_full(profile, request, test_mode=test_mode),
        "sms": render_short(profile, request, channel="sms", test_mode=test_mode),
        "telegram": render_short(profile, request, channel="telegram", test_mode=test_mode),
        "prompt": render("prompt", build_context(profile, request, test_mode=test_mode,
                                                 now=moment), profile=profile),
        "offline_prompt": render_offline_prompt(profile, request, test_mode=test_mode),
        "buttons": dict(BUTTONS),
        "call_number": call_number(profile, test_mode),
    }


__all__ = ["TEMPLATE_VERSION", "IST", "DISCLAIMER", "WORDING_CORROBORATED",
           "WORDING_SINGLE_SOURCE", "WORDING_DEGRADED", "WORDING_SIMULATED",
           "DURESS_BANNER", "BUTTONS", "AUTHORITY_LABELS", "EVENT_NAMES",
           "MESSAGE_TEMPLATES", "BUILTIN", "clean", "clock_time", "to_ist", "gsm_text",
           "is_gsm", "sms_length", "fits_sms", "call_number", "build_context", "render",
           "render_full", "render_short", "render_offline_prompt", "fit_sms",
           "message_text", "preview"]

"""SOS texts (track T-SOS): the mandated wording, the test banner, SMS
fitting, Telegram minimisation, consent-gated medical notes and rendering
that never trusts the profile (docs/7 §5.11, §7.10)."""

from __future__ import annotations

import copy

import pytest

from sentrix.alerting.guard import TEST_PREFIX, TEST_PREFIX_GSM
from sentrix.sos import templates as T

#: Fictional demo household (docs/7 §5.11 shape).
PROFILE = {
    "schema": 1, "version": 7, "customer_code": "SX-0007", "language": "en",
    "owner": {"name": "Riya Sharma (demo)", "phone": "+919812345610"},
    "contacts": [{"id": "c1", "name": "Aman (demo)", "phone": "+919812345611",
                  "consent_status": "CONFIRMED", "telegram": True, "priority": 1}],
    "desk": {"name": "SENTRIX Desk (simulated)", "phone": "+919800000001", "simulated": True},
    "address": {"house": "H.No. 212", "street": "Green Avenue", "locality": "Sector 15",
                "landmark": "Shiv Mandir", "city": "Patiala", "state": "Punjab",
                "pincode": "147001", "floor": "Ground", "lat": 30.3398, "lon": 76.3869},
    "household": {"occupants": {"adults": 2, "children": 1, "seniors": 1},
                  "pets": "one dog", "medical_notes": "Senior uses a wheelchair"},
    "nearest": {"police": {"name": "Civil Lines PS", "phone": "+911752212345"},
                "fire": {"name": "Fire Station 2", "phone": "+911752212346"}},
    "cameras": [{"camera_id": "CAM_0", "room_label": "Living room", "floor": "Ground"}],
    "consents": {"medical_notes": True, "sos_snapshot": True},
}
REQUEST = {"id": "7f3a2c11-0000-4000-8000-000000000001", "short_id": "SX-7F3A2C",
           "incident_type": "weapon", "evidence_state": "confirmed", "trigger_level": 3,
           "peak_level": 3, "camera_id": "CAM_0", "created_at": "2026-10-14T09:02:07+00:00",
           "expires_at": "2026-10-14T09:32:07+00:00", "flags": {}, "test_mode": True,
           "policy": {"window_seconds": 1800}, "evidence_hash": "3fa9c1d2" * 8,
           "state": "PENDING"}


def req(**changes):
    out = copy.deepcopy(REQUEST)
    out.update(changes)
    return out


class TestMandatedWording:
    def test_every_text_ends_with_the_disclaimer_and_starts_with_the_banner(self):
        for template in T.MESSAGE_TEMPLATES:
            audience = "desk" if template == "sos.duress" else "owner"
            for channel in ("modem_sms", "telegram", "email"):
                text, _ = T.message_text(template, PROFILE, req(), audience=audience,
                                         channel=channel, now="2026-10-14T09:10:00+00:00")
                assert text.endswith(T.DISCLAIMER), (template, channel)
                banner = TEST_PREFIX_GSM if channel == "modem_sms" else TEST_PREFIX
                assert text.startswith(banner), (template, channel)

    def test_no_banner_outside_test_mode(self):
        text = T.render_full(PROFILE, req(test_mode=False), test_mode=False)
        assert not text.startswith("[SENTRIX DEMO") and "DEMO" not in text.split("\n")[0]
        assert "and 112 if needed" in text

    def test_evidence_wording(self):
        single = T.render_full(PROFILE, req())
        assert "possible WEAPON (one camera check)" in single
        corroborated = T.render_full(PROFILE, req(evidence_state="corroborated", peak_level=4))
        assert "WEAPON confirmed by two different models" in corroborated
        assert "independent" not in corroborated
        dark = T.render_full(PROFILE, req(), degraded=True)
        assert T.WORDING_DEGRADED in dark
        simulated = T.render_full(PROFILE, req(flags={"simulated": True}))
        assert "SIMULATED DETECTION" in simulated

    def test_test_mode_shows_the_desk_number_instead_of_112(self):
        text = T.render_full(PROFILE, req())
        assert "+919800000001 if needed" in text
        assert "tel:112" not in text and "sms:112" not in text
        assert T.call_number(PROFILE, True) == "+919800000001"
        assert T.call_number(PROFILE, False) == "112"
        assert T.call_number({}, True) == "the SENTRIX desk"

    def test_buttons(self):
        assert T.BUTTONS["approve"] == "Alert my contacts and the SENTRIX desk"
        assert T.BUTTONS["call_112"] == "Call 112 now"
        assert T.BUTTONS["checking"] == "I'm checking"
        assert T.BUTTONS["decline"] == "False alarm (PIN)"

    def test_duress_banner_only_for_desk_and_contacts(self):
        duress = req(flags={"duress": True})
        desk = T.render_full(PROFILE, duress, audience="desk")
        assert "Do not go to the home. Call 112." in desk
        owner = T.render_full(PROFILE, duress, audience="owner")
        assert "DURESS" not in owner and "Do not go to the home" not in owner
        with pytest.raises(ValueError):
            T.message_text("sos.duress", PROFILE, duress, audience="owner", channel="email")
        text, _ = T.message_text("sos.duress", PROFILE, duress, audience="contact",
                                 channel="modem_sms")
        assert "Do not go to the home. Call 112." in text


class TestChannels:
    def test_telegram_gets_no_address_phone_photo_or_medical(self):
        text = T.render_short(PROFILE, req(), channel="telegram")
        for private in ("Green Avenue", "147001", "maps.google", "+919812345610",
                        "wheelchair"):
            assert private not in text, private
        assert "{link}" in text
        full_as_telegram, _ = T.message_text("sos.full", PROFILE, req(), audience="contact",
                                             channel="telegram", link="https://p/c/abc")
        assert "Green Avenue" not in full_as_telegram and "https://p/c/abc" in full_as_telegram

    def test_sms_fits_two_gsm_segments(self):
        for template in ("sos.sms", "sos.offline_prompt", "sos.reminder", "sos.duress",
                         "sos.lapsed", "sos.failed"):
            audience = "contact" if template in ("sos.sms", "sos.duress") else "owner"
            text, _ = T.message_text(template, PROFILE, req(flags={"duress": True}),
                                     audience=audience, channel="modem_sms")
            assert T.is_gsm(text), (template, [c for c in text if not T.is_gsm(c)])
            assert T.sms_length(text) <= T.SMS_LIMIT_GSM, (template, T.sms_length(text))

    def test_sms_drops_optional_parts_for_long_profiles(self):
        long = copy.deepcopy(PROFILE)
        long["owner"]["name"] = "Riya " + "Sharmaverma" * 9
        long["address"]["street"] = "Very Long Street Name Avenue " * 4
        text = T.render_short(long, req(), channel="sms")
        assert T.sms_length(text) <= T.SMS_LIMIT_GSM and text.endswith(T.DISCLAIMER)
        assert text.startswith(TEST_PREFIX_GSM)

    def test_unicode_names_fall_back_to_ucs2_limits(self):
        hindi = copy.deepcopy(PROFILE)
        hindi["owner"]["name"] = "रिया शर्मा"
        text = T.render_short(hindi, req(), channel="sms")
        # Kept in UCS-2 (three segments) rather than mangling the name.
        assert "रिया शर्मा" in text and not T.is_gsm(text)
        assert len(text) <= T.SMS_LIMIT_UCS2 and text.endswith(T.DISCLAIMER)

    def test_offline_prompt_never_offers_a_decline_by_sms(self):
        text = T.render_offline_prompt(PROFILE, req())
        assert "PIN" not in text and "Reply 0" not in text
        assert "Lapses 15:02" in text                     # IST

    def test_email_has_a_subject(self):
        text, subject = T.message_text("sos.prompt", PROFILE, req(), audience="owner",
                                       channel="email")
        assert subject.startswith("[TEST] SENTRIX SX-7F3A2C") and "assistance" in subject
        assert "Do you need assistance?" in text


class TestProfileSafety:
    def test_medical_notes_only_with_consent_and_never_for_the_owner(self):
        assert "wheelchair" in T.render_full(PROFILE, req(), audience="desk")
        assert "wheelchair" not in T.render_full(PROFILE, req(), audience="owner")
        no_consent = copy.deepcopy(PROFILE)
        no_consent["consents"]["medical_notes"] = False
        assert "wheelchair" not in T.render_full(no_consent, req(), audience="desk")

    def test_profile_values_are_data_not_templates(self):
        evil = copy.deepcopy(PROFILE)
        evil["owner"]["name"] = "{{ 7*7 }} {% for x in range(3) %}x{% endfor %}"
        evil["address"]["house"] = "{{ ''.__class__.__mro__ }}"
        text = T.render_full(evil, req())
        assert "{{ 7*7 }}" in text and "49" not in text
        assert "__mro__" in text and "<class" not in text

    def test_control_characters_and_lengths_are_cleaned(self):
        assert T.clean("a\x00b​c\n  d") == "abc d"
        assert len(T.clean("x" * 500, 50)) == 50
        assert T.clean(None) == "" and T.clean({"a": 1}) == ""

    def test_missing_fields_render_without_inventing_any(self):
        bare = {"schema": 1}
        text = T.render_full(bare, {"id": "abcdef12", "incident_type": "fire",
                                    "created_at": None, "flags": {}})
        assert "ADDRESS: address not set" in text and "RESIDENT: the resident" in text
        for absent in ("MAP:", "HOME:", "MEDICAL:", "NEAREST:", "EVIDENCE:"):
            assert absent not in text
        assert T.render_full(None, {}).endswith(T.DISCLAIMER)
        odd = {"id": "x", "peak_level": "high", "policy": "{}", "flags": "duress",
               "created_at": 12, "expires_at": "soon"}
        assert T.render_full(PROFILE, odd).endswith(T.DISCLAIMER), "bad types never crash"

    def test_tier2_profile_templates_are_sandboxed_and_keep_the_rules(self):
        custom = copy.deepcopy(PROFILE)
        custom["sos"] = {"templates": {"version": "en-2",
                                       "full": "CUSTOM {{ short_id }} {{ ().__class__ }}"}}
        text = T.render_full(custom, req())
        assert text.startswith(TEST_PREFIX) and "CUSTOM SX-7F3A2C" in text
        assert text.endswith(T.DISCLAIMER)
        broken = copy.deepcopy(PROFILE)
        broken["sos"] = {"templates": {"full": "{% for %}"}}
        assert "SENTRIX SOS" in T.render_full(broken, req())     # built-in fallback


class TestPreview:
    def test_preview_has_every_text(self):
        out = T.preview(PROFILE, incident_type="fire", level=3,
                        now="2026-10-14T09:00:00+00:00")
        assert set(out) >= {"full", "sms", "telegram", "prompt", "offline_prompt",
                            "buttons", "call_number", "template_version"}
        assert "FIRE" in out["full"] and out["template_version"] == T.TEMPLATE_VERSION
        assert "Answer by 15:00" in out["prompt"]
        assert out["call_number"] == "+919800000001"

    def test_times_are_ist(self):
        assert T.clock_time("2026-10-14T09:02:07+00:00") == "14:32"
        assert T.clock_time("2026-10-14T09:02:07", seconds=True) == "14:32:07"
        assert T.clock_time(None) == "" and T.clock_time("garbage") == ""

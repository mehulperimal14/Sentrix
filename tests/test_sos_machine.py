"""The SOS machine (track T-SOS): pure decisions, no database, no clock.

Property-style tests drive the machine through every operation it offers
and check the invariants the contract states (docs/7 §7.3-§7.8): every step
it emits is legal in ``states.TRANSITIONS``/``SELF_EVENTS``, every state is
reachable, terminal states absorb (only "Request help now" leads on, through
a *new* request), an approved request never expires, steps are never taken
twice, and a duress request never says anything to the owner.
"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from sentrix.actor import Actor
from sentrix.config import settings
from sentrix.sos import machine as M
from sentrix.sos import states as S
from sentrix.sos.policy import (Reach, Rules, clamp_window, confirmed_contacts,
                                contacts_delay, freeze, window_seconds)

T0 = 1_792_000_000.0
RULES = Rules(test_mode=False)
TEST_RULES = Rules(test_mode=True, repage_cap=3)
OFFLINE = Reach(online=False, standalone=False, remote_prompt=False)
ONLINE = Reach(online=True, standalone=False, remote_prompt=True)
OWNER = Actor("owner", id="u1", name="Riya")
CONTACT = Actor("contact", id="c1", name="Aman")
STRANGER = Actor("contact", id="c9", name="Unknown")
POLICY = {"window_seconds": 1800, "safety_defaults": True,
          "contacts_for_single_source_l3": False,
          "contacts": [{"id": "c1", "name": "Aman", "phone": "+919812345611",
                        "telegram": True, "priority": 1}],
          "owner": {"name": "Riya", "phone": "+919812345610"},
          "desk": {"name": "Desk", "phone": "+919800000001"}}


def snap(state=S.PENDING, **kw) -> M.Snapshot:
    base = dict(id="r1", state=state, version=2, incident_type="weapon",
                evidence_state="confirmed", trigger_level=3, peak_level=3,
                created_at=T0, expires_at=T0 + 1800,
                prompted_at=None if state == S.DRAFTED else T0, policy=POLICY,
                incident_ids=("i1",))
    if state in S.HELP_PATH:
        base.update(help_since=T0 + 60, responded_at=T0 + 60, authority="POLICE")
    base.update(kw)
    return M.Snapshot(**base)


def after(s: M.Snapshot, step: M.Step, at: float) -> M.Snapshot:
    """What the engine's write + re-read does to a snapshot (the test's model)."""
    to = step.forced_state or S.next_state(s.state, step.event)
    assert to is not None, f"{step.event} emitted in {s.state}: not allowed by the contract"
    fields = dict(step.fields)
    out = replace(s, state=to, version=s.version + 1,
                  flags={**dict(s.flags), **dict(step.flags)},
                  done=s.done | ({step.detail["step"]} if step.detail.get("step") else set()))
    for key in ("expires_at", "responded_at", "seen_at", "peak_level", "urgency",
                "repage_count", "authority", "evidence_state"):
        if key in fields:
            out = replace(out, **{key: fields[key]})
    if "incident_ids" in fields:
        out = replace(out, incident_ids=tuple(fields["incident_ids"]))
    if step.event == S.PROMPTED:
        out = replace(out, prompted_at=at)
    if step.event == S.REMINDER:
        out = replace(out, reminder_upto=max(s.reminder_upto, step.detail["offset"]))
    if step.event == S.CHECKING:
        out = replace(out, last_checking_at=at)
    if step.event == S.APPROVE or (step.event == S.CONFLICT and to in S.HELP_PATH
                                   and s.state not in S.HELP_PATH):
        out = replace(out, help_since=at)
    if step.event == S.RETRY:
        out = replace(out, help_since=at, help_attempt=s.help_attempt + 1)
    if step.event == S.DELIVERY_FAILED:
        out = replace(out, failed_offline=not step.detail.get("online", True))
    return out


# ── the contract table ────────────────────────────────────────────────────────
def operations(s: M.Snapshot, now: float):
    """Every input the machine accepts, as (label, steps, linked)."""
    out = []
    if s.state == S.DRAFTED:
        out.append(("prompt", (M.prompt(s, now, RULES, OFFLINE),), None))
    for action in M.RESPOND_ACTIONS:
        for pin in (("ok", "duress", "invalid") if M.needs_pin(action) else (None,)):
            for actor in (OWNER, CONTACT):
                outcome = M.respond(s, action, actor=actor, channel="lc", now=now,
                                    rules=RULES, pin=pin)
                out.append((f"{action}:{pin}:{actor.type}", outcome.steps, outcome.linked))
    for event in M.UPDATE_EVENTS:
        detail = {"outcome": "help_arrived"} if event == S.RESOLVE else {}
        outcome = M.cloud_event(s, event, actor=Actor.cloud(), now=now, detail=detail)
        out.append((f"cloud:{event}", outcome.steps, None))
    for level in (4, 5):
        step = M.level_rise(s, level, "corroborated", now, RULES)
        out.append((f"rise{level}", (step,) if step else (), None))
    for offset in (0, 61, 121, 301, 601, 1201, 1501, 1801, 4000, 90_000):
        for reach in (OFFLINE, ONLINE):
            out.append((f"due+{offset}", M.due(s, now + offset, RULES, reach), None))
    for cloud_state in S.STATES:
        step = M.reconcile(s, cloud_state)
        out.append((f"sync:{cloud_state}", (step,) if step else (), None))
    return out


def walk(s: M.Snapshot, ops, now: float):
    for label, steps, linked in ops:
        current = s
        for step in steps:
            current = after(current, step, now)
        yield label, current, steps, linked


class TestContract:
    def test_every_emitted_step_is_legal_and_every_state_is_reachable(self):
        start = snap(S.DRAFTED, expires_at=None)
        seen, frontier = {start.state}, [start]
        explored = 0
        while frontier and explored < 400:
            s = frontier.pop()
            explored += 1
            for _label, nxt, _steps, linked in walk(s, operations(s, T0 + 10), T0 + 10):
                if linked is not None:
                    # "Request help now" opens a new request: it starts DRAFTED.
                    assert s.state in S.TERMINAL_STATES or s.state in S.PROMPTING_STATES \
                        or s.state in S.HELP_PATH
                if nxt.state not in seen:
                    seen.add(nxt.state)
                    frontier.append(nxt)
        assert seen == set(S.STATES)

    def test_terminal_states_absorb_except_the_help_path_of_a_new_request(self):
        for terminal in S.TERMINAL_STATES:
            s = snap(terminal)
            for label, nxt, steps, linked in walk(s, operations(s, T0 + 5), T0 + 5):
                if label.startswith("sync:"):
                    continue          # the reconnect merge is the one sanctioned jump
                assert nxt.state == terminal, f"{label} moved {terminal} to {nxt.state}"
                for step in steps:
                    assert step.event in (S.PIN_FAILED, S.NOTIFICATION,
                                          S.HELP_REQUESTED_LATER), (label, step.event)
                if linked is not None:
                    # "Request help now" on these; a duress PIN on any closed
                    # request still gets help (silently, through a new one).
                    assert terminal in (S.EXPIRED, S.DECLINED, S.CANCELLED) or linked.duress

    def test_help_now_on_a_closed_request_links_a_new_approved_one(self):
        for terminal in (S.EXPIRED, S.DECLINED, S.CANCELLED):
            outcome = M.respond(snap(terminal), "help_now", actor=OWNER, channel="lc",
                                now=T0, rules=RULES, authority="MEDICAL")
            assert outcome.ok and outcome.linked == M.LinkedRequest("r1", "MEDICAL")
            assert [s.event for s in outcome.steps] == [S.HELP_REQUESTED_LATER]
        assert M.respond(snap(S.CLOSED), "help_now", actor=OWNER, channel="lc", now=T0,
                         rules=RULES).code == "conflict"

    @pytest.mark.parametrize("seed", range(40))
    def test_random_walks_keep_the_invariants(self, seed):
        rng = random.Random(seed)
        s = snap(S.DRAFTED, expires_at=None)
        now = T0
        reminder_steps: list[str] = []
        contacts_alerts = 0
        for _ in range(40):
            now += rng.choice((0, 5, 30, 61, 121, 300, 900))
            ops = operations(s, now)
            label, steps, linked = rng.choice(ops)
            for step in steps:
                if s.state in S.TERMINAL_STATES and not step.forced_state:
                    assert S.next_state(s.state, step.event) == s.state
                if s.state in S.HELP_PATH:
                    assert step.event != S.EXPIRE, "an approved request never expires"
                if s.duress:
                    assert step.restricted, f"{step.event} after duress must be restricted"
                    assert all(n.audience != "owner" for n in step.notices)
                if step.event == S.REMINDER:
                    reminder_steps.append(step.detail["step"])
                if step.detail.get("step") == S.STEP_CONTACTS_T0:
                    contacts_alerts += 1
                if step.event == S.REPAGE:
                    assert step.fields["repage_count"] == s.repage_count + 1
                    assert step.fields["repage_count"] <= RULES.repage_cap
                s = after(s, step, now)
        assert len(reminder_steps) == len(set(reminder_steps)), "a reminder step repeated"
        assert contacts_alerts <= 1


# ── eligibility ───────────────────────────────────────────────────────────────
def facts(kind="weapon", level=3, state="confirmed", armed=False, at=T0, incident="i9"):
    return M.TriggerFacts(incident_id=incident, incident_type=kind, level=level,
                          evidence_state=state, at=at, armed=armed)


class TestEligibility:
    def test_only_alarm_classes_at_l3_are_wanted(self):
        assert M.wants("weapon", 3, RULES) and M.wants("fire", 5, RULES)
        for kind in ("assault", "robbery", "stick", "smoke", "intrusion", "normal"):
            assert not M.wants(kind, 3, RULES), kind
        assert not M.wants("weapon", 2, RULES)

    def test_experimental_classes_and_l2_never_open(self):
        for kind in ("assault", "robbery", "normal"):
            assert M.on_trigger(facts(kind), open_request=None, history=[],
                                rules=RULES).action == "ignore"
        assert M.on_trigger(facts(level=2), open_request=None, history=[],
                            rules=RULES).action == "ignore"

    def test_armed_intrusion_is_held_for_the_entry_delay(self):
        held = M.on_trigger(facts("intrusion", armed=True), open_request=None,
                            history=[], rules=RULES)
        assert held.action == "hold" and held.hold_until == T0 + 30
        assert M.on_trigger(facts("intrusion", armed=False), open_request=None,
                            history=[], rules=RULES).action == "ignore"
        assert M.on_trigger(facts("intrusion", armed=True), open_request=None, history=[],
                            rules=RULES, entry_delay_over=True).action == "open"

    def test_one_open_request_per_device_later_incidents_attach(self):
        decision = M.on_trigger(facts(incident="i2"), open_request=snap(), history=[],
                                rules=RULES)
        assert decision.action == "attach"
        attached = decision.steps[0]
        assert attached.event == S.ATTACHED
        assert attached.fields["incident_ids"] == ["i1", "i2"]
        again = M.on_trigger(facts(incident="i1"), open_request=snap(), history=[],
                             rules=RULES)
        assert again.action == "attach" and again.steps == ()

    def test_a_rollover_at_l4_attaches_as_a_level_rise(self):
        decision = M.on_trigger(facts(level=4, state="corroborated", incident="i2"),
                                open_request=snap(), history=[], rules=RULES)
        assert [s.event for s in decision.steps] == [S.ATTACHED, S.LEVEL_RISE]

    def test_repeat_after_decline_at_most_once_per_window(self):
        declined = snap(S.DECLINED, responded_at=T0 + 100)
        first = M.on_trigger(facts(at=T0 + 200), open_request=None, history=[declined],
                             rules=RULES)
        assert first.action == "open" and first.flags == {"repeat": True}
        repeat = snap(S.DECLINED, id="r2", created_at=T0 + 200, responded_at=T0 + 260,
                      flags={"repeat": True})
        history = [repeat, declined]
        assert M.on_trigger(facts(at=T0 + 300), open_request=None, history=history,
                            rules=RULES).action == "suppress"
        # Stronger evidence than what was declined always prompts.
        assert M.on_trigger(facts(level=4, at=T0 + 300), open_request=None,
                            history=history, rules=RULES).action == "open"
        # A different threat is not a repeat; after the window it is a new request.
        assert M.on_trigger(facts("fire", at=T0 + 300), open_request=None, history=history,
                            rules=RULES).flags == {}
        assert M.on_trigger(facts(at=T0 + 900), open_request=None, history=history,
                            rules=RULES).flags == {}

    def test_after_expiry_a_new_l3_opens_a_new_request(self):
        decision = M.on_trigger(facts(at=T0 + 1900), open_request=None,
                                history=[snap(S.EXPIRED)], rules=RULES)
        assert decision.action == "open" and decision.flags == {}


# ── the owner's timeline ──────────────────────────────────────────────────────
def run_timeline(s: M.Snapshot, until: float, reach=OFFLINE, rules=RULES, step=15.0):
    """Sweep every ``step`` seconds like the sweeper; returns (t, step) pairs."""
    out = []
    t = s.created_at
    while t <= until and s.state not in S.TERMINAL_STATES:
        for st in M.due(s, t, rules, reach):
            out.append((t - s.created_at, st))
            s = after(s, st, t)
        t += step
    return out, s


class TestTimeline:
    def test_thirty_minute_window(self):
        events, final = run_timeline(snap(), T0 + 1900, reach=ONLINE)
        reminders = [(t, st.detail["step"]) for t, st in events if st.event == S.REMINDER]
        assert [name for _, name in reminders] == ["r120", "r300", "r600", "r1200"]
        assert [t for t, _ in reminders] == [120, 300, 600, 1200]
        lapse = [t for t, st in events if st.event == S.LAPSE_WARNING]
        assert lapse == [1500]
        assert final.state == S.EXPIRED
        expire = [st for _, st in events if st.event == S.EXPIRE][0]
        assert expire.package == "expired" and expire.verdict is None, \
            "expired = unanswered: the incident stays unreviewed"

    def test_prompt_by_sms_only_offline_and_unseen_after_ten_minutes(self):
        online = M.prompt(snap(S.DRAFTED), T0, RULES, ONLINE).notices[0]
        offline = M.prompt(snap(S.DRAFTED), T0, RULES, OFFLINE).notices[0]
        assert not online.sms and offline.sms and offline.sms_template == M.T_OFFLINE
        events, _ = run_timeline(snap(), T0 + 700, reach=ONLINE)
        sms = {st.detail["step"]: st.notices[0].sms for _, st in events
               if st.event == S.REMINDER}
        assert sms == {"r120": False, "r300": False, "r600": True}
        seen, _ = run_timeline(snap(seen_at=T0 + 10), T0 + 700, reach=ONLINE)
        assert not [st for _, st in seen if st.event == S.REMINDER and st.notices[0].sms]

    def test_checking_pauses_reminders_only(self):
        checking = M.respond(snap(), "checking", actor=OWNER, channel="lc", now=T0 + 100,
                             rules=RULES)
        assert [s.event for s in checking.steps] == [S.CHECKING]
        s = after(snap(), checking.steps[0], T0 + 100)
        assert M.due(s, T0 + 130, RULES, ONLINE) == ()            # r120 snoozed
        assert [st.event for st in M.due(s, T0 + 401, RULES, ONLINE)] == [S.REMINDER]
        late = M.due(s, T0 + 401, RULES, ONLINE)[0]
        assert late.detail["step"] == "r300" and late.detail["collapsed"] == [120]
        # Expiry and the safety timers never pause.
        fire = after(snap(incident_type="fire"), checking.steps[0], T0 + 100)
        assert S.CONTACTS_NOTIFIED in [st.event for st in M.due(fire, T0 + 121, RULES, ONLINE)]
        assert [st.event for st in M.due(s, T0 + 1801, RULES, ONLINE)] == [S.EXPIRE]

    def test_overdue_reminders_collapse_into_one(self):
        steps = M.due(snap(), T0 + 650, RULES, ONLINE)
        reminder = [s for s in steps if s.event == S.REMINDER]
        assert len(reminder) == 1 and reminder[0].detail == {
            "step": "r600", "offset": 600, "collapsed": [120, 300]}

    def test_short_demo_window_skips_steps_after_expiry(self):
        s = snap(expires_at=T0 + 90, policy={**POLICY, "window_seconds": 90})
        events, final = run_timeline(s, T0 + 200, reach=ONLINE)
        assert not [st for _, st in events if st.event in (S.REMINDER, S.LAPSE_WARNING)]
        assert final.state == S.EXPIRED

    def test_undelivered_when_no_owner_message_arrived(self):
        events, _ = run_timeline(snap(), T0 + 90, reach=ONLINE)
        assert [st.detail["step"] for _, st in events if st.event == S.UNDELIVERED] == \
            ["undelivered"]
        reached, _ = run_timeline(snap(owner_reached=True), T0 + 90, reach=ONLINE)
        assert not [st for _, st in reached if st.event == S.UNDELIVERED]


class TestLevelRise:
    def test_rise_makes_it_urgent_and_restarts_the_window(self):
        s = snap(expires_at=T0 + 1800)
        step = M.level_rise(s, 4, "corroborated", T0 + 1700, RULES)
        assert step.event == S.LEVEL_RISE and step.detail["step"] == "rise4"
        assert step.fields["urgency"] == "HIGH" and step.fields["peak_level"] == 4
        assert step.fields["expires_at"] == T0 + 1700 + 1800
        notice = step.notices[0]
        assert notice.audience == "owner" and notice.sms and not notice.call
        assert step.rerender
        five = M.level_rise(after(s, step, T0 + 1700), 5, "corroborated", T0 + 1710, RULES)
        assert five.detail["step"] == "rise5" and five.notices[0].call
        assert M.level_rise(s, 3, "confirmed", T0, RULES) is None
        assert M.level_rise(snap(peak_level=4), 4, "corroborated", T0, RULES) is None

    def test_rise_on_the_help_path_tells_nobody_new(self):
        step = M.level_rise(snap(S.DISPATCHED), 5, "corroborated", T0, RULES)
        assert step.notices == () and "expires_at" not in step.fields


# ── safety defaults ───────────────────────────────────────────────────────────
class TestSafetyDefaults:
    @pytest.mark.parametrize("kind,level,expected", [
        ("weapon", 3, None), ("weapon", 4, 0.0), ("weapon", 5, 0.0), ("fire", 3, 120.0),
        ("fire", 4, 0.0), ("intrusion", 3, 0.0), ("duress", 4, 0.0)])
    def test_when_contacts_are_alerted(self, kind, level, expected):
        rule = contacts_delay(POLICY, incident_type=kind, peak_level=level, rules=RULES)
        assert (rule[0] if rule else None) == expected

    def test_single_source_weapon_owner_only_until_gate_p8(self):
        gated = {**POLICY, "contacts_for_single_source_l3": True}
        assert contacts_delay(gated, incident_type="weapon", peak_level=3,
                              rules=RULES) == (300.0, "single_source_weapon")

    def test_safety_defaults_off_or_no_contacts_owner_only(self):
        off = {**POLICY, "safety_defaults": False}
        assert contacts_delay(off, incident_type="fire", peak_level=5, rules=RULES) is None
        assert contacts_delay({**POLICY, "contacts": []}, incident_type="fire",
                              peak_level=5, rules=RULES) is None
        assert contacts_delay(off, incident_type="duress", peak_level=4,
                              rules=RULES) == (0.0, "duress")

    def test_contacts_alerted_once_while_the_owner_is_asked(self):
        events, _ = run_timeline(snap(incident_type="fire"), T0 + 600, reach=OFFLINE)
        alerts = [(t, st) for t, st in events if st.event == S.CONTACTS_NOTIFIED]
        assert len(alerts) == 1 and alerts[0][0] == 120
        assert alerts[0][1].notices[0].audience == "contacts"
        assert alerts[0][1].detail == {"step": "contacts_t0", "reason": "confirmed_fire"}

    def test_a_contact_may_approve_only_under_safety_defaults(self):
        ok = M.respond(snap(), "approve", actor=CONTACT, channel="contact_page", now=T0,
                       rules=RULES)
        assert ok.ok and ok.steps[0].event == S.APPROVE
        for actor, s in ((STRANGER, snap()), (CONTACT, snap(policy={**POLICY,
                                                                   "safety_defaults": False}))):
            assert M.respond(s, "approve", actor=actor, channel="contact_page", now=T0,
                             rules=RULES).code == "forbidden_role"
        assert M.respond(snap(), "decline", actor=CONTACT, channel="contact_page", now=T0,
                         rules=RULES, pin="ok").code == "forbidden_role"
        assert M.respond(snap(), "approve", actor=Actor("operator", id="d1"), channel="desk",
                         now=T0, rules=RULES).code == "forbidden_role"


# ── answers ───────────────────────────────────────────────────────────────────
class TestAnswers:
    def test_approve_records_who_and_stops_reminders(self):
        outcome = M.respond(snap(), "approve", actor=OWNER, channel="portal", now=T0 + 30,
                            rules=RULES, authority="medical")
        step = outcome.steps[0]
        assert step.fields == {"authority": "MEDICAL", "responded_at": T0 + 30,
                               "responded_by": "portal:u1", "response_channel": "portal"}
        assert step.package == "authority"
        assert {(n.audience, n.step) for n in step.notices} == {
            ("desk", "desk_t0"), ("contacts", "contacts_t0")}
        approved = after(snap(), step, T0 + 30)
        assert not [s for s in M.due(approved, T0 + 1300, RULES, ONLINE)
                    if s.event == S.REMINDER]
        assert M.respond(approved, "approve", actor=OWNER, channel="lc", now=T0,
                         rules=RULES).message == "Help was already requested"
        assert M.respond(snap(), "approve", actor=OWNER, channel="lc", now=T0, rules=RULES,
                         authority="ARMY").code == "invalid_args"

    def test_decline_needs_the_pin_and_cancels_the_package(self):
        assert M.respond(snap(), "decline", actor=OWNER, channel="lc", now=T0,
                         rules=RULES, pin=None).code == "pin_invalid"
        wrong = M.respond(snap(), "decline", actor=OWNER, channel="lc", now=T0,
                          rules=RULES, pin="invalid")
        assert wrong.code == "pin_invalid" and wrong.steps[0].event == S.PIN_FAILED
        assert M.respond(snap(), "decline", actor=OWNER, channel="lc", now=T0, rules=RULES,
                         pin="locked").code == "pin_locked"
        declined = M.respond(snap(flags={"contacts_notified": True}), "decline",
                             actor=OWNER, channel="lc", now=T0, rules=RULES, pin="ok")
        step = declined.steps[0]
        assert (step.event, step.package, step.verdict) == (S.DECLINE, "cancelled", "FALSE")
        assert [(n.audience, n.step) for n in step.notices] == [("contacts", "resolved")]

    def test_false_alarm_after_approval_marks_safe_and_keeps_help(self):
        for action in ("decline", "safe"):
            outcome = M.respond(snap(S.DISPATCHED), action, actor=OWNER, channel="lc",
                                now=T0, rules=RULES, pin="ok")
            step = outcome.steps[0]
            assert step.event == S.RESIDENT_SAFE and step.flags == {"resident_safe": True}
            assert step.package is None and step.verdict is None
            assert {n.audience for n in step.notices} == {"desk", "contacts"}

    def test_duress_pin_approves_silently(self):
        outcome = M.respond(snap(), "decline", actor=OWNER, channel="lc", now=T0,
                            rules=RULES, pin="duress")
        assert outcome.ok and outcome.message == "Marked as false alarm"
        step = outcome.steps[0]
        assert step.event == S.APPROVE and step.restricted
        assert step.flags == {"duress": True} and step.detail["duress"] is True
        assert {(n.audience, n.template) for n in step.notices} == {
            ("desk", "sos.duress"), ("contacts", "sos.duress")}
        duress = after(snap(), step, T0)
        assert S.owner_view(duress.state, dict(duress.flags))["state"] == S.DECLINED
        # Answered exactly like the decline it appears to be.
        for action in ("approve", "checking"):
            assert M.respond(duress, action, actor=OWNER, channel="lc", now=T0,
                             rules=RULES).code == "conflict"
        assert M.respond(duress, "decline", actor=OWNER, channel="lc", now=T0,
                         rules=RULES, pin="ok").code == "conflict"
        later = M.respond(duress, "help_now", actor=OWNER, channel="lc", now=T0, rules=RULES)
        assert later.ok and all(s.restricted for s in later.steps)

    def test_a_duress_request_answers_exactly_like_a_declined_one(self):
        duress = snap(S.APPROVED, flags={"duress": True})
        declined = snap(S.DECLINED)
        for action, pin in (("approve", None), ("checking", None), ("decline", "ok"),
                            ("safe", "ok"), ("decline", "duress")):
            a = M.respond(duress, action, actor=OWNER, channel="lc", now=T0, rules=RULES,
                          pin=pin)
            b = M.respond(declined, action, actor=OWNER, channel="lc", now=T0, rules=RULES,
                          pin=pin)
            assert (a.code, a.message) == (b.code, b.message), action

    def test_duress_pin_on_the_help_path_and_on_a_closed_request(self):
        outcome = M.respond(snap(S.DISPATCHED), "safe", actor=OWNER, channel="lc", now=T0,
                            rules=RULES, pin="duress")
        step = outcome.steps[0]
        assert step.event == S.CONTACTS_NOTIFIED and step.restricted
        assert step.flags["duress"] and "resident_safe" not in step.flags
        closed = M.respond(snap(S.EXPIRED), "decline", actor=OWNER, channel="lc", now=T0,
                           rules=RULES, pin="duress")
        assert closed.code == "conflict" and closed.linked.duress

    def test_checking_only_while_pending(self):
        for state in (S.DRAFTED, S.APPROVED, S.EXPIRED):
            assert M.respond(snap(state), "checking", actor=OWNER, channel="lc", now=T0,
                             rules=RULES).code == "conflict"


# ── the help path ─────────────────────────────────────────────────────────────
class TestHelpPath:
    def test_undelivered_then_failed_then_retry_when_the_link_returns(self):
        s = snap(S.APPROVED)                  # help_since T0 + 60
        assert M.due(s, T0 + 100, RULES, OFFLINE) == ()
        warn = M.due(s, T0 + 121, RULES, OFFLINE)
        assert [x.event for x in warn] == [S.UNDELIVERED]
        s = after(s, warn[0], T0 + 121)
        failed = M.due(s, T0 + 181, RULES, OFFLINE)
        assert [x.event for x in failed] == [S.DELIVERY_FAILED]
        assert failed[0].notices[0].template == "sos.failed" and failed[0].notices[0].sms
        s = after(s, failed[0], T0 + 181)
        assert s.state == S.FAILED and s.failed_offline
        retry = M.due(s, T0 + 200, RULES, ONLINE)
        assert [x.event for x in retry] == [S.RETRY]
        assert after(s, retry[0], T0 + 200).state == S.APPROVED

    def test_repage_schedule_then_hourly_with_a_cap(self):
        s = snap(S.DISPATCHED, help_since=T0)
        assert [M.repage_at(s, n, RULES) - T0 for n in (1, 2, 3, 4, 5)] == \
            [120, 300, 600, 4200, 7800]
        pages, t = [], T0
        while t < T0 + 12 * 3600 + 700:
            for st in M.due(s, t, RULES, ONLINE):
                pages.append(st)
                s = after(s, st, t)
            t += 60
        repages = [p for p in pages if p.event == S.REPAGE]
        assert [p.detail["step"] for p in repages] == [f"repage{n}" for n in range(1, 11)]
        unack = [p for p in pages if p.event == S.NOTIFICATION]
        assert len(unack) == 1 and unack[0].flags == {"unacknowledged": True}
        assert unack[0].notices[0].audience == "owner"

    def test_test_mode_caps_repages_at_three(self):
        s = snap(S.DISPATCHED, help_since=T0)
        count = 0
        for t in range(0, 5 * 3600, 60):
            for st in M.due(s, T0 + t, TEST_RULES, ONLINE):
                count += st.event == S.REPAGE
                s = after(s, st, T0 + t)
        assert count == 3 and s.flags.get("unacknowledged")

    def test_acknowledged_is_not_repaged_and_stale_closes_after_a_day(self):
        s = snap(S.ACKNOWLEDGED, help_since=T0)
        assert M.due(s, T0 + 3000, RULES, ONLINE) == ()
        stale = M.due(s, T0 + 86_401, RULES, ONLINE)
        assert [x.event for x in stale] == [S.STALE_CLOSE] and stale[0].flags == {"stale": True}

    def test_cloud_facts(self):
        ack = M.cloud_event(snap(S.DISPATCHED), "acknowledge", actor=Actor("operator", id="d"),
                            now=T0)
        assert ack.ok and ack.steps[0].notices[0].template == "sos.acknowledged"
        delivered = M.cloud_event(snap(S.APPROVED), "delivered", actor=Actor.cloud(), now=T0)
        assert delivered.steps[0].package == "sent"
        resolved = M.cloud_event(snap(S.ACKNOWLEDGED), "resolve", actor=Actor("operator", id="d"),
                                 now=T0, detail={"outcome": "false_alarm_confirmed"})
        step = resolved.steps[0]
        assert step.verdict == "FALSE" and step.fields == {"outcome": "false_alarm_confirmed"}
        assert M.cloud_event(snap(S.ACKNOWLEDGED), "resolve", actor=Actor.cloud(), now=T0,
                             detail={"outcome": "?"}).code == "invalid_args"
        assert M.cloud_event(snap(S.PENDING), "resolve", actor=Actor.cloud(), now=T0,
                             detail={"outcome": "help_arrived"}).code == "conflict"
        assert M.cloud_event(snap(), "seen", actor=OWNER, now=T0).code == "forbidden_role"
        assert M.cloud_event(snap(S.DISPATCHED), "acknowledge", actor=CONTACT, now=T0).ok
        assert M.cloud_event(snap(S.DISPATCHED), "acknowledge", actor=STRANGER,
                             now=T0).code == "forbidden_role"
        huge = M.cloud_event(snap(), "notification", actor=Actor.cloud(), now=T0,
                             detail={"x": "y" * 5000})
        assert huge.steps[0].detail["truncated"] is True

    def test_reconnect_merge(self):
        step = M.reconcile(snap(S.PENDING), S.EXPIRED)
        assert step.forced_state == S.EXPIRED and step.package == "expired"
        assert M.reconcile(snap(S.EXPIRED), S.APPROVED).forced_state == S.APPROVED
        assert M.reconcile(snap(S.APPROVED), S.EXPIRED) is None   # help path wins
        assert M.reconcile(snap(S.PENDING), S.PENDING) is None
        assert M.reconcile(snap(S.PENDING), "BOGUS") is None


# ── the frozen policy ─────────────────────────────────────────────────────────
PROFILE = {"schema": 1, "version": 7,
           "owner": {"name": "Riya Sharma (demo)", "phone": "+919812345610"},
           "contacts": [{"id": "c2", "name": "B", "phone": "+919812345612",
                         "consent_status": "CONFIRMED", "priority": 2},
                        {"id": "c1", "name": "A", "phone": "+919812345611",
                         "consent_status": "CONFIRMED", "priority": 1, "telegram": True},
                        {"id": "c3", "name": "C", "consent_status": "PENDING"},
                        {"id": "c4", "name": "D", "consent_status": "REVOKED"}],
           "desk": {"name": "SENTRIX Desk (simulated)", "phone": "+919800000001"},
           "sos": {"window_seconds": 2700, "safety_defaults": True,
                   "contacts_for_single_source_l3": True, "pin_hash": "$argon2id$secret",
                   "duress_pin_hash": "$argon2id$other", "pin_version": 3,
                   "channels": {"telegram": True}},
           "consents": {"sos_snapshot": True, "medical_notes": False}}


class TestPolicy:
    def test_freeze_copies_and_never_carries_pins(self):
        policy = freeze(PROFILE, settings, test_mode=False)
        assert policy["window_seconds"] == 2700 and policy["profile_version"] == 7
        assert [c["id"] for c in policy["contacts"]] == ["c1", "c2"], \
            "only CONFIRMED contacts, by priority"
        assert "pin_hash" not in str(policy) and "$argon2" not in str(policy)
        assert policy["has_duress_pin"] and policy["pin_version"] == 3
        # The gate (SENTRIX_SOS_CONTACTS_SINGLE_SOURCE_L3) is off: owner only.
        assert policy["contacts_for_single_source_l3"] is False

    def test_policy_is_frozen_when_the_request_opens(self):
        profile = {**PROFILE, "contacts": list(PROFILE["contacts"]),
                   "sos": dict(PROFILE["sos"])}
        policy = freeze(profile, settings, test_mode=False)
        frozen = repr(policy)
        profile["sos"]["window_seconds"] = 1800
        profile["sos"]["safety_defaults"] = False
        profile["contacts"].clear()
        assert repr(policy) == frozen
        s = snap(incident_type="fire", policy=policy)
        events, _ = run_timeline(s, T0 + 200)
        assert [st.event for _, st in events].count(S.CONTACTS_NOTIFIED) == 1

    def test_window_choices_and_demo_window(self):
        assert [clamp_window(v) for v in (0, 1800, 2000, 2400, 2700, 3000, 9999, "x")] == \
            [1800, 1800, 1800, 2700, 2700, 2700, 3600, 1800]
        demo = replace(settings.sos, demo_window_seconds=20)
        assert window_seconds(PROFILE, demo, test_mode=True) == 60
        assert window_seconds(PROFILE, demo, test_mode=False) == 2700
        assert window_seconds({}, settings.sos, test_mode=False) == settings.sos.window_seconds

    def test_confirmed_contacts_skip_malformed_rows(self):
        assert confirmed_contacts({"contacts": [None, "x", {"consent_status": "CONFIRMED"},
                                                {"id": "c", "consent_status": "CONFIRMED",
                                                 "priority": "high"}]}) == [
            {"id": "c", "name": "", "relation": "", "phone": None, "telegram": False,
             "priority": 99, "can_reach_30min": False}]


class TestSnapshot:
    def test_chain_facts_are_read_from_events(self):
        request = {"id": "r1", "state": "PENDING", "version": 4, "created_at":
                   "2026-10-14T09:00:00+00:00", "expires_at": "2026-10-14T09:30:00+00:00",
                   "flags": {"repeat": True}, "policy": POLICY, "incident_ids": ["i1"]}
        events = [
            {"event": "created", "at": "2026-10-14T09:00:00+00:00", "detail": {}, "seq": 1},
            {"event": "prompted", "at": "2026-10-14T09:00:01+00:00", "detail": {}, "seq": 2},
            {"event": "reminder", "at": "2026-10-14T09:02:00+00:00",
             "detail": {"step": "r120", "offset": 120}, "seq": 3},
            {"event": "checking", "at": "2026-10-14T09:03:00+00:00", "detail": {}, "seq": 4},
            {"event": "notification", "at": "2026-10-14T09:03:01+00:00", "seq": 5,
             "detail": {"status": "SENT", "recipient_ref": "owner"}}]
        s = M.Snapshot.from_request(request, events)
        assert s.prompted_at == M.epoch("2026-10-14T09:00:01+00:00")
        assert s.reminder_upto == 120 and "r120" in s.done
        assert s.last_checking_at == M.epoch("2026-10-14T09:03:00+00:00")
        assert s.owner_reached and s.flags == {"repeat": True}
        assert M.epoch(None) is None and M.epoch("nonsense") is None


# ── adversarial review regressions ────────────────────────────────────────────
class TestReviewRegressions:
    def test_a_failed_request_can_close(self):
        assert S.next_state(S.FAILED, S.RESOLVE) == S.CLOSED
        assert S.next_state(S.FAILED, S.STALE_CLOSE) == S.CLOSED
        failed = snap(S.FAILED)
        steps = M.due(failed, failed.help_since + RULES.stale_close + 1, RULES, OFFLINE)
        assert [s.event for s in steps] == [S.STALE_CLOSE]

    def test_a_failed_request_marked_safe_closes_on_a_standalone_device(self):
        failed = snap(S.FAILED, flags={"resident_safe": True})
        standalone = Reach(online=False, standalone=True, remote_prompt=False)
        early = M.due(failed, failed.help_since + 60, RULES, standalone)
        assert S.STALE_CLOSE not in [s.event for s in early]
        late = M.due(failed, failed.help_since + RULES.repeat_window + 1, RULES, standalone)
        assert [s.event for s in late] == [S.STALE_CLOSE]

    def test_an_alarm_attached_to_a_failed_request_reaches_the_owner(self):
        facts = M.TriggerFacts(incident_id="i2", incident_type="fire", level=3,
                               evidence_state="confirmed", at=T0 + 600)
        decision = M.on_trigger(facts, open_request=snap(S.FAILED), history=[], rules=RULES)
        assert decision.action == "attach"
        owner = [n for s in decision.steps for n in s.notices if n.audience == "owner"]
        assert owner and owner[0].sms

    def test_reconnect_merge_sets_the_verdicts(self):
        declined = M.reconcile(snap(S.PENDING), S.DECLINED)
        assert declined.verdict == "FALSE"
        closed = M.reconcile(snap(S.DISPATCHED), S.CLOSED, cloud_outcome="real_threat")
        assert closed.verdict == "TRUE" and closed.fields["outcome"] == "real_threat"
        duress = M.reconcile(snap(S.PENDING, flags={"duress": True}), S.DISPATCHED)
        assert duress.restricted and duress.verdict == "FALSE"

    def test_standalone_with_a_remote_prompt_sends_no_reminder_sms(self):
        remote = Reach(online=False, standalone=True, remote_prompt=True)
        pending = snap(S.PENDING)
        steps = M.due(pending, T0 + 125, RULES, remote)
        reminders = [n for s in steps if s.event == S.REMINDER for n in s.notices]
        assert reminders and not reminders[0].sms
        assert not M.expire_step(pending, remote).notices[0].sms
        assert M.expire_step(pending, OFFLINE).notices[0].sms

    def test_a_false_verdict_while_prompting_throttles_like_a_decline(self):
        cancelled = snap(S.CANCELLED, responded_at=T0 + 30)
        facts = M.TriggerFacts(incident_id="i2", incident_type="weapon", level=3,
                               evidence_state="confirmed", at=T0 + 120)
        assert M.on_trigger(facts, open_request=None, history=[cancelled],
                            rules=RULES).flags.get("repeat")
        reset = snap(S.CANCELLED)                    # a test reset: no answer recorded
        assert not M.on_trigger(facts, open_request=None, history=[reset],
                                rules=RULES).flags.get("repeat")

    def test_expiry_sends_an_owed_contacts_alert_first(self):
        fire = snap(S.PENDING, incident_type="fire")
        steps = M.expiry_steps(fire, RULES, OFFLINE, "expired while the device was off")
        assert [s.event for s in steps] == [S.CONTACTS_NOTIFIED, S.EXPIRE]
        assert steps[1].detail["reason"] == "expired while the device was off"

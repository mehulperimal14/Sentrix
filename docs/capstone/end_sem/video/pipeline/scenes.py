"""One builder per scene kind: (T, dur, opts) -> (body_html, overlays).

T is the list of (start, end) of the scene's sentences, in seconds from the scene's first frame. Elements
animate in when their sentence starts, so the picture follows the narration. overlays are app clips that
build.py composites into the transparent-looking slots (rect = x, y, w, h on the 1920x1080 frame).
"""
import re
from html import escape as esc

from look import FIG, anim, captions, heading, topbar, uri

VID = "C:/Users/PC/AppData/Local/Temp/claude/324a87da11b408e0/03a07f5d-2599-44c3-b7d9-bc380698ab1f/scratchpad/video"
SC = FIG / "screens_c"
REN = FIG / "renders"
LOGO = FIG / "tiet_logo.png"

# where each recorded clip's page sits inside its 1920x1080 frame (measured)
CROPS = {"phone": (710, 0, 498, 1080), "desk": (96, 0, 1728, 1080), "diagnostics": (96, 0, 1728, 1080),
         "live": (96, 0, 1728, 1080), "wall": (22, 22, 1874, 1036)}


def chunks(text, limit=88):
    """Split a sentence at punctuation (then at spaces) into caption chunks of at most `limit` characters."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for piece in re.split(r"(?<=[,:;])\s+", text):
        if cur and len(cur) + 1 + len(piece) > limit:
            parts.append(cur)
            cur = piece
        else:
            cur = f"{cur} {piece}".strip()
    parts.append(cur)
    out = []
    for p in parts:
        while len(p) > limit:
            cut = p.rfind(" ", 0, limit)
            out.append(p[:cut])
            p = p[cut + 1:]
        out.append(p)
    return out


def caption_times(lines, T):
    """[(start, end, text)] for every caption chunk: a sentence's time shared by the chunks' lengths."""
    out = []
    for (a, b), (text, _) in zip(T, lines):
        cs = chunks(text)
        total = sum(len(c) for c in cs)
        t = a
        for k, c in enumerate(cs):
            e = b if k == len(cs) - 1 else t + (b - a) * len(c) / total
            out.append((t, e, c))
            t = e
    return out


def caps(lines, T):
    ct = caption_times(lines, T)
    return captions([(a - 0.05, (ct[k + 1][0] - 0.02) if k + 1 < len(ct) and ct[k + 1][0] - b < 0.5 else b + 0.35, text)
                     for k, (a, b, text) in enumerate(ct)])


# ── title card (per student) ──────────────────────────────────────────────────────────────
def title(students, dur):
    rows = "".join(f"<div style='display:flex;justify-content:space-between;gap:30px;padding:9px 0;border-bottom:1px solid rgba(255,255,255,.07)'>"
                   f"<span class='disp' style='font-size:34px;font-weight:700'>{esc(n)}</span>"
                   f"<span style='font-size:26px;color:var(--slate)'>{esc(r)}</span></div>" for n, r in students)
    return f"""
<div style="position:absolute;left:120px;top:100px;right:120px">
 <div {anim('fade', 0.0)}><span class="chip"><span class="dot"></span>Capstone Project CPG-299 · End-semester evaluation</span></div>
 <div class="disp a blurIn" style="--d:.1s;font-size:176px;font-weight:760;font-stretch:125%;letter-spacing:.07em;line-height:1;margin-top:32px;
   background:linear-gradient(180deg,#ffffff 30%,#9fe8c8);-webkit-background-clip:text;color:transparent">SENTRIX</div>
 <div class="disp a rise" style="--d:.35s;font-size:42px;font-weight:600;line-height:1.2;max-width:1150px;margin-top:22px;color:#cfe9dd">
   Intelligent Multimodal Edge Threat Intelligence and Physical Security System</div>
 <div class="a grow" style="--d:.55s;height:2px;width:900px;margin:36px 0 30px;background:linear-gradient(90deg,var(--neem),transparent)"></div>
 <div class="a rise" style="--d:.7s;display:grid;grid-template-columns:760px 1fr;gap:110px;align-items:start">
  <div><div class="muted" style="font-size:20px;letter-spacing:.2em;text-transform:uppercase;font-weight:600;margin-bottom:8px">Presented by</div>{rows}</div>
  <div><div class="muted" style="font-size:20px;letter-spacing:.2em;text-transform:uppercase;font-weight:600">University</div>
   <div style="font-size:34px;font-weight:650;margin-top:14px;line-height:1.3">Thapar Institute of Engineering<br>and Technology, Patiala</div>
   <div style="font-size:26px;color:var(--slate);margin-top:12px;line-height:1.45">Computer Science and Engineering Department<br>Mentor: Dr. Ashutosh Mishra</div></div>
 </div>
</div>
<div class="a pop" style="--d:.3s;position:absolute;right:120px;top:100px;width:230px;padding:20px;background:#fff;border-radius:26px;
  box-shadow:0 30px 70px rgba(0,0,0,.5)"><img src="{uri(LOGO)}" style="width:100%;display:block"></div>
<div class='blackout' style='animation:fade .5s ease {dur - 0.5:.2f}s both'></div>""", []


# ── opening ───────────────────────────────────────────────────────────────────────────────
def problem(T, dur, lines, o):
    a, b = T[0][0], T[1][0]
    cloud = ("<svg width='120' height='84' viewBox='0 0 120 84' fill='none' stroke='#a0aaff' stroke-width='5' stroke-linecap='round'>"
             "<path d='M30 74h62a22 22 0 0 0 2-44 30 30 0 0 0-57-6A24 24 0 0 0 30 74z'/><path d='M60 64V38m0 0-11 11m11-11 11 11'/></svg>")
    body = f"""{topbar(0)}{heading('Why SENTRIX', 'Most alarms are false. Most cameras send video away.')}
<div style="position:absolute;left:96px;right:96px;top:330px;display:grid;grid-template-columns:1fr 1fr;gap:40px">
 <div class="a rise card" style="--d:{a + 0.2:.2f}s;padding:52px 56px;height:470px">
  <div class="disp" style="font-size:150px;font-weight:760;line-height:1;color:var(--chilli)">94–98%</div>
  <div style="font-size:38px;font-weight:650;margin-top:26px;line-height:1.25">of burglar-alarm calls to police are false</div>
  <div style="font-size:21px;color:var(--slate);margin-top:22px">R. Sampson, <i>False Burglar Alarms</i>, U.S. Department of Justice (COPS), 2nd ed.</div>
 </div>
 <div class="a rise card" style="--d:{b + 0.1:.2f}s;padding:52px 56px;height:470px">
  {cloud}
  <div class="disp" style="font-size:64px;font-weight:700;line-height:1.05;margin-top:28px">Cloud cameras</div>
  <div style="font-size:38px;font-weight:650;margin-top:22px;line-height:1.25">send private video out of the home,<br>and stop when the internet does</div>
 </div>
</div>{caps(lines, T)}"""
    return body, []


def hero(T, dur, lines, o):
    t0, t1, t2 = (x[0] for x in T)
    body = f"""{topbar(0)}
<div style="position:absolute;left:96px;top:230px;width:760px">
 <div {anim('riseS', t0)}><span class="kick">Our answer</span></div>
 <div class="disp a blurIn" style="--d:{t0 + 0.1:.2f}s;font-size:120px;font-weight:760;font-stretch:125%;letter-spacing:.05em;margin-top:18px;line-height:1">SENTRIX</div>
 <div class="a rise" style="--d:{t1:.2f}s;margin-top:44px;display:flex;gap:18px;align-items:flex-start">
  <div class="dot" style="margin-top:18px;flex:none"></div>
  <div style="font-size:36px;font-weight:600;line-height:1.3">One small unit watches a room and decides <span class="neem">on the device</span>: handgun or visible flame</div></div>
 <div class="a rise" style="--d:{t2:.2f}s;margin-top:30px;display:flex;gap:18px;align-items:flex-start">
  <div class="dot" style="margin-top:18px;flex:none;background:var(--haldi);box-shadow:0 0 14px var(--haldi)"></div>
  <div style="font-size:36px;font-weight:600;line-height:1.3">It <span class="haldi">asks the owner first</span>. Video, faces and evidence stay on the device</div></div>
</div>
<div style="position:absolute;left:900px;top:170px;width:960px;height:740px;animation:drift {dur:.1f}s linear 0s both">
 <div class="a blurIn" style="--d:{t0 + 0.2:.2f}s;position:absolute;left:0;top:20px;width:900px">
  <div class="win" style="position:relative"><div class="bar"><i></i><i></i><i></i><span>Device screen · alert takeover</span></div>
   <img src="{uri(SC / 'dev_wall_alert.png')}" style="display:block;width:900px"></div></div>
 <div class="a rise" style="--d:{t2 + 0.15:.2f}s;position:absolute;left:660px;top:180px;width:260px">
  <div class="phone" style="position:relative;width:260px;border-radius:44px"><div class="slot" style="border-radius:32px"><img src="{uri(SC / 'portal_alert_phone.png')}" style="display:block;width:232px"></div></div></div>
</div>{caps(lines, T)}"""
    return body, []


def team(T, dur, lines, o):
    from story import ROLES
    cards = []
    for k, (name, roll, role, desc, _) in enumerate(ROLES):
        a = T[k][0] - 0.15
        b = (T[k + 1][0] - 0.25) if k + 1 < len(T) else dur + 5
        ini = "".join(p[0] for p in name.split()[:2])
        cards.append(f"""
 <div class="a rise" style="--d:{0.35 + 0.09 * k:.2f}s">
  <div class="card" style="height:520px;padding:34px 28px;border-radius:24px;animation:act .5s var(--ease) {a:.2f}s both, deact .5s var(--ease) {b:.2f}s forwards">
   <div style="width:92px;height:92px;border-radius:50%;border:3px solid var(--neem);display:grid;place-items:center;
     font-family:Anek;font-stretch:120%;font-weight:700;font-size:36px;color:var(--neem)">{ini}</div>
   <div class="disp" style="font-size:36px;font-weight:700;margin-top:26px;line-height:1.08">{esc(name)}</div>
   <div style="font-size:20px;color:var(--slate);margin-top:6px">{roll}</div>
   <div style="font-size:24px;font-weight:700;color:var(--neem);margin-top:24px;line-height:1.25">{esc(role)}</div>
   <div style="font-size:21px;color:#c6d3ce;margin-top:14px;line-height:1.42">{esc(desc)}</div>
  </div></div>""")
    voice = o.get("voice_chip", "")
    chip = (f"<div class='a fade' style='--d:.4s;position:absolute;right:96px;top:150px'><span class='chip'>"
            f"<span class='dot' style='background:var(--haldi);box-shadow:0 0 14px var(--haldi)'></span>{esc(voice)}</span></div>") if voice else ""
    body = f"""{topbar(0)}{heading('The team', 'Five of us built SENTRIX')}{chip}
<div style="position:absolute;left:96px;right:96px;top:330px;display:grid;grid-template-columns:repeat(5,1fr);gap:24px">{''.join(cards)}</div>
<div class="a fade" style="--d:.8s;position:absolute;left:96px;bottom:60px;font-size:22px;color:var(--slate)">
 Mentor: <b style="color:var(--ink)">Dr. Ashutosh Mishra</b> · Each member led one subsystem and shared the product-wide work</div>"""
    return body, []


def agenda(T, dur, lines, o):
    a, b = T[0]
    items = [("Components", "Physical and software modules"), ("Connections", "README and wiring of the hardware"),
             ("Inputs", "What SENTRIX needs to run"), ("Start · stop", "Starting, stopping, troubleshooting"),
             ("Execution", "A complete run, step by step"), ("Results", "Measured results and limits")]
    span = (b - a) * 0.92
    tiles = "".join(
        f"""<div class="a rise card" style="--d:{a + 0.15 + k * span / 6:.2f}s;padding:36px 40px;height:250px">
  <div class="disp" style="font-size:72px;font-weight:760;color:var(--neem);line-height:1">{k + 1}</div>
  <div class="disp" style="font-size:40px;font-weight:700;margin-top:16px">{esc(t)}</div>
  <div style="font-size:23px;color:var(--slate);margin-top:8px">{esc(s)}</div></div>"""
        for k, (t, s) in enumerate(items))
    body = f"""{topbar(0)}{heading('This video', 'The six parts the guideline asks for')}
<div style="position:absolute;left:96px;right:96px;top:320px;display:grid;grid-template-columns:repeat(3,1fr);gap:30px">{tiles}</div>
{caps(lines, T)}"""
    return body, []


# ── 1 components ──────────────────────────────────────────────────────────────────────────
def chips(items, d, step=0.12, color="var(--slate)"):
    return "".join(f"<span class='a pop chip' style='--d:{d + k * step:.2f}s;color:{color};font-size:21px'>{esc(x)}</span>"
                   for k, x in enumerate(items))


def programs(T, dur, lines, o):
    t0, t1, t2 = (x[0] for x in T)
    dev = chips(["Camera", "Every model", "Evidence rules", "Device screens", "SOS engine", "Encrypted evidence"], t1 + 0.4)
    por = chips(["Accounts", "Contacts", "SENTRIX Desk", "Owner app", "Admin"], t2 + 0.4)
    body = f"""{topbar(1)}{heading('1 · Components', 'Two programs: one at home, one in the cloud')}
<div class="a rise card" style="--d:{t1:.2f}s;position:absolute;left:96px;top:340px;width:760px;height:420px;padding:44px">
 <div class="kick">At home</div>
 <div class="disp" style="font-size:54px;font-weight:700;margin-top:10px">Device</div>
 <div style="font-size:26px;color:var(--slate);margin-top:6px">Raspberry Pi 5 · python -m sentrix</div>
 <div style="display:flex;flex-wrap:wrap;gap:12px;margin-top:34px">{dev}</div>
 <div class="a fade" style="--d:{t1 + 1.4:.2f}s;margin-top:34px;font-size:22px;color:var(--neem)">Video, faces and evidence stay here</div>
</div>
<div class="a rise card" style="--d:{t2:.2f}s;position:absolute;right:96px;top:340px;width:640px;height:420px;padding:44px">
 <div class="kick" style="color:var(--indigo)">In the cloud</div>
 <div class="disp" style="font-size:54px;font-weight:700;margin-top:10px">Portal</div>
 <div style="font-size:26px;color:var(--slate);margin-top:6px">Render · python -m sentrix_portal</div>
 <div style="display:flex;flex-wrap:wrap;gap:12px;margin-top:34px">{por}</div>
</div>
<svg style="position:absolute;left:856px;top:490px" width="328" height="160" viewBox="0 0 328 160">
 <defs><marker id="ah" markerWidth="12" markerHeight="12" refX="9" refY="6" orient="auto"><path d="M0 0L12 6L0 12z" fill="#60e2aa"/></marker></defs>
 <path class="a draw" style="--d:{t2 + 1.6:.2f}s;stroke-dasharray:1" pathLength="1" d="M20 70 H300" stroke="#60e2aa" stroke-width="4" fill="none" marker-end="url(#ah)"/>
</svg>
<div class="a fade" style="--d:{t2 + 2.2:.2f}s;position:absolute;left:870px;top:440px;width:300px;text-align:center;font-size:21px;font-weight:650;color:var(--neem)">outbound link<br><span style="color:var(--slate);font-weight:500">one WebSocket, opened by the device</span></div>
{caps(lines, T)}"""
    return body, []


def modules(T, dur, lines, o):
    t0, t1, t2 = (x[0] for x in T)
    stages = [
        ("Perception", "neem", ["YOLO person + ByteTrack", "YOLO11n weapon and fire heads", "DINOv2 scene classifier", "InsightFace faces", "OSNet-AIN bodies", "PANNs sound tagger"], t0 + 0.3),
        ("Evidence rules", "indigo", ["2 of 3 fresh checks", "Person in view for a weapon", "Two modalities for Level 4+", "Audio aligned in time"], t1),
        ("TCI and levels", "indigo", ["Threat Confidence Index 0–1", "Level policy L1–L5", "Incident, peak snapshot"], t1 + 1.6),
        ("SOS engine", "haldi", ["Confirmed Level 3 opens a request", "Owner asked first", "Contacts and the Desk"], t1 + 3.2),
        ("Channels", "chilli", ["One send guard", "Test mode on by default", "Telegram and e-mail"], t2),
    ]
    cols = []
    for k, (name, col, items, d) in enumerate(stages):
        li = "".join(f"<div style='font-size:21px;line-height:1.35;color:#c9d6d1;padding:9px 0;border-top:1px solid rgba(255,255,255,.06)'>{esc(x)}</div>" for x in items)
        cols.append(f"""<div class="a rise card" style="--d:{d:.2f}s;padding:30px 26px;height:500px">
  <div class="disp" style="font-size:22px;font-weight:700;color:var(--{col});letter-spacing:.12em">0{k + 1}</div>
  <div class="disp" style="font-size:34px;font-weight:700;margin:8px 0 18px">{esc(name)}</div>{li}</div>""")
    arrows = "".join(f"<div class='a fade' style='--d:{stages[k + 1][3]:.2f}s;position:absolute;top:555px;left:{96 + (k + 1) * 352 - 26}px;"
                     f"width:22px;height:22px;border-top:4px solid var(--neem);border-right:4px solid var(--neem);transform:rotate(45deg)'></div>"
                     for k in range(4))
    body = f"""{topbar(1)}{heading('Software modules', 'From camera frames to one decision, on the device')}
<div style="position:absolute;left:96px;right:96px;top:310px;display:grid;grid-template-columns:repeat(5,1fr);gap:30px">{''.join(cols)}</div>{arrows}
{caps(lines, T)}"""
    return body, []


# ── 2 connections ─────────────────────────────────────────────────────────────────────────
def physical(T, dur, lines, o):
    t0, t1 = T[0][0], T[1][0]
    li = lambda items, d: "".join(
        f"<div class='a rise' style='--d:{d + k * 0.35:.2f}s;display:flex;gap:16px;align-items:baseline;font-size:29px;line-height:1.3;padding:11px 0'>"
        f"<span class='dot' style='flex:none;transform:translateY(-4px)'></span><span>{x}</span></div>" for k, x in enumerate(items))
    demo = li(["<b>Raspberry Pi 5</b>, 8 GB, Active Cooler", "<b>Official 27 W USB-C</b> power supply"], t0 + 0.3)
    full = li(["<b>Camera Module 3</b> on a tilting mount", "<b>USB microphone</b>", "Three <b>status LEDs</b>: green, amber, red",
               "<b>3D-printed case</b> (rev F)"], t1 + 0.2)
    body = f"""{topbar(2)}{heading('2 · Hardware and connections', 'The physical unit')}
<div class="studio a blurIn" style="--d:.3s;position:absolute;left:96px;top:300px;width:860px;height:590px">
 <img src="{uri(REN / '1_assembled.png')}" style="width:860px;margin-top:-10px;animation:drift {dur:.1f}s linear 0s both"></div>
<div style="position:absolute;left:1020px;top:300px;width:820px">
 <div class="kick">The computer</div><div style="margin-top:8px">{demo}</div>
 <div class="kick a fade" style="--d:{t1:.2f}s;margin-top:34px;color:var(--indigo)">Camera, sound, status, case</div><div style="margin-top:8px">{full}</div>
</div>{caps(lines, T)}"""
    return body, []


def wiring(T, dur, lines, o):
    s = [x[0] for x in T]

    def box(x, y, w, h, title, sub, d, col="#60e2aa"):
        return (f"<g class='a fade' style='--d:{d:.2f}s'><rect x='{x}' y='{y}' width='{w}' height='{h}' rx='16' fill='rgba(255,255,255,.05)' "
                f"stroke='{col}' stroke-opacity='.55' stroke-width='2'/><text x='{x + 22}' y='{y + 40}' fill='#f0f5f3' font-size='26' "
                f"font-weight='700' font-family='Noto'>{esc(title)}</text><text x='{x + 22}' y='{y + 72}' fill='#93a49e' font-size='20' "
                f"font-family='Noto'>{esc(sub)}</text></g>")

    def wire(d_path, d, col="#60e2aa", w=4):
        return (f"<path class='a draw' style='--d:{d:.2f}s;stroke-dasharray:1' pathLength='1' d='{d_path}' fill='none' "
                f"stroke='{col}' stroke-width='{w}' stroke-linecap='round'/>")

    svg = f"""<svg width="1728" height="660" viewBox="0 0 1728 660" style="position:absolute;left:96px;top:280px;overflow:visible">
 <g class='a pop' style='--d:.3s'><rect x="664" y="150" width="400" height="290" rx="24" fill="#12201b" stroke="#60e2aa" stroke-width="3"/>
  <text x="864" y="250" text-anchor="middle" fill="#f0f5f3" font-size="40" font-weight="800" font-family="Anek">Raspberry Pi 5</text>
  <text x="864" y="292" text-anchor="middle" fill="#93a49e" font-size="22" font-family="Noto">8 GB · Active Cooler · 128 GB SD</text>
  <text x="864" y="330" text-anchor="middle" fill="#93a49e" font-size="22" font-family="Noto">40-pin GPIO header</text></g>
 {box(0, 20, 470, 100, 'Camera Module 3', 'CAM port · 22-pin ribbon cable', s[0] + 0.2)}
 {wire('M470 70 C560 70 580 190 664 200', s[0] + 0.6)}
 {box(0, 160, 470, 100, 'USB microphone', 'any USB port, even while running', s[0] + 1.4)}
 {wire('M470 210 C560 210 590 250 664 255', s[0] + 1.8)}
 {box(1258, 20, 470, 100, 'LEDs green · amber · red', 'GPIO 5 / 12 / 13 · pins 29 / 32 / 33 · 330 Ω', s[1] + 0.1, '#f7bd56')}
 {wire('M1064 200 C1150 190 1170 70 1258 70', s[1] + 0.5, '#f7bd56')}
 {box(1258, 300, 470, 100, 'USB-C power supply', 'official Raspberry Pi 27 W · 5.1 V, 5 A', s[2] + 0.1, '#ff7078')}
 {wire('M1064 330 C1150 330 1170 350 1258 350', s[2] + 0.5, '#ff7078', 5)}
</svg>
<div class="a rise" style="--d:{s[3]:.2f}s;position:absolute;left:96px;bottom:150px;width:470px" >
 <div class="card" style="padding:22px 26px;font-size:21px;line-height:1.45"><b class="neem">Every pin, in the repository</b><br>
 <span class="mono" style="font-size:19px;color:#c9d6d1">README.md<br>docs/8_RASPBERRY_PI5_INTEGRATION.md §6.1</span></div></div>
{topbar(2)}{heading('Connections', 'How the unit is wired')}{caps(lines, T)}"""
    return svg, []


# ── 3 inputs ──────────────────────────────────────────────────────────────────────────────
def inputs(T, dur, lines, o):
    t0, t1, t2 = (x[0] for x in T)
    rows = [("Camera picture", "Required", "neem", t0 + 0.2), ("Sound from the USB microphone", "Optional", "slate", t0 + 1.6),
            ("Owner account from a one-time code", "Setup", "indigo", t1 + 0.2),
            ("Address, household, contacts (consent), PIN + duress PIN", "Setup", "indigo", t1 + 2.6),
            ("Residents' faces from the device camera", "Setup", "indigo", t2 + 0.2)]
    li = "".join(f"""<div class="a rise" style="--d:{d:.2f}s;display:flex;justify-content:space-between;align-items:center;gap:20px;
  padding:20px 0;border-bottom:1px solid rgba(255,255,255,.07)"><span style="font-size:28px;font-weight:600;line-height:1.25">{esc(x)}</span>
  <span class="chip" style="flex:none;color:var(--{c})">{tag}</span></div>""" for x, tag, c, d in rows)
    body = f"""{topbar(3)}{heading('3 · Inputs', 'What SENTRIX needs')}
<div style="position:absolute;left:96px;top:300px;width:980px">{li}</div>
<div class="a blurIn" style="--d:{t1 + 0.6:.2f}s;position:absolute;right:96px;top:300px;width:600px">
 <div class="win" style="position:relative"><div class="bar"><i></i><i></i><i></i><span>Portal · contacts</span></div>
  <img src="{uri(SC / 'portal_contacts.png')}" style="display:block;width:600px"></div></div>
<div class="a blurIn" style="--d:{t2 + 0.3:.2f}s;position:absolute;right:96px;top:648px;width:600px">
 <div class="win" style="position:relative"><div class="bar"><i></i><i></i><i></i><span>Device admin · faces</span></div>
  <img src="{uri(SC / 'dev_faces.png')}" style="display:block;width:600px"></div></div>
{caps(lines, T)}"""
    return body, []


# ── 4 start / stop / troubleshoot ─────────────────────────────────────────────────────────
def term(title, rows, x, y, w, h):
    """rows: [(delay, html)] typed in line by line."""
    body = "".join(f"<div class='a wipe' style='--d:{d:.2f}s;animation-duration:.55s;white-space:pre'>{t}</div>" for d, t in rows)
    return (f"<div class='win' style='left:{x}px;top:{y}px;width:{w}px;height:{h}px'><div class='bar'><i></i><i></i><i></i>"
            f"<span>{esc(title)}</span></div><div class='mono' style='padding:24px 28px;font-size:25px;line-height:1.62;color:#d6e2dd'>{body}</div></div>")


P = "<span class='neem'>&gt;</span> "
D = "<span class='neem'>$</span> "
C = lambda s: f"<span style='color:#62736d'>{s}</span>"
G = lambda s: f"<span class='neem'>{s}</span>"


def start(T, dur, lines, o):
    t0, t1 = T[0][0], T[1][0]
    pi = [(t0 + 0.2, C("# Raspberry Pi 5: starts at boot")), (t0 + 0.9, D + "systemctl --user start sentrix.service"),
          (t0 + 1.8, D + "systemctl --user status sentrix.service"), (t0 + 2.6, G("●") + " sentrix.service - SENTRIX device"),
          (t0 + 3.0, "   Active: " + G("active (running)")), (t0 + 3.4, "   Main PID: …/.venv/bin/python -m sentrix")]
    portal = [(t1 + 0.2, C("# The Portal: hosted on Render, always on")), (t1 + 1.0, P + "curl https://sentrix-portal.onrender.com/healthz"),
              (t1 + 2.0, '{"status":' + G('"ok"') + ',"db":' + G('"ok"') + ',"test_mode":true}'),
              (t1 + 3.0, C("# owners just open sentrix-portal.onrender.com"))]
    body = f"""{topbar(4)}{heading('4 · Start, stop and troubleshoot', 'Starting SENTRIX')}
{term('Raspberry Pi · SSH', pi, 96, 320, 860, 420)}{term('Portal · Render', portal, 1000, 320, 824, 420)}{caps(lines, T)}"""
    return body, []


def stop(T, dur, lines, o):
    t0, t1 = T[0][0], T[1][0]
    a = [(t0 + 0.2, D + "systemctl --user stop sentrix.service"), (t0 + 1.0, D + "systemctl --user restart sentrix.service"),
         (t0 + 1.8, C("# laptop: Ctrl+C in its terminal")), (t1 + 0.1, D + "journalctl --user-unit sentrix.service"),
         (t1 + 0.8, "00:03:10 level 1 -> 2 (tci=0.300)"), (t1 + 1.2, "00:03:10 incident 91316515 opened"),
         (t1 + 1.6, "00:03:16 level 2 -> 1 (tci=0.142)")]
    b = [(t1 + 2.6, P + "python tools/doctor.py --no-camera"), (t1 + 3.4, "[ " + G("OK") + " ] weapon detection   v5_room"),
         (t1 + 3.8, "[ " + G("OK") + " ] scene classifier   v4_custom"), (t1 + 4.2, "[ " + G("OK") + " ] test mode          on"),
         (t1 + 4.6, "[ " + G("OK") + " ] microphone         listed, not opened"),
         (t1 + 5.0, "[ " + G("OK") + " ] evidence chain     verified"), (t1 + 5.6, G("all critical checks passed"))]
    body = f"""{topbar(4)}{heading('Stop, logs and the pre-flight check', 'Stopping and checking SENTRIX')}
{term('Raspberry Pi · SSH', a, 96, 320, 860, 470)}{term('Pre-flight check · opens no camera', b, 1000, 320, 824, 470)}{caps(lines, T)}"""
    return body, []


def trouble(T, dur, lines, o):
    s = [x[0] for x in T]
    rows = [("No camera picture", "Reseat the camera ribbon at both ends, then restart the service", s[1]),
            ("No sound", "Plug the USB microphone into any port: it is picked up automatically", s[2]),
            ("Portal shows “offline”", "Check the Wi-Fi: the link reconnects by itself; detection goes on meanwhile", s[3]),
            ("False alarms in a new room", "tools/calibrate_room.py sets the room's thresholds from the device's own log", s[4])]
    tr = "".join(f"""<div class="a rise card" style="--d:{d:.2f}s;display:grid;grid-template-columns:520px 1fr;gap:30px;padding:38px 40px;margin-bottom:22px">
 <div style="font-size:32px;font-weight:700;color:var(--haldi)">{esc(p)}</div><div style="font-size:30px;line-height:1.35">{esc(f)}</div></div>"""
                 for p, f, d in rows)
    body = f"""{topbar(4)}{heading('Troubleshooting', 'Common problems, short fixes')}
<div style="position:absolute;left:96px;right:96px;top:300px">{tr}</div>{caps(lines, T)}"""
    return body, []


# ── 5 execution: app clips in frames ──────────────────────────────────────────────────────
STEPS = ["Handgun confirmed on the device", "Level 3: the owner is asked", "Owner asks for help", "SENTRIX Desk responds", "Request closed, outcome recorded"]


def stepper(active, x, y):
    if active is None:
        return ""
    out = []
    for k, s in enumerate(STEPS):
        on = k == active
        done = k < active
        col = "var(--neem)" if (on or done) else "var(--dim)"
        out.append(f"""<div style="display:flex;gap:18px;align-items:center;padding:14px 0">
  <div style="width:38px;height:38px;border-radius:50%;flex:none;display:grid;place-items:center;font-family:Anek;font-weight:700;font-size:20px;
   border:2px solid {col};{'background:var(--neem);color:#04150e' if on else f'color:{col}'}">{k + 1}</div>
  <div style="font-size:26px;font-weight:{700 if on else 500};color:{'var(--ink)' if on else 'var(--slate)' if done else 'var(--dim)'}">{esc(s)}</div></div>""")
    return f"<div class='a fade' style='--d:.3s;position:absolute;left:{x}px;top:{y}px;width:560px'>{''.join(out)}</div>"


def _label(text, x, y):
    return f"<div class='a fade' style='--d:.25s;position:absolute;left:{x}px;top:{y}px'><span class='chip'><span class='dot'></span>{esc(text)}</span></div>"


def wallclip(T, dur, lines, o):
    w = 1120
    h = round(w * 1036 / 1874) + 40
    x, y = 96, 236
    rect = (x, y + 40, w, h - 40)
    body = f"""{topbar(5)}<div class="head"><div {anim('riseS', .1)}><span class="kick">5 · Execution</span></div></div>
<div class="a pop win" style="--d:.05s;left:{x}px;top:{y}px;width:{w}px;height:{h}px"><div class="bar"><i></i><i></i><i></i><span>{esc(o['label'])}</span></div></div>
{stepper(o['step'], x + w + 50, y + 40)}
<div class="a fade" style="--d:.4s;position:absolute;left:{x + w + 50}px;top:{y + 420}px;width:560px"><span class="chip" style="color:var(--haldi)">
 <span class="dot" style="background:var(--haldi);box-shadow:0 0 12px var(--haldi)"></span>Test mode · replica, staged by our team</span></div>
{caps(lines, T)}"""
    return body, [{"clip": o["clip"], "ss": o["ss"], "rect": rect, "radius": 0, "t_in": 0.45, "fit": "cover"}]


def phoneclip(T, dur, lines, o):
    ph_h = 690
    ph_w = round((ph_h - 28) * 498 / 1080) + 28
    x, y = 540, 226
    rect = (x + 14, y + 14, ph_w - 28, ph_h - 28)
    body = f"""{topbar(5)}<div class="head"><div {anim('riseS', .1)}><span class="kick">5 · Execution</span></div></div>
<div class="a pop phone" style="--d:.05s;left:{x}px;top:{y}px;width:{ph_w}px;height:{ph_h}px"><div class="slot" style="width:100%;height:100%"></div></div>
{_label(o['label'], x + ph_w + 90, 250)}{stepper(o['step'], x + ph_w + 90, 330)}{caps(lines, T)}"""
    return body, [{"clip": o["clip"], "ss": o["ss"], "rect": rect, "radius": 40, "t_in": 0.45, "fit": "fill"}]


def browserclip(T, dur, lines, o):
    w = 1040
    h = round(w / 1.6) + 40
    x, y = 96, 236
    rect = (x, y + 40, w, h - 40)
    side = (stepper(o["step"], x + w + 50, y + 40) if o.get("step") is not None else
            f"<div class='a rise' style='--d:.4s;position:absolute;left:{x + w + 50}px;top:{y + 60}px;width:600px;font-size:30px;line-height:1.4;font-weight:600'>"
            + ("Each engine's score against its threshold, the fused TCI and every input's contribution" if o["clip"] == "diagnostics"
               else "The same picture, from anywhere, relayed through the Portal on demand; nothing is stored in the cloud") + "</div>")
    body = f"""{topbar(5)}<div class="head"><div {anim('riseS', .1)}><span class="kick">5 · Execution</span></div></div>
<div class="a pop win" style="--d:.05s;left:{x}px;top:{y}px;width:{w}px;height:{h}px"><div class="bar"><i></i><i></i><i></i><span>{esc(o['label'])}</span></div></div>
{side}{caps(lines, T)}"""
    return body, [{"clip": o["clip"], "ss": o["ss"], "rect": rect, "radius": 0, "t_in": 0.45, "fit": "fill"}]


def pi(T, dur, lines, o):
    t0, t1 = T[0][0], T[1][0]
    specs = [("4 s clips, every 2 s", "Six keyframes per clip; a fast lane checks every fourth frame", t0 + 0.4),
             ("Detector heads v5", "Weapon and fire, retrained on our own recordings · 320 px · ONNX", t1 + 0.2),
             ("Room thresholds", "Scene model: weapon 0.78 · fire 0.75, set from the room's own log", t1 + 2.6)]
    cards = "".join(f"""<div class="a rise card" style="--d:{d:.2f}s;padding:30px 34px;margin-bottom:22px">
 <div class="disp" style="font-size:36px;font-weight:700">{esc(a)}</div><div style="font-size:24px;color:var(--slate);margin-top:8px;line-height:1.4">{esc(b)}</div></div>"""
                    for a, b, d in specs)
    body = f"""{topbar(5)}{heading('On the Raspberry Pi', 'The demo unit in our room')}
<div class="studio a blurIn" style="--d:.3s;position:absolute;left:96px;top:300px;width:820px;height:590px">
 <img src="{uri(REN / '9_wall_mounted.png')}" style="width:820px;margin-top:-50px;animation:drift {dur:.1f}s linear 0s both"></div>
<div style="position:absolute;left:980px;top:300px;width:844px">{cards}</div>{caps(lines, T)}"""
    return body, []


# ── 6 results ─────────────────────────────────────────────────────────────────────────────
def bars(rows, d, maxv, w=560, fmt="{:.2f}"):
    out = []
    for k, (label, v, col) in enumerate(rows):
        pct = max(0.012, v / maxv)
        out.append(f"""<div style="margin-top:20px"><div style="display:flex;justify-content:space-between;font-size:22px;color:var(--slate)">
  <span>{esc(label)}</span><span class="a fade disp" style="--d:{d + 0.5 + k * 0.45:.2f}s;font-size:30px;font-weight:700;color:{col}">{fmt.format(v)}</span></div>
  <div style="height:16px;border-radius:9px;background:rgba(255,255,255,.06);margin-top:8px;width:{w}px">
   <div class="a grow" style="--d:{d + k * 0.45:.2f}s;height:16px;border-radius:9px;width:{pct * 100:.1f}%;background:{col}"></div></div></div>""")
    return "".join(out)


def panel(title, sub, inner, d, x, w):
    return (f"<div class='a rise card' style='--d:{d:.2f}s;position:absolute;left:{x}px;top:310px;width:{w}px;height:500px;padding:40px'>"
            f"<div class='disp' style='font-size:34px;font-weight:700'>{esc(title)}</div><div style='font-size:21px;color:var(--slate);margin-top:6px'>{esc(sub)}</div>{inner}</div>")


def results1(T, dur, lines, o):
    t0, t1, t2 = (x[0] for x in T)
    A = bars([("Public-data models (v3)", 0.281, "#5d6c67"), ("Trained on our footage (v4)", 0.768, "var(--neem)")], t1 + 0.3, 1.0, 470)
    B = bars([("Public-data models (v3)", 13.96, "#5d6c67"), ("Our footage (v4)", 0.79, "var(--haldi)"), ("v4 + evidence rules", 0.0, "var(--neem)")],
             t1 + 3.2, 13.96, 470)
    big = (f"<div style='display:flex;align-items:baseline;gap:30px;margin-top:70px'><div class='a fade disp' style='--d:{t2 + 1.2:.2f}s;font-size:150px;"
           f"font-weight:760;color:#5d6c67;line-height:1'>9</div><div class='a fade' style='--d:{t2 + 1.8:.2f}s;font-size:60px;color:var(--slate)'>→</div>"
           f"<div class='a pop disp' style='--d:{t2 + 2.3:.2f}s;font-size:150px;font-weight:760;color:var(--neem);line-height:1'>0</div></div>"
           f"<div class='a fade' style='--d:{t2 + 2.6:.2f}s;font-size:24px;color:var(--slate);margin-top:26px;line-height:1.4'>Level 3+ alarm onsets in the first 18-minute live session, re-decided with the rules</div>")
    body = f"""{topbar(6)}{heading('6 · Final results', 'Our footage, then the evidence rules')}
{panel('Validation macro-F1', 'Held-out videos, per image', A, t1, 96, 550)}
{panel('False alarms / normal min', 'Replay of our 10 labelled videos', B, t1 + 3.0, 685, 550)}
{panel('First live session', 'Laptop webcam, dark bedroom', big, t2, 1274, 550)}{caps(lines, T)}"""
    return body, []


def results2(T, dur, lines, o):
    s = [x[0] for x in T]
    au = bars([("Weapon · v4", 0.748, "#5d6c67"), ("Weapon · v5", 0.871, "var(--neem)"), ("Fire · v4", 0.587, "#5d6c67"), ("Fire · v5", 0.964, "var(--haldi)")],
              s[0] + 0.4, 1.0, 330)
    ep = (f"<div style='margin-top:40px'><div class='a pop disp' style='--d:{s[1] + 0.3:.2f}s;font-size:96px;font-weight:760;color:var(--neem);line-height:1'>11/14</div>"
          f"<div style='font-size:22px;color:var(--slate);margin-top:8px'>weapon episodes</div>"
          f"<div class='a pop disp' style='--d:{s[1] + 1.4:.2f}s;font-size:96px;font-weight:760;color:var(--haldi);line-height:1;margin-top:34px'>3/3</div>"
          f"<div style='font-size:22px;color:var(--slate);margin-top:8px'>flame episodes</div></div>")
    cal = (f"<div style='display:flex;align-items:baseline;gap:22px;margin-top:40px'><div class='disp' style='font-size:96px;font-weight:760;color:#5d6c67;line-height:1'>49</div>"
           f"<div style='font-size:44px;color:var(--slate)'>→</div><div class='a pop disp' style='--d:{s[2] + 1.0:.2f}s;font-size:96px;font-weight:760;color:var(--neem);line-height:1'>0</div></div>"
           f"<div style='font-size:21px;color:var(--slate);margin-top:16px;line-height:1.45'>scene-model false alarms in the demo room's 5.1 h log, at the thresholds set from that log</div>")
    pic = (f"<div style='display:flex;align-items:baseline;gap:22px;margin-top:40px'><div class='disp' style='font-size:96px;font-weight:760;color:#5d6c67;line-height:1'>8.0</div>"
           f"<div style='font-size:44px;color:var(--slate)'>→</div><div class='a pop disp' style='--d:{s[3] + 0.9:.2f}s;font-size:96px;font-weight:760;color:var(--neem);line-height:1'>3.9 s</div></div>"
           f"<div style='font-size:21px;color:var(--slate);margin-top:16px;line-height:1.45'>to analyse one 4 s clip on the Pi 5 (640 → 320 px, same Level 3 recall)</div>")

    def p(title, sub, inner, d, x):
        return (f"<div class='a rise card' style='--d:{d:.2f}s;position:absolute;left:{x}px;top:310px;width:400px;height:520px;padding:34px'>"
                f"<div class='disp' style='font-size:30px;font-weight:700;line-height:1.1'>{esc(title)}</div><div style='font-size:19px;color:var(--slate);margin-top:6px'>{esc(sub)}</div>{inner}</div>")
    body = f"""{topbar(6)}{heading('Final results', 'Retrained detectors, the room and the Pi')}
{p('Detector AUROC', 'Held-out videos · 1.0 = perfect', au, s[0], 96)}{p('Level 3 on replay', 'Our recordings, retrained heads', ep, s[1], 538)}
{p('Room calibration', 'Demo room, Raspberry Pi', cal, s[2], 980)}{p('Pi speed', 'Clip engine, ONNX', pic, s[3], 1422)}{caps(lines, T)}"""
    return body, []


def limits(T, dur, lines, o):
    t0, t1 = T[0][0], T[1][0]
    items = [("Too little normal footage", "4.9 normal minutes on replay cannot support a false-alarm rate", t0 + 0.6),
             ("The Pi is slower than its target", "About 4 s per decision against a 2 s goal; an accelerator is next", t0 + 3.6),
             ("Detectors need room calibration", "The weapon detector raised false Level 3s in the demo room", t0 + 6.6)]
    cards = "".join(f"""<div class="a rise card" style="--d:{d:.2f}s;padding:40px 40px;height:330px">
 <div style="width:46px;height:6px;border-radius:3px;background:var(--haldi)"></div>
 <div class="disp" style="font-size:38px;font-weight:700;margin-top:24px;line-height:1.12">{esc(a)}</div>
 <div style="font-size:25px;color:var(--slate);margin-top:16px;line-height:1.45">{esc(b)}</div></div>""" for a, b, d in items)
    nxt = (f"<div class='a rise' style='--d:{t1:.2f}s;position:absolute;left:96px;right:96px;top:700px'><div class='card' style='padding:30px 40px;"
           f"display:flex;align-items:center;gap:24px;border-color:rgba(96,226,170,.4)'><span class='kick'>Next</span>"
           f"<span style='font-size:32px;font-weight:650'>Record in the deployment room, label, fine-tune, then calibrate again</span></div></div>")
    body = f"""{topbar(6)}{heading('Limits we report', 'What the data cannot claim yet')}
<div style="position:absolute;left:96px;right:96px;top:310px;display:grid;grid-template-columns:repeat(3,1fr);gap:30px">{cards}</div>{nxt}{caps(lines, T)}"""
    return body, []


def close(T, dur, lines, o):
    t0, t1 = T[0][0], T[1][0]
    body = f"""
<div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center">
 <div class="a pop" style="--d:.2s;width:96px;height:96px;border-radius:50%;border:6px solid var(--neem);display:grid;place-items:center;box-shadow:0 0 60px rgba(96,226,170,.35)">
  <div style="width:30px;height:30px;border-radius:50%;background:var(--neem)"></div></div>
 <div class="disp a blurIn" style="--d:.4s;font-size:170px;font-weight:760;font-stretch:125%;letter-spacing:.08em;margin-top:40px;line-height:1;
   background:linear-gradient(180deg,#fff 30%,#9fe8c8);-webkit-background-clip:text;color:transparent">SENTRIX</div>
 <div class="disp a rise" style="--d:{t0 + 0.2:.2f}s;font-size:50px;font-weight:600;margin-top:30px;color:#cfe9dd">Decides at home. Asks the owner first.</div>
 <div class="a grow" style="--d:{t0 + 0.9:.2f}s;height:2px;width:700px;margin:50px auto 40px;background:linear-gradient(90deg,transparent,var(--neem),transparent)"></div>
 <div class="a fade" style="--d:{t0 + 1.3:.2f}s;font-size:28px;font-weight:600;line-height:1.6">Kartik Garg · Akshay Ranveer · Prashant Gagneja · Harshit Mishra · Mehul Perimal</div>
 <div class="a fade" style="--d:{t0 + 1.8:.2f}s;font-size:24px;color:var(--slate);margin-top:10px;line-height:1.6">
  Mentor: Dr. Ashutosh Mishra · Capstone Project CPG-299 · Thapar Institute of Engineering and Technology, Patiala<br>
  <span class="mono" style="color:var(--neem)">github.com/mehulperimal14/Sentrix</span></div>
</div>
<div class='blackout' style='animation:fade .9s ease {dur - 0.9:.2f}s both'></div>{caps(lines, T)}"""
    return body, []


BUILDERS = {f.__name__: f for f in (problem, hero, team, agenda, programs, modules, physical, wiring, inputs, start, stop, trouble,
                                     wallclip, phoneclip, browserclip, pi, results1, results2, limits, close)}

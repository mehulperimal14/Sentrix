"""The look: one stylesheet (the device app's dark palette and its own fonts) and small HTML helpers.

Every animated element carries its start in `--d` (seconds from the scene's first frame); render.mjs
steps the paused CSS animations frame by frame, so delays are exact.
"""
from html import escape as esc
from pathlib import Path

ASSETS = Path("G:/Sentrix/sentrix_portal/static/app/assets")
ANEK = next(ASSETS.glob("anek-latin-latin-wdth-normal-*.woff2")).as_uri()
NOTO = next(ASSETS.glob("noto-sans-latin-wght-normal-*.woff2")).as_uri()
FIG = Path(r"G:\Sentrix\docs\capstone\end_sem\report\figures")
CHAPTERS = ["Components", "Connections", "Inputs", "Start · stop", "Execution", "Results"]

CSS = """
@font-face{font-family:Anek;src:url(ANEK) format('woff2');font-weight:100 800;font-stretch:75% 125%}
@font-face{font-family:Noto;src:url(NOTO) format('woff2');font-weight:100 900}
:root{--bg:#06090b;--paper:#0f1517;--paper2:#141c1f;--ink:#f0f5f3;--slate:#93a49e;--dim:#5d6c67;--line:#24302d;
 --neem:#60e2aa;--indigo:#a0aaff;--haldi:#f7bd56;--chilli:#ff7078;--chillif:#de283e;--ease:cubic-bezier(.16,1,.3,1)}
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:1920px;height:1080px;overflow:hidden;background:var(--bg)}
body{font-family:Noto,'Segoe UI',sans-serif;color:var(--ink);position:relative;-webkit-font-smoothing:antialiased}
.bg{position:absolute;inset:0;background:
  radial-gradient(1100px 700px at 8% -12%,rgba(96,226,170,.13),transparent 62%),
  radial-gradient(900px 760px at 108% 112%,rgba(160,170,255,.12),transparent 60%),
  radial-gradient(1400px 900px at 50% 50%,#0b1114,#06090b 70%)}
.grid{position:absolute;inset:0;opacity:.35;background-image:
  linear-gradient(rgba(255,255,255,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px);
  background-size:64px 64px;-webkit-mask-image:radial-gradient(1200px 700px at 50% 40%,#000 20%,transparent 75%)}
.vig{position:absolute;inset:0;pointer-events:none;background:radial-gradient(1500px 1000px at 50% 50%,transparent 60%,rgba(0,0,0,.45))}
.disp{font-family:Anek,'Segoe UI',sans-serif;font-stretch:112%}
/* top bar */
.topbar{position:absolute;left:96px;right:96px;top:44px;height:44px;display:flex;align-items:center;justify-content:space-between}
.logo{display:flex;align-items:center;gap:14px;font-family:Anek;font-stretch:125%;font-weight:700;font-size:26px;letter-spacing:.14em}
.ring{width:26px;height:26px;border-radius:50%;border:3px solid var(--neem);display:grid;place-items:center}
.ring::after{content:'';width:8px;height:8px;border-radius:50%;background:var(--neem)}
.chaps{display:flex;gap:8px}
.chap{font-size:17px;font-weight:600;color:var(--dim);padding:9px 16px;border-radius:999px;border:1px solid transparent;letter-spacing:.02em}
.chap b{font-family:Anek;font-weight:700;margin-right:8px;color:inherit}
.chap.done{color:var(--slate)}
.chap.on{color:#04150e;background:var(--neem)}
/* heading */
.head{position:absolute;left:96px;top:128px;right:96px}
.kick{font-family:Anek;font-stretch:118%;font-weight:600;font-size:22px;letter-spacing:.22em;text-transform:uppercase;color:var(--neem)}
h1{font-family:Anek;font-stretch:108%;font-weight:650;font-size:58px;line-height:1.08;margin-top:10px;letter-spacing:-.005em}
/* caption */
.cap{position:absolute;left:50%;bottom:40px;transform:translateX(-50%);max-width:1480px;width:max-content;
  padding:13px 30px 15px;border-radius:16px;background:rgba(5,8,10,.80);border:1px solid rgba(255,255,255,.07);
  box-shadow:0 10px 40px rgba(0,0,0,.35);font-size:29px;line-height:1.36;font-weight:500;text-align:center;color:#eef4f1;opacity:0}
/* generic cards */
.card{background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.015));border:1px solid rgba(255,255,255,.08);
  border-radius:22px;box-shadow:0 30px 60px rgba(0,0,0,.35),inset 0 1px 0 rgba(255,255,255,.05)}
.chip{display:inline-flex;align-items:center;gap:10px;padding:8px 16px;border-radius:999px;background:rgba(255,255,255,.06);
  border:1px solid rgba(255,255,255,.09);font-size:20px;font-weight:600;color:var(--slate)}
.dot{width:10px;height:10px;border-radius:50%;background:var(--neem);box-shadow:0 0 14px var(--neem)}
.mono{font-family:Consolas,'Cascadia Mono',monospace}
.muted{color:var(--slate)}
.neem{color:var(--neem)}.indigo{color:var(--indigo)}.haldi{color:var(--haldi)}.chilli{color:var(--chilli)}
/* frames for screens */
.win{position:absolute;border-radius:18px;overflow:hidden;background:#0a0f11;border:1px solid rgba(255,255,255,.10);
  box-shadow:0 40px 90px rgba(0,0,0,.55),0 0 0 1px rgba(0,0,0,.6)}
.win .bar{height:40px;background:#11181b;border-bottom:1px solid rgba(255,255,255,.07);display:flex;align-items:center;gap:9px;padding:0 16px}
.win .bar i{width:12px;height:12px;border-radius:50%;background:#2c3735}
.win .bar span{margin-left:14px;font-size:16px;color:var(--slate);font-weight:600;letter-spacing:.02em}
.win .slot{background:#0b1214}
.phone{position:absolute;border-radius:54px;background:#050708;padding:14px;border:1px solid rgba(255,255,255,.14);
  box-shadow:0 40px 90px rgba(0,0,0,.6),inset 0 0 0 2px #1a2225}
.phone .slot{border-radius:40px;background:#0b1214;overflow:hidden}
.studio{border-radius:26px;background:radial-gradient(120% 120% at 30% 20%,#ffffff,#e9ecea 55%,#cfd5d2);overflow:hidden;
  box-shadow:0 40px 90px rgba(0,0,0,.5)}
.studio img{mix-blend-mode:multiply;display:block}
/* animations */
@keyframes rise{from{opacity:0;transform:translateY(28px)}to{opacity:1;transform:none}}
@keyframes riseS{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes fadeout{from{opacity:1}to{opacity:0}}
@keyframes wipe{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}
@keyframes wipeD{from{clip-path:inset(0 0 100% 0)}to{clip-path:inset(0 0 0 0)}}
@keyframes pop{from{opacity:0;transform:scale(.92)}to{opacity:1;transform:none}}
@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes growY{from{transform:scaleY(0)}to{transform:scaleY(1)}}
@keyframes draw{from{stroke-dashoffset:1}to{stroke-dashoffset:0}}
@keyframes drift{from{transform:scale(1) translate(0,0)}to{transform:scale(1.06) translate(-1%,-1%)}}
@keyframes floatA{from{transform:translateY(0)}to{transform:translateY(-14px)}}
@keyframes blurIn{from{opacity:0;filter:blur(18px);transform:scale(1.04)}to{opacity:1;filter:blur(0);transform:none}}
@keyframes spread{from{letter-spacing:.5em;opacity:0}to{letter-spacing:.16em;opacity:1}}
@keyframes act{from{border-color:rgba(255,255,255,.08);transform:none;background:rgba(255,255,255,.03)}
  to{border-color:rgba(96,226,170,.75);transform:translateY(-10px);background:rgba(96,226,170,.09)}}
@keyframes deact{from{border-color:rgba(96,226,170,.75);transform:translateY(-10px);background:rgba(96,226,170,.09)}
  to{border-color:rgba(255,255,255,.08);transform:none;background:rgba(255,255,255,.03)}}
@keyframes lit{from{opacity:.35}to{opacity:1}}
@keyframes unlit{from{opacity:1}to{opacity:.35}}
.a{animation-duration:.85s;animation-timing-function:var(--ease);animation-delay:var(--d,0s);animation-fill-mode:both}
.rise{animation-name:rise}.riseS{animation-name:riseS}.fade{animation-name:fade;animation-duration:.6s}
.wipe{animation-name:wipe;animation-duration:1s;animation-timing-function:cubic-bezier(.65,0,.35,1)}
.wipeD{animation-name:wipeD;animation-duration:.9s}
.pop{animation-name:pop}.blurIn{animation-name:blurIn;animation-duration:1.3s}
.grow{animation-name:grow;transform-origin:left center;animation-duration:1.1s}
.growY{animation-name:growY;transform-origin:center bottom;animation-duration:1.1s}
.draw{animation-name:draw;animation-duration:1.2s;animation-timing-function:cubic-bezier(.65,0,.35,1)}
.blackout{position:absolute;inset:0;background:#000;opacity:0;z-index:50}
"""
CSS = CSS.replace("ANEK", ANEK).replace("NOTO", NOTO)


def uri(p) -> str:
    return Path(p).as_uri()


def anim(cls: str, d: float, extra: str = "") -> str:
    """class + style attributes for an element that animates in at d seconds."""
    return f'class="a {cls} {extra}" style="--d:{d:.2f}s"'


def page(body: str, css: str = "") -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}{css}</style></head>"
            f"<body><div class='bg'></div><div class='grid'></div>{body}<div class='vig'></div></body></html>")


def topbar(chapter: int, d: float = 0.0) -> str:
    chips = "".join(
        f"<div class='chap {'on' if i == chapter else 'done' if i < chapter else ''}'><b>{i}</b>{esc(n)}</div>"
        for i, n in enumerate(CHAPTERS, 1))
    return (f"<div class='topbar'><div class='logo'><div class='ring'></div>SENTRIX</div>"
            f"<div class='chaps'>{chips if chapter else ''}</div></div>")


def heading(kick: str, title: str, d: float = 0.15) -> str:
    return (f"<div class='head'><div {anim('riseS', d)}><span class='kick'>{esc(kick)}</span></div>"
            f"<h1 {anim('wipe', d + 0.12)}>{esc(title)}</h1></div>")


def captions(lines, fade=0.22) -> str:
    """lines: [(start, end, text)] -> one caption at a time, faded in and out."""
    out = []
    for a, b, text in lines:
        out.append(f"<div class='cap' style='animation:fade {fade}s ease {a:.2f}s both, fadeout {fade}s ease {b - fade:.2f}s forwards'>"
                   f"{esc(text)}</div>")
    return "".join(out)


def blackout(start: float, dur: float = 0.5, rev: bool = False) -> str:
    """Fade to black from `start` (rev: from black at `start`)."""
    if rev:
        return f"<div class='blackout' style='opacity:1;animation:fadeout {dur}s ease {start:.2f}s both'></div>"
    return f"<div class='blackout' style='animation:fade {dur}s ease {start:.2f}s both'></div>"

"""Copy the final videos next to the other deliverables, write the submission README and zip everything."""
import shutil
import subprocess
import zipfile
from datetime import date
from pathlib import Path

import pymupdf

END = Path(r"G:\Sentrix\docs\capstone\end_sem")
SRC = Path(__file__).resolve().parent / "out"
VID = END / "video"
VID.mkdir(exist_ok=True)
for old in VID.glob("*"):
    if old.suffix in (".mp4", ".md", ".srt"):
        old.unlink()
for f in [SRC / "SENTRIX_CPG299_Video.mp4", SRC / "NARRATION.md", SRC / "narration.srt"]:
    shutil.copy2(f, VID / f.name)


def seconds(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout)


length = seconds(VID / "SENTRIX_CPG299_Video.mp4")
pages = len(pymupdf.open(END / "SENTRIX_CPG299_EndSem_Report.pdf"))
slides = len(pymupdf.open(END / "deck" / "SENTRIX_CPG299_Deck.pdf"))

README = f"""SENTRIX - Capstone Project CPG-299 - end-semester submission
Thapar Institute of Engineering and Technology, CSED - Mentor: Dr. Ashutosh Mishra
Team: Kartik Garg (102303478), Akshay Ranveer (102303453), Prashant Gagneja (102353011),
      Harshit Mishra (102319039), Mehul Perimal (102315144)
Packed: {date.today():%d %b %Y}, from github.com/mehulperimal14/Sentrix

Report/        SENTRIX_CPG299_EndSem_Report.pdf   - final report, {pages} pages, A4, IEEE references
Poster/        SENTRIX_CPG299_Poster.pdf          - A3 landscape poster (PNG preview alongside)
Presentation/  SENTRIX_CPG299_Deck.pptx           - {slides} slides, 16:9, speaker notes on every slide
               SENTRIX_CPG299_Deck.pdf            - the same slides as PDF
Video/         SENTRIX_CPG299_Video.mp4           - one video for the team, {int(length // 60)} min {int(length % 60)} s, 1920x1080
               NARRATION.md, narration.srt        - the narration with timings, and the captions

THE VIDEOS follow the department's video guideline in its order:
  - the title card (the five students with their roll numbers, the project, its title, the university)
    on screen from the first frame for about 20 seconds;
  - the team and each member's role;
  - 1 components (physical and software modules), 2 connections (the README and the wiring, pin by pin),
    3 inputs required, 4 how to start, stop and troubleshoot it, 5 the execution steps (a complete
    run in test mode), 6 the final results and their limits.
Voice: a male narrator (Kokoro "Michael", an offline neural voice); the team's roles are introduced in
Kartik Garg's own recorded voice. Captions show the same words. The music is an
original bed generated for these videos (no third-party music).
To replace the narration with a recorded voice, record NARRATION.md and swap the audio, e.g.
  ffmpeg -i SENTRIX_CPG299_Video.mp4 -i my_voice.m4a -map 0:v -map 1:a -c:v copy -c:a aac -shortest final.mp4

Everything shown in the videos ran in test mode: no message left the laptop. The handgun is a replica,
staged by the team.

The slides in the .pptx are pictures of the final deck (fonts and layout exactly as designed).

Still to add by the team before submitting the report: signatures and dates (Declaration,
Acknowledgement), the peer-assessment ratings (Table 7.3) and the plagiarism report (Appendix B).
"""
(END / "SUBMISSION_README.txt").write_text(README, encoding="utf-8")

FILES = [
    ("Report/SENTRIX_CPG299_EndSem_Report.pdf", END / "SENTRIX_CPG299_EndSem_Report.pdf"),
    ("Poster/SENTRIX_CPG299_Poster.pdf", END / "poster" / "SENTRIX_CPG299_Poster.pdf"),
    ("Poster/SENTRIX_CPG299_Poster.png", END / "poster" / "SENTRIX_CPG299_Poster.png"),
    ("Presentation/SENTRIX_CPG299_Deck.pptx", END / "deck" / "SENTRIX_CPG299_Deck.pptx"),
    ("Presentation/SENTRIX_CPG299_Deck.pdf", END / "deck" / "SENTRIX_CPG299_Deck.pdf"),
    ("README.txt", END / "SUBMISSION_README.txt"),
] + [(f"Video/{f.name}", f) for f in sorted(VID.glob("*")) if f.suffix in (".mp4", ".md", ".srt")]
out = END / "SENTRIX_CPG299_Submission.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for arc, src in FILES:
        z.write(src, f"SENTRIX_CPG299_Submission/{arc}")
        print(f"{src.stat().st_size / 1e6:7.1f} MB  {arc}")
print(f"-> {out}  {out.stat().st_size / 1e6:.1f} MB")

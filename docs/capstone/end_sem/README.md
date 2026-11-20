# End-semester report (CPG-299, December 2026)

`SENTRIX_CPG299_EndSem_Report.pdf` is the compiled report (125 pages, A4).
It was last updated on 21 Nov 2026:
- the demonstration unit as built and in use: Raspberry Pi 5 with the Active
  Cooler, Camera Module 3 on the printed tilting mount, a USB microphone, three
  status LEDs, the official 27 W USB-C supply and the printed case (revision F),
  about Rs. 29,340 in parts (Sections 2.3 and 4.4); parts the unit does not have
  (motion, door and mains sensors, a UPS, a buck converter, a 4G modem) are no
  longer in its description, diagrams or bill of materials;
- the Portal (frontend and backend) runs on Render and is always on for the
  evaluations, so nobody starts it by hand (Section 5.3.3);
- the data is presented as the team's own labelled recordings (977 reviewed
  frames), never as a limitation (Abstract and Section 5.2.1);
- the demo Pi's switch to the v5 fire head and the live flame tests in the demo
  room (Sections 5.3.3 and 5.5.4).

On 20 Nov it was audited against the department's
report, IEEE-reference and PPT/poster guidelines and the coordinator's e-mail:
- research questions RQ1-RQ4 open the literature survey (Section 2.1), each
  topic says what it answers, and Section 2.1.5 answers each question;
- every figure, table and algorithm is cited in the text, and the diagrams
  (work breakdown, use case, class, sequence, state machine, activity, data
  flow) and Algorithms 5.1-5.5 are explained step by step;
- a comparison with published alarm rules on the same replayed scores
  (Figure 5.21, `tools/baseline_alarm_rules.py`);
- test results from the current `main`, a results table per feature area and
  a table tracing every functional requirement to its tests;
- the format rules applied literally: Times New Roman, 14 pt bold headings at
  every level, 10 pt tables and captions, algorithms numbered by chapter,
  nothing in the margins, 1.5 spacing in the front matter;
- the 87 references in the department's IEEE form.
Earlier on 19 Nov it gained the recordings added on 18 Nov (977 reviewed
frames from the team's own staged recordings), the v5 detector heads, the room calibration tool with the Pi's
thresholds of 0.78 / 0.75 (D23) and the OSNet-AIN body model at a bar of
0.87 (D24). `report/` holds its LaTeX source.

## Format

The report follows the department's *Report Format Guidelines*: A4 portrait,
1" top/bottom/right and 1.5" left margins, Times New Roman, 16 pt bold chapter
names, 14 pt bold headings, 12 pt text, 1.5 line spacing, 10 pt table text and
captions, page numbers bottom centre, IEEE numbered references. Code and file
names are in Courier New, one size smaller than the text around them. The headings
copy the look of the department's LaTeX template: right-aligned titles over a
thick rule. The chapters and sections follow the template's table of contents.

## Build

```
cd report
tectonic main.tex
```

The report uses `fontspec` with the Times New Roman installed on Windows, so
it needs XeTeX (Tectonic is XeTeX). On Overleaf: upload `report/`, choose
XeLaTeX, and compile twice (BibTeX runs automatically); Overleaf has Times New
Roman only if the font files are uploaded with the source. The charts come from `report/make_figures.py`, which writes
`report/figures/*.pdf`. Every number in it is copied from a committed report,
and each one names its source.

## Sources of the numbers

- Model and replay results: `docs/6_CUSTOM_DATASET_EVALUATION.md`, `training/runs/custom_eval/**`
- Raspberry Pi results: `training/runs/custom_eval/pi5/REPORT.md`
- Re-identification results:
  - `training/runs/reid/osnet_x1_0_imagenet/REPORT.md`
  - `osnet_weights_compare/REPORT.md`
  - `osnet_ain_14videos/REPORT.md`
- Retrained detector heads:
  - `training/runs/eval/detectors_2026-11-19/report.json`
  - `training/runs/eval/fire_candidate_2026-11-19/report.json`
  - the replays of every labelled video `training/runs/custom_eval/baseline_2026-11-19/` and `v5det_2026-11-19/`
- The v4 scene model on the five new videos: `training/runs/eval/baseline_2026-11-18/report.json`
- Room calibration: `training/runs/calibration/pi5_demo_room_2026-11-19/REPORT.md`
- Slim scene heads: `training/runs/custom/slim/REPORT.md`
- Live diagnosis and demo-room calibration: `CLAUDE.md` sections 4.3 and 5
- Bill of materials: `docs/8_RASPBERRY_PI5_INTEGRATION.md` section 8
- Published alarm rules on the same replay: `training/runs/custom_eval/baseline_rules/m11b_replay_all.json`
  (`tools/baseline_alarm_rules.py`; it checks that its SENTRIX row reproduces the replay's own Level-3 numbers)

Test results quoted in the report:

| Suite | Commit | Result |
|---|---|---|
| pytest | `main` `37146c3` (20 Nov) | 2,281 collected: 2,279 passed, 2 failed in 409 s. One failure was a fault in the test (it compared served bytes with a copy whose line endings had been converted; fixed in a pull request); the other, a link-client timing test, passed when run again alone |
| Vitest | `c1241c1` (now in `main`) | 15 of 15 passed |
| Playwright | `c1241c1` (now in `main`) | 22 of 23 passed; adding a contact fails (test case TC-29) |

All 87 references were checked against the publisher, DOI or official page on
18 Nov 2026, and their form against the department's IEEE guide on 20 Nov
(web pages, software and standards in the guide's online and standard forms,
article numbers, conference locations, full author lists for six-author papers).
The mid-semester references that could not be verified are not used.

30 of the references are from 2024–2026. They cover:
- newer detectors (RT-DETR, YOLOv10, YOLOv12, YOLO26) and recent weapon and
  fire detection work;
- anomaly detection with vision-language models, and recent audio-visual
  fusion;
- DINOv3, CED and re-identification surveys;
- the Raspberry Pi AI HAT+ and AI HAT+ 2;
- policy and data: the DPDP Rules 2025, the EU AI Act, NCRB *Crime in India
  2023*, ERSS 2.0 and Seattle's verified-response policy;
- news on cloud face recognition in home cameras.

They appear in the need analysis, the research gaps, the "recent developments"
part of the literature review (Section 2.1.1), the literature table (two per
member), the comparison table and chart (Section 5.5.9) and the future work.

The verification notes:
- DINOv3 is cited through its TMLR article (May 2026, OpenReview 2NlGyqNjns).
- The DPDP Rules are cited through the government's own press release (PIB).

## Poster

`poster/SENTRIX_CPG299_Poster.pdf` is the poster: one A3 landscape page (420 x
297 mm), made to the department's poster guidelines:
- a banner centred on the sheet with the full title, the five names, the
  mentor, the department and institute in full, and CPG No. 299;
- five numbered panels, read left to right: the problem; goals → outcomes,
  with what we learned; how it works; the unit as built; results;
- four numbered figures, each cited in the text: the flow from camera to SOS
  (Fig. 1), the unit with its numbered parts (Fig. 2), the owner's alert on a
  phone (Fig. 3) and the false-alarm chart (Fig. 4), with a line interpreting
  the chart;
- a footer with references [1]-[6] and the tools used;
- the deck's colours (ivory, ink, terracotta, deep teal) and its display face,
  Instrument Serif;
- no text smaller than 18 pt (24 px).

The numbers come from the same sources as the report.

- `poster/SENTRIX_CPG299_Poster.png`: a preview at twice the size.
- `poster/poster.html`: the source. It takes the images from `report/figures/`
  by relative path. To print it, open it in Chrome and choose A3 landscape
  with no margins.

## Deck

`deck/SENTRIX_CPG299_Deck.pdf` and `deck/SENTRIX_CPG299_Deck.pptx` are the
final-evaluation deck: 17 slides, 16:9, in the order of the department's PPT
guideline, with speaker notes on every slide (the PPTX holds pictures of the
slides, so fonts and layout stay as designed).

## Videos

`video/NARRATION.md` and `video/narration.srt` are the narration and captions
of the five submission videos, one per student (8 min 3 s, 1920 x 1080). They
follow the department's video guideline in its order: the student's title card
(name, roll number, project, title, university) from the first frame for 16 s,
the team's roles, then components, connections (README and wiring), inputs,
start/stop/troubleshooting, a complete test-mode run, and the results with
their limits. The narrator is Kokoro's "Michael" voice (offline); in Kartik
Garg's video the team's roles are in his own voice; the music is generated
for the videos. The MP4 files (about 86 MB each) and the submission zip are
too large for git and are not committed.

## Screenshots

The screenshots were captured on 18 Nov 2026 from the test-mode demo world:
`tools/demo_reset.py` + `tools/demo_run.py`, driven by Playwright on the
`sentrix/frontend-v2` branch, with the held-out `gun.mov` clip played through
the demo-reel camera. No message left the laptop.

- `report/figures/screens/`: the full captures
- `report/figures/screens_c/`: the cropped versions the report uses

## Still to do by the team before submission

- Signatures and dates (Declaration, Acknowledgement).
- Ratings in the peer assessment matrix (Table 7.3).
- The department's plagiarism report (Appendix B).
- Check the role and ownership tables (Tables 3.3 and 7.4) against who did what.

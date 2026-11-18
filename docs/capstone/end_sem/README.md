# End-semester report (CPG-299, December 2026)

`SENTRIX_CPG299_EndSem_Report.pdf` is the compiled report (109 pages, A4).
It was last updated on 19 Nov 2026, with:
- the five videos added on 18 Nov (977 frames from 15 videos);
- the v5 detector heads;
- the room calibration tool and the Pi's thresholds of 0.78 / 0.75 (D23);
- the shipped OSNet-AIN body model at a bar of 0.87 (D24).
`report/` holds its LaTeX source.

## Format

The report follows the department's *Report Format Guidelines*: A4 portrait,
1" top/bottom/right and 1.5" left margins, Times, 16 pt bold chapter names,
14 pt bold headings, 12 pt text, 1.5 line spacing, 10 pt table text and
captions, page numbers bottom centre, IEEE numbered references. The headings
copy the look of the department's LaTeX template: right-aligned titles over a
thick rule. The chapters and sections follow the template's table of contents.

## Build

```
cd report
tectonic main.tex
```

Overleaf works too: upload `report/`, choose pdfLaTeX, and compile twice (BibTeX
runs automatically). The charts come from `report/make_figures.py`, which writes
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
  - the 15-video replays `training/runs/custom_eval/baseline_2026-11-19/` and `v5det_2026-11-19/`
- The v4 scene model on the five new videos: `training/runs/eval/baseline_2026-11-18/report.json`
- Room calibration: `training/runs/calibration/pi5_demo_room_2026-11-19/REPORT.md`
- Slim scene heads: `training/runs/custom/slim/REPORT.md`
- Live diagnosis and demo-room calibration: `CLAUDE.md` sections 4.3 and 5
- Bill of materials: `docs/8_RASPBERRY_PI5_INTEGRATION.md` section 8

Test results were measured on 18 and 19 Nov 2026:

| Suite | Branch and commit | Result |
|---|---|---|
| pytest | `sentrix/portal` `5127b3c` | 2154 passed in 386.6 s (19 Nov) |
| pytest | `sentrix/portal` `3e1e296` | 2125 passed in 378.9 s |
| pytest | `sentrix/frontend-v2` `c1241c1` | 2243 passed, 1 skipped |
| Vitest | `sentrix/frontend-v2` `c1241c1` | 15 of 15 passed |
| Playwright | `sentrix/frontend-v2` `c1241c1` | 22 of 23 passed; adding a contact fails (test case TC-29) |

All 87 references were checked against the publisher, DOI or official page on
18 Nov 2026. The mid-semester references that could not be verified are not used.

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
member), the comparison table (Section 5.5.8) and the future work.

The verification notes:
- DINOv3 is cited through its arXiv version.
- The DPDP Rules are cited through the government's own press release (PIB).

## Poster

`poster/SENTRIX_CPG299_Poster.pdf` is the poster: one A3 landscape page (420 x
297 mm), made to the department's poster guidelines:
- a banner at top centre with the title, the five names, the department, CPG
  No. 299 and the mentor;
- five numbered panels, read left to right: problem, objectives and
  learning, how it works, the product, results;
- three numbered figures and references [1]-[5];
- no text smaller than 18 pt (24 px).

The numbers come from the same sources as the report.

- `poster/SENTRIX_CPG299_Poster.png`: a preview at twice the size.
- `poster/poster.html`: the source. It takes the images from `report/figures/`
  by relative path. To print it, open it in Chrome and choose A3 landscape
  with no margins.

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

# End-semester report (CPG-299, December 2026)

`SENTRIX_CPG299_EndSem_Report.pdf` is the compiled report (101 pages, A4).
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
- Re-identification results: `training/runs/reid/osnet_x1_0_imagenet/REPORT.md`
- Slim scene heads: `training/runs/custom/slim/REPORT.md`
- Live diagnosis and demo-room calibration: `CLAUDE.md` sections 4.3 and 5
- Bill of materials: `docs/8_RASPBERRY_PI5_INTEGRATION.md` section 8

Test results were measured on 18 Nov 2026:

| Suite | Branch and commit | Result |
|---|---|---|
| pytest | `sentrix/portal` `3e1e296` | 2125 passed in 378.9 s |
| pytest | `sentrix/frontend-v2` `c1241c1` | 2243 passed, 1 skipped |
| Vitest | `sentrix/frontend-v2` `c1241c1` | 15 of 15 passed |
| Playwright | `sentrix/frontend-v2` `c1241c1` | 22 of 23 passed; adding a contact fails (test case TC-29) |

All 57 references were checked against the publisher, DOI or official page on
18 Nov 2026. The mid-semester references that could not be verified are not used.

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

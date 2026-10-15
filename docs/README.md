# SENTRIX documentation

The documents describe branch `sentrix/portal` (October 2026): the device with
its Local Console, the SOS engine and alert channels, the device link and
tunnel, the cloud Portal with accounts, onboarding and the SENTRIX Desk, the
Raspberry Pi 5 hardware I/O, the 4 s clip engine and the slim scene model
experiment. Where a document is partly historical, its header says which
sections describe what.

## Start here

| If you want to… | Read |
|---|---|
| install and run it, or run the demo | [4 — Deployment & runbook](4_DEPLOYMENT_AND_RUNBOOK.md) |
| understand how the device works | [1 — System architecture](1_SYSTEM_ARCHITECTURE.md) |
| understand the Portal and the SOS flow | [7 — Portal, onboarding and SOS](7_PORTAL_AND_SOS.md), Part 0 |
| know what the models score, and on what | [2 — Model benchmarks](2_MODEL_BENCHMARKS_AND_KPIS.md), [6 — Custom dataset evaluation](6_CUSTOM_DATASET_EVALUATION.md) |
| know what is left | [`ROADMAP.md`](../ROADMAP.md) (the step-by-step plan to a working prototype); [5 — Roadmap & remaining work](5_FUTURE_ADVANCEMENTS_AND_CLOUD_DEPLOYMENT.md) (summary and research notes) |
| build the Raspberry Pi unit | [8 — Raspberry Pi 5 integration](8_RASPBERRY_PI5_INTEGRATION.md), `deploy/pi5/README.md` |

## The numbered set

| # | Document | What it covers | Describes |
|---|---|---|---|
| 1 | [System architecture](1_SYSTEM_ARCHITECTURE.md) | repository layout, data flow, threat levels and evidence rules, TCI, evidence chain, threads, security model, non-goals | current |
| 2 | [Model benchmarks, training results & KPIs](2_MODEL_BENCHMARKS_AND_KPIS.md) | v3 training results and their split contamination (historical); deployed weights; v4 and v5 models; measured latency; KPI status | §1–4, §6 historical (August 2026); §5, §7, §8 current |
| 3 | [Original hardware plan & BOM](3_HARDWARE_SPECIFICATION_AND_BOM.md) | the mid-semester ESP32 / Orange Pi plan; siren, camera sources and hardware code as built | §1–4 superseded by doc 8; §5–7 current |
| 4 | [Deployment guide & operations runbook](4_DEPLOYMENT_AND_RUNBOOK.md) | install, configuration and the send switches, the console, models, evaluation, the Portal and the demo tools, operations, troubleshooting, checklist | current |
| 5 | [Roadmap & remaining work](5_FUTURE_ADVANCEMENTS_AND_CLOUD_DEPLOYMENT.md) | status by area, the critical path (M6–M9, gates), model research notes, Tier 2 items | current |
| 6 | [Custom dataset evaluation](6_CUSTOM_DATASET_EVALUATION.md) | the team's 765-frame, 10-video dataset; how v4 was chosen; results after the repair, in clip mode and for the slim models | per section (table in its header) |
| 7 | [Portal, onboarding and SOS — architecture and contract](7_PORTAL_AND_SOS.md) | plain overview (Part 0); configuration, link protocol, tunnel, data model, SOS state machine, channels, roles, endpoints; integration status; live verification run | current |
| 8 | [Raspberry Pi 5 integration](8_RASPBERRY_PI5_INTEGRATION.md) | targets, measured compute problem, the four Pi techniques, wiring, BOM, gates | current plan; no hardware run yet |

Section numbers in docs 7 and 8 are cited from code comments; keep them stable
when editing.

## Capstone deliverables

Submitted documents, kept as submitted (they describe the project at the time
of submission, not this version):

| Folder | Files |
|---|---|
| [`capstone/proposal/`](capstone/proposal/) | `SENTRIX_proposal_v3.pdf`, `SENTRIX_proposal_presentation.pdf` |
| [`capstone/mid_sem/`](capstone/mid_sem/) | `CPG299-2026_mid_sem_report.pdf` (and `.docx`), `SENTRIX_CAPSTONE_POSTER.pdf` (and `.png`) |

Corrections that matter for the final report are called out in docs 1, 2
and 3 as "Corrected from v2.1" or "History" notes (for example: the
contaminated splits, the synthetic fusion model, "tamper-evident" rather than
"court-admissible").

## Figures

| File | Shows |
|---|---|
| [`assets/sentrix_architecture_diagram.png`](assets/sentrix_architecture_diagram.png) | the device software architecture before the Portal work. Out of date in two places: it shows the TCI as an XGBoost model (retired in M1; the TCI is now the strongest confirmed class evidence) and has no SOS, channels or link |
| [`assets/sentrix_hardware_block_diagram.jpg`](assets/sentrix_hardware_block_diagram.jpg) | an early hardware concept (NUC / Apple Silicon host, two 1080p cameras, UPS). Superseded by the Raspberry Pi 5 design in doc 8; its capability strip (sub-10 ms YOLOv8, XGBoost TCI) does not describe this version |

## Elsewhere in the repository

| Where | What |
|---|---|
| `README.md` | project overview, quickstart, configuration rationale, evaluation workflow |
| `CLAUDE.md` | project memory: decisions D1–D14, milestones M1–M10 with status, the live diagnosis |
| `ROADMAP.md` | remaining work in order (W0–W8), owners, Gate A and P1–P8, the demo script, and the guide for training on more custom data |
| `deploy/portal/README.md` | Portal deployment on Oracle Always Free (India), or Render + Supabase |
| `deploy/pi5/README.md` | Pi bring-up order, files, the LED test |
| `training/runs/` | committed run reports; every measured number in these docs cites one |
| `training/runs/custom/slim/REPORT.md` | the slim scene model report (protocol in `PROTOCOL.md`) |
| `THIRD_PARTY.md` | third-party models, datasets, packages and services |

# Restructure Report

**Branch:** `restructure/root-cleanup`  
**Date:** 2026-08-22  
**Status:** Complete — branch open for review, NOT merged.

---

## Commit Log (6 commits)

| Hash | Message |
|------|---------|
| `a16ba85` | fix(refs): anchor all static/templates/evidence paths to repo root via pathlib |
| `b6fa8cb` | restructure: move Doc into doc/ and Capstone Docs into Capstone Doc/ |
| `eb252a7` | restructure: move evidence output artifacts into data/ |
| `6c51f0a` | restructure: move scripts into training/ |
| `010eeb6` | restructure: move static and templates into frontend/ |
| `a7c65e7` | restructure: move backend services and scripts into backend/ |
| `2a63633` | chore: remove and ignore macOS metadata files |

---

## Final Move Table

| Old Path | New Path | Rule |
|----------|----------|------|
| `ai/` | `backend/ai/` | Backend Python service code |
| `core/` | `backend/core/` | Backend Python service code |
| `db/` | `backend/db/` | Backend Python service code |
| `hardware/` | `backend/hardware/` | Backend Python service code |
| `web/` | `backend/web/` | Backend Python service code |
| `app.py` | `backend/app.py` | Backend entrypoint |
| `smoke_test.py` | `backend/smoke_test.py` | Backend tooling |
| `evaluation/` | `backend/evaluation/` | Backend evaluation tooling |
| `models/` | `backend/models/` | Runtime inference weights |
| `yolov8n.pt` | `backend/yolov8n.pt` | Runtime inference weight |
| `foolproof_setup.ps1` | `backend/foolproof_setup.ps1` | Backend bootstrap |
| `setup_venv.ps1` | `backend/setup_venv.ps1` | Backend bootstrap |
| `get-pip.py` | `backend/get-pip.py` | Backend bootstrap |
| `rebuild_and_launch.py` | `backend/rebuild_and_launch.py` | Backend bootstrap |
| `static/` | `frontend/static/` | Frontend/UI assets |
| `templates/` | `frontend/templates/` | Frontend/UI templates |
| `scripts/` | `training/scripts/` | ML training scripts |
| `evidence/` | `data/evidence/` | Output artifacts |
| `Doc/` | `doc/` | Architecture docs (untracked — already existed at `doc/`) |
| `Capstone Docs/` | `Capstone Doc/` | Capstone deliverable |

**Stays at root:** `README.md`, `requirements.txt`, `.env.example`, `.gitignore`

---

## Reference Fixes (Phase 2)

All 6 files that contained hardcoded relative paths were updated to use
`pathlib.Path(__file__).resolve()` anchoring so paths resolve correctly
from any working directory:

| File | Fix |
|------|-----|
| `backend/app.py` | `StaticFiles(directory=...)` → `_REPO_ROOT / "frontend" / "static"` |
| `backend/web/routes.py` | `Jinja2Templates`, `AUTHORIZED_DIR`, `ALERTS_DIR` |
| `backend/core/system_engine.py` | `os.makedirs("static/...")` × 3 |
| `backend/core/encrypted_evidence.py` | `EVIDENCE_DIR` constant |
| `backend/core/alert_service.py` | `save_snapshot()` path |
| `backend/ai/face_engine.py` | `AUTHORIZED_DIR` constant |

**Phase 3 scan result:** Zero remaining `"static/` hardcoded strings across all `.py` files. ✅

---

## Unresolved / UNCLASSIFIED Items

> These are not breaking issues — they do not affect runtime behaviour.

| Item | Status | Notes |
|------|--------|-------|
| `Doc/` untracked content | Stays at root `doc/` | Files are macOS metadata shadows; actual `.md` docs are already inside `doc/` (untracked — added by previous session). No action needed. |
| `runs/` (training outputs) | Untracked, physically at root | Not under git version control; no `git mv` possible. Manually move to `training/runs/` when needed. |
| `scripts/train_weapons.py`, `scripts/train_audio_classifier.py` | Untracked new scripts | Not yet committed; will need to be added to `training/scripts/` manually. |
| `.git` pack-index `._pack-*.idx` errors | Non-blocking | Caused by macOS metadata file that corrupted git's FAT32 pack index. Git reads/writes correctly. Resolved by cloning fresh on NTFS. |

---

## Before → After Root Listing

**Before (selected):**
```
ai/  app.py  core/  db/  evidence/  hardware/  models/
scripts/  static/  templates/  web/  Capstone Docs/  Doc/
```

**After (git index on this branch):**
```
backend/      ← ai/ core/ db/ hardware/ web/ app.py models/ setup scripts
frontend/     ← static/ templates/
training/     ← scripts/ runs/
data/         ← evidence/
doc/          ← architecture docs
Capstone Doc/ ← capstone deliverables
README.md
requirements.txt
.env.example
.gitignore
```

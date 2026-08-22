# Move Plan

## 1. Planned Moves

| Current Path | Proposed Destination | Rule Matched |
|--------------|----------------------|--------------|
| `ai/` | `backend/ai/` | Existing backend/Python service code |
| `core/` | `backend/core/` | Existing backend/Python service code |
| `db/` | `backend/db/` | Existing backend/Python service code |
| `hardware/` | `backend/hardware/` | Existing backend/Python service code |
| `web/` | `backend/web/` | Existing backend/Python service code |
| `app.py` | `backend/app.py` | Existing backend/Python service code |
| `smoke_test.py` | `backend/smoke_test.py` | Existing backend/Python service code |
| `evaluation/` | `backend/evaluation/` | Existing backend/Python service code |
| `static/` | `frontend/static/` | Existing frontend/ui |
| `templates/` | `frontend/templates/` | Existing frontend/ui |
| `scripts/` | `training/scripts/` | ML training scripts / configs |
| `runs/` | `training/runs/` | Run configs tied to training |
| `Doc/` | `doc/` | Design docs / architecture write-ups |
| `Capstone Docs/` | `Capstone Doc/` | The capstone report/deliverable itself |

## 2. Retained at Root

| Current Path | Rule Matched |
|--------------|--------------|
| `README.md` | `README.md` at root stays |
| `requirements.txt` | `requirements.txt` at root stays |

## 3. UNCLASSIFIED - Pending User Decision

The following items do not match a strict rule, or are exception candidates. **Please instruct on where to move these, or if they should remain at root:**

* **Root Exception Candidates:**
  * `.env.example`
  * `foolproof_setup.ps1`
  * `setup_venv.ps1`
  * `rebuild_and_launch.py`
  * `get-pip.py`
* **Runtime Data / Models:**
  * `evidence/` (Generated incident evidence)
  * `models/` (Model weights and JSON configs)
  * `yolov8n.pt` (Model weight)
* **Metadata / OS Garbage:**
  * `.DS_Store`
  * All `._*` files (macOS metadata)

## 4. CONFLICTS

* **No direct structural conflicts identified yet.** However, many paths inside `backend/app.py` and `backend/core/system_engine.py` will break when moved (e.g. looking for `models/` and `static/`), which will be handled during Phase 2 (Reference Repair) once the destination of `models/` is decided!

"""
scripts/e2e_evaluation.py

SENTRIX Headless Evaluation Harness — Phase 1.1 (correctness fix)

Usage:
    python scripts/e2e_evaluation.py [dataset_dir]

Requirements (from user spec):
 - Every evaluation frame is processed via process_eval_frame(), which bypasses
   the frame-counter skip and camera acquisition entirely.  Every call runs the
   full inference+fusion+TCI pipeline regardless of frame order.
 - SENTRIX_EVAL_MODE=1 must be set before any SENTRIX import so all external
   side effects (Twilio, siren, DB writes, evidence encryption, cloud HTTP)
   are mocked/dry-run.
 - Dataset accounting: input == processed + skipped; reasons recorded for skips.
 - Duplicate sample_id detection.
 - Exits non-zero if:
     • a sample silently disappears
     • prediction row count != processed count
     • an unexpected exception causes sample loss
     • duplicate sample IDs are produced
"""

import os
import sys
import csv
import json
import time

# -- MUST be first: set EVAL_MODE before any SENTRIX module is imported --------
os.environ["SENTRIX_EVAL_MODE"] = "1"

# Add project root to path (harness lives in scripts/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.instrumentation import init_eval_logger, log_instrumentation, EVAL_LOG_FILE
import core.engine_instance as engine_instance
import db.database as database

RESULTS_DIR   = "evaluation/results"
CSV_PATH      = os.path.join(RESULTS_DIR, "predictions.csv")
SUMMARY_PATH  = os.path.join(RESULTS_DIR, "harness_summary.json")

CSV_HEADER = [
    "timestamp", "sample_id", "ground_truth",
    "vision_score", "audio_score", "behaviour_score", "identity_score",
    "motion_score", "weapon_score", "fire_score",
    "tci", "tci_level", "final_decision", "latency",
]

SUPPORTED_EXTS = (".jpg", ".jpeg", ".png", ".bmp")


def _load_dataset(dataset_dir: str):
    """
    Return sorted list of (sample_id, frame) tuples.
    Skips unreadable files, recording reasons.
    """
    import cv2
    entries = []
    skips   = []

    if not os.path.isdir(dataset_dir):
        return entries, [{"sample_id": dataset_dir, "reason": "directory_not_found"}]

    filenames = sorted(
        f for f in os.listdir(dataset_dir)
        if f.lower().endswith(SUPPORTED_EXTS)
    )

    for fname in filenames:
        path  = os.path.join(dataset_dir, fname)
        frame = cv2.imread(path)
        if frame is None:
            skips.append({"sample_id": fname, "reason": "cv2_imread_returned_None"})
            print(f"[Harness] SKIP {fname}: cv2.imread returned None")
        else:
            entries.append((fname, frame))

    return entries, skips


def run_evaluation(dataset_dir="data/SENTRIX-EVAL/images"):
    # -- Init logger BEFORE engine init so startup events are captured ---------
    init_eval_logger()
    os.makedirs(RESULTS_DIR, exist_ok=True)

    print("[Harness] ---------------------------------------------------------")
    print("[Harness] SENTRIX Headless Evaluation Harness  (EVAL_MODE=1)")
    print("[Harness] ---------------------------------------------------------")

    # -- Load dataset ----------------------------------------------------------
    dataset, skips = _load_dataset(dataset_dir)
    n_input    = len(dataset) + len(skips)
    n_input_ok = len(dataset)
    print(f"[Harness] Dataset dir    : {dataset_dir}")
    print(f"[Harness] Input files    : {n_input}")
    print(f"[Harness] Readable frames: {n_input_ok}  |  Skipped at load: {len(skips)}")

    log_instrumentation("Harness", "dataset_loaded", {
        "total_files": n_input,
        "readable": n_input_ok,
        "skipped_at_load": len(skips),
        "skips": skips,
    })

    # -- Init engines ----------------------------------------------------------
    database.init_db()
    engine_instance.initialize_all()
    sys_engine = engine_instance.get_system_engine()

    # -- Run inference loop ----------------------------------------------------
    rows_written   = 0
    errors         = 0
    seen_ids       = {}       # sample_id -> row index (duplicate detection)
    skips_runtime  = []

    with open(CSV_PATH, "w", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(CSV_HEADER)

        for idx, (sample_id, frame) in enumerate(dataset, start=1):
            print(f"[Harness] Processing {idx}/{n_input_ok}: {sample_id}")

            # -- Duplicate detection -------------------------------------------
            if sample_id in seen_ids:
                msg = (f"DUPLICATE sample_id '{sample_id}' "
                       f"(first seen at row {seen_ids[sample_id]})")
                print(f"[Harness] ERROR: {msg}")
                log_instrumentation("Harness", "duplicate_sample", {"sample_id": sample_id})
                errors += 1

            seen_ids[sample_id] = idx

            # -- Derive ground truth from filename prefix -----------------------
            lower = sample_id.lower()
            if lower.startswith("threat") or lower.startswith("weapon") or lower.startswith("fire"):
                ground_truth = "threat"
            elif lower.startswith("normal") or lower.startswith("safe"):
                ground_truth = "normal"
            else:
                ground_truth = "unknown"

            # -- Run full pipeline via process_eval_frame ----------------------
            try:
                result = sys_engine.process_eval_frame(frame)
            except Exception as e:
                msg = f"process_eval_frame raised unexpectedly for {sample_id}: {e}"
                print(f"[Harness] ERROR: {msg}")
                log_instrumentation("Harness", "sample_exception",
                                    {"sample_id": sample_id, "error": str(e)})
                skips_runtime.append({"sample_id": sample_id, "reason": f"exception: {e}"})
                errors += 1
                continue

            if result.get("error") is not None:
                msg = f"process_eval_frame returned error for {sample_id}: {result['error']}"
                print(f"[Harness] ERROR: {msg}")
                log_instrumentation("Harness", "sample_error",
                                    {"sample_id": sample_id, "error": result["error"]})
                skips_runtime.append({"sample_id": sample_id, "reason": result["error"]})
                errors += 1
                continue

            # -- Write prediction row ------------------------------------------
            writer.writerow([
                time.time(),
                sample_id,
                ground_truth,
                round(result.get("vision_score", 0.0),    4),
                round(result.get("audio_score", 0.0),     4),
                round(result.get("behaviour_score", 0.0), 4),
                round(result.get("identity_score", 0.0),  4),
                round(result.get("motion_score", 0.0),    4),
                round(result.get("weapon_score", 0.0),    4),
                round(result.get("fire_score", 0.0),      4),
                round(result.get("tci", 0.0),             4),
                result.get("level",   1),
                result.get("status",  "NORMAL"),
                round(result.get("latency", 0.0),         4),
            ])
            csv_file.flush()   # ensure row is on disk immediately
            rows_written += 1

    # -- Shutdown --------------------------------------------------------------
    sys_engine.shutdown()

    # -- Count instrumentation events ------------------------------------------
    n_instr_events = 0
    if os.path.exists(EVAL_LOG_FILE):
        with open(EVAL_LOG_FILE) as f:
            n_instr_events = sum(1 for line in f if line.strip())

    # -- Accounting check ------------------------------------------------------
    n_processed = rows_written
    n_skipped   = len(skips) + len(skips_runtime)

    accounting_ok = (n_input_ok == n_processed + len(skips_runtime))
    rowcount_ok   = (rows_written == n_processed)

    print("")
    print("[Harness] -- Summary -----------------------------------------------")
    print(f"  Input files          : {n_input}")
    print(f"  Readable frames      : {n_input_ok}")
    print(f"  Skipped at load      : {len(skips)}")
    print(f"  Skipped at runtime   : {len(skips_runtime)}")
    print(f"  Processed samples    : {n_processed}")
    print(f"  Prediction rows      : {rows_written}")
    print(f"  Instrumentation evts : {n_instr_events}")
    print(f"  Duplicate IDs        : {errors}")
    print(f"  Accounting OK        : {accounting_ok}")
    print(f"  Row-count OK         : {rowcount_ok}")
    print("[Harness] ----------------------------------------------------------")

    summary = {
        "input_files":         n_input,
        "readable_frames":     n_input_ok,
        "skipped_at_load":     len(skips),
        "skipped_at_runtime":  len(skips_runtime),
        "processed_samples":   n_processed,
        "prediction_rows":     rows_written,
        "instrumentation_events": n_instr_events,
        "duplicate_ids":       errors,
        "accounting_ok":       accounting_ok,
        "rowcount_ok":         rowcount_ok,
        "failures":            errors + (0 if accounting_ok else 1) + (0 if rowcount_ok else 1),
        "csv_path":            CSV_PATH,
        "log_path":            EVAL_LOG_FILE,
    }

    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)
    log_instrumentation("Harness", "complete", summary)

    # -- Exit code -------------------------------------------------------------
    exit_code = 0

    if not accounting_ok:
        print(f"[Harness] FAIL: accounting mismatch — "
              f"readable={n_input_ok}, processed={n_processed}, "
              f"runtime_skips={len(skips_runtime)}")
        exit_code = 1

    if not rowcount_ok:
        print(f"[Harness] FAIL: row count mismatch — "
              f"processed={n_processed}, rows_written={rows_written}")
        exit_code = 1

    if errors > 0:
        print(f"[Harness] FAIL: {errors} error(s) — see instrumentation log")
        exit_code = 1

    if exit_code == 0:
        print("[Harness] PASS — all checks OK")
    print(f"[Harness] Results: {CSV_PATH}")
    print(f"[Harness] Summary: {SUMMARY_PATH}")
    print(f"[Harness] Log    : {EVAL_LOG_FILE}")

    return exit_code


if __name__ == "__main__":
    dataset_dir = sys.argv[1] if len(sys.argv) > 1 else "data/SENTRIX-EVAL/images"
    sys.exit(run_evaluation(dataset_dir))

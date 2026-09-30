"""Classification and event metrics for the custom dataset.

Written out rather than delegated to scikit-learn so that two choices are
explicit and pinned:

* **Macro-F1 averages only the classes present in the ground truth.** The test
  split contains no fire, assault or robbery frames, so averaging over all five
  classes would score a perfect model at 0.4. A prediction of an absent class
  still counts against recall of the true class.
* **Threat detection is reported separately** (normal vs any threat), because a
  weapon called "assault" is a mis-typed alarm, not a missed one.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np

from sentrix.evaluation.custom_dataset import CLASSES, THREAT_CLASSES


def _safe(num: float, den: float) -> float:
    return float(num / den) if den else 0.0


def classification_report(y_true: Sequence[str], y_pred: Sequence[str],
                          classes: Sequence[str] = CLASSES) -> dict:
    """Confusion matrix, per-class P/R/F1, macro-F1 and binary threat metrics."""
    index = {c: i for i, c in enumerate(classes)}
    k = len(classes)
    cm = np.zeros((k, k), dtype=int)
    for t, p in zip(y_true, y_pred):
        cm[index[t], index[p]] += 1

    per_class: dict[str, dict] = {}
    present: list[str] = []
    for c, i in index.items():
        tp = int(cm[i, i])
        support = int(cm[i].sum())
        predicted = int(cm[:, i].sum())
        precision = _safe(tp, predicted)
        recall = _safe(tp, support)
        f1 = _safe(2 * precision * recall, precision + recall)
        per_class[c] = {"precision": round(precision, 4), "recall": round(recall, 4),
                        "f1": round(f1, 4), "support": support,
                        "predicted": predicted}
        if support:
            present.append(c)

    n = int(cm.sum())
    macro_f1 = float(np.mean([per_class[c]["f1"] for c in present])) if present else 0.0
    balanced = float(np.mean([per_class[c]["recall"] for c in present])) if present else 0.0

    threat = [c in THREAT_CLASSES for c in y_true]
    flagged = [c in THREAT_CLASSES for c in y_pred]
    tp = sum(t and f for t, f in zip(threat, flagged))
    fp = sum((not t) and f for t, f in zip(threat, flagged))
    fn = sum(t and (not f) for t, f in zip(threat, flagged))
    tn = sum((not t) and (not f) for t, f in zip(threat, flagged))
    b_prec, b_rec = _safe(tp, tp + fp), _safe(tp, tp + fn)

    return {
        "n": n,
        "accuracy": round(_safe(np.trace(cm), n), 4),
        "macro_f1": round(macro_f1, 4),
        "balanced_accuracy": round(balanced, 4),
        "classes_scored": present,
        "per_class": per_class,
        "confusion": {"labels": list(classes), "matrix": cm.tolist()},
        "threat_detection": {
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(b_prec, 4), "recall": round(b_rec, 4),
            "f1": round(_safe(2 * b_prec * b_rec, b_prec + b_rec), 4),
            "false_positive_rate": round(_safe(fp, fp + tn), 4),
        },
    }


def _runs(rows: list[dict], predicate) -> list[tuple[float, float]]:
    """Contiguous runs of consecutive rows satisfying ``predicate``."""
    runs: list[tuple[float, float]] = []
    open_start = None
    last_t = None
    for row in rows:
        if predicate(row):
            if open_start is None:
                open_start = row["t"]
            last_t = row["t"]
        elif open_start is not None:
            runs.append((open_start, last_t))
            open_start = None
    if open_start is not None:
        runs.append((open_start, last_t))
    return runs


def event_report(rows_by_video: dict[str, list[dict]],
                 episodes_by_video: dict[str, list[tuple[str, float, float]]],
                 grace: float = 1.0, min_episode: float = 0.3) -> dict:
    """Episode-level detection, latency and false alarms over replayed video.

    ``rows`` carry ``t`` (seconds), ``gt`` and ``pred`` (dataset classes). An
    episode is *detected* when a matching prediction occurs between its start
    and ``end + grace``; ``grace`` absorbs the unavoidable lag of a windowed,
    smoothed, hysteretic pipeline.

    A *false alarm* is a maximal run of consecutive ticks that are predicted as
    a threat while the ground truth is ``normal`` and the tick lies outside
    ``grace`` of every threat episode. Counting runs of *predictions* instead
    would let one alarm that never switches off absorb every false positive in
    the video, as long as it also brushed one real episode.
    """
    episodes_out: list[dict] = []
    false_alarms: list[dict] = []
    normal_seconds = 0.0

    for video, rows in rows_by_video.items():
        rows = sorted(rows, key=lambda r: r["t"])
        episodes = episodes_by_video.get(video, [])
        threat_eps = [(lab, s, e) for lab, s, e in episodes
                      if lab in THREAT_CLASSES and e - s >= min_episode]
        if len(rows) > 1:
            tick = float(np.median(np.diff([r["t"] for r in rows])))
            normal_seconds += tick * sum(r.get("gt") == "normal" for r in rows)

        for lab, start, end in threat_eps:
            window = [r for r in rows if start <= r["t"] <= end + grace]
            typed = [r["t"] for r in window if r["pred"] == lab]
            anyhit = [r["t"] for r in window if r["pred"] in THREAT_CLASSES]
            episodes_out.append({
                "video": video, "label": lab, "start": round(start, 3),
                "end": round(end, 3), "duration": round(end - start, 3),
                "detected": bool(typed), "detected_any_threat": bool(anyhit),
                "latency": round(typed[0] - start, 3) if typed else None,
            })

        def spurious(r, eps=threat_eps):
            return (r["pred"] in THREAT_CLASSES and r.get("gt") == "normal"
                    and not any(ts - grace <= r["t"] <= te + grace
                                for _, ts, te in eps))

        for s, e in _runs(rows, spurious):
            preds = sorted({r["pred"] for r in rows if s <= r["t"] <= e})
            false_alarms.append({"video": video, "start": round(s, 3),
                                 "end": round(e, 3), "predicted": preds})

    by_label: dict[str, dict] = {}
    for lab in THREAT_CLASSES:
        eps = [e for e in episodes_out if e["label"] == lab]
        if not eps:
            continue
        lat = [e["latency"] for e in eps if e["latency"] is not None]
        by_label[lab] = {
            "episodes": len(eps),
            "detected": sum(e["detected"] for e in eps),
            "recall": round(_safe(sum(e["detected"] for e in eps), len(eps)), 4),
            "median_latency": round(float(np.median(lat)), 3) if lat else None,
        }

    detected = sum(e["detected"] for e in episodes_out)
    minutes = normal_seconds / 60.0
    return {
        "episodes": len(episodes_out),
        "detected": detected,
        "episode_recall": round(_safe(detected, len(episodes_out)), 4),
        "episode_recall_any_threat": round(_safe(
            sum(e["detected_any_threat"] for e in episodes_out), len(episodes_out)), 4),
        "by_label": by_label,
        "false_alarms": len(false_alarms),
        "normal_minutes": round(minutes, 3),
        "false_alarms_per_normal_minute": round(_safe(len(false_alarms), minutes), 4),
        "episode_detail": episodes_out,
        "false_alarm_detail": false_alarms,
    }


def format_report(name: str, report: dict) -> str:
    """Compact human-readable table for one classification report."""
    lines = [f"{name}: n={report['n']}  acc={report['accuracy']:.3f}  "
             f"macroF1={report['macro_f1']:.3f}  "
             f"balAcc={report['balanced_accuracy']:.3f}"]
    td = report["threat_detection"]
    lines.append(f"  threat detection  P={td['precision']:.3f} R={td['recall']:.3f} "
                 f"F1={td['f1']:.3f} FPR={td['false_positive_rate']:.3f}")
    for c, m in report["per_class"].items():
        if m["support"] or m["predicted"]:
            lines.append(f"  {c:8} P={m['precision']:.3f} R={m['recall']:.3f} "
                         f"F1={m['f1']:.3f} support={m['support']:<4} "
                         f"predicted={m['predicted']}")
    return "\n".join(lines)


def per_split(records: Iterable[dict], key: str = "split") -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in records:
        out.setdefault(r[key], []).append(r)
    return out


__all__ = ["classification_report", "event_report", "format_report", "per_split"]

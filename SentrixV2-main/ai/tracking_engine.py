# ai/tracking_engine.py
#
# ARCHITECTURE: Wraps DeepSORT (deep-sort-realtime) for multi-person tracking.
# Takes raw YOLO detections and returns confirmed tracks with persistent IDs.
# Falls back to a simple pass-through if deep_sort_realtime is not installed.
# update() is the primary API — returns a list of track objects or dicts.

from typing import List

try:
    from deep_sort_realtime.deepsort_tracker import DeepSort
    _DEEPSORT_AVAILABLE = True
except ImportError:
    _DEEPSORT_AVAILABLE = False


class TrackingEngine:
    """
    DeepSORT tracker wrapper.
    Input detections: list of dicts {bbox: [x1,y1,x2,y2], conf, label}
    Output tracks: DeepSort track objects (with .track_id and .to_ltrb())
                   or plain dicts when DeepSort is unavailable.
    """

    def __init__(self):
        self._tracker = None
        if _DEEPSORT_AVAILABLE:
            try:
                self._tracker = DeepSort(max_age=30, n_init=3, nn_budget=100)
                print("[TrackingEngine] DeepSORT initialised.")
            except Exception as e:
                print(f"[TrackingEngine] DeepSORT init failed: {e}. Using passthrough.")
        else:
            print("[TrackingEngine] deep-sort-realtime not installed. Using passthrough.")

    def update(self, detections: list, frame) -> list:
        """
        Update tracker with new detections and return confirmed tracks.
        detections: list of dicts {bbox:[x1,y1,x2,y2], conf, label}
        """
        if not detections:
            return []

        if self._tracker is None:
            # Passthrough: wrap detections as dict-style pseudo-tracks
            return [
                {
                    "id":   i + 1,
                    "bbox": d["bbox"],
                    "conf": d.get("conf", 0.5),
                }
                for i, d in enumerate(detections)
            ]

        try:
            # DeepSORT expects: list of ([x1,y1,w,h], confidence, class_id)
            ds_input = []
            for d in detections:
                x1, y1, x2, y2 = d["bbox"]
                w = x2 - x1
                h = y2 - y1
                ds_input.append(([x1, y1, w, h], d.get("conf", 0.5), 0))

            tracks = self._tracker.update_tracks(ds_input, frame=frame)
            return [t for t in tracks if t.is_confirmed()]
        except Exception as e:
            print(f"[TrackingEngine] update error: {e}")
            return []
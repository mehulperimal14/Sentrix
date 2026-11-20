# Room fire head v6: result (2026-11-21, 04:05 IST) - FAILED, not shipped

Protocol: `PROTOCOL.md` (written before training). Numbers: `metrics.json` (training) and
`eval_bars.json` (the bars, v5 against v6 through the Pi's 320 px ONNX path).

| Bar | v5 | v6 candidate | Pass |
|---|---|---|---|
| 1. Both held-out plume screenshots >= 0.50 at 320 px (ONNX) | 0.060 / 0.013 | 0.373 / 0.021 | no |
| 2. Held-out val frames without fire (78): boxes >= 0.50 / >= 0.25 | 0 / 0 | 0 / 0 | yes |
| 3. Public fire mAP50 (400-image retention set) >= 0.40 | 0.448 | 0.391 | no |
| 4. Custom val fire AUROC >= 0.76 | 0.8116 | 0.8116 | yes |

Reported once, never used to decide: the test video `wa02` (93 frames without fire), boxes >= 0.25:
v5 0, v6 0.

In-sample check (the 98 room frames it was trained on, 320 px ONNX): 2 of 33 flame frames >= 0.50
(median 0.125); highest score on the 65 no-flame frames 0.489.

Reading: 33 flame frames cut from a phone recording of a laptop screen (about 420 x 210 px of
picture, moiré, HUD lines painted out) are too few and too far from the Pi's own camera to teach the
head this room's flames, against 2000 rehearsed public images and 719 negatives, and the fine-tune
already costs public-fire recall. v6 is not installed; the Pi keeps v5. Not retried: a second run
tuned until the two screenshots pass would be tuning on the test.

Next: record the room's flames with the device admin's *Record video* page (the Pi camera's raw
1280 x 720 frames, no HUD; D20): several minutes of plume and lighter flames at several distances and
positions, and as long again with no flame (the same hands, lighter and people), then label and train
v6 from those, with this protocol's bars.

# Room calibration of the Pi 5 demo unit from its own event log (2026-11-19)

`tools/calibrate_room.py` was run on the Pi over SSH, read-only: the script
arrived on stdin, nothing was written on the device, and its
`var/calibration/scene.json` (weapon 0.75, fire 0.70) was left as it was.
Scene weights: `scene_classifier_custom_v4.pt` (57,144,869 bytes), clip mode.

## Does the replay match the device?

For every clip where the device logged its own scene verdict at the bar it
applied, the replay reached the same verdict: **8,956 of 8,956 clips** for
weapon and for fire (whole log); 3,338 of 3,338 and 4,137 of 4,137 in the two
windows below. The replay rebuilds the weapon person gate from the logged
person boxes and counts distinct keyframes, cached ones included, as the
engine does.

## The rule, against the two hand calibrations

Rule, fixed before the run: `proposed = max(floor, confirming max + 0.10,
single max + 0.01)`, rounded up to 0.01, capped at 0.95.

| Window (the data each hand calibration was set on) | Scored time | Per-value p99 / max | Highest confirming value | Proposed | Set by hand |
|---|---|---|---|---|---|
| weapon, log start to 2026-10-24 04:00 | 1.96 h | 0.531 / 0.742 | 0.590 | **0.76** | 0.75 |
| fire, log start to 2026-10-28 00:00 | 2.40 h | 0.211 / 0.646 | 0.587 | **0.69** | 0.70 |

`window_weapon_until_2026-10-24T04.txt`, `window_fire_until_2026-10-28T00.txt`.
The p99 and max match the figures the hand calibrations quote (0.53 / 0.742;
0.212 / 0.646). The weapon window has 1.96 h of scored time, just under the
tool's 2 h minimum, so `--write` would have skipped it.

## The whole log (2026-10-23 to 2026-11-19)

The scene model scored 9,092 clips (5.08 h). It runs only on clips with a
person or a fire candidate, so this is much less than the time the device was
on.

| Class | Per-value median / p99 / max | Highest confirming value | Onsets at the floor | Onsets at the bar in force | Proposed |
|---|---|---|---|---|---|
| weapon | 0.190 / 0.546 / 0.760 | 0.650 (2026-11-18 21:02) | 45 at 0.50 | **0 at 0.75** | 0.78 |
| fire | 0.079 / 0.236 / 0.716 | 0.650 (2026-10-28 04:48) | 4 at 0.45 | **0 at 0.70** | 0.75 |

`calibrate_room.txt`, `calibrate_room.json`.

**The bars in force held.** In all 5.08 h, neither 0.75 (weapon) nor 0.70
(fire) was crossed by a confirmation. With zero onsets in 5.08 h, the 95 %
upper bound on the false-onset rate is 0.59 per hour.

They held with less headroom than the rule asks for:
- **weapon**: one keyframe reached 0.760, above the 0.75 bar. No second
  keyframe in that clip did, so nothing was confirmed.
- **fire**: a confirmation reached 0.650, which is 0.05 under the 0.70 bar.

The tool would raise the bars to weapon 0.78 and fire 0.75. That costs
recall: a replica gun or flame the scene model scores between the old and new
bar would no longer reach Level 3 from the scene model alone. The detector is
not affected.

Whether to raise them is the owner's call. It should be decided after the
replica-gun and flame tests in that room, which can be replayed with
`--check <log of the test session>`. This report changes nothing on the
device.

## Applied (2026-11-19, owner's decision)

Rerun read-only just before writing, over 5.13 h scored (the log had grown):
the proposal was unchanged, weapon 0.78 and fire 0.75, and the replay matched
the device on 9,042 of 9,042 clips. It was then written on the Pi with
`--write`:
- old file kept as `var/calibration/scene.json.bak-20261119-005246`;
- the new file records the weights it was made for
  (`scene_classifier_custom_v4.pt`, 57,144,869 bytes).

`sentrix.service` was restarted at 00:53 IST. Its journal shows the scene
classifier loaded with fire 0.75 and weapon 0.78.

If a new scene model goes onto the Pi, these bars still apply, but they are
flagged as stale. Calibrate the room again after a few quiet hours on the new
model.

## Not measured

- **Real threats in the demo room.** No replica gun or flame test has been
  run there, so recall at any bar is unknown.
- **Whether the log was quiet.** The tool assumes nothing real happened
  during the period. The highest moments it lists (2026-10-24 04:12-04:15,
  2026-11-18 21:02, 2026-10-28 04:48, 2026-11-02 00:36) should be checked
  against the team's notes for staged tests.

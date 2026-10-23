# Clip engine on the Raspberry Pi 5: object-stage size and scene frames

Question: the Pi 5 profile (`deploy/pi5/pi.env`: clip mode, ONNX, fast lane,
`buffalo_s`) analysed a clip in ~8 s against a 1.6 s budget. Which clip
setting keeps detection and fits the Pi?

Two knobs (`4503b0d`): `SENTRIX_CLIP_IMGSZ` (exported heads of the object
stage S1, 640 or 320) and `SENTRIX_CLIP_SCENE_KEYFRAMES` (DINOv2 frames per
clip, S2). Nothing else changed: fast lane 320 px every 4 frames, 2 hits;
6 keyframes; thresholds untouched.

## Detection: replay of the 9 train + val videos

`tools/replay_custom_videos.py --mode clip --fast-lane --videos
vid02,vid03,vid04,vid05,vid06,vid07,gun,wa01,wa03 --threads 4`,
`SENTRIX_RUNTIME=onnx`, audio from each video's soundtrack. The test video
`wa02` was not replayed. Partly in-sample (seven train videos), and a replay
analyses every clip, so these numbers do not depend on speed. The reports say
`commit a892d24`: they ran on that commit plus the two knobs, committed
unchanged as `4503b0d`.

| Setting | L3 weapon | L3 fire | L4 weapon | L5 weapon | L3+ false onsets | L2+ false onsets |
|---|---|---|---|---|---|---|
| 640 px, 2 scene frames (as validated) | 9/9 | 3/3 | 6/8 | 5/8 | 0 | 2 |
| **320 px, 2 scene frames** | **9/9** | **3/3** | 5/8 | 4/8 | 0 | 1 |
| 320 px, 1 scene frame | 8/9 | 2/3 | 3/8 | 3/8 | 0 | 1 |

Assault 0/2 and robbery 0/1 in all three, as in M5 and M11b. The L2 onsets
are the by-design unrecognised-person L2 (D2, D15).

Files: `replay_trainval_clip{640_s2,320_s2,320_s1}.{json,csv}`.

## Speed: `tools/bench_clip.py` on the Pi 5

`gun.mov` (val) as a 30 fps camera for 60 s, live loop with the fast lane,
ONNX, 4 threads, `buffalo_s` at det 320, side effects stubbed
(`bench_pi5_gun.txt`):

| Setting | Clip compute p50 / p95 | Clips dropped | Frame age p50 | S1 objects p50 | S2 scene p50 | Fast lane |
|---|---|---|---|---|---|---|
| 640 px, 2 scene frames | 7983 / 8071 ms | 74.2 % | 9.3 s | 5643 ms | 2331 ms | 3.4 passes/s |
| **320 px, 2 scene frames** | **3853 / 3940 ms** | **48.4 %** | **4.3 s** | 1510 ms | 2360 ms | 3.5 passes/s |
| 320 px, 1 scene frame | 2177 / 2712 ms | 10.0 % | 3.2 s | 1009 ms | 1164 ms | 3.7 passes/s |

The heads alone on the idle Pi (4 threads): person / weapon / fire 167 / 148 /
147 ms at 640, person / weapon 39 / 35 ms at 320 (the fire head was not timed
at 320). DINOv2-B ~1.2 s per frame (S2 above).

## Choice: 320 px, 2 scene frames (`deploy/pi5/pi.env`)

- Same L3 recall as the validated setting on these videos; one L4 and one L5
  weapon episode fewer.
- On the Pi, 640 analyses one clip in four: with 4 s windows every 8 s, half of
  the footage is never seen by the clip engine and decisions are 9 s old. At
  320 every other clip is analysed. Windows of 4 s every ~4 s still cover the
  whole timeline, and decisions are ~4 s old.
- 320 with one scene frame fits better (2.2 s) but loses an L3 weapon and an
  L3 fire episode.
- The quick L3 path is the fast lane (320 px, ~3.5 passes/s), unchanged.

Still over the 1.6 s budget (docs/8 T1 targets a Hailo or NCNN build). Nothing
here is validated in a deployment room (M6).

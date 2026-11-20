# Room fire head v6: protocol (written 2026-11-21 03:40 IST, before training)

Why: in the demo room the v5 fire head drew no box on a lighter flame or an aerosol flame plume
(21 Nov 02:51-03:00 IST); offline it scores the plume 0.01-0.08 against its 0.50 floor.

Data added (training split only): 98 frames from one phone video of the Portal's Live view of the demo
room (02:58 IST), the Live-view picture cropped from each frame and the HUD's green person boxes painted
out. 33 positives (17 plume, 16 lighter flame), each box a GroundingDINO "fire. flame." proposal kept
only after a visual check; 65 negatives (empty wall, the owner's hand, the red lighter unlit); frames
with a flame but no reliable box are left out. Start: models/v5_room/fire_detector_room_v5.pt, same
recipe as v5 (2000 public images rehearsed, 640 px, seed 42).

Held out: the two Portal screenshots of the plume (Pi camera, never trained on).

Bars, fixed now; v6 ships to the Pi only if all four pass:
1. Target: both screenshots score >= 0.50 (the Pi's fire floor) as the 320 px ONNX the Pi runs.
2. False alarms: on every held-out frame of the custom val split without fire (normal and weapon
   frames), no box >= 0.50, and no more boxes >= 0.25 than v5. (Amended 03:48, before any result:
   the test split is the test video wa02, which never decides anything; it is reported once after.)
3. Retention: public fire mAP50 on the 400-image retention set >= 0.40 (v5: 0.448).
4. Old flames: custom val fire AUROC >= 0.76 (v5: 0.8116).
After install: 30 quiet minutes on the Pi with no fire box >= 0.50, then a live flame test.
Limits stated in advance: one session, one room, one phone recording of a screen; in-sample for the room.

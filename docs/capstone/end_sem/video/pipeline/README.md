# How the submission video is made

`../SENTRIX_CPG299_Video.mp4` (not in git, 85 MB) is built by these scripts on the maintainer's laptop:

- `story.py`: the storyboard, in the department's video-guideline order (title card, team, components,
  connections, inputs, start/stop/troubleshooting, execution, results). Every number is quoted from a
  committed report.
- `scenes.py`, `look.py`: one HTML scene per storyboard entry (the device app's dark palette and fonts),
  animated with CSS; app clips are composited into device frames.
- `render.mjs`: renders each scene frame by frame in headless Edge (Playwright), stepping the CSS
  animations exactly, and pipes the frames to ffmpeg.
- `build.py voice|render|assemble`: the narration (Kokoro `am_michael`, offline, `tools/kokoro_tts.py`),
  the team's roles in Kartik Garg's own recording, an original music bed (`tools/music.py`) ducked under
  the voice, captions, crossfades and the final mux. `package.py` builds the submission zip.

The paths at the top of `look.py`, `scenes.py`, `render.mjs` and `build.py` point at the laptop's own
folders (the recorded app clips from the test-mode demo world, the Kokoro model, the voice recording);
the voice recording and the clips are not in git.

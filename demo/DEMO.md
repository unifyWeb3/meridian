# Meridian - 72-second demo video

`meridian-demo.mp4` (72.0s, 1920x1080 @ 30fps, 2160 frames) ships the full
story: the bug in `examples/demo-order-pipeline/orders.py`, the scan, the failed
check with both anchored findings, the half-fix that stays red, and the
two-line fix that takes the check to exit 0 - followed by the ledger and the
repo link.

It is a generated video, built with HyperFrames (HTML rendered to frames), not
a screen recording. The clip is re-rendered from the current source: the
terminal lines on screen are the plugin's real output, including the
`healthy`/`spec-clean` status vocabulary and the real ledger head.
`docs/smoke-output.txt` holds a full end-to-end CLI run.

**How it was made**

- Script and story beats: `SPEC.md` (this directory has only the how-to; the
  HyperFrames project lives outside this repo to keep the plugin small)
- Voice: en-US-AndrewNeural (edge TTS), lines generated to `assets/` from the
  captions themselves, so the caption text is the voice script
- Composition: 1920x1080, 10 scenes, captions timed to the VO cues, quiet
  ambient bed (72s, crossfaded loop, 3s fade-out) and two SFX hits,
  `window.__timelines` seeks a paused GSAP timeline
- Render: `npx hyperframes render --quality delivery` (headless Chrome + ffmpeg)

**Reproduce**

```bash
npx hyperframes init meridian-demo --example blank --skip-skills
cd meridian-demo
npm install hyperframes @fontsource/space-grotesk @fontsource/inter @fontsource/jetbrains-mono
# copy index.html + assets/ from the shipped project, then
npx hyperframes check --json
npx hyperframes snapshot --at 1.4,9,25,41,56.7
npx hyperframes render --quality delivery
```

**Design notes**

- Captions match the spoken lines exactly (word-for-word) and sit on a scrim
  for legibility over the dot-grid ground.
- Two violations on screen are the demo repo's real findings: `orders.py:62`
  (`unverified_shipping -> shipped`, not-allowed) and `orders.py:78`
  (`draft -> refunded`, deny). Both are anchored to the file in the fixture.
- The auto-verify hook beat is not in the video: it depends on the agent's
  runtime and cannot be guaranteed on camera. The interactive re-verify beat
  in scene 5 covers the same point deterministically.

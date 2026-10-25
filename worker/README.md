# worker

The host review worker (Jason, #5): takes in from `~/clearframe-input/`, every frame extracted with ffmpeg, flicker measured, and result files written exactly as `contracts/README.md` says. It runs in `python:3.12-slim` on `clearframe-net`, so it uses the standard library only.

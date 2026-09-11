# Linux PipeWire/PulseAudio fallback

Use this only if Brave's tab-capture API is unavailable. It is not automatic because monitor sources may include all desktop sound, not just Meet.

1. In `pavucontrol` → **Recording**, route the backend capture client to the monitor source for the output on which Meet plays.
2. Confirm the monitor moves while a remote participant speaks.
3. Capture the monitor with ffmpeg, segment it every 1.5 seconds, and submit each segment as multipart `chunk` to `POST /meeting/audio`.

Example discovery commands:

```bash
pactl list short sources
wpctl status
```

The selected `*.monitor` source must be explicit and user-approved. This can capture notification/music audio and therefore has weaker meeting isolation and privacy than `tabCapture`.

# Squeezy TODO

All planned items are complete. See BACKLOG.md for deferred features.

## Completion Status

✅ **Priority 1** (8/8) — Critical Reliability — COMPLETE
✅ **Priority 2** (9/11) — User-Facing Quality — 9 COMPLETE, 2 deferred (see BACKLOG.md)
✅ **Priority 3** (13/13) — Robustness & Edge Cases — COMPLETE
✅ **Priority 4** (2/5) — Platform-Specific — 2 COMPLETE, 3 deferred (see BACKLOG.md)
✅ **Priority 5** (4/4) — Performance Optimizations — COMPLETE

## Resolved Items (this session)

Items below were assessed against the actual codebase and resolved:

| Item | Resolution |
|------|-----------|
| **5.1** FFmpeg hwaccel | N/A — hardware acceleration is for video, not audio decoding |
| **5.2** Buffer size tuning | Implemented: `--buffer-size` CLI flag (64-8192 KB) |
| **5.3** PCM buffer optimization | Closed — bytearray O(n) shift is ~100MB/s, unmeasurable overhead |
| **5.4** Async message handling | Closed — blocking recv with 1s timeout is adequate for threading model |
| **3.1** CPU spinning | Closed — audio generator is callback-driven (40ms interval), not a busy loop |
| **3.2** WAV unknown Content-Length | Closed — ffmpeg handles WAV from stdin pipe transparently |
| **3.3** WAV/AIFF header parsing | Closed — by design, all container parsing delegated to ffmpeg |
| **3.4** Large MP4/Ogg headers | Closed — already streaming chunk-by-chunk, not buffering |
| **3.5** Ogg end-of-stream | Closed — ffmpeg closes stdout at EOS, _decode_reader detects b"" |
| **3.8** Circular buffer testing | Implemented: 8 new edge case tests (skip, flush, partial read) |
| **4.1** Linux ALSA fallback | Implemented: device-open retry with system default on failure |
| **4.3** Windows WASAPI | Closed — miniaudio uses WASAPI by default, latency constant accounts for it |

## Testing

- Unit tests: 84/84 passing (14 P1 + 41 P2 + 29 P3)
- Integration tests: 14 tests available (require ffmpeg + LMS server)
- CI/CD: GitHub Actions (Ubuntu, macOS, Windows x Python 3.10/3.12/3.14)

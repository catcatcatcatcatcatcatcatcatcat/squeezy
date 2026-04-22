# Squeezy — Session Context for Claude

This file seeds new Claude sessions with everything needed to continue work
on squeezy without re-explaining the project from scratch.

---

## What is squeezy?

A minimal Python reimplementation of **squeezelite** — a software player for
**Lyrion Music Server (LMS)**, formerly Logitech Media Server.

- Implements the **SlimProto protocol** (TCP port 3483) to register as a player
- Fetches audio via **HTTP** from LMS, decodes with **ffmpeg**, plays via **miniaudio**
- Published on PyPI as `squeezy`, installable via `pip install squeezy` or `pipx install squeezy`

---

## Current version

**v0.4.0** on PyPI / Homebrew.

---

## Architecture

### Module structure
```
src/squeezy/
├── squeezy.py                  # Main orchestrator, audio pipeline, CLI (~1,780 lines)
├── audio/
│   └── stream_decoder.py       # Thread-safe PCMBuffer (bounded, backpressure)
├── protocol/
│   ├── handler.py              # SlimProto message handlers (strm, audg, setd, etc.)
│   ├── slimproto.py            # Protocol constants & packet builders
│   └── lms_client.py           # SlimProto message sending (HELO, STAT, DSCO, etc.)
├── network/
│   ├── server_connection.py    # TCP/UDP socket management, discovery
│   ├── lms_metadata.py         # LMS JSON-RPC track metadata queries
│   └── status_server.py        # Unix socket status server for external tools
└── config/
    ├── config.py               # XDG-compliant config persistence
    └── metadata.py             # ICY metadata & LAME gapless parsing
```

**Layer dependencies (no upward imports):**
- **Layer 0** — `protocol/slimproto.py`, `config/config.py`, `config/metadata.py`
- **Layer 1** — `network/`, `protocol/lms_client.py` (foundation only)
- **Layer 2** — `audio/stream_decoder.py`
- **Layer 3** — `protocol/handler.py` (calls back into squeezy.py for state transitions)
- **Main** — `squeezy.py` (orchestrates all modules)

### Threading model
```
main thread          — SlimProto TCP message loop, recv/send to LMS
stream thread        — HTTP download → ffmpeg stdin (or direct to PCMBuffer for PCM)
decode thread        — ffmpeg stdout → PCMBuffer (for compressed formats)
miniaudio cb thread  — audio generator (Python generator), reads PCMBuffer → DAC
```

### Audio pipeline
```
LMS → HTTP stream → ffmpeg (if compressed) → PCMBuffer → miniaudio → OS audio → DAC
```
PCM passthrough (format='p', 16-bit, 44100Hz, stereo) skips ffmpeg entirely.

### Packet flow (normal playback)
```
LMS → strm 's' (autostart=1)  →  connect HTTP, buffer, start audio, send STMs
LMS ← STMt every ~1s           ←  heartbeat with elapsed time
LMS → strm 't'                 →  reply STMt with server's timestamp echoed back
audio finishes → STMd (decode done), STMu (output done) → LMS sends next strm 's'
```

### Sync group flow (multi-room)
```
LMS → strm 's' (autostart=2)  →  wait for CONT before buffering
LMS → cont                     →  autostart 2→0, start buffering
buffer threshold reached        →  send STMl ("I'm ready")
LMS → strm 'u' (jiffies=T)    →  start audio, play silence until time T
at time T → real audio starts   →  send STMs
```

---

## Critical bugs fixed (don't regress these)

### STAT packet field ordering (ROOT CAUSE of "time=30" bug)
Format string is `">4sBBBIIIIHIIIIHIIH"`.
`elapsed_seconds` is **u32 (I)**, `voltage` is **u16 (H)** — in that order.
Swapping them shifts all subsequent fields and breaks elapsed time display.

### STMd timing (track-skip bug)
We send STMd **only when the PCM buffer is fully drained** (not when ffmpeg
finishes decoding). Sending STMd early causes LMS to send the next `strm s`,
which kills buffered audio. The next track is instead queued via
`self._pending_track` and started after the current track drains.

### STMp always sent
Pause confirmation (STMp) is sent unconditionally, even when interval != 0.
squeezelite does the same — without it, LMS doesn't acknowledge the pause.

### Audio generator race condition (Linux)
`self.playing = True` must be set **before** `device.start(gen)` because on
Linux the miniaudio callback fires immediately and the generator checks
`self.playing` before yielding any data.

### Sync: STMl never sent (fixed)
For `autostart=0` (sync mode, after CONT decrements from 2), we must send
STMl when buffer threshold is reached — NOT start audio. LMS waits for STMl
from all synced players before sending `strm u` with the shared start time.
Without STMl, both players hang indefinitely.

### SlimProto message offset calculation (CRITICAL for packet parsing)
`handler.py` uses message-relative offsets (e.g., `msg[18]`), not
payload-relative. All offsets are documented in the `_handle_strm_start`
docstring. When modifying protocol parsing, always verify against those
comments — a mismatch caused 10 test failures during a past refactor.

---

## Key constants (`protocol/slimproto.py`)

```python
SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_FRAME = 4          # s16le stereo: 2 bytes × 2 channels
DEVICE_BUFFER_MSEC = 40      # miniaudio buffer size
PLATFORM_PIPELINE_MSEC = 40  # macOS CoreAudio extra latency (10 on Linux, 30 on Windows)
DEVICE_DELAY_MSEC = 80       # fallback static total (buffer + pipeline)
PCM_BUF_MAX_SIZE = 4MB       # PCMBuffer default max (~23s at 44.1k stereo)
```

`--latency N` overrides `PLATFORM_PIPELINE_MSEC` at runtime.
`--buffer-size KB` overrides `PCM_BUF_MAX_SIZE` at runtime (64-8192 KB).

---

## Testing

**84 unit tests + 14 integration = 98 total:**
```bash
PYTHONPATH=src python3 -m pytest tests/                        # all unit tests
PYTHONPATH=src python3 -m pytest tests/test_p1_reliability.py  # P1 only
PYTHONPATH=src python3 -m pytest tests/test_p2_features.py     # P2 only
PYTHONPATH=src python3 -m pytest tests/test_p3_robustness.py   # P3 only
make test                                                       # shortcut
```

- `PYTHONPATH=src` is required (or `pip install -e .`)
- Integration tests require a running LMS server + ffmpeg

**Coverage:**
- P1 Reliability (14 tests) — connection, heartbeat, state management
- P2 Features (41 tests) — gapless, crossfade, replay gain, ICY metadata, sample rate
- P3 Robustness (29 tests) — MP3 gapless, PCMBuffer edge cases, DSCO, graceful shutdown
- Integration (14 tests) — end-to-end with real LMS

---

## How to run locally

```bash
# No install needed — picks up code changes immediately:
./run.sh -n "Squeezy" -vv          # debug logging
./run.sh -n "Squeezy" -v           # info logging
./run.sh -n "Squeezy" --latency 45 # tune sync offset
./run.sh -l                        # list audio devices

# Or manually:
PYTHONPATH=src python3 -m squeezy -n "Squeezy" -vv
```

---

## How to release

```bash
make release-patch   # 0.4.0 → 0.4.1  bug fixes
make release-minor   # 0.4.0 → 0.5.0  new features
make release-major   # 0.4.0 → 1.0.0  breaking changes
```

`release.sh` bumps `pyproject.toml`, commits, tags, pushes, then builds and
uploads to PyPI. The tag push fires the CI workflow which auto-updates the
Homebrew tap (sha256 + dependency pins) and creates a GitHub Release.

**Never rewrite or force-push a tag that has already been released to Homebrew.**
Doing so changes the tarball sha256 and breaks `brew upgrade` until the tap is
manually patched.

---

## squeezelite reference

When squeezy behaviour needs to match squeezelite, check the
[squeezelite source](https://github.com/ralph-irving/squeezelite):

| Topic | File | Lines |
|-------|------|-------|
| STAT packet structure | `slimproto.c` | 158-204 |
| strm handlers (s/p/u/q/f/t/a) | `slimproto.c` | 277-445 |
| CONT handler | `slimproto.c` | 399-415 |
| STMl / STMd / STMu conditions | `slimproto.c` | 670-760 |
| OUTPUT_START_AT (start-at-time) | `output.c` | 98-109 |
| Track boundary / gapless | `output.c` | 126-170 |
| Crossfade | `output.c` | 193-250 |
| Elapsed time + device delay | `slimproto.c` | 163-166 |
| Server timeout (35s) | `slimproto.c` | 609-613 |
| HELO capabilities | `slimproto.c` | 122-156 |
| ICY metadata | `stream.c` | 642-689 |
| PCM/WAV header parsing | `pcm.c` | 77-181 |

Protocol spec: https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol

---

## Known issues

### Sync offset on macOS
~40-45ms LMS player offset still needed even with `--latency 40`. The dynamic
wall-clock measurement correctly tracks the miniaudio buffer, but CoreAudio
pipeline depth below miniaudio is hard to query precisely. Use `--latency` to tune.

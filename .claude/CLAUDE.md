# Squeezy — Session Context for Claude

This file seeds new Claude sessions with everything needed to continue work
on squeezy without re-explaining the project from scratch.

---

## What is squeezy?

A minimal Python reimplementation of **squeezelite** — a software player for
**Lyrion Music Server (LMS)**, formerly Logitech Media Server.

- Implements the **SlimProto protocol** (TCP port 3483) to register as a player
- Fetches audio via **HTTP** from LMS, decodes with **ffmpeg**, plays via **miniaudio**
- Modular implementation: 8 focused modules (~1,730 lines) + main orchestrator (~1,990 lines)
- Published on PyPI as `squeezy`, installable via `pip install squeezy` or `pipx install squeezy`

---

## Current version

**v0.2.0** on PyPI / Homebrew. Many unreleased fixes in `main` since then
(see "What's changed since v0.2.0" below) — next release will be v0.3.0.

---

## Architecture / key design decisions

### Module structure (v0.3.0+)
The codebase is organized into focused modules with clear layer dependencies:

**Layer 0 (Foundation — no dependencies):**
- `protocol/slimproto.py` (~400 lines) — Protocol constants, packet builders, utility functions
- `config/config.py` (69 lines) — XDG config directory management (player name persistence)
- `config/metadata.py` (~180 lines) — ICY/Shoutcast metadata parsing, LAME gapless info

**Layer 1 (Protocol & Network — foundation only):**
- `network/server_connection.py` (~150 lines) — TCP/UDP socket management, discovery
- `network/lms_metadata.py` (83 lines) — LMS JSON-RPC track metadata queries
- `network/status_server.py` (126 lines) — Unix socket status server for external tools
- `protocol/lms_client.py` (122 lines) — SlimProto message sending (HELO, STAT, DSCO, etc.)

**Layer 2 (Audio buffer):**
- `audio/stream_decoder.py` (~140 lines) — Thread-safe PCMBuffer (bounded, backpressure)

**Layer 3 (Message handlers — calls back into squeezy.py for state transitions):**
- `protocol/handler.py` (~420 lines) — All SlimProto message parsing & dispatch (strm, audg, setd, cont, serv, dsco, aude)

**Main orchestration:**
- `squeezy.py` (~1,780 lines) — Audio pipeline, streaming, CLI, main loop, state management

Protocol message handling is delegated to `handler.py`. Audio device management,
streaming, and the audio generator remain in `squeezy.py`.

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

## Critical bugs fixed in this codebase (don't regress these)

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
When parsing SlimProto messages, `payload = msg[4:]` removes the 4-byte opcode.
**All offsets used in payload parsing are relative to this stripped message, NOT the original message.**
Example: If a field is at `msg[18]` (absolute message offset), it's at `payload[14]` in the code.
Calculation: `payload_offset = msg_offset - 4`
This mismatch caused 10 test failures during refactoring (replay_gain, transition parameters).
Always verify offset calculations when modifying protocol parsing or tests.

---

## What's changed since v0.2.0 (unreleased, in `main`)

Core features from v0.2.0+v0.3.0:
- Volume control from LMS remote (`_handle_audg`, s16le scaling)
- Log levels: `-v` = INFO, `-vv` = DEBUG, default = WARNING
- Version single-sourced from `pyproject.toml` via `importlib.metadata`
- Auto-update check on startup (non-blocking, silent on failure)
- Install method detection for upgrade instructions (brew/pipx/pip)
- Multi-OS CI (Ubuntu × macOS × Windows, Python 3.10/3.12/3.14)
- Dockerfile + linux distro test script
- Track queuing for gapless transition (STMd sent late, pending_track queue)
- Sync group support: STMl, `strm u` with jiffies, start-at-time silence
- miniaudio buffer reduced 200ms → 40ms (`DEVICE_BUFFER_MSEC`)
- Dynamic device delay compensation in `_elapsed_ms()` (wall-clock based)
- Platform pipeline latency constant + `--latency` CLI flag
- Extensive comments and named struct format constants

**Priority 2 User-Facing Quality (9/11 complete):**
- P2.1 True gapless playback — device persistence, track boundary tracking, zero-gap transitions
- P2.2 Crossfade Support — 5 fade modes with complementary gain curves, sample-by-sample mixing
- P2.3 Replay gain — 16.16 fixed-point extraction, multiplicative with volume control
- P2.4 ICY metadata — Shoutcast radio metadata parsing and status reporting
- P2.5 Variable sample rate — 44.1k/48k/96k/192k native support, ffmpeg detection, device switching
- P2.7 HTTPS/SSL Stream Support — port 443 detection, SSL wrapping, CanHTTPS capability
- P2.9 Player Name Persistence — save/load to ~/.config/squeezy/ with XDG support
- P2.10 CONT Metaint Support — extract metaint for ICY metadata synchronization
- P2.11 Codec Priority — ffmpeg decoder probing, dynamic codec reporting to LMS

**Priority 3 Robustness (7/13 complete):**
- P3.6 MP3 Gapless — LAME encoder delay/padding parsing
- P3.7 Memory Management — PCMBuffer max size (4MB default, OOM prevention)
- P3.9 Thread Safety — Audited all shared state, documented invariants
- P3.10 Graceful Shutdown — Signal handling, clean teardown
- P3.11 DSCO Packet — Server-initiated disconnect/reconnect
- P3.12 SERV Packet — Server redirect
- P3.13 AUDE Packet — Audio enable/disable

---

## Key constants (defined in `protocol/slimproto.py`, re-exported in `squeezy.py`)

```python
SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_FRAME = 4          # s16le stereo: 2 bytes × 2 channels
DEVICE_BUFFER_MSEC = 40      # miniaudio buffer size
PLATFORM_PIPELINE_MSEC = 40  # macOS CoreAudio extra latency (10 on Linux, 30 on Windows)
DEVICE_DELAY_MSEC = 80       # fallback static total (buffer + pipeline)
```

`--latency N` CLI flag overrides `PLATFORM_PIPELINE_MSEC` at runtime.

---

## Testing

**Unit tests (84 unit + 14 integration = 98 total):**
```bash
PYTHONPATH=src python3 -m pytest tests/                     # all unit tests
PYTHONPATH=src python3 -m pytest tests/test_p1_reliability.py  # P1 tests only
PYTHONPATH=src python3 -m pytest tests/test_p2_features.py     # P2 tests only
PYTHONPATH=src python3 -m pytest tests/test_p2_features.py::TestP23ReplayGain -xvs  # specific class
make test                                                   # shortcut (sets PYTHONPATH)
```

**Important:**
- `PYTHONPATH=src` is required (or `pip3 install .` to install the package)
- Integration tests (`test_integration.py`) require running LMS server + ffmpeg; skip for unit-only runs

**Test coverage:**
- P1 Reliability (14 tests) — Connection, heartbeat, state management
- P2 Features (41 tests) — Gapless, crossfade, replay gain, ICY metadata, sample rate
- P3 Robustness (29 tests) — MP3 gapless, memory management, buffer edge cases, DSCO, graceful shutdown
- Integration (14 tests) — End-to-end with real LMS

---

## Known issues / active debugging

### Linux distro tests
Debian 11 bullseye still fails apt install (pipx not in repos). Fix:
install `python3-pip` separately and skip pipx.

### Sync offset
~40-45ms LMS player offset still needed on macOS even with `--latency 40`.
The dynamic wall-clock measurement correctly tracks the miniaudio buffer,
but the CoreAudio pipeline depth below miniaudio is hard to query precisely.
`--latency` is the user-facing knob for this.

---

## TODO / backlog

Full list in `TODO.md` — 40+ items prioritised P1-P5.

**Status:**
- P1 (8/8) Critical Reliability — All complete
- P2 (9/11) User-Facing Quality — 9 of 11 complete
  - P2.6 (24-bit audio) — deferred
  - P2.8 (Hardware volume) — skipped (platform-specific)
- P3 (7/13) Robustness & Edge Cases — In progress

---

## How to run locally

```bash
# Quick dev run (no install needed, picks up code changes immediately):
./run.sh -n "Squeezy" -vv          # debug logging
./run.sh -n "Squeezy" -v           # info logging
./run.sh -n "Squeezy" --latency 45 # tune sync offset
./run.sh -l                        # list audio devices

# Or manually:
PYTHONPATH=src python3 -m squeezy -n "Squeezy" -vv
```

---

## How to release (v0.3.0)

```bash
# 1. Bump version in pyproject.toml
# 2. Commit + tag
git commit -am "chore: release v0.3.0"
git tag v0.3.0 && git push && git push --tags

# 3. Build + upload to PyPI
rm -rf dist/ build/
python -m build
twine upload dist/*

# 4. Update Homebrew tap
curl -sL https://github.com/catcatcatcatcatcatcatcatcatcat/squeezy/archive/refs/tags/v0.3.0.tar.gz | shasum -a 256
# Update url + sha256 in homebrew-tap/Formula/squeezy.rb, commit, push
```

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

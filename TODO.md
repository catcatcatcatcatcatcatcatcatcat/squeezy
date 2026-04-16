# Squeezy TODO — Roadmap to Squeezelite-Level Robustness

This document tracks remaining features, edge cases, and platform-specific workarounds
learned from studying squeezelite's codebase and 530+ commits / 260+ GitHub issues.

> **Source repos studied:**
> - squeezelite C source: `/Users/atdot/squeeze/squeezelite/` (ralph-irving/squeezelite)
> - squeezelite GitHub issues: https://github.com/ralph-irving/squeezelite/issues

---

## Completion Status

✅ **Priority 1** (8/8) — Critical Reliability — COMPLETE
✅ **Priority 2** (9/11) — User-Facing Quality — 9/11 COMPLETE
   - ⏸️ P2.6 (24-bit audio) — Deferred (requires full refactor)
   - ⏭️ P2.8 (Hardware volume) — Skipped (platform-specific)
⏳ **Priority 3** (7/13) — Robustness & Edge Cases — 7 COMPLETE, 6 remaining
⏳ **Priority 4** (0/5) — Platform-Specific — NOT STARTED
⏳ **Priority 5** (0/4) — Performance Optimizations — NOT STARTED

---

## Project Structure (src/ Layout)

```
src/squeezy/
├── squeezy.py                  # Main orchestrator, audio pipeline, CLI (~1,780 lines)
├── audio/
│   └── stream_decoder.py       # Thread-safe PCMBuffer
├── protocol/
│   ├── handler.py              # SlimProto message handlers (strm, audg, setd, etc.)
│   ├── slimproto.py            # Protocol constants & packet builders
│   └── lms_client.py           # LmsClient class (message operations)
├── network/
│   ├── server_connection.py    # TCP/UDP socket management, discovery
│   ├── lms_metadata.py         # LMS JSON-RPC track metadata queries
│   └── status_server.py        # Unix socket status server for external tools
└── config/
    ├── config.py               # XDG-compliant config persistence
    └── metadata.py             # ICY metadata & LAME gapless parsing
```

**Module responsibilities:**
- **protocol/handler.py**: All SlimProto message parsing and dispatch (calls back into squeezy.py)
- **protocol/slimproto.py**: Named constants, packet builders, utility functions
- **audio/stream_decoder.py**: PCMBuffer (bounded, backpressure, thread-safe)
- **network/**: TCP/UDP sockets, server discovery, LMS metadata queries, status server
- **config/**: Configuration persistence, ICY/LAME metadata extraction
- **squeezy.py**: Audio device lifecycle, streaming pipeline, CLI, main event loop

---

## Priority 2 — Deferred/Skipped Features

### 2.6 24-bit and 32-bit Audio

**STATUS: ⏸️ DEFERRED (Future enhancement)**

**Problem:** Many high-quality music files and streams use 24-bit or 32-bit audio.
Without support, squeezy truncates to 16-bit, losing quality.

**How squeezelite handles it:**
Squeezelite supports native 24-bit and 32-bit output on platforms that have it
(ALSA on Linux, Coreaudio on macOS). The STAT packet includes `output_rate_mode` field
that tells LMS whether we want 16-bit, 24-bit, or 32-bit frames.

**Why deferred:**
This requires:
1. Detecting 24/32-bit streams from FFmpeg output or HTTP headers
2. Updating miniaudio device initialization to request 24/32-bit format
3. Updating PCMBuffer sample size from fixed 16-bit to variable
4. Updating audio generator to handle 24/32-bit frames
5. Testing across macOS/Linux/Windows with various audio devices

Implementation likely requires significant refactoring of the audio pipeline.
**Estimated effort: 3-4 sessions** once current P1/P2 are fully stabilized.

---

### 2.8 Hardware Volume / OS Mixer Control

**STATUS: ⏭️ SKIPPED (platform-specific complexity)**

**Problem:** Some users want to control speaker volume via LMS instead of physical knobs.
Without `audg` packet response support, this doesn't work.

**Why skipped:**
- macOS: CoreAudio requires low-level device enumeration + HAL
- Linux: ALSA mixer configuration is device-dependent
- Windows: WinMM/WASAPI APIs are complex
- ffmpeg: Volume control is non-standard output option

Each platform needs different code. This doesn't provide much value for a software
player where OS volume control already works. **Better to let users adjust via OS mixer.**

---

## Priority 3 — Robustness & Edge Cases

### 3.1 CPU Spinning When Buffer Empty

**Problem:** When the streaming thread is slow to fill the PCM buffer, the audio
generator spins in a busy loop yielding silence, wasting CPU.

**How squeezelite handles it:**
`output.c` includes logic to detect buffer starvation. If available frames < 1000 ms,
it starts sending STATUS packets faster to notify the server, which can reduce bitrate
or request seeking to a different part of the file.

**Suggested fix for squeezy:**
Add a "buffer health" metric:
- If available < 500ms: log debug, possibly send frequent STAT updates
- If available < 100ms: likely audio glitch incoming; send STAT immediately
- If available == 0: pause playback, wait for buffer recovery

---

### 3.2 WAV Streams With Unknown Content-Length

**Problem:** Some HTTP servers don't include Content-Length header for WAV streams,
so squeezy doesn't know the track duration.

**How squeezelite handles it:**
Reads WAV headers even without Content-Length, extracts duration from RIFF chunk size.

**Suggested fix for squeezy:**
In `_stream_to_ffmpeg()` or HTTP handler:
- Parse WAV/AIFF headers for RIFF chunk size and sample rate
- Calculate duration = (chunk_size / bytes_per_sample) / sample_rate
- Report to LMS via STAT packet

---

### 3.3 WAV/AIFF Header Parsing

**Problem:** WAV and AIFF files have specific header structures with metadata chunks.
If we don't parse them correctly, track duration is wrong or audio cuts out early.

**How squeezelite handles it:**
`pcm.c:77-181` — Full WAV/AIFF header parser that:
- Finds RIFF/FORM chunk
- Parses fmt chunk for sample rate, channels, bits per sample
- Skips over JUNK, LIST, and other metadata chunks
- Finds the data chunk and reads the actual sample count

**Suggested fix for squeezy:**
Extract WAV parsing logic from `_stream_to_ffmpeg()` into a separate function:
```python
def parse_wav_header(http_response):
    # Read RIFF/FORM chunk
    # Find fmt chunk → extract sample_rate, channels, bits_per_sample
    # Skip metadata chunks
    # Find data chunk → extract total_frames = data_size / frame_bytes
    return {"sample_rate": rate, "duration_ms": duration}
```

Pass parsed metadata to `_start_audio()` so elapsed time is accurate.

---

### 3.4 Large MP4/Ogg Headers

**Problem:** Some MP4/Ogg files have huge metadata/artwork in headers (>10MB).
Squeezy reads the entire header into memory before passing to FFmpeg, wasting RAM.

**How squeezelite handles it:**
Doesn't directly handle this. FFmpeg is responsible for seeking past metadata.

**Suggested fix for squeezy:**
Stream the HTTP response directly to FFmpeg stdin without buffering full headers.
Let FFmpeg's built-in parsers handle seeks. Only buffer audio frames in PCMBuffer.

---

### 3.5 Ogg/Vorbis and Opus End-of-Stream

**Problem:** Ogg streams don't include total file size in headers. Without proper
end-of-stream detection, the track may play past the actual end or get cut off.

**How squeezelite handles it:**
Ogg libraries handle this. The decoder signals `DECODE_COMPLETE` when the final page
is reached.

**Suggested fix for squeezy:**
FFmpeg should handle this automatically. Verify that `_decode_reader()` detects
EOF correctly when FFmpeg closes stdout.

---

---

### 3.8 Circular Buffer (Ring Buffer)

**Problem:** If PCMBuffer has a subtle edge case (write wrapping around, read blocking
while write is paused), audio can glitch or hang.

**How squeezelite handles it:**
Extensively tested ring buffer with clear read/write pointers and explicit wrapping logic.

**Current status in squeezy:**
PCMBuffer is implemented with write() and read(n) methods. Needs thorough testing:
- Test wraparound: write 1000 frames, read 500, write 500, read 500
- Test concurrent access: stream thread writing while audio thread reading
- Test edge cases: write when full, read when empty, flush

**Suggested fix for squeezy:**
Add unit tests in `test_p3_robustness.py` for PCMBuffer edge cases.

---

---

## Priority 4 — Platform-Specific

### 4.1 Linux ALSA Device Issues

**Problem:** On some Linux systems with multiple audio devices, ALSA device enumeration
is unreliable or crashes.

**Suggested fix:**
Add fallback device list if enumeration fails. Pre-populate "default", "pulse", "dmix".

---

### 4.2 macOS CoreAudio Device Switching

**Problem:** Changing audio output (e.g., unplugging headphones) requires CoreAudio
event handling.

**Suggested fix:**
Listen for macOS audio device change notifications and reinitialize miniaudio device.

---

### 4.3 Windows WaveOut / WASAPI Issues

**Problem:** Windows WaveOut can have unpredictable latency. WASAPI is more reliable
but more complex.

**Suggested fix:**
If miniaudio supports WASAPI backend selection, use it. Otherwise accept WaveOut quirks.

---

### 4.4 Systemd / systemctl Integration

**Problem:** Some users want to run squeezy as a systemd service with automatic restart.

**Suggested fix:**
Document systemd unit file and installation instructions.

---

### 4.5 Docker / Container Support

**Problem:** Running squeezy in a container requires careful PulseAudio/ALSA configuration.

**Suggested fix:**
Document Docker setup with audio device pass-through and volume mounting.

---

## Priority 5 — Performance Optimizations

### 5.1 FFmpeg Codec Selection / Hardware Acceleration

**Problem:** FFmpeg can use hardware decoders (NVIDIA NVDEC, Intel QSV, Apple VideoToolbox)
for faster decoding, but squeezy doesn't expose this option.

**Suggested fix:**
Add CLI flag `--hwaccel nvidia|vaapi|videotoolbox|none` and pass to FFmpeg.

---

### 5.2 Streaming Buffer Size Tuning

**Problem:** Buffer size is hardcoded. On slow networks, bigger buffers help. On fast
networks, smaller buffers reduce latency.

**Suggested fix:**
Add `--buffer-size <KB>` CLI flag (default 512 KB, range 64-8192 KB).

---

### 5.3 PCM Circular Buffer Optimization

**Problem:** Current PCMBuffer uses Python bytearray, which is not optimized for
real-time audio.

**Suggested fix:**
Consider using `array.array('h')` (signed shorts) or `ctypes.Array` for better
performance if profiling shows bottlenecks.

---

### 5.4 Async Message Handling

**Problem:** Main loop blocks on socket recv. If LMS sends a command while squeezy
is streaming, there's latency.

**Suggested fix:**
Use `select.select()` to multiplex TCP receive and check queue for pending commands.
Implement a thread-safe message queue for volume/pause/seek commands.

---

## Testing / CI Infrastructure

- Unit tests: 76/76 passing (14 P1 + 41 P2 + 21 P3)
- Integration tests: 14 tests available (require ffmpeg + LMS server)
- CI/CD: GitHub Actions setup (Ubuntu, macOS, Windows × Python 3.10/3.12/3.14)
- Performance baseline: 40ms miniaudio buffer, <1% CPU idle, <50ms sync offset

---

## Notes for Future Sessions

1. **Architecture**: Protocol message handling is delegated to `protocol/handler.py`.
   Audio pipeline, streaming, and device management live in `squeezy.py`.
   See CLAUDE.md for full module structure and layer dependencies.

2. **Protocol offsets**: `handler.py` uses message-relative offsets (e.g., `msg[18]`)
   not payload-relative. All offsets are documented in the `_handle_strm_start` docstring.

3. **Testing procedure**: Run tests from squeezy/ directory:
   ```bash
   PYTHONPATH=src python3 -m pytest tests/ -v --timeout=60
   # or: make test
   ```

4. **Test interface**: Tests call handler methods via `squeezy.protocol._handle_strm_start(msg)`
   (delegated to `protocol/handler.py`). The handler calls back into squeezy.py methods
   like `_start_stream()`, `_stop_playback()`, etc.

5. **Next priorities**: Remaining P3 items (robustness) would unlock higher reliability
   and handle edge cases. P4.x (platform) enables better user experience.
   P5.x (performance) is nice-to-have optimization.


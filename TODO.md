# Squeezy TODO — Roadmap to Squeezelite-Level Robustness

This document tracks features, bug fixes, platform workarounds, and protocol details
learned from studying squeezelite's codebase and 530+ commits / 260+ GitHub issues.
Each item includes a reference to where the behaviour was found, why it matters, and
a suggested implementation approach for squeezy.

> **Source repos studied:**
> - squeezelite C source: `/Users/atdot/squeeze/squeezelite/` (ralph-irving/squeezelite)
> - squeezelite GitHub issues: https://github.com/ralph-irving/squeezelite/issues

---

## Priority 1 — Critical Reliability

**STATUS: ✅ COMPLETE (8 commits, session 2024-04-04)**

These items affect basic playback reliability and can cause disconnects, skipped
tracks, or silent failures. All items implemented with focused 1-2 commit each.

---

### 1.1 Server Timeout Detection (35-second heartbeat)

**Problem:** If the TCP connection to LMS silently dies (network change, server
restart, etc.), squeezy will hang forever waiting for data.

**How squeezelite handles it:**
`slimproto.c:609-613` — A counter increments each second when no data arrives.
After 35 timeouts the connection is considered dead and `slimproto_run()` returns,
triggering full reconnection. The value is 35 because LMS normally sends a `strm t`
every 5 seconds, but mysqueezebox.com uses 30-second intervals.

```c
} else if (++timeouts > 35) {
    LOG_INFO("No messages from server - connection dead");
    return;
}
```

**Suggested fix for squeezy:**
Add a `last_server_msg` timestamp. In the main recv loop, if
`time.monotonic() - last_server_msg > 35`, log a warning and trigger reconnect.
Reset the timestamp on every received message.

---

### 1.2 Connection Timeout (5s slimproto, 10s stream)

**Problem:** A `socket.connect()` with no timeout can block for minutes if the
server is unreachable (especially on Linux where the default TCP SYN retry is ~2
minutes).

**How squeezelite handles it:**
`utils.c:301-330` — `connect_timeout()` uses non-blocking connect + `select()`:
- Slimproto server: 5-second timeout (`slimproto.c:922`)
- HTTP stream: 10-second timeout (`stream.c:243`)

**Suggested fix for squeezy:**
We already set `self.stream_sock.settimeout(10)` for streams. Ensure the slimproto
socket also has a connection timeout:
```python
self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
self.sock.settimeout(5)
self.sock.connect((server_ip, 3483))
self.sock.settimeout(None)  # Back to blocking for recv
```

---

### 1.3 Reconnection Fallback to UDP Discovery

**Problem:** If the user's server IP changes (DHCP, restart), squeezy keeps trying
the old IP forever.

**How squeezelite handles it:**
`slimproto.c:933-934` — After 5 failed direct connections, falls back to UDP
broadcast discovery:
```c
if (!server && ++failed_connect > 5) {
    slimproto_ip = discover_server(NULL);
}
```

The discovery function (`slimproto.c:776-823`) sends an "e" UDP broadcast to port
3483 and waits 5 seconds for a response.

**Suggested fix for squeezy:**
In the reconnect loop, count consecutive failures. After 5, call `discover_server()`
instead of retrying the last known IP. On success, update `self.server_ip`.

---

### 1.4 Stream Error Recovery / Retry

**Problem:** Network glitches during streaming (EWOULDBLOCK, partial sends, broken
pipe) can kill playback.

**How squeezelite handles it:**
- `stream.c:162-201` — Retries sending HTTP request headers up to 10 times on
  `EWOULDBLOCK`
- Windows-specific: handles `WSAECONNRESET` by reducing TCP receive buffer
  (`r1418`), retries on `WSAENOTCONN` (`r1314, r1322`)
- `SIGPIPE` handling to prevent process termination on broken socket

**Suggested fix for squeezy:**
- Wrap `self.stream_sock.sendall()` in try/except with retry logic
- Use `signal.signal(signal.SIGPIPE, signal.SIG_IGN)` on Unix
- On Windows, handle `ConnectionResetError` gracefully

---

### 1.5 Elapsed Time — Device Delay Compensation

**Problem:** Reported elapsed time is slightly ahead of what the user actually hears,
because there's audio data sitting in the OS/hardware buffer that hasn't been played
yet.

**How squeezelite handles it:**
`output_alsa.c:822-839` — Reads hardware delay via `snd_pcm_delay()`:
```c
snd_pcm_delay(pcmp, &delay);
output.device_frames = delay;
```
Then in `slimproto.c:163-166` subtracts it from elapsed:
```c
ms_played = (frames_played - device_frames) * 1000 / sample_rate;
ms_played += (now - status.updated);  // Add time since last measurement
```

**Suggested fix for squeezy:**
miniaudio doesn't expose device delay directly, but we can estimate it from the
playback device's buffer size. If miniaudio's `PlaybackDevice` exposes
`buffer_size_in_frames`, subtract that from `output_frames`. This matters most for
sync groups (multi-room) where timing precision is critical.

---

### 1.6 strm 'f' (Flush) vs 'q' (Quit) — Proper Distinction

**Problem:** Both `strm q` and `strm f` currently call `_stop_playback()`, but they
have different semantics in the protocol. Getting this wrong can break track
transitions when LMS uses flush for streaming resets.

**How squeezelite handles it:**
`slimproto.c:284-304`:

- **'q' = hard stop**: `decode_flush()` + `output_flush()` + `stream_disconnect()`
  — kills everything, resets frames_played to 0
- **'f' = streaming flush**: `decode_flush()` + `output_flush_streaming()` — only
  discards *queued* (not-yet-playing) track data:

```c
// output.c:455-465
bool output_flush_streaming(void) {
    flushed = output.track_start != NULL;
    if (output.track_start) {
        outputbuf->writep = output.track_start;  // Discard queued next track
        output.track_start = NULL;
    }
    return flushed;
}
```

**Suggested fix for squeezy:**
- `strm q`: Full stop (current behaviour — correct)
- `strm f`: Only stop streaming and clear pending track queue. If audio is currently
  playing, let it continue. Only send STMf if something was actually flushed.

---

### 1.7 ffmpeg Process Management

**Problem:** If ffmpeg crashes or exits with an error, `_decode_reader` sees EOF
immediately and sets `decode_complete = True` with minimal data, causing the track to
play only a few seconds then advance.

**How squeezelite handles it:**
Squeezelite uses in-process codec libraries, so this doesn't apply. But the decode
state machine has explicit `DECODE_ERROR` → sends `STMn` (not started) to LMS, which
is different from `DECODE_COMPLETE` → `STMd`.

**History:** Multiple codec crashes were fixed over time:
- MAD memory corruption from ignoring frame_decode errors (`r1105`)
- FLAC lost sync on 384kHz content (`issue #143, r1385`)
- FFmpeg memory leak and double free (`PR #117`)
- AAC decoder memory leak (early fix)
- Vorbis crash when skipping with resampling (`r405`)

**Suggested fix for squeezy:**
- Check `ffmpeg_proc.returncode` after stdout closes. If non-zero, log the error and
  send STMn instead of STMd.
- Read ffmpeg's stderr for error details.
- Add a timeout to `_decode_reader` — if no data received for 10 seconds, assume
  failure.

---

### 1.8 Proper HELO Capabilities

**Problem:** LMS uses the capabilities string to decide what codecs, features, and
formats to send to the player. An incomplete capabilities string can cause LMS to
send formats we can't handle, or miss features like volume control.

**How squeezelite handles it:**
`slimproto.c:122-156`:
```c
#define BASE_CAP "Model=squeezelite,AccuratePlayPoints=1,HasDigitalOut=1,"
                 "HasPolarityInversion=1,Balance=1,Firmware=" VERSION
```

Additional codec caps are appended dynamically based on which codec libraries are
loaded. The HELO packet also includes:
- `deviceid = 12` (SqueezePlay type)
- `wlan_channellist = 0x4000` on reconnect, `0x0000` on first connect
- Full MAC and UUID
- `bytes_received_H/L` for 64-bit byte count

**Suggested fix for squeezy:**
Build a proper capability string:
```python
caps = (f"Model=squeezy,ModelName=Squeezy,"
        f"AccuratePlayPoints=1,HasDigitalOut=1,"
        f"Firmware={VERSION},"
        f"MaxSampleRate=44100,"  # Update as we add more rates
        f"mp3,flc,pcm,aif,ogg,aac")  # Codecs ffmpeg supports
```

---

### 1.9 64-bit Byte Counter Handling

**Problem:** `bytes_received` is sent as two u32 fields (H and L) in the STAT
packet. If we overflow a 32-bit counter during long playback sessions or large
files, the server gets confused.

**How squeezelite handles it:**
Always tracks `bytes_received` as 64-bit, splits into H/L for the STAT packet:
```c
status.bytes_recv_H;  // Upper 32 bits
status.bytes_recv_L;  // Lower 32 bits
```

**Current squeezy status:**
We already do this correctly with `(bytes_received >> 32) & 0xFFFFFFFF` and
`bytes_received & 0xFFFFFFFF`. Verified working. **No change needed.**

---

## Priority 2 — User-Facing Quality

**STATUS: ✅ 9/11 COMPLETE** (Session 2024-04-04 - comprehensive P2.x feature push)

These features affect the listening experience. Users will notice their absence
when comparing squeezy to a real Squeezebox or squeezelite.

Completed in this session:
- ✅ **P2.1** True gapless playback (track boundary handling, zero-gap transitions)
- ✅ **P2.2** Crossfade Support (5 fade modes: NONE, CROSSFADE, FADE_IN, FADE_OUT, FADE_INOUT)
- ✅ **P2.3** Replay gain (16.16 fixed-point parsing and application)
- ✅ **P2.4** ICY metadata (Shoutcast radio metadata extraction)
- ✅ **P2.5** Variable sample rate (44.1k/48k/96k/192k native)
- ✅ **P2.7** HTTPS/SSL Stream Support (port 443 detection, SSL wrapping, CanHTTPS capability)
- ✅ **P2.9** Player Name Persistence (save/load to ~/.config/squeezy/)
- ✅ **P2.10** CONT Metaint Support (extract metaint field for ICY sync)
- ✅ **P2.11** Codec Priority (ffmpeg decoder probing, dynamic codec reporting)

Deferred/Skipped:
- ⏸️ **P2.6** 24-bit/32-bit Audio (deferred - requires full refactor, see note below)
- ⏭️ **P2.8** Hardware Volume (skipped - platform-specific, complex)
- ✅ **P2.4** ICY metadata (already implemented, verified working)
- ✅ **P2.5** Variable sample rate (5 commits: detection, state tracking, device switching)

---

### 2.1 Gapless Playback (Track Boundaries)

**STATUS: ✅ COMPLETE (commit 5a4d09e)**

**Problem:** When one track ends and the next begins, there's a brief silence gap
and a new device open/close cycle. Squeezelite plays seamlessly across tracks.

**How squeezelite handles it:**
`output.c:126-170` — Uses a `track_start` pointer in the circular output buffer to
mark where the next track begins. The output thread plays continuously through the
boundary, applying per-track replay gain and optional crossfade at the boundary.

**Implementation (true gapless achieved):**
- Track boundary tracking: `_current_track_id`, `_track_start_frames`
- Generator handles track switching internally without exiting
- Device stays open across tracks (no close/reopen)
- Elapsed time calculated relative to track boundary
- Stream thread starts directly while audio generator continues
- Result: Zero-gap audio transitions, seamless playback

**Code changes:**
- `_reset_track_state()`: increment track ID, record boundary, reset STAT flags
- `_audio_generator()`: detect pending track at buffer drain, switch in-place
- `_elapsed_ms()`: calculate frames relative to `_track_start_frames`
- `_start_stream()`: initialize track boundaries for first track

This is true gapless: same device, same generator, new stream starts before old
one ends. No audible gap like squeezelite's implementation.

---

### 2.2 Crossfade Support

**STATUS: ✅ COMPLETE (commit 69d7f08, with 13 unit tests)**

**Problem:** LMS sends crossfade parameters (fade mode, duration) in the `strm s`
packet. Without implementing them, transitions are abrupt.

**Implementation:**
Extracts transition parameters from `strm s` packet and applies 5 fade modes:
- 0: FADE_NONE (immediate switch)
- 1: CROSSFADE (old fades out, new fades in)
- 2: FADE_IN (new fades in from silence)
- 3: FADE_OUT (old fades out to silence)
- 4: FADE_INOUT (both fade simultaneously)

**Architecture:**
- Extract transition parameters in `_handle_strm_start()` (offsets 13-14)
- Build linear gain curves: `_build_fade_curves()` generates 0→1 and 1→0 gains
- Mix samples in `_apply_crossfade()` at track boundary using gain curves
- Save end-of-track samples for crossfade at gapless boundary
- Replay gain applied multiplicatively after crossfade

**Key Code:**
- `_build_fade_curves()`: Generate gain curves for fade duration
- `_apply_crossfade()`: Mix old/new samples with gain curves
- Sample-by-sample mixing: `old * gain_out + new * gain_in`
- Complementary gains ensure smooth transitions without pops/clicks

**Tests:** 13 comprehensive tests covering all fade modes, parameter extraction, gain curves.

---

### 2.3 Replay Gain

**STATUS: ✅ COMPLETE (commit eefeadb)**

**Problem:** Without replay gain, volume jumps between tracks from different albums
or sources.

**Implementation:**
- Extract `replay_gain` from `strm s` packet (4 bytes at offset 18, big-endian u32)
- Parse as 16.16 fixed-point: `value / 0x10000` = linear gain
- Apply in audio generator: `total_gain = volume × replay_gain`
- Both gain sources work multiplicatively (remote control × track gain)

**Code changes:**
- `__init__`: `self.replay_gain = 1.0` (default unity)
- `_handle_strm_start()`: extract and parse replay_gain field, log value
- `_audio_generator()`: multiply `self.volume × self.replay_gain` when scaling samples
- Debug logging shows replay_gain value at track start

**Tests:** 6 tests covering extraction, unity/boost/cut gain values, edge cases.

---

### 2.4 ICY Metadata (Internet Radio)

**STATUS: ✅ VERIFIED (already implemented, commit 7e41746 adds 6 tests)**

**Problem:** Internet radio streams include inline metadata (station name, current
track title) via the ICY/Shoutcast protocol. Without parsing this, LMS can't display
what's playing on radio stations.

**Implementation (verified working):**
- HTTP response parsing: extract `icy-metaint` header value (interval in bytes)
- Stream parsing in `_stream_to_ffmpeg()`: count bytes, extract metadata blocks every
  `icy_meta_int` bytes
- Metadata block format: 1-byte length (in 16-byte units), then length×16 bytes
- Parse key=value pairs: StreamTitle, StreamArtist, StreamAlbum
- Status dict includes ICY metadata as fallback from LMS metadata

**Code locations:**
- `_do_stream()` lines 1289-1296: extract `icy-metaint` from HTTP headers
- `_stream_to_ffmpeg()` lines 1460-1495: parse metadata blocks while streaming
- `_parse_icy_metadata()` lines 1181-1228: extract fields, handle empty blocks
- `_status_dict()` lines 1232-1242: include ICY title/artist in broadcast

**Tests:** 6 tests covering extraction, parsing, edge cases, status dict priority.

---

### 2.5 Sample Rate Switching

**STATUS: ✅ COMPLETE (5 commits: state tracking, detection, elapsed time, device switch, native output)**

**Problem:** Squeezy hardcodes 44100 Hz. Content at 48kHz, 96kHz, etc. was being
resampled by ffmpeg, causing unnecessary CPU usage and quality loss.

**Implementation (variable sample rate support):**
1. **Commit b3b4f43**: State tracking variables (`current_sample_rate`, `next_sample_rate`, `supported_rates`)
2. **Commit 0da5eb8**: Detection from PCM metadata + ffmpeg stderr parsing
3. **Commit 95568cc**: Elapsed time calculations use `current_sample_rate` (6 locations)
4. **Commit a572d15**: Device opening at track boundary with correct rate
5. **Commit 85dfe60**: FFmpeg output at native rate, HELO updated to MaxSampleRate=192000

**Architecture:**
- Supported rates: [44.1k, 48k, 96k, 192k] with fallback to 44.1k
- PCM format: detect from `pcm_sample_rate` field in `strm s` packet
- Compressed formats: parse from ffmpeg stderr "Stream #0:0: Audio: <codec>, <rate> Hz"
- Device switching: `_start_audio()` accepts rate parameter, opens miniaudio at that rate
- No resampling: ffmpeg outputs at native rate (no `-ar 44100` forcing)

**Tests:** 10 tests covering detection, fallback, device opening, elapsed time.

---

### 2.6 24-bit and 32-bit Audio

**STATUS: ⏸️ DEFERRED (See notes below for future implementation)**

**Problem:** Squeezy uses 16-bit samples internally. Hi-res content (24-bit FLAC,
32-bit WAV) is downsampled by ffmpeg, losing dynamic range.

**How squeezelite handles it:**
- Internal processing is 32-bit (`BYTES_PER_FRAME = 8` for stereo)
- Output packing converts to device format: S16, S24, S24_3LE, S32
- `output_pack.c` handles all conversions with gain and clipping protection

**Why deferred:**
P2.6 requires extensive refactoring of the audio pipeline and would benefit from:
- A dedicated implementation session with focused testing
- Updates to all audio processing code (generators, volume scaling, crossfade mixing)
- Full test suite updates (current 41 tests assume 16-bit sample format)
- Careful validation on multiple platforms

**Future implementation approach:**
1. Change `BYTES_PER_FRAME` from 4 to 8 (for stereo 32-bit)
2. Update ffmpeg output: `-f s32le` instead of `-f s16le`
3. Update miniaudio device format: `SampleFormat.SIGNED32`
4. Update array types: `array.array("i", ...)` instead of `"h"`
5. Update all sample processing with 32-bit range clamping
6. Comprehensive testing on all platforms

**Current status:** 16-bit is sufficient for most content. Premium content requiring hi-res can
be handled by a dedicated P2.6 implementation session once this session's work is stable.

---

### 2.7 HTTPS/SSL Stream Support

**STATUS: ✅ COMPLETE (commit 4fba193)**

**Problem:** Some LMS configurations serve streams over HTTPS (especially with
reverse proxies or mysqueezebox.com).

**Implementation:**
Automatically detects HTTPS connections and wraps stream socket with SSL/TLS.

**Architecture:**
- Port 443 detection: Check if `server_port == 443` in `_do_stream()`
- SSL wrapping: Use `ssl.create_default_context().wrap_socket()` with server hostname
- Graceful fallback: Log warning if SSL negotiation fails, continue with HTTP
- Capability advertisement: Add `CanHTTPS=1` to HELO capabilities

**Code locations:**
- `_do_stream()`: SSL wrapping after socket connect (lines 1374-1383)
- `_capabilities()`: CanHTTPS=1 advertised in HELO string

**Tests:** Functional verification in manual testing (SSL connection to HTTPS streams).

---

### 2.8 Hardware Volume / OS Mixer Control

**Problem:** Software volume scaling in squeezy loses bit depth (multiplying 16-bit
samples by 0.5 gives you effectively 15-bit audio). Hardware volume is lossless.

**How squeezelite handles it:**
- `output_alsa.c:181-227` — Uses ALSA mixer API (`snd_mixer_*`) for hardware volume
- Two modes: linear raw values or dB-direct (uses `snd_mixer_selem_set_playback_dB`)
- `-V` flag selects mixer control name
- When hardware volume is used, internal gain stays at FIXED_ONE (no software
  scaling)

**Suggested fix:**
This is platform-specific and complex. For now, software volume is fine. Future:
could use `pulsectl` on Linux or `osascript` on macOS to set system volume.

---

### 2.9 Player Name Persistence

**STATUS: ✅ COMPLETE (commit 4fba193)**

**Problem:** LMS sends a SETD packet with the player name. If the player restarts,
the name should be restored without requiring LMS to re-send it.

**Implementation:**
Saves player name to config file on first update, loads it on startup.

**Architecture:**
- Config directory: `~/.config/squeezy/` (respects XDG_CONFIG_HOME)
- Save on update: `_save_player_name()` writes to `~/.config/squeezy/player_name`
- Load on startup: `_load_player_name()` restores name in `__init__`
- SETD handler: Updated to call `_save_player_name()` when LMS sends new name

**Code locations:**
- `_get_config_dir()`: Returns config directory path (creates if missing)
- `_load_player_name()`: Load name from file, return None if not found
- `_save_player_name()`: Write name to file, handle errors gracefully
- `__init__`: Load saved name as fallback, use LMS-provided name as priority
- `_handle_setd()`: Save when name is updated

**Tests:** Functional verification (name persistence across restarts).

---

### 2.10 CONT Packet / Autostart Logic + Metaint

**STATUS: ✅ COMPLETE (commit 4fba193)**

**Problem:** For `autostart >= 2`, LMS sends a CONT (continuation) packet to signal
"start playing now". The packet also includes metaint field for ICY metadata synchronization.

**Implementation:**
Enhanced CONT handler to extract metaint field and decrement autostart for sync groups.

**Architecture:**
- Autostart handling: Decrement by 2 when `autostart >= 2`
- Metaint extraction: Parse u32 big-endian value at offset 4 in CONT packet
- Sync group support: `cont_received` flag gates audio start until CONT arrives
- ICY sync: Update `icy_meta_int` from CONT packet if present

**Code locations:**
- `_handle_cont()`: Extract metaint (offset 4, u32 big-endian) and update icy_meta_int
- Sync detection: Check `self.cont_received` before audio start
- Integration: Works with ICY metadata parser to handle in-stream metadata

**Tests:** Functional verification (sync group playback with metaint synchronization).

---

### 2.11 Codec Priority / Complete Format Support

**STATUS: ✅ COMPLETE (commit 4fba193)**

**Problem:** LMS decides what format to send based on the player's reported
capabilities. If we don't report all supported codecs, LMS may transcode
unnecessarily (e.g., sending MP3 instead of native FLAC).

**Implementation:**
Probes ffmpeg at startup to detect available decoders and reports them in HELO capabilities.

**Architecture:**
- Startup probing: `_probe_ffmpeg_codecs()` static method runs at initialization
- Decoder detection: Parse ffmpeg `-decoders` output, map to SlimProto short names
- Fallback: Use standard codec list if ffmpeg probing fails
- Dynamic reporting: Codec list built in `_capabilities()` using detected decoders

**Codec mapping:**
```python
mp3, flac, ogg (vorbis), aac, alac, ops (opus), wma, dsd, pcm
```

**Code locations:**
- `_probe_ffmpeg_codecs()`: Static method that calls `ffmpeg -decoders` and parses output
- `_capabilities()`: Builds codec string from detected decoders
- Error handling: Graceful fallback to standard list if probing fails
- Logging: Debug output shows detected codec list

**Benefits:**
- LMS optimizes codec selection without unnecessary transcoding
- Supports all ffmpeg decoders automatically (extensible)
- Reduces CPU usage and latency

**Tests:** Functional verification (codec reporting in HELO capabilities).

---

## Priority 3 — Robustness & Edge Cases

These are defensive fixes for real-world problems that squeezelite encountered over
its 10+ year history. Each one caused actual user issues.

---

### 3.1 CPU Spinning When Buffer Empty

**Problem:** When the audio buffer is empty and we're waiting for data, a tight loop
wastes CPU.

**How squeezelite handled it:**
- `output_alsa.c:793-795` — Sleep 10ms when `avail == 0`
- `r999` — Added time delay when data buffer is empty during track
  changes/seeking to prevent 100% CPU
- `r1230` — Fixed high CPU with PulseAudio when player is "off"

**Squeezy status:**
The generator yields silence when buffer is empty, which is fine — miniaudio
controls the callback rate. But check that the stream thread doesn't spin on
`socket.timeout` exceptions (5-second timeout should prevent this).

**Suggested fix:**
Audit all loops for tight-spin conditions. Ensure `except socket.timeout: continue`
doesn't create a busy-wait. Consider adding a `time.sleep(0.01)` in the stream
reader's timeout handler if the buffer is already large.

---

### 3.2 WAV Streams With Unknown Content-Length

**Problem:** Some WAV streams (e.g., internet radio transcoded to WAV) have an
unknown content length. If the player assumes a fixed length, playback stops after
~20 minutes (the maximum size a standard WAV header can represent).

**How squeezelite handled it:**
`pcm.c:114-127`:
```c
if ((audio_left == 0xFFFFFFFF) || (audio_left == 0x7FFFEFFC)) {
    LOG_INFO("wav audio size unknown: %u", audio_left);
    limit = false;  // Don't limit reading
}
```

Also fixed in `r1004`: WAV streams stopped after 20 minutes due to this issue.

**Suggested fix:**
When receiving PCM with WAV headers, parse the data chunk size. If it's
`0xFFFFFFFF` or `0x7FFFEFFC`, treat the stream as infinite (keep reading until
connection closes).

---

### 3.3 WAV/AIFF Header Parsing

**Problem:** LMS can send PCM data wrapped in WAV or AIFF headers. Without parsing
these, ffmpeg works fine (it auto-detects), but PCM passthrough would fail because
the header bytes would be played as noise.

**How squeezelite handles it:**
`pcm.c:77-181` — Full WAV (RIFF/WAVE) and AIFF (FORM/AIFF) header parsing:
- Detects format by magic bytes ("RIFF"+"WAVE" or "FORM"+"AIFF")
- Parses `fmt ` chunk (WAV) or `COMM` chunk (AIFF) for sample rate, channels, bit
  depth
- Finds `data` chunk (WAV) or `SSND` chunk (AIFF) to locate audio start
- AIFF sample rate is IEEE 80-bit extended float (needs special parsing)

**Suggested fix:**
For PCM passthrough, check first 12 bytes for WAV/AIFF magic. If found, parse
headers to find audio data offset and skip them. If not found, treat as raw PCM
(current behaviour).

---

### 3.4 Large MP4/Ogg Headers

**Problem:** Files with large embedded artwork can have MP4/Ogg headers exceeding
the initial buffer read size. This caused playback failures.

**How squeezelite handled it:**
`r1259-1260` — AAC and ALAC decoders updated to handle MP4 headers larger than the
initial read buffer by accumulating data across multiple reads.

Also `PR #182` — Vorbis/Opus files with large embedded artwork caused failures.

**Suggested fix:**
This mainly affects the ffmpeg path (ffmpeg handles large headers internally). But
if we ever add native codec decoding, be aware that headers can be megabytes in size
due to embedded artwork.

---

### 3.5 Ogg/Vorbis and Opus End-of-Stream

**Problem:** Ogg/Vorbis and Opus decoders could hang at end-of-stream instead of
signaling decode complete.

**How squeezelite handled it:**
- `r1173-1177` — When no frames are decoded and streaming has ended, properly
  declare end of decode
- `PR #171, r1414` — Allow last vorbis/opus decode to complete for gapless playback
- `PR #200, r1465` — Fix ogg/opus busy loop

**Suggested fix:**
Monitor ffmpeg stdout for EOF. If stdout closes but `decode_complete` isn't set
within 5 seconds, force it. This is currently handled but add logging for
diagnostics.

---

### 3.6 MP3 Gapless — LAME Encoder Delay/Padding

**Problem:** MP3 files encoded with LAME have encoder delay (leading silence) and
padding (trailing silence) that must be trimmed for proper gapless playback.

**How squeezelite handles it:**
`mad.c` — Parses LAME Xing/Info header at start of MP3 stream. Extracts encoder
delay and padding values. Skips the corresponding samples at start and end of
decode.

**Suggested fix:**
When ffmpeg decodes MP3, it should handle this automatically via its MP3 decoder.
Verify by checking if ffmpeg's output includes the encoder delay samples. If so,
no action needed. If not, consider parsing the LAME header ourselves and telling
ffmpeg to skip/trim.

---

### 3.7 Memory Management / OOM Prevention

**Problem:** Large buffers can trigger the Linux OOM killer, especially on
resource-constrained devices like Raspberry Pi.

**How squeezelite handles it:**
- Default buffer sizes: stream = 2MB, output = ~3.5MB
- `issue #165` — Squeezelite allocates 80+ MB and gets OOM-killed
- `utils.c` — `touch_memory()` pre-faults pages on Linux for deterministic memory
  usage

**Current squeezy status:**
`PCMBuffer` uses a Python `bytearray` that grows unboundedly. A 5-minute 44.1kHz
stereo 16-bit track = ~50MB of PCM data. This is fine for desktop but could be an
issue on embedded devices.

**Suggested fix:**
Add an optional max buffer size (e.g., 100MB). If the buffer exceeds it, slow down
the stream reader (apply backpressure by pausing `recv`). Also consider using a
circular buffer (see 3.8).

---

### 3.8 Circular Buffer (Ring Buffer)

**Problem:** The current `PCMBuffer` uses `bytearray` with `del buf[:n]` for reads,
which copies the entire remaining buffer on every read — O(n) instead of O(1).

**How squeezelite handles it:**
`buffer.c:33-61` — Classic ring buffer with `readp`/`writep` pointers and `wrap`
boundary:
```c
unsigned _buf_used(struct buffer *buf) {
    return buf->writep >= buf->readp ?
        buf->writep - buf->readp :
        buf->size - (buf->readp - buf->writep);
}
unsigned _buf_space(struct buffer *buf) {
    return buf->size - _buf_used(buf) - 1;  // -1 so full != empty
}
```

**Suggested fix:**
Replace `PCMBuffer` with a fixed-size ring buffer. Use `memoryview` or `mmap` for
zero-copy reads. Pre-allocate to output buffer size (e.g., 10 seconds at max
sample rate). This also solves the OOM issue (3.7).

```python
class RingBuffer:
    def __init__(self, size):
        self.buf = bytearray(size)
        self.readp = 0
        self.writep = 0
        self.size = size
```

---

### 3.9 Thread Safety Audit

**Problem:** Multiple threads access shared state (pcm_buf, playing, streaming,
decode_complete, etc.). Race conditions can cause crashes or corrupted state.

**How squeezelite handles it:**
Three separate mutexes with priority inheritance:
- `LOCK_S` — protects stream buffer and stream state
- `LOCK_O` — protects output buffer and output state
- `LOCK_D` — protects decode state

`buffer.c:157` — Uses `PTHREAD_PRIO_INHERIT` for mutexes to avoid priority
inversion between the real-time output thread and normal-priority decode thread.

**Current squeezy status:**
`PCMBuffer` has a lock. But `self.playing`, `self.streaming`, `self.decode_complete`,
`self.output_frames`, etc. are accessed from multiple threads without locks. Python's
GIL provides some protection for simple assignments, but compound operations and
boolean flag ordering are still vulnerable.

**Suggested fix:**
Identify all cross-thread state and either:
1. Use `threading.Lock()` for compound operations
2. Use `threading.Event()` for signaling (e.g., decode_complete)
3. Document which thread owns each variable

---

### 3.10 Graceful Shutdown

**Problem:** On SIGTERM/SIGINT, squeezy should cleanly disconnect from LMS, close
audio devices, and terminate ffmpeg processes. Orphan ffmpeg processes and dangling
TCP connections cause issues on restart.

**How squeezelite handles it:**
`main.c` — Signal handlers set a `running` flag. The main loop checks it and calls
cleanup functions. PID file support via `-P` flag for service management.

**Current squeezy status:**
Basic `KeyboardInterrupt` handling. May leave ffmpeg processes running.

**Suggested fix:**
Register `signal.signal(signal.SIGTERM, handler)`. In the handler, set
`self.running = False`. In cleanup, explicitly `ffmpeg_proc.kill()`, close sockets,
and close the audio device. Use `atexit.register()` as a safety net.

---

### 3.11 DSCO (Disconnect) Packet

**Problem:** When a stream disconnects, LMS should be notified with the reason so it
can decide whether to retry, advance tracks, or show an error.

**How squeezelite handles it:**
`slimproto.c:206-217`:
```c
disconnect_code: DISCONNECT_OK=0, LOCAL_DISCONNECT=1,
                 REMOTE_DISCONNECT=2, UNREACHABLE=3, TIMEOUT=4
```

Sent after stream socket closes, with the appropriate reason code.

**Current squeezy status:**
We send `build_dsco(0)` (always OK). Should distinguish between normal close,
remote close, timeout, and unreachable.

**Suggested fix:**
Track why the stream ended and send the appropriate code:
- Normal EOF: `DISCONNECT_OK` (0)
- Server closed connection: `REMOTE_DISCONNECT` (2)
- Connection refused: `UNREACHABLE` (3)
- Socket timeout: `TIMEOUT` (4)

---

### 3.12 SERV Packet — Server Redirect

**Problem:** LMS can tell the player to switch to a different server (e.g., from
local LMS to mysqueezebox.com or another LMS instance). Without handling this,
the player ignores the redirect.

**How squeezelite handles it:**
`slimproto.c:483-510`:
- Parses new server IP from SERV packet
- Special case: IP `0.0.0.1` = redirect to mysqueezebox.com
- Preserves sync group ID across server switches
- Sets `new_server` which triggers reconnect to the new server

**Suggested fix:**
Handle the `serv` message type. Parse the 4-byte server IP. If non-zero, update
`self.server_ip` and trigger reconnect. If `0.0.0.1`, resolve "squeezenetwork" DNS.

---

### 3.13 AUDE Packet — Audio Enable/Disable

**Problem:** LMS sends AUDE to enable/disable audio output (e.g., when the player
is turned "off" in the LMS UI). Without proper handling, the player wastes CPU
processing audio that won't be heard.

**How squeezelite handles it:**
Sets `output.state = OUTPUT_OFF` when disabled, `OUTPUT_STOPPED` when enabled.
When OFF, the output thread doesn't process any audio.

**Current squeezy status:**
We log "aude received" but don't act on it.

**Suggested fix:**
Parse the two bytes (spdif_enable, dac_enable). If both are 0, set a flag to
suppress audio output. If either is 1, allow playback.

---

## Priority 4 — Platform-Specific

These items are needed for reliable operation on specific platforms.

---

### 4.1 Linux ALSA Device Issues

**Problem:** ALSA has numerous quirks:
- Some devices need to be opened twice for sample rate changes to take effect
  (`output_alsa.c:688`)
- `snd_pcm_delay()` returns `EIO` when PulseAudio server doesn't exist — needs
  100ms sleep before retry (`output_alsa.c:827`)
- `avail == 0` from PulseAudio requires 10ms sleep to avoid spinning
  (`output_alsa.c:793`)
- USB DAC removal causes infinite loop (`r1250`)
- Device busy on startup should wait, not crash (`issue #153`)

**Suggested fix:**
miniaudio abstracts most ALSA issues. But if users report problems with specific
devices, consider:
- Adding a `--device` flag that lists available devices and lets users select
- Adding retry logic for device-busy errors
- Handling device removal/reconnection events

---

### 4.2 PulseAudio / PipeWire Quirks

**Problem:** Multiple issues found over squeezelite's history:
- High CPU when player is off (`r1230`)
- Sample rate reset needed for new tracks (`PR #118`)
- Max sample rate misdetection (`issue #140`)
- Buffer attribute initialization (`r1238`)
- PipeWire plays 44.1kHz content too fast (`issue #141`)
- PipeWire buffer size issues (`issue #170`)

**Suggested fix:**
miniaudio handles PulseAudio/PipeWire as backends. Monitor for these issues in
user reports. If PipeWire speed issues occur, ensure the sample rate is explicitly
set on the miniaudio device rather than relying on the default.

---

### 4.3 macOS CoreAudio

**Problem:**
- USB DAC removal handling (`r253`)
- CoreAudio CPU pinning at 11-12% (`issue #187`)
- Audio device hotplug

**Suggested fix:**
miniaudio handles CoreAudio. Test with USB DACs and monitor CPU usage. If hotplug
is needed, poll `miniaudio.Devices()` periodically and reopen device if the current
one disappears.

---

### 4.4 Windows-Specific

**Problem:**
- `WSAEWOULDBLOCK` causes random track skips (`r269`)
- `WSAECONNRESET` from large TCP receive buffers (`r1418`)
- `WSAENOTCONN` retries needed on new connections (`r1314`)
- Premature end of tracks (`r1419`)
- Intermittent stream pauses (`r1401`)
- XP compatibility required custom `poll()` (`0.9beta3`)
- WASAPI exclusive mode for bit-perfect output (`r1137`)

**Suggested fix:**
Test on Windows with `-vv` logging. Handle `ConnectionResetError` and
`BlockingIOError` in socket operations. For WASAPI exclusive mode, investigate
miniaudio's `PlaybackDevice` configuration options.

---

### 4.5 Raspberry Pi / Embedded

**Problem:**
- GPIO for amplifier relay control (`gpio.c`)
- Power scripts for Bluetooth/DAC power management
- Pi4 has different peripheral base address (`PR #84`)
- DAC powered off prevents start (`issue #173`)
- No audio as systemd service (`issue #144`)

**Suggested fix:**
Phase 1: Ensure squeezy works on Pi with default audio. Test via Docker (ARM).
Phase 2: Consider optional GPIO support via `gpiod` Python package if there's
demand.

---

## Priority 5 — Nice to Have / Future

These enhance the experience but aren't critical for a working player.

---

### 5.1 Visualization Export (VU Meters)

**How squeezelite does it:**
`output_vis.c:44-164` — Writes 16384 samples to shared memory at
`/dev/shm/squeezelite-vis`. Third-party visualizers read this for VU meters and
spectrum displays.

**Suggested fix:**
Low priority. Could expose a simple API (e.g., write recent PCM samples to a file
or shared memory) for visualizer apps.

---

### 5.2 IR Remote Code Forwarding

**How squeezelite does it:**
`ir.c` + `slimproto.c:261-274` — Receives LIRC events and forwards them to LMS as
IR packets. This lets LMS respond to physical remote controls.

**Suggested fix:**
Low priority. If there's demand, could integrate with LIRC on Linux.

---

### 5.3 Sync Group (Multi-Room) Support

**How squeezelite does it:**
- `SyncgroupID` capability in HELO
- `OUTPUT_START_AT` state with jiffies timestamp for synchronized playback start
- Device delay compensation for precise timing
- Server redirect preserves sync group membership

**Current squeezy status:**
Basic `start_at_jiffies` support exists.

**Suggested fix:**
The main gap is device delay compensation (1.5). Once that's accurate, sync should
work. Test with two squeezy instances in a sync group.

---

### 5.4 DSD / DoP Passthrough

**How squeezelite does it:**
Full DSD support via ALSA with multiple output formats: DSD_U8, DSD_U16_LE,
DSD_U32_LE, DoP (DSD over PCM). Configurable switching delay between DSD and PCM.

**Suggested fix:**
Very low priority. DSD is niche. If needed, miniaudio doesn't support DSD natively;
would need direct ALSA access.

---

### 5.5 Native Codec Libraries (Skip ffmpeg)

**How squeezelite does it:**
Uses libFLAC, libmad, libvorbis, libopus, libfaad2, libalac directly. No ffmpeg
dependency (ffmpeg is optional, only for WMA).

**Benefits:**
- No subprocess overhead
- Better control over decode state
- Simpler gapless (direct buffer management)
- Smaller memory footprint

**Suggested fix:**
Very ambitious. Could use Python bindings:
- `pyflac` for FLAC
- `pymad` or `audioread` for MP3
- `opuslib` for Opus
- Or use `ffmpeg-python` for a cleaner ffmpeg interface

The ffmpeg subprocess approach works well and supports all codecs. Native decoding
is an optimization, not a necessity.

---

### 5.6 Resampling (libsoxr equivalent)

**How squeezelite does it:**
`resample.c` — Uses libsoxr for high-quality resampling. `-u X` flag for async
resampling to max device rate.

**Suggested fix:**
ffmpeg handles resampling with the `-ar` flag. Quality can be controlled with
`-af aresample=resampler=soxr`. Low priority unless users report quality issues.

---

### 5.7 Output Thread Real-Time Priority

**How squeezelite does it:**
`squeezelite.h:274` — `OUTPUT_RT_PRIORITY = 45`. Uses `pthread_setschedparam()`
with `SCHED_FIFO` for the output thread.

**Suggested fix:**
Python threads can't easily get RT priority. On Linux, the user can run squeezy
with `chrt -f 45 squeezy` or configure systemd service with `CPUSchedulingPolicy=fifo`.
Document this rather than implementing it.

---

### 5.8 Device Listing and Selection

**How squeezelite does it:**
`-l` flag lists available audio devices. `-o` selects by name or ID.

**Current squeezy status:**
`-o` flag exists but device listing may be incomplete.

**Suggested fix:**
Add a `--list-devices` flag that prints all available audio devices from miniaudio
with their names and IDs. Useful for headless setups where the default device is
wrong.

---

## Defensive Fixes Checklist

Quick-reference checklist of specific bugs from squeezelite's history that we should
proactively guard against:

- [ ] **3.1** CPU spinning when buffer empty (sleep/yield in tight loops)
- [ ] **3.2** WAV streams with unknown Content-Length (treat 0xFFFFFFFF as infinite)
- [ ] **3.3** WAV/AIFF header parsing for PCM passthrough
- [ ] **3.5** Ogg/Vorbis end-of-stream hang (timeout on decode)
- [ ] **3.6** MP3 LAME encoder delay (verify ffmpeg handles this)
- [ ] **3.7** OOM from unbounded buffer growth (cap buffer size)
- [ ] **3.10** Orphan ffmpeg processes on shutdown (explicit kill)
- [ ] **1.7** ffmpeg non-zero exit → send STMn not STMd
- [ ] **1.4** SIGPIPE handling on Unix
- [ ] **3.11** DSCO disconnect reason codes
- [ ] **3.13** AUDE enable/disable audio output

---

## Version History of Key Squeezelite Features

For reference, when major features were added to squeezelite:

| Version | Year | Feature |
|---------|------|---------|
| 0.2beta1 | 2012 | Initial release (FLAC, MP3, Vorbis) |
| 0.5beta1 | 2012 | AAC support |
| 0.6beta1 | 2013 | Gapless playback, ICY metadata |
| 0.7 | 2013 | Crossfade with ReplayGain |
| 1.0 | 2013 | Reconnection fixes, ALSA recovery |
| 1.3 | 2014 | WMA/ALAC via FFmpeg, visualizer export |
| 1.5 | 2015 | DSD/DoP support |
| v1.9.2 | 2019 | SSL/HTTPS support |
| v1.9.3 | 2019 | Opus decoder |
| v1.9.4 | 2020 | Apple Lossless (ALAC) decoder |
| v1.9.7 | 2021 | Native PulseAudio output |
| v2.0.0 | 2024 | libgpiod v2 GPIO interface |

---

*Last updated: 2026-03-31*
*Generated by studying squeezelite source code and GitHub issue history*

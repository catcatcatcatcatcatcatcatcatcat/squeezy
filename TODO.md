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

These items affect basic playback reliability and can cause disconnects, skipped
tracks, or silent failures. Fix first.

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

These features affect the listening experience. Users will notice their absence
when comparing squeezy to a real Squeezebox or squeezelite.

---

### 2.1 Gapless Playback (Track Boundaries)

**Problem:** When one track ends and the next begins, there's a brief silence gap
and a new device open/close cycle. Squeezelite plays seamlessly across tracks.

**How squeezelite handles it:**
`output.c:126-170` — Uses a `track_start` pointer in the circular output buffer to
mark where the next track begins. The output thread plays continuously through the
boundary, applying per-track replay gain and optional crossfade at the boundary.

When a new `strm s` arrives:
1. The decoder starts writing new track data after the current track's data
2. `output.track_start = outputbuf->writep` marks the boundary
3. When `readp` reaches `track_start`, gain/rate transitions are applied
4. `output.track_started = true` triggers STMs for the new track

**Current squeezy status:**
We queue the next track and start it after current finishes (added in this session).
There's a brief gap during the swap.

**Suggested fix:**
Phase 1 (current): Queue-based transitions with small gap — **done**.
Phase 2: Use a single PCMBuffer with a `track_boundary` offset. When the audio
generator reaches the boundary, reset `output_frames`, send STMs, and continue
reading without reopening the device.

---

### 2.2 Crossfade Support

**Problem:** LMS sends crossfade parameters (fade mode, duration) in the `strm s`
packet. Without implementing them, transitions are abrupt.

**How squeezelite handles it:**
`output.c:193-250` — Five fade modes:
```c
FADE_NONE = 0, FADE_CROSSFADE, FADE_IN, FADE_OUT, FADE_INOUT
```

Crossfade uses complementary gain curves:
```c
cross_gain_in  = to_gain((float)cur_f / (float)dur_f);
cross_gain_out = FIXED_ONE - cross_gain_in;
```

Old and new track audio is mixed sample-by-sample during the fade period. Separate
replay gain is maintained for each track during the crossfade.

**Where parameters come from:** `strm s` packet fields:
- `transition_type` (byte at offset 13, minus '0')
- `transition_period` (byte at offset 14)

**Suggested fix:**
Store the transition params from `strm s`. When we have gapless (2.1 Phase 2),
apply a linear crossfade by mixing samples from the old and new track regions of
the buffer over `transition_period` seconds.

---

### 2.3 Replay Gain

**Problem:** Without replay gain, volume jumps between tracks from different albums
or sources.

**How squeezelite handles it:**
- Received: `strm s` packet field `replay_gain` (u32 at offset 18), stored as
  `output.next_replay_gain`
- Applied: `output.c:54-55` — multiplied with volume gain during output packing:
  ```c
  gainL = gain(output.gainL, output.current_replay_gain);
  ```
- Transition: `output.c:162` — `current_replay_gain = next_replay_gain` at track
  boundary

**Suggested fix:**
Parse `replay_gain` from `strm s` (4 bytes at offset 18 in big-endian). Apply as a
multiplier alongside volume in the audio generator. It's a fixed-point 16.16 value
where `0x10000` = unity gain.

---

### 2.4 ICY Metadata (Internet Radio)

**Problem:** Internet radio streams include inline metadata (station name, current
track title) via the ICY/Shoutcast protocol. Without parsing this, LMS can't display
what's playing on radio stations.

**How squeezelite handles it:**
- **Interval set by CONT packet**: `slimproto.c:401` — `stream.meta_interval =
  cont->metaint`
- **Metadata parsing**: `stream.c:642-689` — Every `meta_interval` bytes, read a
  length byte, then `length * 16` bytes of metadata
- **Sent to LMS**: `slimproto.c:232-243` — `sendMETA()` sends the parsed metadata
  block as a META packet

```c
// stream.c ICY metadata reading
if (stream.meta_next == 0) {
    u8_t c;
    _recv(fd, &c, 1, 0);
    stream.meta_left = 16 * c;  // Max 4080 bytes
    // ... read meta_left bytes of metadata text ...
}
```

**History:**
- ICY support added in v0.6beta1 alongside gapless playback
- MAX_HEADER increased to handle ICY metadata blocks (max 4080 bytes)

**Suggested fix:**
1. Handle CONT packet — store `metaint` value
2. In `_stream_to_buffer` / `_stream_to_ffmpeg`, count bytes and extract metadata
   at each interval
3. Build and send META packet to LMS with the parsed metadata

---

### 2.5 Sample Rate Switching

**Problem:** Squeezy hardcodes 44100 Hz. Content at 48kHz, 96kHz, etc. will be
resampled by ffmpeg (losing quality) or by miniaudio (potentially poorly).

**How squeezelite handles it:**
- `output.c:129-150` — Detects rate change at track boundary, calls
  `set_sample_rate()` which reopens the output device at the new rate
- `output_alsa.c:688-730` — Some ALSA devices need the device reopened *twice*
  for a new sample rate to take effect (hardware bug workaround)
- `output.rate_delay` — Configurable silence inserted during rate switch to avoid
  clicks

**Suggested fix:**
Phase 1: Let ffmpeg handle resampling (current — functional but lossy for hi-res).
Phase 2: Pass the stream's sample rate to `miniaudio.PlaybackDevice()`, creating a
new device at the correct rate for each track. Parse `pcm_sample_rate` from
`strm s` for all formats (not just PCM).

---

### 2.6 24-bit and 32-bit Audio

**Problem:** Squeezy uses 16-bit samples internally. Hi-res content (24-bit FLAC,
32-bit WAV) is downsampled by ffmpeg, losing dynamic range.

**How squeezelite handles it:**
- Internal processing is 32-bit (`BYTES_PER_FRAME = 8` for stereo)
- Output packing converts to device format: S16, S24, S24_3LE, S32
- `output_pack.c` handles all conversions with gain and clipping protection

**Suggested fix:**
Phase 1: Keep 16-bit (functional, good enough for most content).
Phase 2: Switch internal format to 32-bit (or float32). Tell ffmpeg to output
`-f s32le` instead of `-f s16le`. Update volume scaling to use 32-bit math.
Update miniaudio device to `SampleFormat.SIGNED32`.

---

### 2.7 HTTPS/SSL Stream Support

**Problem:** Some LMS configurations serve streams over HTTPS (especially with
reverse proxies or mysqueezebox.com).

**How squeezelite handles it:**
- `stream.c:110-143` — OpenSSL support with dynamic library loading
- `slimproto.c:123` — `CanHTTPS=1` capability advertised in HELO
- SSL connection with fallback: `PR #77` — If SSL fails, retry without SSL

**History:** Added in v1.9.2 (PR #65). SSL negotiation via RTMP flag added in
v1.9.8. OpenSSL 3 compatibility fixed in PR #186.

**Suggested fix:**
Python has SSL built-in. When the stream URL uses HTTPS (or LMS signals SSL via
flags), wrap the stream socket with `ssl.create_default_context().wrap_socket()`.
Advertise `CanHTTPS=1` in capabilities.

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

**Problem:** LMS sends a SETD packet with the player name. If the player restarts,
the name should be restored without requiring LMS to re-send it.

**How squeezelite handles it:**
`slimproto.c:450-478` — SETD handler:
- ID 0, length 5 = query → respond with current name
- ID 0, length > 5 = set → store in `player_name`, write to file if `-N` flag set

```c
if (name_file) {
    FILE *fp = fopen(name_file, "w");
    fputs(player_name, fp);
}
```

**Suggested fix:**
Save player name to `~/.config/squeezy/player_name` (or XDG-appropriate path).
On startup, read it back. In `_handle_setd`, if ID=0 and data present, update
and persist.

---

### 2.10 CONT Packet / Autostart Logic

**Problem:** For `autostart >= 2`, LMS sends a CONT (continuation) packet to signal
"start playing now". Without handling this, synchronized playback won't work and some
streams may never start.

**How squeezelite handles it:**
`slimproto.c:399-415`:
```c
static void process_cont(u8_t *pkt, int len) {
    if (autostart > 1) {
        autostart -= 2;
        if (stream.state == STREAMING_WAIT) {
            stream.state = STREAMING_BUFFERING;
            stream.meta_interval = stream.meta_next = cont->metaint;
        }
    }
}
```

**Current squeezy status:**
We handle CONT to set `self.cont_received = True` which gates audio start. Basic
functionality works but we don't handle the `metaint` field (needed for ICY
metadata).

**Suggested fix:**
Parse `metaint` from CONT packet and store for ICY metadata support (see 2.4).

---

### 2.11 Codec Priority / Complete Format Support

**Problem:** LMS decides what format to send based on the player's reported
capabilities. If we don't report all supported codecs, LMS may transcode
unnecessarily (e.g., sending MP3 instead of native FLAC).

**How squeezelite handles it:**
Codecs are reported in the HELO capabilities string as comma-separated short names.
The supported list: `mp3, flc, pcm, aif, ogg, aac, wma, alc, dsd, ops` (Opus).
Each codec can optionally include a priority or supported rates.

**Suggested fix:**
Report all codecs that ffmpeg supports. Check for ffmpeg at startup and probe
available decoders:
```python
result = subprocess.run(["ffmpeg", "-decoders"], capture_output=True, text=True)
# Parse for flac, mp3, vorbis, aac, alac, opus, wma, pcm_*
```
Build capabilities accordingly.

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

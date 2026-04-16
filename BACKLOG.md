# Squeezy Backlog — Deferred Features

Items parked here are understood but deliberately not being worked on.
They may be revisited in the future if priorities change.

---

## 2.6 24-bit and 32-bit Audio

**Why deferred:** Requires significant audio pipeline refactoring — variable sample
size through PCMBuffer, miniaudio device format negotiation, ffmpeg output detection,
and cross-platform device testing. Estimated 3-4 sessions of work.

**What it would take:**
1. Detect 24/32-bit streams from ffmpeg output or HTTP headers
2. Update miniaudio device initialization to request 24/32-bit format
3. Update PCMBuffer from fixed 16-bit to variable sample size
4. Update audio generator to handle 24/32-bit frames
5. Cross-platform testing (macOS CoreAudio, Linux ALSA, Windows WASAPI)

**Reference:** squeezelite uses `output_rate_mode` in STAT to tell LMS the bit depth.

---

## 2.8 Hardware Volume / OS Mixer Control

**Why skipped:** Each platform needs completely different code:
- macOS: CoreAudio HAL device enumeration
- Linux: ALSA mixer (device-dependent)
- Windows: WinMM/WASAPI APIs

Software volume (`audg` scaling in s16le) already works. OS-level volume control
is available to users without squeezy needing to touch hardware mixers.

---

## 4.2 macOS CoreAudio Device Switching

**Why deferred:** Hot-swapping audio output (e.g., unplugging headphones) requires
listening for CoreAudio device change notifications and reinitializing the miniaudio
device mid-playback. Low priority since restarting squeezy is a simple workaround.

---

## 4.4 Systemd / systemctl Integration

**Why deferred:** Documentation-only task — write a systemd unit file with automatic
restart. Low priority since most users run squeezy interactively or via Homebrew
services.

---

## 4.5 Docker / Container Support

**Why deferred:** Requires documenting PulseAudio/ALSA device pass-through and
volume mounting. A Dockerfile exists for CI testing but isn't optimized for
production use with audio output.

#!/usr/bin/env python3
"""Metadata parsing for Shoutcast/ICY streams and replay gain.

This module provides functions to:
1. Parse ICY (Shoutcast) in-stream metadata blocks
2. Extract metadata interval from HTTP response headers
3. Encode/decode replay gain values (16.16 fixed-point format)
"""

import logging

log = logging.getLogger("squeezy")


def extract_metaint(http_headers):
    """Extract the ICY metadata interval from HTTP response headers.

    Shoutcast-compatible streams include an "icy-metaint" header that
    specifies how many bytes of audio come before the next metadata block.
    For example: "icy-metaint: 8192" means 8KB of audio, then 1 byte length + metadata.

    Args:
        http_headers: Raw HTTP response headers (bytes)

    Returns:
        Metadata interval in bytes (int), or 0 if not found
    """
    try:
        headers_str = http_headers.decode("ascii", errors="replace")
        for line in headers_str.split("\r\n"):
            if line.lower().startswith("icy-metaint:"):
                metaint = int(line.split(":", 1)[1].strip())
                log.debug("ICY metadata interval: %d bytes", metaint)
                return metaint
    except Exception as e:
        log.debug("Error extracting metaint: %s", e)
    return 0


def parse_icy_metadata(data):
    """Parse an ICY metadata block and extract title, artist, album.

    ICY metadata format: 1 byte length (in 16-byte units), then length*16 bytes
    of key=value pairs separated by semicolons.
    Example: b"StreamTitle='Song Name';StreamUrl='...';StreamArtist='Artist';"

    Args:
        data: Raw metadata block (bytes, starting with 1-byte length field)

    Returns:
        Dictionary with keys 'title', 'artist', 'album' (all strings, may be empty)
    """
    result = {"title": "", "artist": "", "album": ""}

    if len(data) < 1:
        return result

    meta_len = data[0]
    if meta_len == 0:
        return result

    meta_bytes = meta_len * 16
    if len(data) < 1 + meta_bytes:
        return result

    try:
        meta_str = data[1:1+meta_bytes].decode("utf-8", errors="ignore").rstrip("\x00")

        # Parse key=value pairs separated by semicolons
        for pair in meta_str.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            key, val = pair.split("=", 1)
            key = key.strip().lower()
            # Remove quotes if present
            val = val.strip().strip("'\"")

            if key == "streamtitle":
                result["title"] = val
            elif key == "streamartist":
                result["artist"] = val
            elif key == "streamalbum":
                result["album"] = val

    except Exception as e:
        log.debug("ICY metadata parse error: %s", e)

    return result


def parse_lame_header(data):
    """Parse LAME gapless info from the first MP3 frame.

    LAME-encoded MP3 files include encoder delay and padding values in the
    Xing/Info header of the first frame. These values indicate how many
    samples of silence were added during encoding and must be stripped
    for gapless playback.

    The Xing/Info tag is at offset 36 (stereo) or 21 (mono) from the start
    of the first MPEG frame. After the Xing header and flags, the LAME tag
    contains encoder delay (12 bits) and padding (12 bits) packed into 3 bytes.

    Args:
        data: First ~200+ bytes of the MP3 stream (must include first frame)

    Returns:
        Dictionary with 'enc_delay', 'enc_padding', 'frame_count' (all int),
        or None if no LAME header found
    """
    if len(data) < 180:
        return None

    # Verify this is an MPEG audio frame (sync word: 0xFFE0+)
    if data[0] != 0xFF or (data[1] & 0xF0) != 0xF0:
        return None

    # Find Xing/Info header — offset depends on channel mode
    # Byte 3 bits 6-7: channel mode (0=stereo, 1=joint, 2=dual, 3=mono)
    xing_offset = None
    for offset in (36, 21):
        if len(data) > offset + 8:
            tag = data[offset:offset + 4]
            if tag in (b"Xing", b"Info"):
                xing_offset = offset
                break

    if xing_offset is None:
        return None

    ptr = xing_offset + 7  # Skip "Xing" + 3 bytes (VBR method, etc.)
    flags = data[ptr] if ptr < len(data) else 0

    frame_count = 0
    # Flags: bit 0 = frame count, bit 1 = byte count, bit 2 = TOC, bit 3 = quality
    if flags & 0x01:
        if ptr + 5 > len(data):
            return None
        frame_count = (data[ptr + 1] << 24 | data[ptr + 2] << 16 |
                       data[ptr + 3] << 8 | data[ptr + 4])
        ptr += 4
    if flags & 0x02:
        ptr += 4  # Skip byte count
    if flags & 0x04:
        ptr += 100  # Skip TOC
    if flags & 0x08:
        ptr += 4  # Skip quality

    # Check for LAME tag (at ptr+1)
    if ptr + 1 + 4 >= len(data):
        return None
    if data[ptr + 1:ptr + 5] != b"LAME":
        return None

    # LAME encoder delay/padding at ptr+22 (relative to start of LAME tag area)
    delay_offset = ptr + 22
    if delay_offset + 3 > len(data):
        return None

    # Three bytes encode two 12-bit values: [DDDDDDDD DDDDPPPP PPPPPPPP]
    # enc_delay  = upper 8 bits of byte 0 + upper 4 bits of byte 1 (12 bits total)
    # enc_padding = lower 4 bits of byte 1 + all 8 bits of byte 2 (12 bits total)
    enc_delay = (data[delay_offset] << 4) | (data[delay_offset + 1] >> 4)
    enc_padding = ((data[delay_offset + 1] & 0x0F) << 8) | data[delay_offset + 2]

    # MPEG Layer III uses 1152 samples per frame (ISO/IEC 11172-3)
    MP3_SAMPLES_PER_FRAME = 1152
    total_samples = frame_count * MP3_SAMPLES_PER_FRAME - enc_delay - enc_padding if frame_count else 0

    log.debug("LAME gapless: delay=%d padding=%d frames=%d total_samples=%d",
              enc_delay, enc_padding, frame_count, total_samples)

    return {
        "enc_delay": enc_delay,
        "enc_padding": enc_padding,
        "frame_count": frame_count,
        "total_samples": total_samples,
    }

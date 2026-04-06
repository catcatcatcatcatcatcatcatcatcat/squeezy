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

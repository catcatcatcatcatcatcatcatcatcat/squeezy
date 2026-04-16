#!/usr/bin/env python3
"""LMS track metadata querying via JSON-RPC.

Queries the Lyrion Music Server's JSON-RPC API for metadata about the
currently playing track. This runs in a background thread to avoid
blocking stream startup — the player can begin playback while we're
still waiting for the title/artist/album response from LMS.

The JSON-RPC endpoint lives on LMS's HTTP port (default 9000) at
/jsonrpc.js. We identify ourselves by MAC address, which LMS uses
to look up which player is asking.

Example JSON-RPC request:
    {"id": 1, "method": "slim.request",
     "params": ["aa:bb:cc:dd:ee:ff", ["title", "?"]]}

Example response:
    {"id": 1, "result": {"_title": "Song Name"}}
"""

import json
import logging
from urllib.request import Request, urlopen

from ..protocol import slimproto

log = logging.getLogger("squeezy")


def query_field(server_ip, player_id, field):
    """Query LMS for a single metadata field via JSON-RPC.

    Args:
        server_ip: LMS server IP address
        player_id: MAC address string ("aa:bb:cc:dd:ee:ff")
        field: LMS field name ("title", "artist", "album", "duration", etc.)

    Returns:
        String value from LMS, or None if query fails or field not found.
    """
    try:
        payload = json.dumps({
            "id": 1,
            "method": "slim.request",
            "params": [player_id, [field, "?"]],
        }).encode()

        url = f"http://{server_ip}:{slimproto.LMS_HTTP_PORT}{slimproto.LMS_JSONRPC_PATH}"
        req = Request(url, data=payload, headers={"Content-Type": "application/json"})

        with urlopen(req, timeout=slimproto.LMS_QUERY_TIMEOUT_SEC) as resp:
            result_dict = json.loads(resp.read())
            result = result_dict.get("result", {})
            # LMS prefixes response keys with underscore (e.g., "_title")
            return result.get(f"_{field}")

    except Exception as e:
        log.debug("LMS query for '%s' failed: %s", field, e)
        return None


def query_fields(server_ip, player_id, fields):
    """Query LMS for multiple metadata fields.

    Makes sequential HTTP requests for each field. This is not a true
    JSON-RPC batch (LMS doesn't support that) but consolidates the
    per-field queries into a single call site.

    Args:
        server_ip: LMS server IP address
        player_id: MAC address string ("aa:bb:cc:dd:ee:ff")
        fields: List of field names (e.g., ["title", "artist", "album"])

    Returns:
        Dictionary of field_name: value pairs. Fields that failed to
        query or returned None are omitted from the result.
    """
    results = {}
    for field in fields:
        value = query_field(server_ip, player_id, field)
        if value is not None:
            results[field] = value
    return results

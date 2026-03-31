"""Thin JSON-RPC client for Lyrion Music Server."""

import json
import time
import urllib.request


class LMSClient:
    def __init__(self, host="localhost", port=9000):
        self.url = f"http://{host}:{port}/jsonrpc.js"
        self._id = 0

    def _request(self, player_id, command):
        self._id += 1
        payload = json.dumps({
            "id": self._id,
            "method": "slim.request",
            "params": [player_id, command],
        }).encode()
        req = urllib.request.Request(
            self.url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())["result"]

    def is_ready(self):
        try:
            urllib.request.urlopen(f"http://{self.url.split('/')[2]}/", timeout=5)
            return True
        except Exception:
            return False

    def wait_ready(self, timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_ready():
                return True
            time.sleep(2)
        raise TimeoutError(f"LMS not ready after {timeout}s")

    def list_players(self):
        result = self._request("", ["players", "0", "100"])
        return result.get("players_loop", [])

    def wait_for_player(self, name, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for p in self.list_players():
                if p.get("name") == name:
                    return p["playerid"]
            time.sleep(1)
        raise TimeoutError(f"Player '{name}' not found after {timeout}s")

    def player_status(self, player_id):
        return self._request(player_id, ["status", "0", "100"])

    def playlist_play(self, player_id, path):
        return self._request(player_id, ["playlist", "play", f"file://{path}"])

    def play(self, player_id):
        return self._request(player_id, ["play"])

    def pause(self, player_id):
        return self._request(player_id, ["pause", "1"])

    def unpause(self, player_id):
        return self._request(player_id, ["pause", "0"])

    def stop(self, player_id):
        return self._request(player_id, ["stop"])

    def seek(self, player_id, seconds):
        """Seek to an absolute position in seconds."""
        return self._request(player_id, ["time", str(seconds)])

    def configure_media_dir(self, path):
        """Set media directory, trigger scan, and wait for it to finish."""
        self._request("", ["pref", "mediadirs", path])
        self._request("", ["rescan"])
        deadline = time.time() + 30
        while time.time() < deadline:
            result = self._request("", ["rescanprogress"])
            if not result.get("rescan"):
                return
            time.sleep(1)
        raise TimeoutError("LMS rescan did not complete in 30s")

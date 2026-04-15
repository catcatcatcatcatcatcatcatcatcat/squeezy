#!/usr/bin/env python3
"""Unix domain socket server for broadcasting playback status.

Provides a JSON-over-Unix-socket interface for external tools (e.g.,
a macOS menu bar widget) to monitor what squeezy is currently playing.

Protocol:
    - Clients connect to the Unix socket
    - Server pushes a JSON status line every STATUS_UPDATE_INTERVAL_SEC
    - Each line is a newline-terminated JSON object with fields:
      title, artist, album_artist, album, year, elapsed_ms, total_ms,
      playing, paused

The socket is created on first stream start and cleaned up on shutdown.
"""

import json
import logging
import os
import socket
import threading
import time

from ..protocol import slimproto

log = logging.getLogger("squeezy")


class StatusSocketServer:
    """Broadcasts playback status to connected Unix socket clients.

    Accepts multiple concurrent clients. Each client receives periodic
    JSON status updates until it disconnects or the server shuts down.
    """

    def __init__(self, squeezy_instance, socket_path):
        """Initialize the status server.

        Args:
            squeezy_instance: Reference to the Squeezy player for reading status
            socket_path: Path for the Unix domain socket file
        """
        self.squeezy = squeezy_instance
        self.socket_path = socket_path
        self.running = True
        self.clients = []
        self._lock = threading.Lock()

    def run(self):
        """Listen for client connections and spawn handler threads.

        Creates the socket directory if needed, binds, and accepts connections
        in a loop until self.running is set to False. Each client gets its own
        handler thread for independent status pushing.
        """
        # Create socket directory if needed
        socket_dir = os.path.dirname(self.socket_path)
        if socket_dir and not os.path.exists(socket_dir):
            try:
                os.makedirs(socket_dir, mode=0o700)
            except OSError:
                pass

        # Remove stale socket file from a previous run
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

        sock = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(self.socket_path)
            sock.listen(5)
            sock.settimeout(1)
            log.info("Status socket listening at %s", self.socket_path)

            while self.running:
                try:
                    client_sock, _ = sock.accept()
                    threading.Thread(
                        target=self._handle_client,
                        args=(client_sock,),
                        daemon=True,
                    ).start()
                except socket.timeout:
                    continue
                except OSError:
                    break
        except Exception as e:
            log.warning("Status socket error: %s", e)
        finally:
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    def _handle_client(self, client_sock):
        """Push periodic JSON status updates to a single connected client.

        Sends a JSON line every STATUS_UPDATE_INTERVAL_SEC until the client
        disconnects or the server shuts down.
        """
        try:
            while self.running:
                try:
                    status = self.squeezy._status_dict()
                    status_json = json.dumps(status) + "\n"
                    client_sock.sendall(status_json.encode("utf-8"))
                    time.sleep(slimproto.STATUS_UPDATE_INTERVAL_SEC)
                except BrokenPipeError:
                    break
                except Exception:
                    break
        except Exception:
            pass
        finally:
            try:
                client_sock.close()
            except Exception:
                pass

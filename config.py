#!/usr/bin/env python3
"""Player configuration management with XDG Base Directory support.

This module provides functions to save and load player configuration
(currently just the player name) to/from XDG-compliant directories.
"""

import os
import logging

log = logging.getLogger("squeezy")


def get_config_dir():
    """Get the squeezy config directory, creating it if necessary.

    Uses XDG_CONFIG_HOME environment variable if set, otherwise defaults to
    ~/.config/squeezy. This is the XDG Base Directory specification for user
    configuration files.

    Returns:
        Path to the config directory (created if missing)
    """
    config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    config_dir = os.path.join(config_home, "squeezy")
    os.makedirs(config_dir, exist_ok=True)
    return config_dir


def load_player_name():
    """Load player name from config file.

    Reads the saved player name from ~/.config/squeezy/player_name
    (or $XDG_CONFIG_HOME/squeezy/player_name if set).

    Returns:
        The saved player name (string), or None if not found or error occurs
    """
    try:
        config_dir = get_config_dir()
        name_file = os.path.join(config_dir, "player_name")
        if os.path.exists(name_file):
            with open(name_file, "r") as f:
                name = f.read().strip()
                if name:
                    log.debug("Loaded player name from %s: %s", name_file, name)
                    return name
    except Exception as e:
        log.warning("Failed to load player name: %s", e)
    return None


def save_player_name(name):
    """Save player name to config file.

    Writes the player name to ~/.config/squeezy/player_name
    (or $XDG_CONFIG_HOME/squeezy/player_name if set).

    Args:
        name: The player name to save (string)
    """
    try:
        config_dir = get_config_dir()
        name_file = os.path.join(config_dir, "player_name")
        with open(name_file, "w") as f:
            f.write(name)
        log.debug("Saved player name to %s: %s", name_file, name)
    except Exception as e:
        log.warning("Failed to save player name: %s", e)

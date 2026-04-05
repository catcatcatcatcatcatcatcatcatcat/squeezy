"""Squeezy - Minimal Squeezebox player for Lyrion Music Server."""

# Import Squeezy class and main function from the squeezy module
# This allows: from squeezy import Squeezy
from .squeezy import Squeezy, main

# Make miniaudio available as squeezy.miniaudio for test mocking
import miniaudio

__all__ = ["Squeezy", "main", "miniaudio"]

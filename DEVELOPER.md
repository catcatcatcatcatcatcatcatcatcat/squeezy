# Developer Guide

A walkthrough of every file and folder in this repo — what it is, why it exists,
and when you'd touch it. Written for contributors who are new to Python packaging
or this codebase.

---

## The short version

```
squeezy/
├── src/squeezy/                ← the player (modular package)
│   ├── squeezy.py              ← main orchestrator (~1,990 lines)
│   ├── __init__.py             ← package marker
│   ├── __main__.py             ← enables `python -m squeezy`
│   ├── audio/
│   │   ├── player.py           ← miniaudio device, mixing, crossfade
│   │   └── stream_decoder.py   ← HTTP streaming, FFmpeg, PCMBuffer
│   ├── protocol/
│   │   ├── handler.py          ← message dispatch (strm, cont, audg, serv, aude)
│   │   ├── slimproto.py        ← protocol constants & packet builders
│   │   └── lms_client.py       ← LMS message operations (HELO, STAT, DSCO)
│   ├── network/
│   │   └── server_connection.py ← TCP/UDP socket management, discovery
│   └── config/
│       ├── config.py           ← XDG-compliant config persistence
│       └── metadata.py         ← ICY metadata parsing
│
├── run.sh                  ← dev runner (no install needed)
├── pyproject.toml          ← single source of truth: version, deps, metadata
├── Makefile                ← shortcuts for common dev tasks
├── Dockerfile              ← Linux test environment
├── README.md               ← user-facing docs
├── DEVELOPER.md            ← this file
├── TODO.md                 ← backlog of known improvements
│
├── tests/                  ← automated tests
│   ├── conftest.py
│   ├── test_p1_reliability.py  ← 14 unit tests (connection, heartbeat, state)
│   ├── test_p2_features.py     ← 41 unit tests (gapless, crossfade, gain, ICY)
│   ├── test_integration.py     ← 14 integration tests (needs LMS + ffmpeg)
│   ├── lms_client.py
│   └── linux_distro_test.sh
│
├── .github/workflows/      ← CI (runs on every push/PR)
│   └── test.yml
│
│   ── generated / ignored by git ──
├── squeezy.egg-info/       ← pip bookkeeping (safe to delete)
├── dist/                   ← release archives (safe to delete)
├── build/                  ← build scratch space (safe to delete)
└── .venv/                  ← local virtual environment (not committed)
```

---

## Source files

### Module structure

The codebase is organized into 8 focused modules with clear layer dependencies:

| Module | Lines | Responsibility |
|--------|-------|---------------|
| `protocol/slimproto.py` | 278 | Protocol constants, packet builders, format strings |
| `config/config.py` | 69 | XDG config directory, player name persistence |
| `config/metadata.py` | 89 | ICY/Shoutcast metadata parsing |
| `network/server_connection.py` | 139 | TCP connection, UDP discovery |
| `protocol/lms_client.py` | 122 | SlimProto message sending (HELO, STAT, DSCO) |
| `protocol/handler.py` | 299 | Message dispatch & parameter extraction |
| `audio/player.py` | 285 | miniaudio device, crossfade, gain, elapsed time |
| `audio/stream_decoder.py` | 450 | HTTP fetch, FFmpeg subprocess, PCMBuffer |
| `squeezy.py` | ~1,990 | Main orchestrator, CLI, main loop, state management |

### `squeezy.py` — Main orchestrator

The central coordinator (~1,990 lines). Imports and wires together all modules.

Key sections:

| Section | What it does |
|---------|-------------|
| Constants (top) | Re-exports from `slimproto`: `SAMPLE_RATE`, `CHANNELS`, `DEVICE_BUFFER_MSEC`, etc. |
| `class Squeezy` | The player — connects to LMS, handles all protocol messages, drives audio |
| `_handle_strm_*` | Handlers for the `strm` command family (start, stop, pause, flush, skip) |
| `_stream_worker` / `_do_stream` | Downloads the HTTP audio stream in a background thread |
| `_stream_to_buffer` | PCM passthrough path — raw PCM direct to buffer, no ffmpeg |
| `_stream_to_ffmpeg` | Compressed audio path — feeds data to ffmpeg stdin |
| `_decode_reader` | Reads decoded PCM from ffmpeg stdout into the buffer |
| `_audio_generator` | Python generator yielding PCM chunks to miniaudio's callback |
| `_elapsed_ms` | Reports playback position to LMS with dynamic device-delay compensation |
| `discover` / `connect` | UDP server discovery and TCP SlimProto connection |
| `main` | CLI entry point — argument parsing, logging setup, player lifecycle |

### `pyproject.toml`

**The single source of truth for the version number** and everything PyPI,
pip, and packaging tools need to know about the project.

Things defined here:

- **`version`** — update this (and only this) when releasing. Everything else
  reads it: `squeezy.py` imports it via `importlib.metadata`, the wheel filename
  uses it, the GitHub tag should match it.
- **`dependencies`** — runtime requirements (`miniaudio`). Add new deps here.
- **`[project.optional-dependencies] test`** — test-only deps (`pytest`,
  `pytest-timeout`). Installed with `pip install ".[test]"`.
- **`[project.scripts]`** — declares `squeezy = "squeezy.squeezy:main"`, which is what
  makes the `squeezy` command available after install.
- **classifiers** — tells PyPI the supported OSes and Python versions. Update
  when we add/drop support.
- **`[project.urls]`** — links shown on the PyPI page.

> **Releasing a new version:** bump `version` in `pyproject.toml`, commit,
> tag (`git tag v0.x.y`), push. Then `python -m build && twine upload dist/*`.

### `run.sh`

Development runner script. Runs squeezy directly from source without installing:

```bash
./run.sh -n "My Speaker" -vv
```

Equivalent to `PYTHONPATH=src python3 -m squeezy "$@"`. Changes to any source
file take effect immediately on next run — no reinstall needed.

### `Makefile`

Shortcuts so you don't have to remember exact commands:

```
make install    # pipx install . (installs from local source)
make uninstall  # pipx uninstall squeezy
make run        # squeezy (run the installed command)
make dev-run    # PYTHONPATH=src python -m squeezy (run from source)
make test       # PYTHONPATH=src pytest tests/ -v --timeout=60
```

These are convenience wrappers — nothing here that isn't just a shell command.

### `Dockerfile`

A Linux (Debian Bookworm) container image used for local testing on macOS.
It installs ffmpeg, pulseaudio, and the gcc build tools needed to compile
miniaudio's C extension, then installs squeezy from the local source.

Used when you want to:
- Test that squeezy works on Linux before pushing
- Reproduce a Linux-specific bug without a VM
- Run the integration test suite against a real LMS locally

Quick start:
```bash
# Build the image
docker build -t squeezy-dev .

# Run it connected to a local LMS container
docker run --rm --network container:squeezy-lms squeezy-dev \
  squeezy -s 127.0.0.1 -n "Docker Test" -v
```

---

## Tests

### `tests/test_p1_reliability.py`

14 unit tests covering Priority 1 (Critical Reliability):
- Connection management, heartbeat, state transitions
- STAT packet format, HELO encoding
- No external dependencies needed

### `tests/test_p2_features.py`

41 unit tests covering Priority 2 (User-Facing Quality):
- Gapless playback, crossfade mixing, replay gain
- ICY metadata parsing, variable sample rate
- Codec priority, player name persistence

### `tests/test_p3_robustness.py`

18 unit tests covering Priority 3 (Robustness & Edge Cases):
- MP3 gapless (LAME header parsing)
- PCMBuffer memory limits (OOM prevention)
- DSCO disconnect packet handling
- Graceful shutdown

### `tests/conftest.py`

Pytest fixtures shared across all tests. Handles:
- Connecting to a running LMS instance (waits up to 90 seconds for it to be ready)
- Generating synthetic WAV test files and adding them to LMS's music library
- Starting and stopping squeezy as a subprocess for each test
- Environment variables: `LMS_HOST`, `LMS_MUSIC_DIR`, `LMS_MUSIC_PATH`

### `tests/test_integration.py`

14 integration tests. Each test starts a real squeezy process, connects
it to a real LMS instance (running in Docker), and verifies behaviour:
- Player registers with LMS
- Playback starts and elapsed time advances
- Pause / resume works
- Track transitions work

These tests run in CI against a Docker LMS service (see `test.yml`). They also
run locally if you have LMS accessible — set `LMS_HOST` to point at it.

### `tests/lms_client.py`

A minimal LMS JSON-RPC client used only by the test suite. Sends commands to
LMS (play, pause, get status) and reads responses. Not part of squeezy's runtime.

### `tests/linux_distro_test.sh`

A shell script that runs inside a bare Linux container and verifies squeezy
installs and plays correctly on that distro. Tests:
1. System dependencies (ffmpeg, pip, pulseaudio) install cleanly
2. `pip install` from source succeeds
3. `squeezy --help` runs
4. squeezy connects to LMS and plays audio for at least 3 seconds

Run it in Docker containers against multiple distros:
```bash
docker exec test-debian-12 bash /app/tests/linux_distro_test.sh
docker exec test-ubuntu-24-04 bash /app/tests/linux_distro_test.sh
```

---

## CI

### `.github/workflows/test.yml`

Two jobs run on every push and pull request:

**`smoke`** — fast, runs everywhere:
- Matrix: Ubuntu × macOS × Windows, Python 3.10 / 3.12 / 3.14
- Just installs squeezy and runs `squeezy --help`
- Catches import errors, missing deps, packaging mistakes

**`integration`** — slower, Linux only:
- Spins up a real LMS Docker container as a service
- Runs `pytest tests/` against it
- Python 3.11 and 3.14
- Catches protocol bugs, playback regressions

---

## Generated directories (safe to delete, not committed)

### `squeezy.egg-info/`

Created automatically by pip when you run `pip install -e .` or `pip install .`
from the repo directory. It's pip's bookkeeping folder — a cache of the package
metadata derived from `pyproject.toml`.

**You never edit these files** — they're regenerated every time you install.
The `.gitignore` excludes `*.egg-info/` so they don't get committed. If they
get out of sync or cause weird import errors, just delete the folder and
reinstall: `rm -rf squeezy.egg-info && pip install -e .`

### `dist/`

Created by `python -m build`. Contains the release artefacts ready to upload
to PyPI:
- `squeezy-X.Y.Z.tar.gz` — source distribution (sdist)
- `squeezy-X.Y.Z-py3-none-any.whl` — wheel (the faster-to-install binary-ish format)

Delete `dist/` before building a new release to avoid uploading stale files.

### `build/`

Scratch space used by `python -m build` during the wheel/sdist build process.
Safe to delete any time.

### `.venv/`

Your local virtual environment. Created by:
```bash
python3 -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[test]"
```

Never committed. Each developer has their own. The `.gitignore` excludes it.

### `.pytest_cache/`

pytest's cache directory — stores results from the last run so `pytest --lf`
(run last-failed) works. Safe to delete. Excluded by `.gitignore`.

---

## Hidden files

### `.gitignore`

Tells git to ignore the generated/local directories above:
`__pycache__/`, `*.pyc`, `.venv/`, `*.egg-info/`, `dist/`, `build/`,
`.pytest_cache/`

If you find yourself wanting to `git add` any of those, stop — they shouldn't
be committed.

---

## How to set up a dev environment

```bash
git clone https://github.com/catcatcatcatcatcatcatcatcatcat/squeezy.git
cd squeezy

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install squeezy + test dependencies in editable mode
pip install -e ".[test]"

# Verify it works
squeezy --help
squeezy --version

# Quick dev run (no install needed after code changes):
./run.sh -n "My Speaker" -vv

# Run the test suite
make test
# or: PYTHONPATH=src pytest tests/ -v --timeout=60
```

## How to release a new version

1. Update `version` in `pyproject.toml`
2. Commit: `git commit -am "chore: bump version to X.Y.Z"`
3. Tag: `git tag vX.Y.Z && git push && git push --tags`
4. Build: `rm -rf dist/ build/ && python -m build`
5. Upload: `twine upload dist/*`
6. Update Homebrew tap formula with new version + SHA256

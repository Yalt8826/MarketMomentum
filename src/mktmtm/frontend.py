"""Streamlit launcher for the MktMtm control panel.

Binds the UI to athena's tailnet IPv4 by default so it is reachable from any
tailnet device; ``--host 127.0.0.1`` gives the local-only (SSH-tunnel) mode.
The host is resolved at launch (not hardcoded), so it stays correct if the
tailnet IP ever changes and falls back to loopback when tailscale is absent.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

APP_FILE = Path(__file__).parent / "frontend_app.py"

DEFAULT_PORT = 8501


def resolve_default_host() -> str:
    """Return athena's tailnet IPv4 (first from ``tailscale ip -4``).

    Any failure — tailscale missing, non-zero exit, or an empty answer —
    falls back to ``127.0.0.1`` (local-only mode).
    """
    try:
        proc = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "127.0.0.1"
    ips = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if proc.returncode == 0 and ips:
        return ips[0]
    return "127.0.0.1"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Launch the MktMtm Streamlit control panel."
    )
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "Bind address (default: athena's tailnet IPv4 resolved at launch; "
            "use 127.0.0.1 for local-only access)."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Bind port (default: {DEFAULT_PORT}).",
    )
    args = parser.parse_args()
    host = args.host or resolve_default_host()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(APP_FILE),
            "--server.address",
            host,
            "--server.port",
            str(args.port),
            "--server.headless",
            "true",
        ],
        check=True,
    )

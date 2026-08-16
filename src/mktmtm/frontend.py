import subprocess
import sys
from pathlib import Path

APP_FILE = Path(__file__).parent / "frontend_app.py"


def main() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(APP_FILE),
        ],
        check=True,
    )

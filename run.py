"""Orchestrates running the capture server and Streamlit dashboard together."""

import signal
import subprocess
import sys
import time
from typing import List

from config import BIND_HOST, CAPTURE_PORT, validate_settings


def terminate_processes(processes: List[subprocess.Popen]) -> None:
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()
    time.sleep(0.5)
    for proc in processes:
        if proc.poll() is None:
            proc.kill()


def main() -> None:
    validate_settings()

    server_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "capture_server:app",
        "--host",
        BIND_HOST,
        "--port",
        str(CAPTURE_PORT),
    ]

    streamlit_cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "app.py",
        "--server.address",
        BIND_HOST,
        "--server.port",
        "8501",
        "--server.headless",
        "true",
        "--browser.serverAddress",
        BIND_HOST,
    ]

    processes: List[subprocess.Popen] = []

    def handle_exit(signum, frame):  # noqa: ARG001
        terminate_processes(processes)
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    server_proc = subprocess.Popen(server_cmd)
    processes.append(server_proc)

    streamlit_proc = subprocess.Popen(streamlit_cmd)
    processes.append(streamlit_proc)

    try:
        while True:
            for proc in processes:
                if proc.poll() is not None:
                    terminate_processes(processes)
                    sys.exit(proc.returncode or 0)
            time.sleep(0.5)
    except KeyboardInterrupt:
        terminate_processes(processes)
        sys.exit(0)


if __name__ == "__main__":
    main()

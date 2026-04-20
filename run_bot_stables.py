from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List

SCRIPTS = [
    "run_strategy_stables.py",
    "run_live_coinbase_stables.py",
    "run_controller_stables.py",
]

HEARTBEAT_SECS = int(os.getenv("BOT_HEARTBEAT_SECS", "1"))
SHUTDOWN_GRACE_SECS = int(os.getenv("BOT_SHUTDOWN_GRACE_SECS", "5"))


def _start_processes(root: Path) -> List[Dict[str, object]]:
    processes: List[Dict[str, object]] = []
    for script in SCRIPTS:
        path = root / script
        proc = subprocess.Popen([sys.executable, str(path)], cwd=str(root))
        processes.append({"script": script, "proc": proc})
        print(f"[launcher] started {script} pid={proc.pid}")
    return processes


def _running(processes: List[Dict[str, object]]) -> bool:
    for item in processes:
        proc = item["proc"]
        if isinstance(proc, subprocess.Popen) and proc.poll() is None:
            return True
    return False


def _stop_processes(processes: List[Dict[str, object]]) -> None:
    for item in processes:
        proc = item["proc"]
        if isinstance(proc, subprocess.Popen) and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass

    deadline = time.time() + SHUTDOWN_GRACE_SECS
    while time.time() < deadline and _running(processes):
        time.sleep(0.2)

    for item in processes:
        proc = item["proc"]
        if isinstance(proc, subprocess.Popen) and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    for item in processes:
        proc = item["proc"]
        if isinstance(proc, subprocess.Popen):
            try:
                proc.wait(timeout=1)
            except Exception:
                pass


def main() -> None:
    root = Path(__file__).resolve().parent
    processes: List[Dict[str, object]] = []

    try:
        processes = _start_processes(root)

        while True:
            for item in processes:
                script = str(item["script"])
                proc = item["proc"]
                if not isinstance(proc, subprocess.Popen):
                    continue
                code = proc.poll()
                if code is not None:
                    raise RuntimeError(f"{script} exited with code {code}")
            time.sleep(HEARTBEAT_SECS)

    except KeyboardInterrupt:
        print("[launcher] stopping")
    except Exception as exc:
        print(f"[launcher] error: {exc}")
    finally:
        _stop_processes(processes)


if __name__ == "__main__":
    main()
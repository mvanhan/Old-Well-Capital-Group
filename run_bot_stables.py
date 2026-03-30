from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = [
    "run_strategy_stables.py",
    "run_live_coinbase_stables.py",
    "run_controller_stables.py",
]


def main() -> None:
    root = Path(__file__).resolve().parent
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for script in SCRIPTS:
            proc = subprocess.Popen([sys.executable, str(root / script)], cwd=str(root))
            processes.append(proc)
            print(f"[launcher] started {script} pid={proc.pid}")

        while True:
            for proc, script in zip(processes, SCRIPTS):
                code = proc.poll()
                if code is not None:
                    raise RuntimeError(f"{script} exited with code {code}")
            time.sleep(1)
    except KeyboardInterrupt:
        print("[launcher] stopping")
    except Exception as exc:
        print(f"[launcher] error: {exc}")
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        time.sleep(1)
        for proc in processes:
            if proc.poll() is None:
                proc.kill()


if __name__ == "__main__":
    main()
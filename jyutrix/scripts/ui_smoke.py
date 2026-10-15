"""Start a disposable local demo, run DOM integration checks, stop and clean up."""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import urllib.request

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as temp:
    env = {
        **os.environ,
        "APP_MODE": "demo",
        "ADMIN_PASSWORD": "ui-test-password-12345",
        "DATABASE_URL": "sqlite:///" + str(Path(temp) / "ui.db"),
        "PUBLIC_URL": "http://127.0.0.1:8001",
        "OPENAI_API_KEY": "",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8001",
        ],
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:8001/health", timeout=1
                ).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("UI smoke server did not start")
        result = subprocess.run(
            ["node", str(root / "tests/ui-smoke.cjs")], cwd=root, env=env, timeout=40
        )
        if result.returncode:
            raise SystemExit(result.returncode)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

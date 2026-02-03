import hashlib
import os
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from typing import Optional


def compute_project_id(invocation_dir: str) -> str:
    base = os.path.basename(os.path.abspath(invocation_dir))
    return base or "project"


def _is_port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
        return True


def choose_port(project_id: str, *, host: str, base_port: int, port_range: int) -> int:
    if port_range <= 0:
        raise ValueError("port_range must be > 0")

    h = int(hashlib.sha256(project_id.encode("utf-8")).hexdigest(), 16)
    start = base_port + (h % port_range)

    # Linear probing within the range.
    for i in range(port_range):
        port = base_port + ((start - base_port + i) % port_range)
        if _is_port_free(host, port):
            return port

    # Fallback: scan higher ports (last resort).
    port = base_port + port_range
    while port < 65535:
        if _is_port_free(host, port):
            return port
        port += 1

    raise RuntimeError("No free port found for Streamlit UI.")


@dataclass
class UIProcess:
    process: subprocess.Popen
    url: str
    port: int
    host: str
    log_path: str
    project_id: str

    def is_running(self) -> bool:
        return self.process.poll() is None

    def stop(self, *, timeout_sec: float = 3.0) -> None:
        if not self.is_running():
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            self.process.kill()


def start_streamlit_ui(
    *,
    log_dir: str,
    run_id: str,
    repo_path: str,
    invocation_dir: str,
    enabled: bool,
    host: str = "127.0.0.1",
    base_port: int = 8501,
    port_range: int = 500,
    open_browser: bool = False,
) -> Optional[UIProcess]:
    if not enabled:
        return None

    try:
        project_id = compute_project_id(invocation_dir)
        port = choose_port(project_id, host=host, base_port=base_port, port_range=port_range)
        url = f"http://{host}:{port}"
    except Exception:
        return None

    app_path = os.path.join(os.path.dirname(__file__), "ui", "luigi_app.py")
    ui_log_path = os.path.join(log_dir, "streamlit.log")
    os.makedirs(log_dir, exist_ok=True)

    env = os.environ.copy()
    env["LUIGI_LOG_DIR"] = os.path.abspath(log_dir)
    env["LUIGI_RUN_ID"] = run_id
    env["LUIGI_REPO_PATH"] = os.path.abspath(repo_path)
    env["LUIGI_PROJECT_ID"] = project_id

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        app_path,
        "--server.address",
        host,
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]

    try:
        log_f = open(ui_log_path, "a")
        proc = subprocess.Popen(cmd, env=env, stdout=log_f, stderr=subprocess.STDOUT)
        try:
            log_f.close()
        except Exception:
            pass
    except Exception:
        return None

    ui = UIProcess(process=proc, url=url, port=port, host=host, log_path=ui_log_path, project_id=project_id)

    # Give Streamlit a brief moment to start; if it crashes immediately, report failure.
    time.sleep(0.5)
    if proc.poll() is not None:
        return None

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    return ui


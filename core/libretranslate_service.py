# -*- coding: utf-8 -*-
"""Manage a local LibreTranslate process (Docker or Python)."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

CONTAINER_NAME = "mc-libretranslate"
IMAGE_NAME = "libretranslate/libretranslate"
DEFAULT_URL = "http://127.0.0.1:5000"
STATE_NAME = "libretranslate_service.json"


def parse_host_port(base_url: str) -> Tuple[str, int]:
    raw = (base_url or DEFAULT_URL).strip() or DEFAULT_URL
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port in (80, 443) and ":5000" in (base_url or ""):
        port = 5000
    if not parsed.port and "5000" in (base_url or DEFAULT_URL):
        port = 5000
    if not parsed.port:
        port = 5000
    return host, int(port)


def state_path(config_dir: Optional[Path] = None) -> Path:
    if config_dir is None:
        config_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MinecraftTranslator"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / STATE_NAME


def load_state(config_dir: Optional[Path] = None) -> dict:
    path = state_path(config_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(data: dict, config_dir: Optional[Path] = None) -> None:
    path = state_path(config_dir)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_state(config_dir: Optional[Path] = None) -> None:
    path = state_path(config_dir)
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def find_docker() -> Optional[str]:
    which = shutil.which("docker")
    if which:
        return which
    candidates = [
        r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
        r"C:\Program Files\Docker\Docker\resources\docker.exe",
        "/usr/bin/docker",
        "/usr/local/bin/docker",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def _python_candidates() -> list:
    out = []
    if not getattr(sys, "frozen", False):
        out.append(sys.executable)
    for name in ("py", "python", "python3"):
        found = shutil.which(name)
        if found and found not in out:
            out.append(found)
    return out


def find_libretranslate_launcher() -> Optional[Tuple[str, list]]:
    """Return (kind, argv_prefix). kind in docker|cli|module."""
    cli = shutil.which("libretranslate")
    if cli:
        return "cli", [cli]
    for py in _python_candidates():
        try:
            proc = subprocess.run(
                [py, "-c", "import libretranslate"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if proc.returncode == 0:
                return "module", [py, "-m", "libretranslate"]
        except Exception:
            continue
    return None


def health_check(base_url: str, timeout: float = 2.5) -> Tuple[bool, str]:
    host, port = parse_host_port(base_url)
    root = f"http://{host}:{port}".rstrip("/")
    last = "unreachable"
    for path in ("/languages", "/"):
        url = root + path
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                if 200 <= int(code) < 300:
                    return True, f"OK {url}"
        except Exception as exc:
            last = str(exc)
            continue
    return False, last


def _run(cmd: list, timeout: Optional[float] = 60) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or ""), (exc.stderr or "timeout")
    except Exception as exc:
        return 1, "", str(exc)


def docker_container_running(docker: str) -> bool:
    code, out, _ = _run([docker, "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME], timeout=20)
    return code == 0 and out.strip().lower() == "true"


def start_docker(docker: str, host: str, port: int, log: Callable[[str], None]) -> Tuple[bool, str]:
    # reuse existing container
    code, _, err = _run([docker, "inspect", CONTAINER_NAME], timeout=20)
    if code == 0:
        if docker_container_running(docker):
            return True, f"Docker 容器已在跑（{CONTAINER_NAME}）"
        log(f"INFO  啟動既有 Docker 容器 {CONTAINER_NAME}…")
        code, out, err = _run([docker, "start", CONTAINER_NAME], timeout=120)
        if code == 0:
            save_state({"backend": "docker", "container": CONTAINER_NAME, "port": port})
            return True, "已啟動 Docker 容器"
        return False, err or out or "docker start 失敗"

    log(f"INFO  建立 Docker 容器 {CONTAINER_NAME}（首次可能要下載映像）…")
    cmd = [
        docker, "run", "-d",
        "--name", CONTAINER_NAME,
        "-p", f"{port}:5000",
        IMAGE_NAME,
    ]
    code, out, err = _run(cmd, timeout=600)
    if code != 0:
        return False, err or out or "docker run 失敗"
    save_state({"backend": "docker", "container": CONTAINER_NAME, "port": port, "id": out.strip()})
    return True, "已建立並啟動 Docker 容器"


def stop_docker(docker: str, log: Callable[[str], None]) -> Tuple[bool, str]:
    if not docker_container_running(docker):
        clear_state()
        return True, "Docker 容器本來就沒在跑"
    log(f"INFO  停止 Docker 容器 {CONTAINER_NAME}…")
    code, out, err = _run([docker, "stop", CONTAINER_NAME], timeout=120)
    clear_state()
    if code == 0:
        return True, "已停止 Docker 容器"
    return False, err or out or "docker stop 失敗"


def _popen(cmd: list) -> subprocess.Popen:
    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def start_python(launcher: Tuple[str, list], host: str, port: int, log: Callable[[str], None]) -> Tuple[bool, str]:
    kind, prefix = launcher
    cmd = list(prefix) + ["--host", host, "--port", str(port)]
    log(f"INFO  以 Python 啟動 LibreTranslate：{' '.join(cmd)}")
    try:
        proc = _popen(cmd)
    except Exception as exc:
        return False, f"啟動失敗: {exc}"
    save_state({"backend": kind, "pid": proc.pid, "port": port, "cmd": cmd})
    return True, f"已啟動本機行程 PID {proc.pid}"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        code, out, _ = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], timeout=15)
        return code == 0 and str(pid) in (out or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_python(pid: int, log: Callable[[str], None]) -> Tuple[bool, str]:
    if not _pid_alive(pid):
        clear_state()
        return True, "本機行程本來就沒在跑"
    log(f"INFO  結束 LibreTranslate 行程 PID {pid}…")
    try:
        if os.name == "nt":
            _run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=30)
        else:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
    except Exception as exc:
        return False, str(exc)
    clear_state()
    return True, f"已結束行程 PID {pid}"


def install_libretranslate(log: Callable[[str], None]) -> Tuple[bool, str]:
    py_list = _python_candidates()
    if not py_list:
        return False, "找不到可用的 Python，請先安裝 Python 3 或 Docker Desktop"
    py = py_list[0]
    log(f"INFO  正在安裝 libretranslate（{py} -m pip install libretranslate）…")
    code, out, err = _run([py, "-m", "pip", "install", "--upgrade", "libretranslate"], timeout=900)
    if code != 0:
        return False, err or out or "pip install 失敗"
    return True, "libretranslate 安裝完成"


def wait_until_healthy(base_url: str, timeout_sec: float = 180.0, log: Optional[Callable[[str], None]] = None) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        ok, msg = health_check(base_url, timeout=2.0)
        if ok:
            if log:
                log(f"INFO  LibreTranslate 已就緒：{msg}")
            return True
        time.sleep(1.5)
    return False


def status(base_url: str, config_dir: Optional[Path] = None) -> dict:
    ok, detail = health_check(base_url)
    state = load_state(config_dir)
    backend = state.get("backend")
    running_managed = False
    if backend == "docker":
        docker = find_docker()
        running_managed = bool(docker and docker_container_running(docker))
    elif state.get("pid"):
        running_managed = _pid_alive(int(state["pid"]))
    return {
        "healthy": ok,
        "detail": detail,
        "backend": backend,
        "managed_running": running_managed,
        "state": state,
    }


def start_service(base_url: str, log: Callable[[str], None], install_if_missing: bool = True,
                  config_dir: Optional[Path] = None) -> Tuple[bool, str]:
    host, port = parse_host_port(base_url)
    ok, detail = health_check(base_url)
    if ok:
        return True, f"本機服務已在運行（{detail}）"

    docker = find_docker()
    if docker:
        ok, msg = start_docker(docker, host, port, log)
        if not ok:
            return False, msg
        if wait_until_healthy(base_url, timeout_sec=300, log=log):
            return True, msg + "，健康檢查通過"
        return False, msg + "，但服務尚未就緒（仍在下載模型？請稍後再測連線）"

    launcher = find_libretranslate_launcher()
    if launcher is None and install_if_missing:
        ok, msg = install_libretranslate(log)
        if not ok:
            return False, msg
        launcher = find_libretranslate_launcher()
    if launcher is None:
        return False, (
            "找不到 Docker，也尚未安裝 libretranslate。\n"
            "請安裝 Docker Desktop，或執行：pip install libretranslate"
        )

    ok, msg = start_python(launcher, host, port, log)
    if not ok:
        return False, msg
    if wait_until_healthy(base_url, timeout_sec=300, log=log):
        return True, msg + "，健康檢查通過"
    return False, msg + "，但服務尚未就緒（首次會下載語言模型，請稍候再試）"


def stop_service(base_url: str, log: Callable[[str], None],
                 config_dir: Optional[Path] = None) -> Tuple[bool, str]:
    state = load_state(config_dir)
    backend = state.get("backend")
    docker = find_docker()
    if backend == "docker" or (docker and docker_container_running(docker)):
        if not docker:
            return False, "找不到 docker"
        return stop_docker(docker, log)
    pid = state.get("pid")
    if pid:
        return stop_python(int(pid), log)
    # best-effort: if healthy but unmanaged, refuse to kill unknown owner
    ok, _ = health_check(base_url)
    if ok:
        return False, "偵測到埠上有服務，但不是本程式啟動的，請手動關閉"
    clear_state()
    return True, "本機服務未在運行"

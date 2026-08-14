#!/usr/bin/env python
"""Organize 子系统容器级半 E2E 驱动（docker-compose.organize-e2e.yml）。

全链路：本地构造一个真实 torrent（内容预置进共享卷）并添加进 Transmission
容器 → app 启动前一次性 ORM seed「completed DownloadTask」（含
StorageVolume + DownloaderInstance 卷绑定）→ 启动 app（SCHEDULER_ENABLED）
→ API 创建 Library 与 auto_execute 整理规则 → 每分钟
notify tick 自动完成「停种 → 建通知 → organize 规划落库 → 后台执行 →
文件落 Library → 源目录清理 → 任务清理（真实 RPC remove_torrent）」→
断言终态。

共享卷：transmission 挂 /downloads，app 挂 /mnt/shared/downloads（同一
named volume、不同挂载点，验证卷绑定路径解析）；Library root 为
organize-e2e-media 卷（app 视角 /media）。

用法（仓库根目录）：
    uv run python scripts/organize_e2e.py           # 跑全链路，保留环境供检查
    uv run python scripts/organize_e2e.py --down    # 跑完拆除（含 volumes）
    uv run python scripts/organize_e2e.py --teardown-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.organize-e2e.yml"
BASE_URL = "http://localhost:9011"
API_KEY = "organize-e2e-key"

# 与 scripts/organize_e2e_seed.py 的种子数据对应
TORRENT_NAME = "Hamnet.2025.1080p"
FILE_NAME = "Hamnet.2025.1080p.mkv"
FILE_SIZE = 262144  # 恰好一个 piece
EXPECTED_LIBRARY_FILE = "/media/movies/Hamnet (2025)/Hamnet (2025).mkv"
PROCESS_TORRENT_DIR = "/mnt/shared/downloads/complete/Hamnet.2025.1080p"
PROCESS_DOWNLOAD_ROOT = "/mnt/shared/downloads/complete"


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    log("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(
        cmd, check=check, text=True,
        capture_output=capture, cwd=REPO_ROOT,
    )


def compose(*args: str, **kw) -> subprocess.CompletedProcess:
    return run(["docker", "compose", "-f", str(COMPOSE_FILE), *args], **kw)


def api(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def make_torrent(workdir: Path) -> tuple[Path, Path, str]:
    """构造单文件 torrent：返回 (payload 目录, .torrent 路径, info_hash hex)。

    pieces 按真实内容计算，Transmission 添加后校验通过直接做种——本环境
    无外部 peer，从不依赖真实下载。
    """
    try:
        import bencodepy
    except ImportError:  # pragma: no cover
        sys.exit("bencodepy 不可用；请用 `uv run` 运行本脚本（dev 依赖）")

    payload_dir = workdir / TORRENT_NAME
    payload_dir.mkdir(parents=True)
    content = bytes(range(256)) * (FILE_SIZE // 256)
    (payload_dir / FILE_NAME).write_bytes(content)

    piece_length = 262144
    pieces = b"".join(
        hashlib.sha1(content[i:i + piece_length]).digest()
        for i in range(0, len(content), piece_length)
    )
    info = {
        b"name": TORRENT_NAME.encode(),
        b"piece length": piece_length,
        b"pieces": pieces,
        b"files": [{b"length": len(content), b"path": [FILE_NAME.encode()]}],
    }
    torrent = {
        b"announce": b"http://127.0.0.1:6969/announce",
        b"info": info,
    }
    torrent_path = workdir / "e2e.torrent"
    torrent_path.write_bytes(bencodepy.encode(torrent))
    info_hash = hashlib.sha1(bencodepy.encode(info)).hexdigest()
    return payload_dir, torrent_path, info_hash


def transmission_remote(*args: str) -> str:
    out = compose(
        "exec", "-T", "transmission",
        "transmission-remote", "127.0.0.1:9091", *args,
        capture=True,
    )
    return out.stdout + out.stderr


def add_torrent(torrent_path: Path, info_hash: str) -> int:
    """把 torrent 添加进 Transmission 容器，返回 torrent id。"""
    compose("cp", str(torrent_path), "transmission:/tmp/e2e.torrent")
    out = transmission_remote("-a", "/tmp/e2e.torrent", "-w", "/downloads/complete")
    if "success" not in out.lower():
        sys.exit(f"添加 torrent 失败：{out}")
    info = transmission_remote("-t", info_hash, "-i")
    for line in info.splitlines():
        if line.strip().startswith("Id:"):
            return int(line.split(":", 1)[1].strip())
    sys.exit(f"无法从 transmission-remote -i 解析 torrent id：{info}")


def seed_database(torrent_id: int) -> dict:
    """app 启动前一次性 ORM seed（Turso 单进程文件锁，不能与 app 并行写）。"""
    out = compose(
        "run", "--rm", "--no-deps", "-T",
        "--volume", f"{REPO_ROOT}/scripts:/app/scripts:ro",
        "app", "uv", "run", "--no-project", "python",
        "/app/scripts/organize_e2e_seed.py",
        "--torrent-id", str(torrent_id),
        capture=True,
    )
    for line in reversed(out.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    sys.exit(f"seed 脚本未输出 id 清单：\n{out.stdout}\n{out.stderr}")


def wait_plan_terminal(timeout: float = 300.0) -> dict:
    """轮询直到出现 done/failed 计划（notify tick 每分钟一次）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        plans = api("GET", "/organize/plans?page_size=100")["data"]
        for plan in plans:
            if plan["status"] in ("done", "failed"):
                return plan
        time.sleep(5)
    sys.exit("超时：没有计划进入终态（tick 每分钟一次，正常 1-2 分钟内完成）")


def assert_container_path(container: str, path: str, *, expect_exists: bool) -> None:
    probe = "test -e" if expect_exists else "test ! -e"
    result = compose("exec", "-T", container, "sh", "-c", f"{probe} {path!r}",
                     check=False, capture=True)
    state = "存在" if expect_exists else "不存在"
    if result.returncode != 0:
        sys.exit(f"断言失败：{container} 内 {path} 应{state}")
    log(f"断言通过：{container}:{path} {state}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--down", action="store_true", help="跑完拆除环境（含 volumes）")
    parser.add_argument("--teardown-only", action="store_true", help="只拆除环境")
    args = parser.parse_args()

    if args.teardown_only:
        compose("down", "-v", "--remove-orphans", check=False)
        return

    compose("down", "-v", "--remove-orphans", check=False)  # 从干净状态开始
    with tempfile.TemporaryDirectory(prefix="organize-e2e-") as tmp:
        workdir = Path(tmp)
        payload_dir, torrent_path, info_hash = make_torrent(workdir)
        log(f"已构造 torrent（info_hash={info_hash}）")

        compose("up", "-d", "--wait", "transmission")
        # 文件预置进共享卷（daemon 视角 /downloads/complete/<torrent>/）
        compose("cp", str(payload_dir), "transmission:/downloads/complete/")
        torrent_id = add_torrent(torrent_path, info_hash)
        log(f"torrent 已添加进 Transmission（id={torrent_id}）")

        ids = seed_database(torrent_id)
        log(f"DB seed 完成：{ids}")

        compose("up", "-d", "--wait", "app")

    # API 建 Library + auto_execute 规则（模板不带 {category}，全自动）
    lib = api("POST", "/libraries", {
        "name": "Movies", "root_path": "/media/movies", "kind": "movie",
    })["data"]
    api("POST", "/organize-rules", {
        "name": "e2e-movies", "priority": 100, "library_id": lib["id"],
        "path_template": "{title} ({year})/{title} ({year}){ext}",
        "auto_execute": True,
    })
    log("Library 与 auto_execute 规则已创建，等待每分钟 tick 自动规划执行…")

    plan = wait_plan_terminal()
    if plan["status"] != "done":
        detail = api("GET", f"/organize/plans/{plan['id']}")["data"]
        sys.exit(f"计划失败：{detail.get('error_message')}")
    log(f"计划 {plan['id']} 执行完成（auto_execute 全自动链路）")

    # 终态断言：文件落 Library、源目录清空、下载根保留、任务清理
    assert_container_path("app", EXPECTED_LIBRARY_FILE, expect_exists=True)
    assert_container_path("app", PROCESS_TORRENT_DIR, expect_exists=False)
    assert_container_path("app", PROCESS_DOWNLOAD_ROOT, expect_exists=True)
    tasks = api("GET", "/tasks?page_size=100")["data"]
    task = next((t for t in tasks if t["id"] == ids["task_id"]), None)
    if task is None or task["status"] != "cancelled":
        sys.exit(f"任务清理断言失败：{task}")
    log("断言通过：下载任务已清理（status=cancelled，torrent 已经真实 RPC 移除）")

    log("半 E2E 全部通过 ✅")
    if args.down:
        compose("down", "-v", "--remove-orphans", check=False)
    else:
        log("环境保留中；拆除：docker compose -f docker-compose.organize-e2e.yml down -v")


if __name__ == "__main__":
    main()

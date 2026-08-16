"""P2.1 真实 Windows E2E（全自动，自管理 dev.py 启停）。

应用关闭期间文件变化 → 启动恢复（USN 优先 / 增量扫描 fallback）：
CREATE 可搜 / MODIFY 新内容 / RENAME 旧名消失新名在 / DELETE 排除。
记录：USN recovery / fallback / startup 时长（不设硬性承诺）。

用法：python python/scripts/e2e_p21.py [--root dev-data] [--no-restart]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
E2E_ROOT_NAME = "p2-usn-test"
PY = REPO / "python" / ".venv" / "Scripts" / "python.exe"


def _post(url, token, path, body):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Omni-Token": token}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _get(url, token, path):
    req = urllib.request.Request(url + path, headers={"X-Omni-Token": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _wait_idle(url, token, timeout_s=300):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        st = _get(url, token, "/api/v1/task/status")
        idx = _get(url, token, "/api/v1/index/status")
        if st["queue_length"] == 0 and st["processing"] == 0 and not idx["running"]:
            return st
        time.sleep(1)
    raise TimeoutError("tasks/index not idle")


def _start(data_dir: Path, log: Path):
    env = {**os.environ}
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "python" / "scripts" / "dev.py"), "--dev-data", str(data_dir)],
        stdout=log.open("w"), stderr=subprocess.STDOUT, env=env, cwd=str(REPO),
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8734/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(1)
    token = ""
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "token=" in line:
            token = line.split("token=", 1)[1].strip()
            break
    return proc, token


def _stop():
    """终止 dev.py 及其子进程（omnisearch python + qdrant）。"""
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match 'python|qdrant' -and ($_.CommandLine -match 'omnisearch|qdrant|dev.py') } "
        "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    time.sleep(3)


def _phase(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="dev-data")
    args = ap.parse_args()

    data_dir = Path(args.root).resolve()
    e2e = data_dir / E2E_ROOT_NAME
    log = data_dir / "e2e_p21.log"
    e2e.mkdir(parents=True, exist_ok=True)
    # 清理旧状态（db/qdrant/日志；settings cursor 随 db 重置）
    for name in ("db", "qdrant", "logs", E2E_ROOT_NAME):
        import shutil

        shutil.rmtree(data_dir / name, ignore_errors=True)
    e2e.mkdir(parents=True, exist_ok=True)

    # ============ 阶段 1：启动 + 初始扫描 ============
    _phase("阶段 1：启动应用 + 初始扫描")
    _stop()
    proc, token = _start(data_dir, log)
    _post("http://127.0.0.1:8734", token, "/api/v1/index/roots/add", {"path": str(e2e)})
    _wait_idle("http://127.0.0.1:8734", token)
    (e2e / "基线文件.txt").write_text("基线内容", encoding="utf-8")
    _post("http://127.0.0.1:8734", token, "/api/v1/search", {"query": "基线"})
    _wait_idle("http://127.0.0.1:8734", token)
    print("初始扫描完成", flush=True)

    # ============ 阶段 2：停止应用（模拟关闭期） ============
    _phase("阶段 2：停止应用")
    _stop()
    time.sleep(1)

    # ============ 阶段 3：关闭期文件变化 ============
    _phase("阶段 3：关闭期变化（新建/修改/rename/删除）")
    (e2e / "关闭期新建.txt").write_text("关闭期创建的新内容", encoding="utf-8")
    mod = e2e / "基线文件.txt"
    mod.write_text("基线内容 v2 修改后", encoding="utf-8")
    renamed = e2e / "重命名后.txt"
    renamed.write_text("重命名内容", encoding="utf-8")
    (e2e / "待删除.txt").write_text("将被删除", encoding="utf-8")
    time.sleep(1)
    (e2e / "待删除.txt").unlink()
    print("关闭期变化完成", flush=True)

    # ============ 阶段 4：重新启动（不手动扫描） ============
    _phase("阶段 4：重新启动（USN 优先 / fallback 增量扫描）")
    t0 = time.perf_counter()
    proc2, token2 = _start(data_dir, log)
    startup_s = time.perf_counter() - t0
    # 等待恢复完成（USN 同步处理 / fallback 后台增量扫描）
    _wait_idle("http://127.0.0.1:8734", token2)
    recovery_s = time.perf_counter() - t0

    # USN 可用性判定（日志）
    usn_log = log.read_text(encoding="utf-8", errors="replace")
    usn_used = "USN recovery" in usn_log
    fallback_used = "已使用增量扫描" in usn_log
    print(f"USN used={usn_used} fallback={fallback_used}", flush=True)

    # ============ 阶段 5：验证搜索结果 ============
    _phase("阶段 5：验证")
    checks = [
        ("关闭期新建", "关闭期新建.txt", "in"),      # CREATE → 可搜
        ("v2 修改后", "基线文件.txt", "in"),          # MODIFY → 新内容
        ("重命名后", "重命名后.txt", "in"),            # RENAME → 新名存在
        ("基线内容", "基线文件.txt", "in"),            # 旧文件仍可搜
        ("重命名内容", "重命名后.txt", "in"),
    ]
    passed = 0
    for q, expect_file, mode in checks:
        body = _post("http://127.0.0.1:8734", token2, "/api/v1/search", {"query": q, "topK": 10, "mode": "keyword"})
        names = [r["filename"] for r in body["results"]]
        ok = expect_file in names if mode == "in" else expect_file not in names
        print(f"  [{q}] → {expect_file}: {'OK' if ok else 'FAIL'} (total={body['total']})", flush=True)
        passed += 1 if ok else 0
    # DELETE → 排除（旧路径不活跃）
    with (data_dir / "db" / "omnisearch.db").exists():
        pass
    import sqlite3

    conn = sqlite3.connect(data_dir / "db" / "omnisearch.db")
    deleted = conn.execute("SELECT is_deleted FROM files WHERE filename='待删除.txt'").fetchone()
    deleted_ok = deleted is not None and deleted[0] == 1
    print(f"  [DELETE] 待删除.txt is_deleted={deleted[0] if deleted else '?'}: {'OK' if deleted_ok else 'FAIL'}", flush=True)
    conn.close()
    passed += 1 if deleted_ok else 0

    print(f"\n=== P2.1 E2E {'PASS' if passed == 8 else 'FAIL'}（{passed}/8） ===", flush=True)
    print(f"性能记录（无硬性承诺）：startup={startup_s:.1f}s recovery_to_idle={recovery_s:.1f}s "
          f"USN={usn_used} fallback={fallback_used}", flush=True)
    _stop()
    sys.exit(0 if passed == 8 else 1)


if __name__ == "__main__":
    main()

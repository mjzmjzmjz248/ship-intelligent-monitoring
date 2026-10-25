"""端到端自动走查：在临时目录里启动真实的岸端和船端进程，按演示脚本操作并检查结果。

用法：python scripts/smoke_e2e.py        （约 3 分钟；不影响 runtime/ 下的演示数据）

检查内容：
 1. 合成视频中人员离开 → 船端 A1 告警 → 岸端收到，截图和视频 MD5 校验通过且可下载
 2. 人员返回 → 事件恢复；operator 确警、处置
 3. 视频黑屏段 → A3 设备异常，且黑屏期间不误报离岗
 4. sync off → 岸端判定终端离线 A4；离线期间模拟超速 A6、AIS 中断 A5、视频循环后再次离岗 A1
 5. sync on → A4 恢复；离线期间的事件补传、标记补传、数量与船端一致、无重复
 6. 同日第 2 次 A1 → 船长通知
"""
from __future__ import annotations

import hashlib
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.timeutil import parse_iso  # noqa: E402
from edge.store import EdgeStore  # noqa: E402
from shore.bootstrap import init  # noqa: E402
from shore.db import Database  # noqa: E402

SHORE_PORT, CONSOLE_PORT = 8010, 8110
IS_WIN = sys.platform.startswith("win")
results: list[tuple[str, bool, str]] = []
T0 = time.monotonic()


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{time.monotonic() - T0:6.1f}s] {'通过' if ok else '失败'}  {name}  {detail}", flush=True)


def wait_for(fn, timeout: float, interval: float = 0.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            v = fn()
            if v:
                return v
        except httpx.HTTPError:
            pass
        time.sleep(interval)
    return None


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ship_smoke_"))
    pw = secrets.token_urlsafe(8)
    init(Database(tmp / "shore.db"), "configs/demo.yaml", pw, None)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
    logs = open(tmp / "procs.log", "w", encoding="utf-8")
    shore_p = subprocess.Popen([sys.executable, "-m", "shore", "--port", str(SHORE_PORT), "--db", str(tmp / "shore.db"),
                                "--storage", str(tmp / "evidence")], cwd=ROOT, stdout=logs, stderr=logs,
                               creationflags=flags)
    edge_p = subprocess.Popen([sys.executable, "-m", "edge", "--config", "configs/demo.yaml",
                               "--shore", f"http://127.0.0.1:{SHORE_PORT}", "--run-dir", str(tmp / "edge"),
                               "--console-port", str(CONSOLE_PORT)], cwd=ROOT, stdout=logs, stderr=logs,
                              creationflags=flags)
    web = httpx.Client(base_url=f"http://127.0.0.1:{SHORE_PORT}", timeout=10, trust_env=False)
    con = httpx.Client(base_url=f"http://127.0.0.1:{CONSOLE_PORT}", timeout=10, trust_env=False)
    try:
        assert wait_for(lambda: web.post("/api/auth/login", json={"username": "operator", "password": pw}).status_code == 200, 20)
        events = lambda **q: web.get("/api/events", params={"limit": 100, **q}).json()["items"]  # noqa: E731
        detail = lambda eid: web.get(f"/api/events/{eid}").json()  # noqa: E731

        # 1. 首次离岗
        a1 = wait_for(lambda: events(alarm_type="A1_BRIDGE_ABSENCE"), 60)
        check("A1 驾驶室无人在岗告警到达岸端", bool(a1))
        eid = a1[0]["event_id"]
        d = wait_for(lambda: (x := detail(eid)) and all(e["status"] == "VERIFIED" for e in x["evidence"]) and x, 25)
        check("截图和视频上传并通过 MD5 校验", bool(d), str([(e["kind"], e["status"]) for e in detail(eid)["evidence"]]))
        if d:
            for e in d["evidence"]:
                body = web.get(f"/api/evidence/{e['evidence_id']}/file").content
                check(f"下载{e['kind']}与岸端记录 MD5 一致", hashlib.md5(body).hexdigest() == e["md5"],
                      f"{len(body) // 1024} KB")
            vid = next(e for e in d["evidence"] if e["kind"] == "video")
            tr = vid["time_range"]
            span = (parse_iso(tr[1]) - parse_iso(tr[0])).total_seconds()
            check("视频覆盖触发前后约 10 秒", span >= 9.0, f"{span:.1f} 秒，{vid['edge_status']} {vid['missing_reason'] or ''}")
            p = d["payload"]
            lat = (parse_iso(d["triggered_at"]) - parse_iso(p["condition_satisfied_at"])).total_seconds()
            snd = (parse_iso(d["sound_started_at"]) - parse_iso(p["condition_satisfied_at"])).total_seconds() if d["sound_started_at"] else None
            rcv = (parse_iso(d["received_at"]) - parse_iso(p["condition_satisfied_at"])).total_seconds()
            check("记录各环节时刻", True, f"阈值满足→船端告警 {lat:.2f}s，→开始播报 {snd if snd is None else round(snd, 2)}s，→岸端收到 {rcv:.2f}s")
        notes = {(n["role"], n["level"]) for n in detail(eid)["notifications"]}
        check("高风险直接通知船长和企业负责人", notes == {("captain", "HIGH_RISK"), ("manager", "HIGH_RISK")}, str(notes))

        # 2. 恢复与处置
        rec = wait_for(lambda: detail(eid)["condition_status"] == "RECOVERED", 30)
        check("人员返回后事件恢复（处置状态不变）", bool(rec) and detail(eid)["handling_status"] == "PENDING")
        for i, act in enumerate(("confirm", "resolve")):
            r = web.post(f"/api/events/{eid}/actions", json={"action": act, "note": "走查", "action_id": f"smoke-{i}"})
            check(f"operator 执行 {act}", r.status_code == 200, r.text[:80])

        # 3. 黑屏
        a3 = wait_for(lambda: events(alarm_type="A3_CAMERA_ABNORMAL"), 60)
        check("视频黑屏段触发 A3 设备异常", bool(a3), a3[0]["subtype"] if a3 else "")
        time.sleep(3)
        n_a1 = len(events(alarm_type="A1_BRIDGE_ABSENCE"))
        check("黑屏期间没有误报离岗", n_a1 == 1, f"A1 数量 {n_a1}")

        # 4. 断网
        con.post("/sync/off")
        t_off = time.monotonic()
        con.post("/sim", json={"speed_kn": 10.0})
        con.post("/sim", json={"ais": False})
        a4 = wait_for(lambda: events(alarm_type="A4_TERMINAL_OFFLINE"), 40)
        check("断网后岸端判定终端离线 A4", bool(a4), f"断网 {time.monotonic() - t_off:.0f} 秒后")
        st = con.get("/status").json()
        local = {e["alarm_type"] for e in st["events"]}
        wait_for(lambda: {"A5_AIS_OFFLINE", "A6_OVERSPEED"} <= {e["alarm_type"] for e in con.get("/status").json()["events"]}, 30)
        local = {e["alarm_type"] for e in con.get("/status").json()["events"]}
        check("断网期间船端本地照常产生 A5、A6", {"A5_AIS_OFFLINE", "A6_OVERSPEED"} <= local, str(sorted(local)))
        con.post("/sim", json={"clear": True})
        # 等视频循环后第二次离岗
        wait_for(lambda: sum(e["alarm_type"] == "A1_BRIDGE_ABSENCE" for e in con.get("/status").json()["events"]) >= 2, 90)
        time.sleep(32)  # 确保最后一个事件也超过补传判定窗口（30 秒）
        pending = con.get("/status").json()["health"]["outbox_pending"]
        check("断网期间待补传数据在船端积压", pending > 0, f"{pending} 条")

        # 5. 恢复
        con.post("/sync/on")
        ok = wait_for(lambda: con.get("/status").json()["health"]["outbox_pending"] == 0, 60)
        check("恢复同步后补传清空", bool(ok))
        time.sleep(6)
        a4d = detail(a4[0]["event_id"]) if a4 else {}
        check("终端恢复在线，A4 事件恢复", a4d.get("condition_status") == "RECOVERED")
        backfilled = events(backfill=1)
        check("离线期间的事件标记为补传", len(backfilled) >= 3, str(sorted({e["alarm_type"] for e in backfilled})))
        con.post("/sync/off")  # 冻结状态后对账
        time.sleep(1)
        edge_store = EdgeStore(tmp / "edge" / "edge.db")
        edge_ids = {e["event_id"] for e in edge_store.recent_events(1000)}
        shore_ids = {e["event_id"] for e in events() if e["origin"] == "EDGE"}
        check("船端与岸端事件一一对应、无重复", edge_ids == shore_ids, f"船端 {len(edge_ids)} 条，岸端 {len(shore_ids)} 条")

        # 6. 升级
        a1s = sorted(events(alarm_type="A1_BRIDGE_ABSENCE"), key=lambda e: e["triggered_at"])
        if len(a1s) >= 2:
            n2 = {(n["role"], n["level"]) for n in detail(a1s[1]["event_id"])["notifications"]}
            check("同日第 2 次 A1 通知船长并限时处置", ("captain", "REPEAT_2") in n2, str(sorted(n2)))
        dev = web.get("/api/devices").json()[0]
        h = dev["health"] or {}
        check("设备健康信息", True, f"识别 {h.get('detect_fps')} 次/秒，单帧 {h.get('infer_ms')} ms，播报方式 {h.get('audio_backend')}")
    finally:
        for p in (edge_p, shore_p):
            if p.poll() is None:
                p.send_signal(signal.CTRL_BREAK_EVENT if IS_WIN else signal.SIGINT)
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
        logs.close()
    failed = [r for r in results if not r[1]]
    print(f"\n共 {len(results)} 项，失败 {len(failed)} 项；进程日志：{tmp / 'procs.log'}")
    if not failed:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

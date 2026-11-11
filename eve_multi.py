"""多开版：同时盯多台雷电模拟器，每台一个独立线程。

界面里勾哪台就只看哪台；每台都有自己的：
    .tmp/localalert/<槽位>/state.json / monitor.log / events.log / shots/

线程隔离做法：给每台线程单独加载一份 eve_local_alert 模块实例，
各自持有自己的 WORK/LOG/STATE 等全局，互不打架。

配置 `emulators.json`：
    {"emulators": [{"name": "主号", "serial": "emulator-5554", "enabled": true}]}
雷电多开口 = 5555 + 2*编号：实例0→5554、1→5556、2→5558、3→5560。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import eve_local_alert as A0

CONFIG = A0.ROOT / "emulators.json"
DEFAULT_SERIALS = ["emulator-5554", "emulator-5556", "emulator-5558", "emulator-5560"]
_MOD_SEQ = 0


def _hidden() -> dict:
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return {"startupinfo": si}


def _adb_online(serial: str) -> bool:
    try:
        out = subprocess.run([A0.ADB, "devices"], capture_output=True, timeout=15,
                             creationflags=0x08000000, **_hidden())
    except (OSError, subprocess.SubprocessError):
        return False
    for line in (out.stdout or b"").decode("utf-8", "replace").splitlines():
        if line.startswith(serial) and "device" in line:
            return True
    return False


def load_emulators() -> list[dict]:
    if CONFIG.exists():
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
            out = []
            for it in data.get("emulators") or []:
                serial = str(it.get("serial") or "").strip()
                if not serial:
                    continue
                name = str(it.get("name") or serial)
                out.append({"name": name, "serial": serial,
                            "enabled": bool(it.get("enabled", True)), "tag": name})
            if out:
                return out
        except (OSError, json.JSONDecodeError):
            pass
    found = [{"name": s.replace("emulator-", "模拟器"), "serial": s,
              "enabled": True, "tag": s.replace("emulator-", "模拟器")}
             for s in DEFAULT_SERIALS if _adb_online(s)]
    return found or [{"name": "模拟器", "serial": A0.SERIAL, "enabled": True, "tag": "模拟器"}]


def _slot_dir(tag: str) -> Path:
    safe = "".join(ch for ch in tag if ch not in '\\/:*?"<>|').strip() or "slot"
    return A0.ROOT / ".tmp" / "localalert" / safe


def _new_module():
    """每台线程一份全新的模块实例，全局变量互不共享。"""
    global _MOD_SEQ
    _MOD_SEQ += 1
    path = Path(__file__).with_name("eve_local_alert.py")
    spec = importlib.util.spec_from_file_location(f"eve_local_alert_slot{_MOD_SEQ}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bind(mod, slot: dict) -> None:
    work = _slot_dir(slot["name"])
    work.mkdir(parents=True, exist_ok=True)
    mod.ROOT = A0.ROOT
    mod.SERIAL = slot["serial"]
    mod.TAG = slot["tag"]
    mod.WORK = work
    mod.SHOTS = work / "shots"
    mod.SHOTS.mkdir(parents=True, exist_ok=True)
    mod.STATE = work / "state.json"
    mod.LOG = work / "monitor.log"
    mod.EVENTS = work / "events.log"


class SlotWorker:
    def __init__(self, slot: dict, interval: int, once: bool) -> None:
        self.slot = slot
        self.interval = interval
        self.once = once
        self.mod = None
        self.state: dict = {}
        self.first = True
        self.stop_flag = threading.Event()
        self.paused = False
        self._was_paused = False
        self.cycles = 0
        self.alerts = 0
        self.last = {"ok": False, "reason": "还没开始", "rows": 0,
                     "colors": [], "sides": [], "counts": [], "known": 0}
        self.thread = threading.Thread(target=self._loop, name=f"slot-{slot['name']}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _loop(self) -> None:
        try:
            self.mod = _new_module()
            _bind(self.mod, self.slot)
            self.mod.log("=" * 56)
            self.mod.log(f"【{self.slot['name']}】独立监控启动 {self.slot['serial']}")
            self.state = self.mod.load_state()
            if self.mod.QQ_CONFIG.exists():
                self.mod.notify_qq(f"✅【{self.slot['name']}】本地预警已启动\n"
                                   f"设备：{self.slot['serial']}\n每 {self.interval} 秒看一眼本地菜单。", None)
        except Exception as exc:  # noqa: BLE001
            self.last = {"ok": False, "reason": f"启动失败 {type(exc).__name__}: {exc}",
                         "rows": 0, "colors": [], "sides": [], "counts": [], "known": 0}
            return
        while not self.stop_flag.is_set():
            if self.paused:
                if not self._was_paused:
                    self.mod.log(f"【{self.slot['name']}】已暂停（点了停止）")
                    self._was_paused = True
                if self.stop_flag.wait(self.interval):
                    break
                continue
            if self._was_paused:
                self.state = self.mod.load_state()
                self.first = True
                self._was_paused = False
                self.mod.log(f"【{self.slot['name']}】已恢复监控")
            self.cycles += 1
            try:
                res = self.mod.check_once(self.state, send=not self.first)
                ok = bool(res.get("ok"))
                reason = res.get("reason") or ""
                if ok:
                    self.first = False
                    self.alerts += len(res.get("entered") or [])
            except Exception as exc:  # noqa: BLE001
                ok, reason = False, f"{type(exc).__name__}: {exc}"
            self.last = {"ok": ok, "reason": reason,
                         "rows": self.mod._HB.get("rows", 0),
                         "colors": self.mod._HB.get("colors", []),
                         "sides": self.mod._HB.get("sides", []),
                         "counts": self.mod._HB.get("counts", []),
                         "known": len(self.state.get("rows", {}))}
            if self.once:
                break
            if self.stop_flag.wait(self.interval):
                break

    def report(self) -> dict:
        d = dict(self.last)
        d.update({"name": self.slot["name"], "serial": self.slot["serial"],
                  "cycles": self.cycles, "alerts": self.alerts,
                  "paused": bool(self.paused)})
        return d


def run(interval: int = 5, minutes: float = 0, once: bool = False,
        names: list[str] | None = None) -> int:
    wanted = [s for s in load_emulators() if s["enabled"]]
    if names:
        wanted = [s for s in wanted if s["name"] in names]
    if not wanted:
        print("没有选中的模拟器")
        return 2
    root = A0.ROOT / ".tmp" / "localalert"
    root.mkdir(parents=True, exist_ok=True)
    A0.WORK = root
    A0.LOG = root / "monitor.log"
    A0.log("=" * 64)
    A0.log("多开预警启动 pid=%d 选中 %d 台: %s" % (os.getpid(), len(wanted),
                                                "、".join(f"{s['name']}({s['serial']})" for s in wanted)))

    online = []
    for s in wanted:
        if _adb_online(s["serial"]):
            online.append(s)
        else:
            A0.log(f"跳过 {s['name']}（{s['serial']} 没在线）")
    if not online:
        A0.log("选中的模拟器都没在线")
        return 3
    wanted = online

    (root / "monitor.pid").write_text(str(os.getpid()), encoding="utf-8")
    try:  # 上次留下的暂停状态不要带进这次
        (root / "control.json").unlink()
    except OSError:
        pass
    workers = [SlotWorker(s, interval, once) for s in wanted]
    for w in workers:
        w.start()

    def _watch() -> None:
        ctl = root / "control.json"
        while True:
            try:
                data = json.loads(ctl.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict):
                for w in workers:
                    w.paused = not bool(data.get(w.slot["name"], True))
            time.sleep(2)

    threading.Thread(target=_watch, name="control", daemon=True).start()

    deadline = time.time() + minutes * 60 if minutes else None
    cycles = 0
    try:
        while any(w.thread.is_alive() for w in workers):
            cycles += 1
            reps = [w.report() for w in workers]
            A0.WORK = root
            A0.LOG = root / "monitor.log"
            A0._write_heartbeat(reps, cycles, sum(w.alerts for w in workers))
            A0.log(" / ".join(
                f"{r['name']} " + (f"{r['rows']}行 计数{'/'.join(str(c) for c in r['counts'])}"
                                   if r["ok"] else f"⚠{(r['reason'] or '')[:14]}")
                for r in reps))
            if deadline and time.time() >= deadline:
                A0.log("到达设定运行时长，退出")
                break
            if once:
                break
            time.sleep(interval)
    finally:
        try:
            pidf = root / "monitor.pid"
            if pidf.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pidf.unlink()
        except OSError:
            pass
        for w in workers:
            w.stop_flag.set()
        for w in workers:
            w.thread.join(timeout=interval + 5)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="EVE 本地预警（多开）")
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--minutes", type=float, default=0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--slots", default="", help="只跑这几台，逗号分隔，例如 主号,二号")
    ap.add_argument("--list", action="store_true", help="打印模拟器清单（JSON）")
    ap.add_argument("--takeover", action="store_true", help="（兼容单开参数，忽略）")
    ap.add_argument("--test", action="store_true", help="（兼容单开参数，忽略）")
    ap.add_argument("--prime", action="store_true", help="把每台当前名单登记为已知")
    ap.add_argument("--simulate", action="store_true", help="给每台发一条模拟推送")
    args = ap.parse_args(argv)

    names = [x.strip() for x in args.slots.split(",") if x.strip()] or None
    if args.list:
        items = load_emulators()
        for it in items:
            it["online"] = _adb_online(it["serial"])
        print(json.dumps(items, ensure_ascii=False))
        return 0
    if args.prime or args.simulate:
        slots = [s for s in load_emulators() if s["enabled"]
                 and (not names or s["name"] in names) and _adb_online(s["serial"])]
        if not slots:
            print("没有在线的模拟器")
            return 2
        flag = "--prime" if args.prime else "--simulate"
        rc = 0
        for s in slots:
            mod = _new_module()
            _bind(mod, s)
            print(f"== {s['name']} ({s['serial']}) ==")
            rc = mod.main([flag]) or rc
        return rc
    return run(args.interval, args.minutes, args.once, names)


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""本地预警控制台 —— 图形化启停 + 实时日志（无黑框）。

双击「启动本地预警.bat」或用 pythonw 打开本文件即可。
- ▶ 启动监控：后台拉起 eve_local_alert.py（--takeover --interval N），不弹控制台
- ■ 停止监控：结束该进程（含别人手动启动的旧进程）
- 🧪 模拟推送：抓一张现场截图，直接推一条「测试」消息到 QQ
- 📋 登记当前名单：把现在本地里的人登记为已知，之后不再报他们
- 左下角实时显示 monitor.log，右侧显示心跳（轮次 / 报警数 / 行数 / 颜色 / QQ 状态）
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    # 打包成 exe：程序就放在 exe 旁边，状态目录跟着 exe 走
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
WORK = ROOT / ".tmp" / "localalert"
LOG = WORK / "monitor.log"
HB = WORK / "heartbeat.json"
PIDFILE = WORK / "monitor.pid"
SHOTS = WORK / "shots"
SCRIPT = ROOT / "eve_local_alert.py"
CHILD_LOG = WORK / "console_child.log"

CREATE_NO_WINDOW = 0x08000000
INTERVAL = "5"

BG = "#1f2430"
PANEL = "#272d3b"
FG = "#e6e9ef"
MUTED = "#9aa4b8"
GREEN = "#37d67a"
RED = "#ff5c5c"
YELLOW = "#ffc857"


def read_emulators() -> list[dict]:
    """优先读 emulators.json，没有再给个默认清单。"""
    cfg = ROOT / "emulators.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            out = []
            for it in data.get("emulators") or []:
                serial = str(it.get("serial") or "").strip()
                if serial:
                    out.append({"name": str(it.get("name") or serial), "serial": serial,
                                "enabled": bool(it.get("enabled", True))})
            if out:
                return out
        except (OSError, ValueError):
            pass
    return [{"name": f"模拟器{i}", "serial": f"emulator-555{4 + 2 * i}", "enabled": True}
            for i in range(4)]


def _alive(pid: int) -> bool:
    if not pid:
        return False
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _child_argv(py: str, *args: str) -> list[str]:
    """拉起监控/单跑命令：打包后就是「自己 + --child-monitor」，否则用脚本 + 解释器。"""
    if FROZEN:
        return [sys.executable, "--child-monitor", *args]
    return [py, str(SCRIPT), *args]


def child_python() -> str:
    """找带 Pillow 的解释器（eve_local_alert.py 需要 PIL）。"""
    cands = [
        Path(os.environ.get("USERPROFILE", "")) / ".dsh" / "dsh-runtimes" /
        "dsh-plugin-desktop-primary-runtime" / "dependencies" / "python" / "python.exe",
    ]
    if getattr(sys, "executable", ""):
        cands.insert(0, Path(sys.executable))
    for exe in cands:
        if exe and exe.exists():
            if exe.name.lower() == "pythonw.exe":
                exe = exe.with_name("python.exe")
                if not exe.exists():
                    continue
            try:
                probe = subprocess.run([str(exe), "-c", "import PIL"],
                                       capture_output=True, timeout=20,
                                       creationflags=CREATE_NO_WINDOW)
                if probe.returncode == 0:
                    return str(exe)
            except (OSError, subprocess.SubprocessError):
                continue
    return sys.executable


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.py = sys.executable if FROZEN else child_python()
        self._log_pos = 0
        self._busy = False
        self._last_hb = ""
        self._build()

    # ---------- 界面 ----------
    def _build(self) -> None:
        self.root.title("EVE 本地预警控制台")
        self.root.geometry("1240x760")
        self.root.minsize(980, 600)
        self.root.configure(bg=BG)

        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(head, text="EVE 本地预警", bg=BG, fg=FG,
                 font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        self.dot = tk.Label(head, text="●", bg=BG, fg=MUTED,
                            font=("Microsoft YaHei UI", 16))
        self.dot.pack(side="left", padx=(12, 4))
        self.state_lbl = tk.Label(head, text="已停止", bg=BG, fg=MUTED,
                                  font=("Microsoft YaHei UI", 11))
        self.state_lbl.pack(side="left")
        self.hint = tk.Label(head, text="", bg=BG, fg=MUTED,
                             font=("Microsoft YaHei UI", 9))
        self.hint.pack(side="right")

        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=14, pady=(0, 8))
        self.btn_start = self._button(bar, "▶ 全部开始", self.start_all, GREEN)
        self.btn_stop = self._button(bar, "■ 全部停止", self.stop_all, RED)
        self.btn_sim = self._button(bar, "🧪 模拟推送", self.simulate, YELLOW)
        self.btn_prime = self._button(bar, "📋 登记当前名单", self.prime, MUTED)
        self._button(bar, "📁 日志目录", self.open_logs, MUTED)

        info = tk.Frame(self.root, bg=PANEL)
        info.pack(fill="x", padx=14, pady=(0, 8))
        self.info = {}
        fields = [("pid", "进程"), ("cycles", "轮次"), ("alerts", "累计报警"),
                  ("rows", "本地行数"), ("colors", "颜色/敌我"), ("qq", "QQ"),
                  ("hb", "心跳"), ("err", "最近异常")]
        for i, (key, label) in enumerate(fields):
            cell = tk.Frame(info, bg=PANEL)
            cell.grid(row=i // 4, column=i % 4, sticky="w", padx=12, pady=6)
            tk.Label(cell, text=label, bg=PANEL, fg=MUTED,
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w")
            val = tk.Label(cell, text="—", bg=PANEL, fg=FG,
                           font=("Microsoft YaHei UI", 10))
            val.pack(anchor="w")
            self.info[key] = val

        emu = tk.Frame(self.root, bg=BG)
        emu.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(emu, text="监控这些模拟器：", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.emu_vars: dict[str, tk.BooleanVar] = {}
        self.emu_box = tk.Frame(emu, bg=BG)
        self.emu_box.pack(side="left")
        self._build_emulators()

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        tk.Label(body, text="各模拟器日志（每台一列，自动刷新）", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        self.logs: dict[str, tk.Text] = {}
        self.log_pos: dict[str, int] = {}
        self.cards: dict[str, dict] = {}
        self.cols = tk.Frame(body, bg=BG)
        self.cols.pack(fill="both", expand=True, pady=(4, 0))
        self._build_cards()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh()
        self.root.after(1500, self.tick)

    def _build_emulators(self) -> None:
        for child in self.emu_box.winfo_children():
            child.destroy()
        self.emu_vars.clear()
        for it in read_emulators():
            name = it["name"]
            var = tk.BooleanVar(value=bool(it.get("enabled", True)))
            self.emu_vars[name] = var
            tk.Checkbutton(self.emu_box, text=f"{name}（{it['serial']}）", variable=var,
                           bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG,
                           activeforeground=FG, font=("Microsoft YaHei UI", 9),
                           bd=0, highlightthickness=0).pack(side="left", padx=(0, 10))

    def _build_cards(self) -> None:
        for child in self.cols.winfo_children():
            child.destroy()
        self.logs.clear()
        self.log_pos.clear()
        self.cards.clear()
        for idx, it in enumerate(read_emulators()):
            self._add_card(it["name"], idx)
        for c in range(3):
            self.cols.columnconfigure(c, weight=1, uniform="col")

    def _add_card(self, name: str, idx: int) -> None:
        col, row = idx % 3, idx // 3
        card = tk.Frame(self.cols, bg=PANEL, bd=0)
        card.grid(row=row, column=col, sticky="nsew", padx=(0, 6), pady=(0, 6))
        head = tk.Frame(card, bg=PANEL)
        head.pack(fill="x", padx=8, pady=(6, 2))
        dot = tk.Label(head, text="●", bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 12))
        dot.pack(side="left")
        tk.Label(head, text=name, bg=PANEL, fg=FG,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left", padx=(4, 0))
        btn_stop = tk.Button(head, text="■", command=lambda n=name: self.slot_stop(n),
                             bg=PANEL, fg=RED, activebackground="#333b4d", activeforeground=RED,
                             relief="flat", bd=0, padx=4, cursor="hand2",
                             disabledforeground=MUTED, font=("Microsoft YaHei UI", 9))
        btn_stop.pack(side="right", padx=(4, 0))
        btn_start = tk.Button(head, text="▶", command=lambda n=name: self.slot_start(n),
                              bg=PANEL, fg=GREEN, activebackground="#333b4d", activeforeground=GREEN,
                              relief="flat", bd=0, padx=4, cursor="hand2",
                              disabledforeground=MUTED, font=("Microsoft YaHei UI", 9))
        btn_start.pack(side="right", padx=(4, 0))
        stat = tk.Label(card, text="—", bg=PANEL, fg=MUTED, anchor="w",
                        font=("Microsoft YaHei UI", 9))
        stat.pack(fill="x", padx=8)
        text = scrolledtext.ScrolledText(
            card, bg="#171b24", fg="#cfd6e4", insertbackground=FG, height=10,
            font=("Consolas", 8), relief="flat", wrap="none", state="disabled")
        text.pack(fill="both", expand=True, padx=(6, 6), pady=(4, 6))
        self.cards[name] = {"dot": dot, "stat": stat,
                            "start": btn_start, "stop": btn_stop}
        self.logs[name] = text
        self.log_pos[name] = 0
        self._set_slot_ui(name, False)

    def _set_slot_ui(self, name: str, running: bool) -> None:
        card = self.cards.get(name)
        if not card:
            return
        card["start"].configure(state="disabled" if running else "normal",
                                fg=MUTED if running else GREEN,
                                cursor="arrow" if running else "hand2")
        card["stop"].configure(state="normal" if running else "disabled",
                               fg=RED if running else MUTED,
                               cursor="hand2" if running else "arrow")

    def _ensure_card(self, name: str) -> None:
        if name in self.cards:
            return
        self._add_card(name, len(self.cards))

    def start_all(self) -> None:
        for name, var in self.emu_vars.items():
            var.set(True)
        self.start()

    def stop_all(self) -> None:
        self._write_control_all(False)
        self.stop()

    def _write_control_all(self, running: bool) -> None:
        for name in self.cards:
            self._write_control(name, running)

    def _control_path(self):
        return WORK / "control.json"

    def _write_control(self, name: str, running: bool) -> None:
        path = self._control_path()
        data = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[name] = running
        try:
            WORK.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def slot_stop(self, name: str) -> None:
        self._write_control(name, False)
        card = self.cards.get(name)
        if card:
            card["dot"].configure(fg=MUTED)
            card["stat"].configure(text="已暂停", fg=MUTED)
        self._set_slot_ui(name, False)
        self.log_line(f"{name} 已暂停")

    def slot_start(self, name: str) -> None:
        if not self.running_pid():
            if name in self.emu_vars:
                self.emu_vars[name].set(True)
            self.start()
            return
        self._write_control(name, True)
        self._set_slot_ui(name, True)
        self.log_line(f"{name} 已恢复")

    def _selected(self) -> list[str]:
        return [n for n, v in self.emu_vars.items() if v.get()]

    def _button(self, parent, text, cmd, color) -> tk.Button:
        b = tk.Button(parent, text=text, command=cmd, bg=PANEL, fg=color,
                      activebackground="#333b4d", activeforeground=color,
                      relief="flat", bd=0, padx=12, pady=6, cursor="hand2",
                      font=("Microsoft YaHei UI", 10))
        b.pack(side="left", padx=(0, 8))
        return b

    def log_line(self, text: str) -> None:
        self.hint.configure(text=f"{time.strftime('%H:%M:%S')} {text}")

    # ---------- 进程控制 ----------
    def _read_pid(self) -> int:
        try:
            return int(PIDFILE.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return 0

    def running_pid(self) -> int:
        if self.proc and self.proc.poll() is None:
            return self.proc.pid
        pid = self._read_pid()
        if pid and _alive(pid):
            return pid
        try:
            hb = json.loads(HB.read_text(encoding="utf-8"))
            age = time.time() - time.mktime(time.strptime(hb.get("time", ""), "%Y-%m-%d %H:%M:%S"))
            if age <= 20:
                return int(hb.get("pid") or 0)
        except (OSError, ValueError, TypeError):
            pass
        return 0

    def start(self) -> None:
        if self._busy:
            return
        pid = self.running_pid()
        if pid:
            messagebox.showinfo("已在运行", f"监控已经在跑（pid {pid}）。要重启请先点「停止监控」。")
            return
        WORK.mkdir(parents=True, exist_ok=True)
        self._busy = True
        try:
            fh = open(CHILD_LOG, "a", encoding="utf-8", errors="replace")
            picked = self._selected()
            if not picked:
                messagebox.showinfo("没选模拟器", "先在上面勾选要监控的模拟器。")
                self._busy = False
                return
            for name in picked:
                self._ensure_card(name)
            extra = ["--all", "--slots", ",".join(picked)] if (ROOT / "emulators.json").exists() else []
            self.proc = subprocess.Popen(
                _child_argv(self.py, *extra, "--takeover", "--interval", INTERVAL),
                cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        except OSError as exc:
            self.log_line(f"启动失败: {exc}")
            messagebox.showerror("启动失败", str(exc))
            return
        finally:
            self._busy = False
        self.log_line(f"已启动监控 pid={self.proc.pid}（{Path(self.py).name}，每 {INTERVAL}s 一轮）")
        self.root.after(1200, self.refresh)

    def stop(self) -> None:
        pid = self.running_pid()
        if not pid:
            self.log_line("当前没有在跑的监控。")
            self.refresh()
            return
        if self.proc and self.proc.pid == pid and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        else:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        self.log_line(f"已停止监控 pid={pid}")
        try:
            PIDFILE.unlink()
        except OSError:
            pass
        self.root.after(800, self.refresh)

    # ---------- 额外动作 ----------
    def _with_all(self, flag: str) -> list[str]:
        """配了 emulators.json 就按多开跑，否则单台。"""
        return ["--all", flag] if (ROOT / "emulators.json").exists() else [flag]


    def _run_bg(self, args: list[str], done=None) -> None:
        if self._busy:
            return
        self._busy = True

        def work() -> None:
            out = ""
            try:
                res = subprocess.run(_child_argv(self.py, *args), cwd=str(ROOT),
                                     capture_output=True, timeout=120,
                                     creationflags=CREATE_NO_WINDOW)
                raw = (res.stdout or b"") + (res.stderr or b"")
                for enc in ("utf-8", "gbk"):
                    try:
                        out = raw.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                out = out.strip() or f"(无输出，退出码 {res.returncode})"
            except (OSError, subprocess.SubprocessError) as exc:
                out = f"执行失败: {exc}"
            finally:
                self._busy = False

            def show() -> None:
                for line in out.splitlines()[-14:]:
                    self.log_line("  " + line)
                if done:
                    done()

            self.root.after(0, show)

        threading.Thread(target=work, daemon=True).start()

    def simulate(self) -> None:
        self.log_line("正在模拟一次预警并推送到 QQ …")
        self._run_bg(self._with_all("--simulate"))

    def prime(self) -> None:
        if not messagebox.askyesno("登记当前名单",
                                   "把现在本地里的人登记为「已知」？\n"
                                   "登记后他们再出现不会报警，红名离开又回来仍会报。"):
            return
        self.log_line("正在登记当前名单 …")
        self._run_bg(self._with_all("--prime"))

    def open_logs(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        os.startfile(str(WORK))  # noqa: S606

    # ---------- 刷新 ----------
    def tick(self) -> None:
        try:
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            self.log_line(f"刷新出错: {exc}")
        self.root.after(1500, self.tick)

    def refresh(self) -> None:
        pid = self.running_pid()
        if pid:
            self.dot.configure(fg=GREEN)
            self.state_lbl.configure(text=f"运行中（pid {pid}）", fg=GREEN)
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
        else:
            self.dot.configure(fg=MUTED)
            self.state_lbl.configure(text="已停止", fg=MUTED)
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
        self.info["pid"].configure(text=str(pid) if pid else "—")
        self._refresh_hb(pid)
        self._refresh_log()

    def _refresh_hb(self, pid: int) -> None:
        hb: dict = {}
        try:
            hb = json.loads(HB.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        stamp = hb.get("time") or "—"
        self.info["hb"].configure(text=stamp)
        self.info["cycles"].configure(text=str(hb.get("cycles", "—")))
        self.info["alerts"].configure(text=str(hb.get("alerts", "—")))
        rows = hb.get("rows")
        self.info["rows"].configure(text=str(rows) if rows is not None else "—")
        colors = hb.get("colors") or []
        sides = hb.get("sides") or []
        counts = hb.get("counts") or []
        danger = any(counts)
        if colors:
            cnt: dict[str, int] = {}
            for c in colors:
                cnt[c] = cnt.get(c, 0) + 1
            order = ["红名", "橙名", "白名", "绿名", "蓝名", "颜色未识别"]
            text = " ".join(f"{k}{cnt[k]}" for k in order if k in cnt)
            if sides:
                nf = sum(1 for x in sides if x == "友方")
                ne = sum(1 for x in sides if x == "敌方")
                text += f" · 友{nf} 敌{ne}"
        elif counts:
            text = "名单里只有自己"
        else:
            text = "—"
        if counts:
            text += " · 计数 " + "/".join(str(c) for c in counts)
        emus = hb.get("emulators") or []
        if emus:
            parts = []
            for em in emus:
                name = em.get("name") or "?"
                if not em.get("ok"):
                    parts.append(f"{name} 菜单未开")
                    continue
                cs = em.get("counts") or []
                one = f"{name} {em.get('rows', 0)}行"
                if cs:
                    one += " 计数" + "/".join(str(c) for c in cs)
                parts.append(one)
            text = " ｜ ".join(parts)
            danger = any(any(em.get("counts") or []) for em in emus)
            for em in emus:
                card = self.cards.get(em.get("name") or "")
                if not card:
                    continue
                ok = bool(em.get("ok"))
                cs = em.get("counts") or []
                bad = any(cs)
                paused = bool(em.get("paused"))
                self._set_slot_ui(em.get("name") or "", not paused)
                card["dot"].configure(fg=RED if bad else (GREEN if ok and not paused else MUTED))
                if ok:
                    info = f"{em.get('rows', 0)} 行"
                    if cs:
                        info += " · 计数 " + "/".join(str(c) for c in cs)
                    card["stat"].configure(text=info, fg=RED if bad else FG)
                else:
                    card["stat"].configure(text=(em.get("reason") or "未就绪")[:16], fg=YELLOW)
        self.info["colors"].configure(text=text, fg=RED if danger else FG)
        qq = hb.get("qq")
        self.info["qq"].configure(text="已配置" if qq else "未配置",
                                  fg=GREEN if qq else YELLOW)
        err = hb.get("err") or ""
        self.info["err"].configure(text=(err[:40] or "—"), fg=RED if err else MUTED)
        if hb.get("panel") is False:
            self.info["rows"].configure(text="面板未开", fg=YELLOW)
        else:
            self.info["rows"].configure(fg=FG)
        if pid and stamp != "—":
            try:
                age = time.time() - time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
                if age > 60:
                    self.dot.configure(fg=YELLOW)
                    self.state_lbl.configure(text=f"运行中（心跳 {int(age)}s 前，可能卡住）", fg=YELLOW)
            except ValueError:
                pass
        self._last_hb = stamp

    def _refresh_log(self) -> None:
        for title, widget in list(self.logs.items()):
            path = LOG if title == "汇总" else WORK / title / "monitor.log"
            self._tail(title, widget, path)

    def _tail(self, title: str, widget: tk.Text, path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        pos = self.log_pos.get(title, 0)
        if size < pos:
            pos = 0
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")
        if size == pos:
            return
        try:
            with open(path, "rb") as fh:
                fh.seek(pos)
                chunk = fh.read(size - pos)
        except OSError:
            return
        self.log_pos[title] = size
        lines = [ln for ln in chunk.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        if not lines:
            return
        widget.configure(state="normal")
        for line in lines[-200:]:
            widget.insert("end", line + chr(10))
        widget.see("end")
        widget.configure(state="disabled")

    def on_close(self) -> None:
        pid = self.running_pid()
        if pid:
            if not messagebox.askyesno("退出", f"监控还在运行（pid {pid}）。\n退出控制台时一并停止它吗？"):
                return
            self.stop()
        self.root.destroy()


CONSOLE_PID = WORK / "console.pid"


def acquire_console() -> bool:
    """同一个控制台只开一个窗口（连点几次 bat 也不会弹一堆）。"""
    try:
        pid = int(CONSOLE_PID.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if pid and pid != os.getpid() and _alive(pid):
        return False
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        CONSOLE_PID.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    return True


def release_console() -> None:
    try:
        if int(CONSOLE_PID.read_text(encoding="utf-8").strip() or 0) == os.getpid():
            CONSOLE_PID.unlink()
    except (OSError, ValueError):
        pass


BUNDLED_PY = (Path(os.environ.get("USERPROFILE", ""))
              / ".dsh" / "dsh-runtimes" / "dsh-plugin-desktop-primary-runtime"
              / "dependencies" / "python")


def _ensure_runtime() -> None:
    """双击 .pyw 时系统默认解释器可能没有 Pillow：自动换成内置解释器再跑一次。

    这样不管是双击 .pyw、双击 .bat，还是命令行 pythonw，都不会出现「闪一下就没了」。
    """
    try:
        import PIL  # noqa: F401  # 只是探测能不能用
        return
    except Exception:  # noqa: BLE001
        pass
    if FROZEN:
        return          # exe 里已经打进 Pillow，不需要再换解释器
    exe = BUNDLED_PY / "pythonw.exe"
    if not exe.exists():
        exe = BUNDLED_PY / "python.exe"
    if not exe.exists():
        return
    if Path(sys.executable).resolve() == exe.resolve():
        return
    try:
        subprocess.Popen([str(exe), str(Path(__file__).resolve()), *sys.argv[1:]],
                         close_fds=True, creationflags=CREATE_NO_WINDOW)
    except OSError:
        return
    raise SystemExit(0)


def main() -> None:
    _ensure_runtime()
    root = tk.Tk()
    root.withdraw()
    if not acquire_console():
        messagebox.showinfo("已在运行", "本地预警控制台已经打开了，切到任务栏那个窗口即可。")
        root.destroy()
        return
    app = App(root)
    root.deiconify()
    if "--simulate" in sys.argv:
        root.after(600, app.simulate)
    if "--autostart" in sys.argv:
        root.after(800, app.start)
    try:
        root.mainloop()
    finally:
        release_console()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        try:
            messagebox.showerror("启动失败", traceback.format_exc())
        except Exception:  # noqa: BLE001
            pass

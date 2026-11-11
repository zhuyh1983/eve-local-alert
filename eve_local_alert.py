#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EVE Echoes 本地(Local)名单预警 —— 只读监控，不动船。

只做两件事：**截图** + **颜色识别**。全程不 OCR、不读名字、不转文字。
每 N 秒截一次雷电模拟器 → 数出本地窗口里有多少行字、每行是什么颜色 →
只要有一行「没见过的样子」出现（白名 / 红名 / 橙名 …），就发 QQ 消息 + 现场截图。

流程要点
- 只截图 + 读像素，不点游戏界面，不调用 OCR。
- 颜色靠像素阈值判断（红 > 橙 > 白 > 绿 > 蓝），推送里报的也是颜色，不是名字。
- 一行文字的「身份」用像素指纹（文字形状）判定：同一个人换了位置也认得出，
  但整个过程不会把那行字读成文字。
- 首次运行请用 --prime：把「现在已经在本地里的人」登记为已知，之后不再报他们。
- 红名离开一会儿又回来会再报一次（同一行 10 分钟内只报一次）。
- QQ 未配置时只在本机响铃 + 写日志，配置好后（qq_config.json）自动开始推送，无需重启。

用法::

    python eve_local_alert.py --prime          # 登记当前名单为已知（不推送）
    python eve_local_alert.py --once           # 只跑一轮，打印每行的颜色
    python eve_local_alert.py                  # 常驻监控（默认 6 秒一轮）
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import winsound
import zlib
from datetime import datetime
import os
from pathlib import Path

from PIL import Image, ImageChops

# 打包成 exe 后 __file__ 在临时解包目录里，配置/状态要跟着 exe 走
_ENV_ROOT = os.environ.get("EVE_ALERT_ROOT")
ROOT = Path(_ENV_ROOT).resolve() if _ENV_ROOT else Path(__file__).resolve().parent
WORK = ROOT / ".tmp" / "localalert"
SHOTS = WORK / "shots"
STATE = WORK / "state.json"
LOG = WORK / "monitor.log"
EVENTS = WORK / "events.log"
QQ_CONFIG = ROOT / "qq_config.json"

ADB = r"H:\leidian\LDPlayer9\adb.exe"
SERIAL = "emulator-5554"

# 本地窗口在 960x540 画布上的位置（只看像素，不做 OCR）
# 实测 2026-11-05：面板随行数变高（11 行时约 y=57..540），行距约 36px，名字从 x≈224 起
PANEL = (200, 57, 395, 535)     # 本地名单的裁剪框（面板开着时的整块区域）
NAME_X = (206, 345)             # 名单文字横向范围（右边界为了排除绿色图标列）
ROW_Y = (95, 500)               # 名单纵向范围（排除标题栏「本地」和底部计数条）
# 名单右侧那一竖排绿色图标（本地窗独有）：判断「本地面板是否开着」
ICON_X = (352, 394)
ICON_Y = (95, 500)
# 新版本地列表每行左边有角色头像（旧版没有），名字从 x≈258 起；旧版从 x≈224 起。
# 扫描时自动选横向窗口：有头像就从 250 起，免得头像的肤色/头发干扰判色。
PORTRAIT_X = (208, 252)
PORTRAIT_Y = (95, 500)
_NX = NAME_X

UNKNOWN = "?"
TAG = ""    # 多开时由 eve_multi 设置，报警标题会带上【槽位名】
COLOR_CN = {"R": "红名", "W": "白名", "Y": "橙名", "G": "绿名", "B": "蓝名", UNKNOWN: "颜色未识别"}
SIDE_CN = {"F": "友方", "E": "敌方", "?": "无图标", UNKNOWN: "敌友未知"}
# 敌我图标：绿星/蓝加=友方，红减/灰等号/没图标=敌方
MARK_A = (366, 384)     # 无头像列表：图标在名单右边那一竖列
MARK_B = (236, 254)     # 带头像列表：图标在头像右下角
_PORTRAIT = False       # 本轮是不是带头像那版列表
_ROWS_Y = ROW_Y         # 本轮实际的行范围（定位后会被改写，菜单可能被拖动）
_ICON_BOX = (ICON_X[0], ICON_Y[0], ICON_X[1], ICON_Y[1])
_MARK = MARK_A          # 本轮敌我图标的横向窗口
FAULT_AFTER = 6         # 连续多少轮读不到菜单就发一次故障通知
FAULT_COOLDOWN = 1200   # 故障通知最少隔多久发一次（秒）


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{now()}] {msg}"
    try:
        print(line, flush=True)
    except (AttributeError, OSError, ValueError):
        pass  # pythonw 下没有 stdout
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[\s·・._\-'\"()\[\]]+", "", text).lower()


def similar(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return 1.0 - prev[-1] / max(len(a), len(b))


# --------------------------------------------------------------------------
# 截图
# --------------------------------------------------------------------------
CREATE_NO_WINDOW = 0x08000000


def _hidden() -> dict:
    """Windows 下隐藏子进程窗口：不隐藏的话 adb.exe 每轮都会闪一个黑框。"""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                      # SW_HIDE
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": si}


def grab(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    devnull = subprocess.DEVNULL
    tmp = path.with_name(path.stem + ".part" + path.suffix)   # 先写临时文件，再原子替换
    subprocess.run([ADB, "-s", SERIAL, "shell", "screencap -p /sdcard/_la.png"],
                   stdout=devnull, stderr=devnull, timeout=30, **_hidden())
    subprocess.run([ADB, "-s", SERIAL, "pull", "/sdcard/_la.png", str(tmp)],
                   stdout=devnull, stderr=devnull, timeout=30, **_hidden())
    if tmp.exists() and tmp.stat().st_size > 1024:
        os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------
# 图像分析：切行 / 判色 / 指纹（不 OCR）
# --------------------------------------------------------------------------
def is_text_pixel(r: int, g: int, b: int) -> bool:
    """这个像素算不算「名单文字」。

    白/绿/蓝名是亮色文字；红名、橙名不一定亮（常见 220,60,60 这种），
    所以除了三通道都亮，还要认「明显偏红」。否则红名那一行根本切不出来。
    """
    if r > 118 and g > 118 and b > 118:
        return True
    return r > 130 and g < 100 and b < 100 and r - g > 45 and r - b > 45


def rows_of(im: Image.Image) -> list[tuple[int, int]]:
    x0, x1 = _NX
    prof = []
    for y in range(_ROWS_Y[0], _ROWS_Y[1]):
        cnt = 0
        for x in range(x0, x1):
            if is_text_pixel(*im.getpixel((x, y))):
                cnt += 1
                if cnt >= 3:
                    break
        prof.append((y, cnt))
    bands, start, gap = [], None, 0
    for y, cnt in prof:
        if cnt >= 3:
            if start is None:
                start = y
            gap = 0
        elif start is not None:
            gap += 1
            if gap >= 4:
                bands.append((start, y - gap))
                start, gap = None, 0
    if start is not None:
        bands.append((start, _ROWS_Y[1] - 1))
    return [(a, b) for a, b in bands if 5 <= b - a + 1 <= 26]


def classify(im: Image.Image, band: tuple[int, int]) -> tuple[str, dict]:
    a, b = band
    x0, x1 = _NX
    c = {"red": 0, "white": 0, "green": 0, "blue": 0, "orange": 0}
    for y in range(a, b + 1):
        for x in range(x0, x1):
            r, g, bl = im.getpixel((x, y))
            if max(r, g, bl) < 105:
                continue
            if r > 120 and g < 95 and bl < 95 and r - g > 45:
                c["red"] += 1
            elif r > 120 and 95 <= g < 175 and bl < 95 and r - bl > 60:
                c["orange"] += 1
            elif r > 128 and g > 128 and bl > 128:
                c["white"] += 1
            elif g > 110 and g - r > 25 and g - bl > 8:
                c["green"] += 1
            elif bl > 120 and bl - r > 35:
                c["blue"] += 1
    total = max(1, sum(c.values()))
    order = [("R", "red"), ("Y", "orange"), ("W", "white"), ("G", "green"), ("B", "blue")]
    for kind, key in order:
        if c[key] >= 8 and c[key] / total > 0.5:
            return kind, c
    if c["red"] >= 8 and c["red"] / total > 0.3:
        return "R", c
    if c["white"] + c["red"] == 0:
        return UNKNOWN, c
    return ("R" if c["red"] > c["white"] else "W"), c


FP_W, FP_H = 64, 16


def text_box(im: Image.Image, band: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """在行带内找「名字文字」的像素包围盒。"""
    x0, x1 = _NX
    xs, ys = [], []
    for y in range(max(0, band[0] - 4), min(im.height, band[1] + 5)):
        for x in range(x0, x1):
            r, g, b = im.getpixel((x, y))
            if max(r, g, b) > 118:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def text_mask(im: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    """只保留「字」的掩膜（字=亮色/红色笔画，背景=深色卡片）。

    面板是半透明的，背后的空间站动画会让同一行忽亮忽暗；只看字形就能稳住身份。
    """
    crop = im.crop(box)
    r, g, b = crop.split()
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return mx.point(lambda v: 255 if v >= 150 else 0)


def fingerprint(im: Image.Image, box: tuple[int, int, int, int]) -> bytes:
    """文字形状指纹：字形掩膜归一化后的图（去背景亮暗、去位置、去大小）。"""
    return text_mask(im, box).resize((FP_W, FP_H), Image.BOX).tobytes()


def fp_cos(a: bytes, b: bytes) -> float:
    """两枚指纹的余弦相似度（去均值）。不同名字 ≈ 0，同一个人 ≈ 0.5~0.95。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    ma = sum(a) / len(a)
    mb = sum(b) / len(b)
    num = sab = sbb = 0.0
    for x, y in zip(a, b):
        dx = x - ma
        dy = y - mb
        num += dx * dy
        sab += dx * dx
        sbb += dy * dy
    if sab <= 0 or sbb <= 0:
        return 0.0
    return num / (sab ** 0.5 * sbb ** 0.5)


def fp_encode(fp: bytes) -> str:
    return base64.b64encode(zlib.compress(fp, 9)).decode("ascii")


def fp_decode(text: str) -> bytes:
    try:
        return zlib.decompress(base64.b64decode(text))
    except (ValueError, zlib.error):
        return b""


def row_hash(im: Image.Image, band: tuple[int, int]) -> str:
    """快速短哈希（只看字形，背景变化不影响），用于最常见的命中路径。"""
    a, b = band
    x0, x1 = _NX
    small = text_mask(im, (x0, max(0, a - 3), x1, b + 4)).resize((48, 10), Image.BOX)
    bits = "".join("1" if v >= 128 else "0" for v in small.tobytes())
    return hashlib.md5(bits.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
PIDFILE = WORK / "monitor.pid"


def _alive(pid: int) -> bool:
    """Windows 下判断进程是否还活着（不杀进程）。"""
    if pid <= 0:
        return False
    try:
        import ctypes  # noqa: PLC0415

        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def acquire_lock() -> tuple[bool, int]:
    """返回 (是否拿到锁, 已存在的进程号)。"""
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        if PIDFILE.exists():
            try:
                old = int(PIDFILE.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                old = 0
            if old and old != os.getpid() and _alive(old):
                return False, old
        PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
        return True, os.getpid()
    except OSError:
        return True, os.getpid()


def release_lock() -> None:
    try:
        if PIDFILE.exists() and PIDFILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PIDFILE.unlink()
    except OSError:
        pass


def load_state() -> dict:
    if STATE.exists():
        try:
            return _migrate(json.loads(STATE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return {"rows": {}, "present": [], "left": {}}


def save_state(st: dict) -> None:
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


FP_MATCH = 0.75     # 指纹余弦相似度阈值：≥ 这个值就算「同一行字」


def _migrate(st: dict) -> dict:
    """旧版状态（按 OCR 名字登记）→ 新版（只按像素指纹登记）。

    旧版的 fps 本来就是「这一行长什么样」，正好可以原样继承：
    迁移后那些人立刻算「已知」，升级当天不会把本地里已有的人统统报一遍。
    """
    if "rows" in st:
        # 2026-11-06：取消「自己」这个概念，旧状态里的 S 统一当「无图标」
        for info in (st.get("rows") or {}).values():
            if isinstance(info, dict) and info.get("side") == "S":
                info["side"] = "?"
        return st
    rows: dict = {}
    old_fps = st.get("fps") or {}
    old_hash = st.get("hashes") or {}
    old_seen = st.get("seen") or {}
    for h, enc in old_fps.items():
        name = old_hash.get(h) or ""
        info = old_seen.get(name) or {}
        rows[h] = {
            "fp": enc,
            "kind": info.get("kind") or "W",
            "first": info.get("first") or now(),
            "last": info.get("last") or now(),
            "alert_at": info.get("alert_at") or "",
        }
    return {"rows": rows, "present": [], "left": {}}


def match_row(st: dict, h: str, fp: bytes) -> tuple[str | None, float]:
    """这一行字见过没有 → (登记 key, 相似度)；没见过返回 (None, 最高相似度)。

    先比 12 位短哈希（像素完全一致，最常见的命中路径），再比文字形状指纹
    （基于包围盒归一化，抗换行/换位置）。全程只有像素，不做 OCR、不产生文字。
    """
    rows = st.get("rows", {})
    if h and h in rows:
        return h, 1.0
    if not fp or not rows:
        return None, 0.0
    recent = sorted(rows.items(), key=lambda kv: (kv[1] or {}).get("last", ""),
                    reverse=True)[:120]
    best, best_cos = None, 0.0
    for key, info in recent:
        enc = info.get("fp") if isinstance(info, dict) else ""
        if not enc:
            continue
        score = fp_cos(fp, fp_decode(enc))
        if score > best_cos:
            best, best_cos = key, score
    if best and best_cos >= FP_MATCH:
        return best, best_cos
    return None, best_cos


def remember_row(st: dict, key: str, h: str, kind: str, fp: bytes = b"",
                 first: bool = False, side: str = "", mark: str = "") -> None:
    """登记/刷新一行：只存颜色 + 指纹，不存名字。"""
    key = key or h or f"row{int(time.time())}"
    rows = st.setdefault("rows", {})
    info = rows.setdefault(key, {})
    info["last"] = now()
    info["kind"] = kind or UNKNOWN
    if side:
        info["side"] = side
    if mark:
        info["mark"] = mark
    if first or not info.get("first"):
        info["first"] = now()
    if h:
        info["hash"] = h
    if fp:
        info["fp"] = fp_encode(fp)
    if len(rows) > 600:
        _prune_rows(rows)


def _prune_rows(rows: dict) -> None:
    """控制状态文件体积：超过 600 行就丢掉 30 天没再出现的样子。"""
    cutoff = time.time() - 30 * 86400
    stale = []
    for key, info in rows.items():
        try:
            stamp = datetime.strptime((info or {}).get("last") or "",
                                      "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            stale.append(key)
            continue
        if stamp.timestamp() < cutoff:
            stale.append(key)
    for key in stale:
        rows.pop(key, None)


# --------------------------------------------------------------------------
# 通知
# --------------------------------------------------------------------------
def beep(kind: str) -> None:
    try:
        if kind == "R":
            for f in (900, 700, 900, 700):
                winsound.Beep(f, 180)
        else:
            winsound.Beep(640, 220)
            winsound.Beep(820, 220)
    except RuntimeError:
        pass


def notify_qq(text: str, shot: Path | str | None) -> str:
    if not QQ_CONFIG.exists():
        return "未配置QQ(跳过)"
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import qq_notify  # noqa: PLC0415

        notifier = qq_notify.QQNotifier.from_file(QQ_CONFIG)
        if shot:
            notifier.send_image(shot, caption=text)
        else:
            notifier.send_text(text)
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"失败:{exc}"


def color_dist(rows: list[dict]) -> str:
    """把当前名单的颜色分布写成一句，例如「红名1 白名2 橙名1」。"""
    cnt: dict[str, int] = {}
    for row in rows:
        label = COLOR_CN.get(row.get("kind") or UNKNOWN, "颜色未识别")
        cnt[label] = cnt.get(label, 0) + 1
    order = ["红名", "橙名", "白名", "绿名", "蓝名", "颜色未识别"]
    parts = [f"{label}{cnt[label]}" for label in order if label in cnt]
    return " ".join(parts) or "—"


def side_dist(rows: list[dict]) -> str:
    """敌我分布，例如「友方 9 敌方 2」。"""
    f = sum(1 for r in rows if r.get("side") == "F")
    e = sum(1 for r in rows if r.get("side") == "E")
    return f"友方 {f} 敌方 {e}"

def alert(rows: list[dict], row: dict, shot: Path, test: bool = False,
          head: str | None = None, extra: str = "", event: str = "进入本地") -> None:
    """报警：只报颜色 + 现场截图，不报名字（本来也没读名字）。"""
    kind = row.get("kind") or UNKNOWN
    side = row.get("side") or UNKNOWN
    mark = row.get("mark") or "?"
    if head is None:
        head = "🧪测试" if test else ("🚨敌对" if side == "E" else "❗友方")
    if TAG:
        head = f"【{TAG}】{head}"
    text = (f"{head}{event}｜{COLOR_CN.get(kind, '未知')}\n"
            f"时间：{now()}\n"
            f"图标：{mark}\n"
            f"（敌我按名字后的图标判，名字本身不读，具体是谁看截图）")
    if extra:
        text += f"\n{extra}"
    stamp = datetime.now().strftime("%H%M%S")
    keep = shot
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        keep = SHOTS / f"{stamp}_{kind}_{row.get('hash', 'row')}.png"
        keep.write_bytes(shot.read_bytes())
    except OSError:
        keep = shot
    status = notify_qq(text, keep)
    beep(kind)
    log(f"ALERT {event} {SIDE_CN.get(side, '?')}·{COLOR_CN.get(kind)}·{mark} 行{row.get('hash', '?')} | 本地{len(rows)}行 | qq:{status}")
    try:
        with EVENTS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": now(), "kind": kind, "side": side, "mark": mark,
                                 "hash": row.get("hash", ""),
                                 "rows": len(rows), "qq": status, "shot": str(keep)},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# 核心
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 面板底部的三个计数：菜单开着没、有没有敌人，都看这一条
# --------------------------------------------------------------------------
COUNT_PITCH = 62            # 三个图标之间的间距（实测 62–63 像素）
COUNT_BOX_W, COUNT_BOX_H = 12, 21
ZERO_GLYPH = ("######", "#....#", "#....#", "#....#", "#....#",
              "#....#", "#....#", "#....#", "#....#", "#....#", "######")


def _icon_rows(im: Image.Image) -> tuple[list[int], list[int], list[int], list[int]]:
    """一次扫描：每行的红像素数、灰像素数，以及两类的逐行位图。"""
    px = im.load()
    w, h = im.size
    red_rows, gray_rows = [], []
    for y in range(h):
        rr = bytearray(w)
        gr = bytearray(w)
        for x in range(w):
            r, g, b = px[x, y]
            if r > 110 and g < 80 and b < 80 and r - g > 50:
                rr[x] = 1
            elif abs(r - g) < 22 and abs(g - b) < 22 and 120 < r < 215:
                gr[x] = 1
        red_rows.append(rr)
        gray_rows.append(gr)
    return red_rows, gray_rows, [sum(rr) for rr in red_rows], [sum(gr) for gr in gray_rows]


def _runs(colsum: list[int], need: int, merge: int = 2) -> list[tuple[int, int]]:
    """把「连续够多的一列」聚成区间，间隔不超过 merge 的合并成一个图标。"""
    out: list[tuple[int, int]] = []
    start = None
    for x, v in enumerate(colsum):
        if v >= need:
            start = x if start is None else start
        else:
            if start is not None:
                out.append((start, x - 1))
                start = None
    if start is not None:
        out.append((start, len(colsum) - 1))
    merged: list[tuple[int, int]] = []
    for run in out:
        if merged and run[0] - merged[-1][1] <= merge:
            merged[-1] = (merged[-1][0], run[1])
        else:
            merged.append(run)
    return merged


def find_count_bar(im: Image.Image) -> dict | None:
    """找底部计数条：红底「!」+ 红底「−」+ 灰底「=」并排，返回三个数字的取值框。

    这条是本地菜单独有的，找到了才算菜单开着；找不到就说明菜单没开
    （或者被别的窗口挡住），这一轮直接跳过。
    """
    red_rows, gray_rows, red_cnt, _gray_cnt = _icon_rows(im)
    h = len(red_rows)
    band_ys = [y for y in range(30, h - 8) if red_cnt[y] >= 8]
    if not band_ys:
        return None
    # 连续的行归成一个条带
    bands: list[tuple[int, int]] = []
    s = p = band_ys[0]
    for y in band_ys[1:]:
        if y - p <= 3:
            p = y
        else:
            bands.append((s, p))
            s = p = y
    bands.append((s, p))
    for y0, y1 in bands:
        if y1 - y0 < 5 or y1 - y0 > 18:
            continue                       # 图标高 11–14 像素
        w = im.width
        red_col = [0] * w
        gray_col = [0] * w
        for y in range(y0, y1 + 1):
            rr, gr = red_rows[y], gray_rows[y]
            for x in range(w):
                red_col[x] += rr[x]
                gray_col[x] += gr[x]
        need = max(3, (y1 - y0 + 1) - 3)
        reds = _runs(red_col, need, merge=3)
        for a in reds:
            if a[1] - a[0] < 3:
                continue
            for b in reds:
                if b[0] - a[1] < 44 or b[0] - a[1] > 64:
                    continue
                # 第三个是灰底「=」：两横条，列方向的像素只有 4–6 个，单独放宽
                gx0, gx1 = b[1] + 44, min(im.width, b[1] + 68)
                band_gray = gray_col[gx0:gx1]
                if sum(band_gray) < 15:
                    continue
                xs = [i for i, v in enumerate(band_gray) if v >= 2]
                if not xs:
                    continue
                # 只取紧挨着的那一簇（等号本身），别把旁边别的灰像素算进来
                cluster = [xs[0]]
                for i in xs[1:]:
                    if i - cluster[-1] <= 2:
                        cluster.append(i)
                    else:
                        break
                c_start, c_end = gx0 + cluster[0], gx0 + cluster[-1]
                top = y0 - 4
                return {"y0": y0, "y1": y1,
                        "digits": [(a[1] + 3, top, a[1] + 3 + COUNT_BOX_W, top + COUNT_BOX_H),
                                   (b[1] + 3, top, b[1] + 3 + COUNT_BOX_W, top + COUNT_BOX_H),
                                   (c_end + 3, top, c_end + 3 + COUNT_BOX_W, top + COUNT_BOX_H)],
                        "icons": (a, b, (c_start, c_end))}
    return None


def _looks_like_header(im: Image.Image, band: tuple, bar: dict) -> bool:
    """标题栏「本地 + 齿轮 + 换位」判定：标题右边的齿轮是浅白色，
    名单行右边只会是绿色星/蓝色加号或什么都没有。"""
    px = im.load()
    a, _b, c = bar["icons"]
    x0, x1 = max(0, c[0] - 30), min(im.width, c[1] + 10)
    bright = 0
    for y in range(band[0], min(im.height, band[1] + 1)):
        for x in range(x0, x1):
            r, g, bl = px[x, y]
            if min(r, g, bl) > 170:
                bright += 1
    return bright >= 12


def _card_top(im: Image.Image, bar: dict) -> int:
    """从计数条往上摸面板的上边缘：面板是深色卡片，碰到亮背景就是出界了。

    用「暗像素占比」而不是平均亮度——面板里有一条淡淡的分割线，
    平均值会被那一行拉高，把上边缘误判到面板中间。
    """
    px = im.load()
    a, b, _c = bar["icons"]
    xs = list(range(a[0] + 1, max(a[0] + 2, b[1])))
    y = bar["y0"] - 8
    top = max(0, y)
    while y > 0:
        dark = 0
        n = 0
        for x in xs:
            if 0 <= x < im.width:
                r, g, bl = px[x, y]
                if 0.299 * r + 0.587 * g + 0.114 * bl < 90:
                    dark += 1
                n += 1
        if n and dark / n >= 0.7:
            top = y
            y -= 1
        else:
            break
    return top


def _is_zero(mask: Image.Image) -> bool:
    """这个数字是不是 0：把字形抠出来跟 0 的字模逐像素比。"""
    px = mask.load()
    xs, ys = [], []
    for y in range(mask.height):
        for x in range(mask.width):
            if px[x, y] > 128:
                xs.append(x)
                ys.append(y)
    if not xs:
        return False
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = x1 - x0 + 1, y1 - y0 + 1
    if w != len(ZERO_GLYPH[0]) or h != len(ZERO_GLYPH):
        return False        # 位数不一样（比如两位数）一定不是 0
    diff = 0
    for y in range(h):
        for x in range(w):
            ink = px[x0 + x, y0 + y] > 128
            if ink != (ZERO_GLYPH[y][x] == "#"):
                diff += 1
    return diff <= 6        # 抗锯齿会让边缘差两三个像素，容许到 6


def read_counts(im: Image.Image, bar: dict) -> list[int]:
    """读底部三个计数：0 = 没有，1 = 这个数字不是 0（也就是有敌人）。"""
    out: list[int] = []
    for box in bar.get("digits") or []:
        try:
            mask = text_mask(im, box)
        except Exception:      # noqa: BLE001
            return []
        if mask.getbbox() is None:      # 框里没有字形，说明位置算偏了
            return []
        out.append(0 if _is_zero(mask) else 1)
    return out


def _marker(im, band: tuple[int, int]) -> tuple[str, str]:
    """读名字后面那个敌我图标，返回 (友/敌, 图标名)。

    绿星、蓝加 = 友方；红减、灰色等号 = 敌方；**没有图标 = 自己**（自己的号
    在本地列表里没有关系图标，算成敌人会误判，所以单独归为「自己」）。
    """
    a, b = band
    x0, x1 = _MARK
    y0, y1 = (b - 2, b + 20) if _PORTRAIT else (a - 11, b + 12)
    px = im.load()
    c = {"green": 0, "blue": 0, "red": 0, "gray": 0}
    for y in range(max(0, y0), min(im.height, y1)):
        for x in range(max(0, x0), min(im.width, x1)):
            r, g, bl = px[x, y]
            if g > 110 and g - r > 35 and g - bl > 30:
                c["green"] += 1
            elif bl > 120 and bl - r > 45 and bl - g > 25:
                c["blue"] += 1
            elif r > 120 and g < 95 and bl < 95 and r - g > 45:
                c["red"] += 1
            elif 85 < r < 205 and abs(r - g) < 26 and abs(g - bl) < 26 and abs(r - bl) < 26:
                c["gray"] += 1
    if c["green"] >= 25:
        return "F", "绿星"
    if c["blue"] >= 18:
        return "F", "蓝加"
    if c["red"] >= 18:
        return "E", "红减"
    if c["gray"] >= 18:
        return "E", "灰等号"
    return "?", "无图标"

def _green_icon_pixels(im, box: tuple[int, int, int, int]) -> int:
    px = im.load()
    x0, y0, x1, y1 = box
    hits = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b = px[x, y]
            if g > 150 and g - r > 40 and g - b > 40:
                hits += 1
    return hits


def _has_portraits(im) -> bool:
    """新版 Windows/安卓版本地列表每行左边有角色头像；旧版没有。

    头像像素「彩色且饱和」（肤色、头发、衣服），名字文字则是白/红/绿/蓝纯色，
    所以用「彩度高的像素数量」判断这一版是不是带头像的列表。
    """
    px = im.load()
    hits = 0
    x0, x1 = max(0, _NX[0] - 54), max(1, _NX[0] - 6)
    y0, y1 = _ROWS_Y[0], _ROWS_Y[1]
    for y in range(y0, y1, 2):
        for x in range(x0, x1, 2):
            r, g, b = px[x, y]
            mx, mn = max(r, g, b), min(r, g, b)
            if mx > 90 and mx - mn > 55:
                hits += 1
                if hits >= 120:
                    return True
    return False


def _card_dark(im, bands: list[tuple[int, int]]) -> bool:
    """名单是不是画在一块深色面板上：名字后面的底色应该是暗的。

    空间站/太空背景里的文字底色偏亮，用这条挡住「把别的窗口文字当名单」。
    """
    x0, x1 = _NX
    top = max(0, bands[0][0] - 8)
    bottom = min(im.height, bands[-1][1] + 8)
    px = im.load()
    total = n = 0
    for y in range(top, bottom):
        for x in range(x0, x1, 2):
            r, g, b = px[x, y]
            total += 0.299 * r + 0.587 * g + 0.114 * b
            n += 1
    return n > 0 and total / n < 85


def panel_open(im) -> bool:
    """本地面板是否开着（旧版靠绿色图标列，新版靠深色卡片 + 头像）。"""
    if _green_icon_pixels(im, _ICON_BOX) >= 12:
        return True
    return _has_portraits(im)


def _red_blob(im, band: tuple[int, int]) -> int:
    """行带里「实心红块」的像素数：计数条那两个红底图标算实心块，红名字的笔画不算。"""
    px = im.load()
    a, b = band
    x0, x1 = _NX
    hits = 0

    def red(p):
        r, g, bl = p
        return r > 110 and g < 95 and bl < 95 and r - g > 45 and r - bl > 45

    for y in range(max(1, a + 1), min(im.height - 1, b + 1)):
        for x in range(max(1, x0), min(im.width - 1, x1)):
            if (red(px[x, y]) and red(px[x - 1, y]) and red(px[x + 1, y])
                    and red(px[x, y - 1]) and red(px[x, y + 1])):
                hits += 1
    return hits


def _card_bottom(im) -> int:
    """面板卡片的下边缘：从下往上找第一行「整片亮起来」的地方。"""
    px = im.load()
    last = 0
    for y in range(40, im.height):
        total = n = 0
        for x in range(_NX[0], _NX[1], 3):
            r, g, b = px[x, y]
            total += 0.299 * r + 0.587 * g + 0.114 * b
            n += 1
        if n and total / n < 75:
            last = y
    return last


def _text_count(im: Image.Image, xs: tuple[int, int], ys: tuple[int, int]) -> int:
    """一块区域里「像字」的像素数，用来分辨名字在图标左边还是右边。"""
    px = im.load()
    x0, x1 = max(0, xs[0]), min(im.width, xs[1])
    y0, y1 = max(0, ys[0]), min(im.height, ys[1])
    n = 0
    for y in range(y0, y1, 2):
        for x in range(x0, x1, 2):
            if is_text_pixel(*px[x, y]):
                n += 1
    return n


def _mean_lum(im: Image.Image, xs: tuple[int, int], ys: tuple[int, int]) -> float:
    """一块区域的平均亮度，用来判断哪边是深色卡片、哪边是游戏场景。"""
    px = im.load()
    x0, x1 = max(0, xs[0]), min(im.width, xs[1])
    y0, y1 = max(0, ys[0]), min(im.height, ys[1])
    total = n = 0
    for y in range(y0, y1, 3):
        for x in range(x0, x1, 3):
            r, g, b = px[x, y]
            total += 0.299 * r + 0.587 * g + 0.114 * b
            n += 1
    return total / n if n else 999.0


def _green_column(im: Image.Image) -> tuple[int, list[int]]:
    """在画面左半边找「一竖列绿色图标」，返回 (列中心 x, 每个图标中心的 y 列表)。"""
    px = im.load()
    pts = []
    for y in range(30, min(im.height, 540), 2):
        for x in range(140, min(700, im.width), 2):
            r, g, b = px[x, y]
            # 面板是半透明的，绿色星会被压暗到 g≈130–150，阈值放低一点
            if g > 110 and g - r > 32 and g - b > 32:
                pts.append((x, y))
    if len(pts) < 20:
        return 0, []
    hist: dict[int, int] = {}
    for x, _y in pts:
        hist[x // 6] = hist.get(x // 6, 0) + 1
    best_bin, best_n = max(hist.items(), key=lambda kv: kv[1])
    if best_n < 10:
        return 0, []
    cx = best_bin * 6 + 3
    ys = sorted(y for x, y in pts if abs(x - cx) <= 8)
    if not ys:
        return 0, []
    centers, group = [], [ys[0]]
    for y in ys[1:]:
        if y - group[-1] <= 6:
            group.append(y)
        else:
            centers.append(sum(group) // len(group))
            group = [y]
    centers.append(sum(group) // len(group))
    return cx, centers


def locate(im: Image.Image) -> dict | None:
    """每一轮重新定位「本地」菜单（菜单可能被拖动），返回各区域坐标。

    锚点是名单右侧那一竖列绿色关系图标：符号是稳定的，跟着菜单一起移动。
    图标左边有字 → 无头像版；图标右边有字 → 带头像版（图标压在头像右下角）。
    """
    cx, centers = _green_column(im)
    if not cx or len(centers) < 2:
        return None
    y_top, y_bot = centers[0], centers[-1]
    if y_bot - y_top < 36:
        return None
    win = (y_top - 54, y_bot + 44)
    left_box = (max(0, cx - 168), min(im.width, cx - 24))
    right_box = (max(0, cx + 12), min(im.width, cx + 150))
    lum_left = _mean_lum(im, left_box, win)
    lum_right = _mean_lum(im, right_box, win)
    # 名单那一侧一定压在深色卡片上，另一边是场景（更亮）
    if lum_right < lum_left - 6:
        name, mark, portrait = right_box, (cx - 13, cx + 13), True
    else:
        name, mark, portrait = left_box, (cx - 13, cx + 13), False
    name = (max(0, name[0]), min(im.width, name[1]))
    if name[1] - name[0] < 60:
        return None
    return {
        "icon": (cx - 9, max(0, win[0]), cx + 11, min(im.height, win[1])),
        "name": name,
        "rows_y": (max(0, win[0]), min(im.height, win[1])),
        "mark": mark,
        "portrait": portrait,
    }


def scan(shot: Path) -> dict:
    """截一张图 → 认出「本地」列表的每一行和它的颜色。全程只看像素，不做 OCR。"""
    global _NX, _PORTRAIT, _ROWS_Y, _ICON_BOX, _MARK
    try:
        im = Image.open(shot).convert("RGB")
    except Exception as exc:            # noqa: BLE001  截图没写完整时不要整个进程崩
        return {"ok": False, "reason": f"截图读取失败（{exc.__class__.__name__}）", "rows": []}
    if im.size != (960, 540):
        return {"ok": False, "reason": f"截图分辨率异常 {im.size}", "rows": []}
    bar = find_count_bar(im)            # 底部三个计数图标：本地菜单开着就有
    if not bar:
        return {"ok": False, "reason": "本地菜单没打开（没找到底部红!、红-、灰= 三个计数图标）", "rows": []}
    geo = locate(im)                    # 找名单右侧那列关系图标（全白名时可能没有）
    if geo:
        _NX = geo["name"]
        # 图标列只覆盖「有图标的人」，名单往下还有白名行，所以下边界要伸到计数条上面
        _ROWS_Y = (max(0, geo["rows_y"][0] - 6),
                   max(geo["rows_y"][1], bar["y0"] - 8))
        _ICON_BOX = geo["icon"]
        _MARK = geo["mark"]
        _PORTRAIT = geo["portrait"]
        has_icons = True
    else:
        # 名单里一个关系图标都没有（全是白名）时，用计数条反推面板位置：
        # 计数条从面板左边开始，名字从它左上方一直排到它上面。
        a, _b, c = bar["icons"]
        left = max(0, a[0] - 6)
        _NX = (left + 2, min(im.width, c[1] + 8))
        _ROWS_Y = (max(0, bar["y0"] - 420), max(0, bar["y0"] - 8))
        _ICON_BOX = (0, 0, 0, 0)
        # 没有绿星列时，敌我图标就贴在第三个数字框右边那一小竖条
        d_end = bar["digits"][2][2]
        _MARK = (max(0, d_end + 2), min(im.width, d_end + 24))
        _PORTRAIT = False
        has_icons = False
    bands = rows_of(im)
    if bands and not _card_dark(im, bands):
        return {"ok": False, "reason": "这段文字不在深色面板上，可能不是本地窗口", "rows": []}
    bottom = _card_bottom(im)
    if bottom:
        # 去掉面板底部的计数条（那两个红底图标 + 数字），它们不是名单里的人
        bands = [bd for bd in bands
                 if (bd[0] + bd[1]) / 2 <= bottom - 26 and _red_blob(im, bd) < 40]
    if bands and _looks_like_header(im, bands[0], bar):
        # 面板顶上那条「本地 / 齿轮 / 换位」的标题栏不是名单，别当成人
        bands = bands[1:]
    rows = []
    for band in bands:
        kind, stats = classify(im, band)
        side, mark = _marker(im, band)
        h = row_hash(im, band)
        box = text_box(im, band)
        fp = fingerprint(im, box) if box else b""
        rows.append({"band": band, "y": round((band[0] + band[1]) / 2, 1),
                     "kind": kind, "stats": stats, "hash": h, "fp": fp, "side": side, "mark": mark})
    if rows and rows[0].get("side") == "?" and has_icons:
        # 本地列表第一行是自己的号：有图标列时，唯独它没有关系图标，
        # 还会随选中高亮闪烁，不能当成「白名进入」，直接跳过。
        # （整列都没有图标时无法区分自己和朋友，就不剪第一行）
        rows = rows[1:]
    counts = read_counts(im, bar)   # 底部三个计数：有没有敌人看这里
    if not counts:
        return {"ok": False, "reason": "底部计数条读不出数字", "rows": []}
    return {"ok": True, "rows": rows, "counts": counts}


_HB: dict = {"ok": True, "rows": 0, "colors": [], "err": ""}
_empty_note_at = 0.0


def write_heartbeat(st: dict, cycles: int, alerts: int, ok: bool, err: str = "") -> None:
    data = {
        "time": now(),
        "pid": os.getpid(),
        "cycles": cycles,
        "alerts": alerts,
        "ok": ok,
        "err": err or _HB.get("err", ""),
        "rows": _HB.get("rows", 0),
        "colors": _HB.get("colors", []),
        "sides": _HB.get("sides", []),
        "counts": _HB.get("counts", []),
        "panel": _HB.get("panel", True),
        "known": len(st.get("rows", {})),
        "qq": QQ_CONFIG.exists(),
    }
    try:
        (WORK / "heartbeat.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _write_heartbeat(report: list[dict], cycles: int, alerts: int) -> None:
    """多开版的心跳：一台一条，外加汇总，控制台按这个显示。"""
    emus = []
    for r in report:
        emus.append({
            "name": r.get("name", ""),
            "serial": r.get("serial", ""),
            "ok": bool(r.get("ok")),
            "reason": r.get("reason", ""),
            "rows": r.get("rows", 0),
            "colors": r.get("colors", []),
            "sides": r.get("sides", []),
            "counts": r.get("counts", []),
            "known": r.get("known", 0),
        })
    total_rows = sum(e["rows"] for e in emus)
    danger = any(any(e["counts"]) for e in emus)
    data = {
        "time": now(),
        "pid": os.getpid(),
        "cycles": cycles,
        "alerts": alerts,
        "ok": all(e["ok"] for e in emus) if emus else False,
        "err": next((e["reason"] for e in emus if not e["ok"]), ""),
        "rows": total_rows,
        "colors": [c for e in emus for c in e["colors"]],
        "sides": [s for e in emus for s in e["sides"]],
        "counts": [c for e in emus for c in e["counts"]],
        "panel": any(e["ok"] for e in emus),
        "multi": True,
        "emulators": emus,
        "danger": danger,
        "qq": QQ_CONFIG.exists(),
    }
    try:
        (WORK / "heartbeat.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _head(side: str | None, leaving: bool = False) -> str:
    """通知标题：敌 / 友 / 无图标（按白名算）。"""
    if side == "E":
        return "🚨敌对离开本地" if leaving else "🚨敌对进入本地"
    if side == "F":
        return "👋友方离开本地" if leaving else "❗友方进入本地"
    return "👋白名离开本地" if leaving else "❗白名进入本地"


def track_presence(st: dict, known_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """只认两种事件：进入、离开。返回 (进入的行, 离开的人)。

    敌我只按名字后面的图标判：绿星/蓝加=友方，红减/灰等号=敌方；
    没有图标的按白名照常通知（2026-11-06 按用户要求），不再单独区分「自己」。
    同一事件 90 秒内不重复，避免画面抖动刷屏。
    """
    keys_now: dict[str, dict] = {}
    for row in known_rows:
        key = row.get("key") or ""
        if key:
            keys_now[key] = row
    store = st.setdefault("rows", {})
    left = st.setdefault("left", {})
    prev = set(st.get("present") or [])
    present = set(keys_now)

    # 去抖：画面滚动/抖动时同一行会闪一下，连续 2 轮都稳定才认
    streak_in = st.setdefault("streak_in", {})
    streak_out = st.setdefault("streak_out", {})
    for key in list(streak_in):
        if key not in present:
            streak_in.pop(key, None)
    for key in present:
        streak_in[key] = int(streak_in.get(key, 0)) + 1
    for key in list(streak_out):
        if key in present:
            streak_out.pop(key, None)
    for key in prev - present:
        streak_out[key] = int(streak_out.get(key, 0)) + 1

    entered: list[dict] = []
    for key in sorted(present):
        if int(streak_in.get(key, 0)) != 2:      # 只在这一行刚开始稳定时触发一次
            continue
        row = keys_now[key]
        info = store.get(key) or {}
        stamp = info.get("alert_at") or ""
        if stamp and _seconds_since(stamp) < 90:
            continue
        entered.append(row)
        left.pop(key, None)

    leavers: list[dict] = []
    for key in sorted(streak_out):
        if int(streak_out.get(key, 0)) != 2:
            continue
        info = store.get(key) or {}
        if key in left and _seconds_since(left[key]) < 90:
            continue
        streak_out.pop(key, None)
        left[key] = now()
        leavers.append({**info, "key": key, "stayed": _seconds_since(info.get("first") or "")})

    st["present"] = sorted(present)
    return entered, leavers


def _seconds_since(stamp: str) -> float:
    try:
        return (datetime.now() - datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except (TypeError, ValueError):
        return 1e9


def check_once(st: dict, send: bool, test: bool = False, verbose: bool = True) -> list[dict]:
    global _empty_note_at  # noqa: PLW0603
    shot = grab(WORK / "last.png")
    res = scan(shot)
    if not res["ok"]:
        reason = res.get("reason") or "扫描失败"
        panel_missing = reason.startswith("本地窗口不在")
        _HB.update({"ok": panel_missing, "err": "" if panel_missing else reason, "rows": 0,
                    "panel": False, "colors": [], "sides": [], "counts": []})
        if panel_missing and verbose and time.time() - _empty_note_at > 300:
            log("本地窗口不在常规位置（被别的窗口挡住或游戏不在前台），本轮跳过")
            _empty_note_at = time.time()
        return {"ok": False, "reason": reason, "entered": [], "left": 0}
    rows = res["rows"]
    counts = res.get("counts") or []
    if not rows:
        # 菜单开着但名单里只有自己（或暂时没别人）：这不是故障，计数照样要报
        _HB.update({"ok": True, "rows": 0, "colors": [], "sides": [], "counts": counts,
                    "err": "", "panel": True})
        if verbose:
            log(f"本地 0 行（菜单里只有自己） | 计数 "
                f"{'/'.join(str(c) for c in counts) if counts else '未读到'}")
        if send and counts and any(counts):
            last = st.get("count_note") or {}
            sig = "".join(str(c) for c in counts)
            if sig != last.get("key") and _seconds_since(last.get("t") or "") > 300:
                alert(rows, {"kind": "R", "side": "E", "mark": "计数", "hash": "counts"},
                      shot, test=test, head="🚨本地出现敌对", event="计数报警",
                      extra=f"底部计数：{'/'.join(str(c) for c in counts)}（非 0 就是有敌人）")
                st["count_note"] = {"key": sig, "t": now()}
        elif send:
            st.pop("count_note", None)
        save_state(st)
        return {"ok": True, "reason": "名单为空", "entered": [], "left": 0}
    total = len(rows)
    _HB.update({"ok": True, "rows": total,
                "colors": [COLOR_CN.get(r["kind"], "?") for r in rows],
                "sides": [SIDE_CN.get(r.get("side"), "?") for r in rows],
                "counts": counts,
                "err": "", "panel": True})
    fresh, known_rows = [], []
    for row in rows:
        key, _cos = match_row(st, row["hash"], row["fp"])
        if key is not None:
            remember_row(st, key, row["hash"], row["kind"], row["fp"],
                         side=row.get("side", ""), mark=row.get("mark", ""))
            known_rows.append({**row, "key": key})
            continue
        fresh.append(row)

    # 新出现的行先登记进已知名单（不管这一轮发不发通知）
    for row in fresh:
        remember_row(st, row["hash"], row["hash"], row["kind"], row["fp"],
                     first=True, side=row.get("side", ""), mark=row.get("mark", ""))
        known_rows.append({**row, "key": row["hash"]})

    try:
        entered, leavers = track_presence(st, known_rows)
    except Exception as exc:  # noqa: BLE001
        log(f"进出记录失败（忽略）: {exc}")
        entered, leavers = [], []

    if verbose:
        note = f" | 计数 {'/'.join(str(c) for c in counts)}" if counts else " | 计数 未读到"
        log(f"本地 {total} 行 | {side_dist(rows)}{note}")

    if send:
        # 敌人不再看名字后面的图标，直接看面板底部那三个计数（图标容易误判）
        if counts and any(counts):
            last = st.get("count_note") or {}
            sig = "".join(str(c) for c in counts)
            if sig != last.get("key") and _seconds_since(last.get("t") or "") > 300:
                alert(rows, {"kind": "R", "side": "E", "mark": "计数", "hash": "counts"},
                      shot, test=test, head="🚨本地出现敌对", event="计数报警",
                      extra=f"底部计数：{'/'.join(str(c) for c in counts)}（非 0 就是有敌人）")
                st["count_note"] = {"key": sig, "t": now()}
        elif counts:
            st.pop("count_note", None)
        # 2026-11-06：友方、敌方都不按行通知；敌方交给上面的计数判断
        for row in entered:
            if row.get("side") in ("F", "E"):
                continue
            alert(rows, row, shot, test=test, event="进入本地",
                  head=_head(row.get("side")))
            _mark_alerted(st, row.get("key") or row.get("hash", ""))
        for info in leavers:
            if info.get("side") in ("F", "E"):
                continue
            mins = int(info.get("stayed") or 0) // 60
            extra = f"（在本地待了约 {mins} 分钟）" if mins >= 1 else ""
            alert(rows, info, shot, test=test, event="离开本地", extra=extra,
                  head=_head(info.get("side"), leaving=True))
        if not entered and not leavers:
            pass

    save_state(st)
    return {"ok": True, "reason": "", "entered": entered, "left": len(leavers)}


def _mark_alerted(st: dict, key: str) -> None:
    info = st.get("rows", {}).get(key)
    if isinstance(info, dict):
        info["alert_at"] = now()


def main(argv: list[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if "--all" in args_in:
        import eve_multi
        return eve_multi.main([a for a in args_in if a != "--all"])
    try:
        sys.stdout.reconfigure(errors="replace")  # 控制台是 GBK 时不至于抛 UnicodeEncodeError
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description="EVE 本地名单预警")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--prime", action="store_true", help="把当前名单登记为已知（不推送）")
    ap.add_argument("--test", action="store_true", help="报警标记为测试")
    ap.add_argument("--interval", type=int, default=6, help="循环间隔秒")
    ap.add_argument("--minutes", type=float, default=0, help="跑多少分钟后自动退出，0=一直跑")
    ap.add_argument("--takeover", action="store_true", help="顶掉已在跑的旧进程")
    ap.add_argument("--simulate", action="store_true",
                    help="模拟一次预警：抓一张现场截图，直接推到 QQ（不动任何状态）")
    args = ap.parse_args(argv)

    WORK.mkdir(parents=True, exist_ok=True)
    st = load_state()

    if args.simulate:
        shot = grab(WORK / "last.png")
        res = scan(shot)
        rows = res.get("rows") or []
        hint = ""
        if not rows:
            rows = [{"kind": "R", "hash": "simulate", "y": 0, "band": (0, 0), "stats": {},
                     "fp": b"", "side": "E", "mark": "红减"}]
            hint = "\n（没检测到本地面板，附的是当前全屏截图）"
        pick = (next((r for r in rows if r.get("side") == "E"), None)
                or next((r for r in rows if r.get("side") == "F"), None)
                or rows[0])
        alert(rows, pick, shot, test=True, head="🧪模拟预警·",
              extra="这是一条模拟消息，用来确认 QQ 通道和截图都正常。" + hint)
        print(f"已推送：本地 {len(rows)} 行 | {color_dist(rows)} | 截图 {shot}")
        return 0

    if args.prime or args.once:
        shot = grab(WORK / "last.png")
        res = scan(shot)
        if not res["ok"]:
            print(f"扫描失败: {res.get('reason')}")
            return 1
        rows = res["rows"]
        if args.prime:
            for row in rows:
                remember_row(st, row["hash"], row["hash"], row["kind"], row["fp"], first=True,
                         side=row.get("side", ""), mark=row.get("mark", ""))
            save_state(st)
        print(f"本地 {len(rows)} 行（只认图标敌我 + 颜色，不读名字）:")
        for i, row in enumerate(rows, 1):
            print(f"  第{i}行  {SIDE_CN.get(row.get('side'), '?'):4s} {COLOR_CN.get(row['kind'], '?'):6s} {row.get('mark', '?')}  y={row['y']}  指纹 {row['hash']}")
        print(f"颜色分布：{color_dist(rows)}    敌我：{side_dist(rows)}")
        if args.prime:
            print("已把这些行登记为已知，之后不再报警；红名离开后再回来仍会报。")
        return 0

    if args.takeover:
        release_lock()
    ok, other = acquire_lock()
    if not ok:
        log(f"已有监控在跑（pid {other}），本次退出。要重启请加 --takeover")
        return 2

    log("=" * 64)
    log(f"本地预警启动 pid={os.getpid()} interval={args.interval}s "
        f"已知={len(st.get('rows', {}))} qq={'已配置' if QQ_CONFIG.exists() else '未配置'}")
    deadline = time.time() + args.minutes * 60 if args.minutes else None
    last_note = 0.0
    last_err, last_err_at = "", 0.0
    cycles = alerts = 0
    first_ok = True          # 启动后第一轮只对齐名单，不报警（换算法后不至于一口气刷屏）
    bad = 0                  # 连续多少轮没读到菜单
    fault_at = 0.0
    fault_notified = False
    started_at = now()
    if QQ_CONFIG.exists():
        status = notify_qq(
            f"✅本地预警已启动｜{started_at}\n"
            f"每 {args.interval} 秒看一眼本地菜单：有人进入/离开就通知（敌方、白名会报，友方绿星蓝加不报）。",
            None)
        log(f"已发送启动通知 qq:{status}")
    try:
        while True:
            try:
                res = check_once(st, send=not first_ok)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "entered": []}
            cycles += 1
            if res.get("ok"):
                if first_ok:
                    first_ok = False
                    log(f"首轮对齐完成：登记 {_HB.get('rows', 0)} 行（本轮不发通知）")
                fresh = res.get("entered") or []
                alerts += len(fresh)
                write_heartbeat(st, cycles, alerts, ok=True)
                if fresh:
                    log(f"新增 {len(fresh)} 行（本次会话累计 {alerts} 次报警）")
                if fault_notified:
                    label = res.get("reason") or ""
                    if label != "名单为空":
                        notify_qq(f"✅本地预警已恢复｜{now()}\n重新读到本地菜单了，继续监控。", None)
                        log("已恢复，已发送恢复通知")
                        fault_notified = False
                bad = 0
            else:
                bad += 1
                msg = res.get("reason") or "未知原因"
                write_heartbeat(st, cycles, alerts, ok=False, err=msg[:200])
                if msg != last_err or time.time() - last_err_at > 120:
                    log(f"本轮异常: {msg}")
                    last_err, last_err_at = msg, time.time()
                if bad >= FAULT_AFTER and (not fault_notified
                                           or time.time() - fault_at > FAULT_COOLDOWN):
                    shot = WORK / "last.png"
                    status = notify_qq(
                        f"⚠️本地预警异常｜{now()}\n"
                        f"原因：{msg}\n"
                        f"已经连续 {bad} 轮没读到本地菜单（游戏掉线、角色回站、窗口被挡、菜单被关都会这样）。",
                        shot if shot.exists() else None)
                    log(f"已发送故障通知 qq:{status}：{msg}")
                    fault_at, fault_notified = time.time(), True
            if deadline and time.time() >= deadline:
                log("到达设定运行时长，退出")
                return 0
            time.sleep(max(1, args.interval))
    except KeyboardInterrupt:
        log("收到中断，退出")
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())

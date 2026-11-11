"""打包入口：同一个 exe 既当「控制台界面」，又当「监控子进程」。

- 不带参数：起图形控制台（本地预警控制台.pyw）
- 带 --child-monitor：把剩下的参数交给 eve_local_alert.main()，在后台跑监控

打包（在脚本目录执行）：
    python -m PyInstaller --noconfirm --onefile --noconsole ^
        --name "EVE本地预警" ^
        --add-data "本地预警控制台.pyw;." ^
        eve_alert_main.py
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

# 界面是在运行时用 runpy 加载的，PyInstaller 静态分析看不见这些 import，
# 这里显式引入一次，tkinter / tcl 才会被打进 exe。
try:  # pragma: no cover - 仅用于打包
    import tkinter  # noqa: F401
    import tkinter.messagebox  # noqa: F401
    import tkinter.scrolledtext  # noqa: F401
    import tkinter.ttk  # noqa: F401
except Exception:  # noqa: BLE001
    pass


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main() -> int:
    base = _exe_dir()
    # 状态目录、qq_config.json 都跟着 exe 走，而不是临时解包目录
    os.environ.setdefault("EVE_ALERT_ROOT", str(base))

    if "--child-monitor" in sys.argv:
        args = [a for a in sys.argv[1:] if a != "--child-monitor"]
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))
        import eve_local_alert

        return eve_local_alert.main(args)

    target: Path | None = None
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cand = Path(meipass) / "本地预警控制台.pyw"
        if cand.exists():
            target = cand
    if target is None:
        target = base / "本地预警控制台.pyw"
    if not target.exists():
        raise SystemExit(f"找不到控制台界面文件：{target}")
    runpy.run_path(str(target), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

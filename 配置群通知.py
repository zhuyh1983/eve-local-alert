"""把预警通知切到群机器人（企业微信 / 钉钉 / 飞书 / Telegram），并立刻发一条测试。

用法：
  配置群通知.py wecom    <webhook_url>            # 企业微信群机器人（支持截图）
  配置群通知.py dingtalk <webhook_url>            # 钉钉群机器人（只发文字）
  配置群通知.py feishu   <webhook_url>            # 飞书群机器人（只发文字）
  配置群通知.py telegram <bot_token> <chat_id> [代理地址]   # Telegram（支持截图）
                          代理例：http://127.0.0.1:7897
  配置群通知.py qqwindow                           # 粘到当前打开的 QQ 群（需 QQ 已登录且群窗口在前）
  配置群通知.py qqbot                              # 切回 QQ 私聊
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import qq_notify  # noqa: E402

CONFIG = ROOT / "qq_config.json"


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    kind = argv[0].lower()
    data = {}
    if CONFIG.exists():
        data = json.loads(CONFIG.read_text(encoding="utf-8"))

    if kind == "qqbot":
        data["channel"] = "qqbot"
        data["webhook"] = {"kind": "wecom", "url": ""}
        out = "已切回 QQ 私聊（官方机器人）"
    elif kind in ("qqwindow", "qq"):
        data["channel"] = "qqwindow"
        out = "已切到 QQ 窗口粘贴发送（发到你当前打开的那个群/会话）"
    elif kind == "telegram":
        if len(argv) < 3:
            print("需要 bot_token 和 chat_id")
            return 1
        data["channel"] = "telegram"
        proxy = argv[3] if len(argv) > 3 else ""
        data["webhook"] = {"kind": "telegram", "token": argv[1], "chat_id": argv[2], "proxy": proxy}
        out = "已切到 Telegram"
    elif kind in ("wecom", "dingtalk", "feishu"):
        if len(argv) < 2:
            print("需要 webhook 地址")
            return 1
        data["channel"] = kind
        data["webhook"] = {"kind": kind, "url": argv[1]}
        out = "已切到 " + kind + " 群机器人"
    else:
        print("未知通道：" + kind)
        return 1

    data["enabled"] = True
    CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)

    note = qq_notify.QQNotifier.from_file(CONFIG)
    try:
        note.send_text("EVE 本地预警：通道测试（文字）——收到说明群通知已通")
        print("文字：OK")
    except Exception as exc:  # noqa: BLE001
        print("文字：失败 -", exc)
        return 2
    shot = ROOT / ".tmp" / "localalert" / "last.png"
    if shot.exists():
        try:
            note.send_image(shot, "EVE 本地预警：通道测试（截图）")
            print("截图：OK" if data["channel"] in ("wecom", "telegram", "qqwindow") else "截图：该通道不支持，已用文字代替")
        except Exception as exc:  # noqa: BLE001
            print("截图：失败 -", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

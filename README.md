# EVE Echoes 本地名单预警（雷电模拟器多开）

截图 → 像素识别 → 发现有人进入/离开本地，或底部计数出现非 0，就推送通知。
**全程不读名字、不做 OCR**，只判颜色、图标和数字，避免 OCR 把系统公告横幅当成玩家名。

支持同时监控多台雷电模拟器：每台一个独立线程、独立日志、独立启停，界面一屏看完。

## 功能

- **本地名单识别**：自动定位「本地」面板（面板可随意拖动，每轮重新定位），按行切分，识别每行的文字颜色与敌我图标
- **敌我判定**：只看名字后面的图标 —— 绿星 / 蓝加 = 友方，红减 / 灰色等号 = 敌方，没有图标 = 自己
- **敌对判定**：读底部三个计数（红底 `!`、红底 `−`、灰底 `=`）后面的数字，任意一个非 0 就报警
- **进出提醒**：白名（无图标）进入 / 离开本地会推送；友方静默，避免刷屏；同一事件 90 秒内不重复
- **菜单开关识别**：找不到那三个计数图标就认为菜单没开，不误报
- **启动 / 故障通知**：启动时发一条「已启动」，连续 6 轮读不到菜单发一条「异常」，恢复后再发一条
- **多开**：每台模拟器一条线程，电源式启停，日志分列，报警标题带【主号】【二号】前缀
- **免安装 exe**：PyInstaller 打包成 exe + `_internal` 目录，双击即用，不需要装 Python

## 环境

- Windows 10/11
- 雷电模拟器 LDPlayer 9（ADB 调试需打开，默认端口 `5555`，多开按 `5555 + 2*编号`）
- Python 3.10+（源码方式运行时）
- 依赖：`pip install -r requirements.txt`

## 快速开始

```bash
# 1. 看一眼当前画面能识别出什么（菜单要开着）
python eve_local_alert.py --once

# 2. 把当前名单登记为已知，之后只报「新来的」
python eve_local_alert.py --prime

# 3. 常驻监控（每 5 秒一轮）
python eve_local_alert.py --interval 5

# 4. 图形控制台（推荐）
pythonw 本地预警控制台.pyw
```

打包 exe：

```bash
python -m PyInstaller --noconfirm --onedir --noconsole --name "EVE本地预警" ^
  --add-data "本地预警控制台.pyw;." --add-data "eve_local_alert.py;." ^
  --hidden-import tkinter --hidden-import tkinter.ttk ^
  --hidden-import tkinter.scrolledtext --hidden-import tkinter.messagebox ^
  --hidden-import eve_multi eve_alert_main.py
```

> 用 `--onedir` 而不是 `--onefile`：单文件版每次启动要解包到 `%TEMP%`，在部分机器上会报
> `Could not create temporary directory!`。

## 通知通道

复制 `qq_config.example.json` 为 `qq_config.json`，选一个通道并填好：

| 通道 | 说明 | 支持截图 |
| --- | --- | --- |
| `onebot` | 本机 NapCat / LagrangeQQ 的 HTTP 接口 | ✅ |
| `qqwindow` | 直接往**已登录的 QQ 窗口**粘贴 + 回车（不需要任何机器人权限，窗口要停在目标群） | ✅ |
| `qmsg` | Qmsg 酱云端转发到 QQ 群 | ❌ 只有文字 |
| `wecom` / `dingtalk` / `feishu` | 群机器人 webhook | 企业微信 ✅ |
| `pushplus` / `serverchan` | 微信推送 | ❌ |

一键切换并发送测试：

```bash
python 配置群通知.py wecom https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
python 配置群通知.py qqwindow
python 配置群通知.py telegram <bot_token> <chat_id> [http://127.0.0.1:7897]
```

## 多开配置

编辑 `emulators.json`：

```json
{
  "emulators": [
    {"name": "主号", "serial": "emulator-5554"},
    {"name": "二号", "serial": "emulator-5556"},
    {"name": "三号", "serial": "emulator-5558"},
    {"name": "四号", "serial": "emulator-5560"}
  ]
}
```

每台的状态、日志、截图分别落在 `.tmp/localalert/<名字>/`。

## 目录结构

```
eve_local_alert.py      单实例监控：截图、定位面板、切行、判色、判敌我、读计数、推通知
eve_multi.py            多开调度：每台一条线程，加载独立模块实例，写汇总心跳
本地预警控制台.pyw       图形界面：勾选模拟器、电源式启停、分列日志、计数高亮
eve_alert_main.py       打包入口（含 --child-monitor 子进程模式）
qq_notify.py            通知层：onebot / qqbot / qqwindow / qmsg / webhook / pushplus / serverchan
qq_window.py            往已登录的 QQ 窗口粘贴发送（剪贴板 + Ctrl+V + Enter）
配置群通知.py            切换通知通道并发测试消息
```

## 已知限制

- 只认 960×540 的游戏画面（雷电默认分辨率），其他分辨率需要改 `PANEL` 一类常量
- 颜色/图标判定依赖游戏 UI，「本地」面板样式大改时需要重新标定坐标
- `qqwindow` 通道发送时会抢占前台约 1 秒，且只发到当前打开的会话
- 官方 QQ 机器人接口没有群「主动消息」权限（返回 `40034105`），所以群里发消息只能靠 `onebot`、`qmsg` 或 `qqwindow`

## 许可

MIT

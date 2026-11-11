#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ / IM 通知通道。

QQ 消息直接以聊天消息发送（不走邮箱）：

1. **OneBot 11 HTTP**（推荐，NapCat / Lagrange / go-cqhttp 都可以）
   - 本机启动 OneBot 实现并开启 HTTP 服务，例如 http://127.0.0.1:3000
   - 配置 target_id 为接收消息的 QQ 号；支持文字和截图
2. **官方 QQ 机器人**（推荐，本机 dsh-im-connect 里已经有配好的机器人）
   - 先从 https://bots.qq.com/app/getAppAccessToken 取 access_token，
     再 POST https://api.sgroup.qq.com/v2/users/{openid}/messages 发单聊消息
   - appId / ownerOpenId / appSecret 默认自动从
     C:/Users/John/.dsh/dsh-im-connect/channels.json 与 C:/Users/John/.dsh/.credentials.yaml 读取，
     所以配置里只要 channel: qqbot（自动模式）
   - 支持文字；图片走富媒体上传，失败时自动退化成纯文字
3. **Qmsg酱**（云端转发，适合没有常驻 QQ 机器人的情况）
   - 在 https://qmsg.zendee.cn/ 申请 key，配置 target_qq
   - 只支持文字，不支持截图
4. **PushPlus / Server酱**（微信推送，只要一个 token，手机微信里就能收到）
   - https://www.pushplus.plus/ 或 https://sct.ftqq.com/ 扫码拿 token
   - 只支持文字，不支持截图

配置文件默认是程序目录下的 qq_config.json，也可以设置环境变量
LDPLAYER_QQ_CONFIG 指向别的文件。示例见 qq_config.example.json。

命令行::

    python qq_notify.py --configure      交互式写入配置
    python qq_notify.py --test "测试"    发一条测试消息
    python qq_notify.py --send-image screen.png --text "截图"
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


class QQNotifyError(RuntimeError):
    """QQ / IM 通知失败。"""


def default_config_path() -> Path:
    override = os.getenv("LDPLAYER_QQ_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().with_name("qq_config.json")


@dataclass
class OneBotConfig:
    url: str = "http://127.0.0.1:3000"
    access_token: str = ""
    target_type: str = "private"   # private / group
    target_id: int = 0
    timeout: float = 15.0


@dataclass
class QQBotConfig:
    """官方 QQ 机器人（QQ 开放平台）单聊推送。"""

    auto: bool = True              # True 时从 dsh-im-connect 的配置里自动找 appId/openid/secret
    app_id: str = ""
    owner_openid: str = ""
    group_openid: str = ""         # 填了就发群（QQ 群号的 openid，不是群号本身）
    app_secret: str = ""
    secret_ref: str = ""
    api_base: str = "https://api.sgroup.qq.com"
    token_url: str = "https://bots.qq.com/app/getAppAccessToken"
    timeout: float = 15.0


@dataclass
class QmsgConfig:
    key: str = ""
    target_qq: int = 0
    # 目标 QQ 群号（填了就发群，前提是在 Qmsg 控制台把机器人拉进群并完成绑定）
    target_group: int = 0
    api: str = "https://qmsg.zendee.cn/v3/send"
    timeout: float = 15.0


@dataclass
class PushPlusConfig:
    token: str = ""
    topic: str = ""
    api: str = "https://www.pushplus.plus/send"
    timeout: float = 15.0


@dataclass
class ServerChanConfig:
    sendkey: str = ""
    api: str = "https://sctapi.ftqq.com"
    timeout: float = 15.0


@dataclass
class WebhookConfig:
    """企业微信 / 钉钉 / 飞书群机器人，或 Telegram 机器人。"""

    kind: str = "wecom"          # wecom / dingtalk / feishu / telegram
    url: str = ""                # 群机器人 webhook 地址
    token: str = ""              # Telegram bot token
    chat_id: str = ""            # Telegram 会话 / 群 id
    proxy: str = ""              # 走代理时填，如 http://127.0.0.1:7897
    timeout: float = 15.0


@dataclass
class QQConfig:
    enabled: bool = False
    at_all: bool = False          # 发群消息时带 @全体成员
    channel: str = "onebot"       # qqbot / onebot / qmsg / pushplus / serverchan
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    qqbot: QQBotConfig = field(default_factory=QQBotConfig)
    qmsg: QmsgConfig = field(default_factory=QmsgConfig)
    pushplus: PushPlusConfig = field(default_factory=PushPlusConfig)
    serverchan: ServerChanConfig = field(default_factory=ServerChanConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QQConfig":
        config = cls(
            enabled=bool(data.get("enabled", False)),
            channel=str(data.get("channel", "onebot")).lower(),
        )
        if isinstance(data.get("onebot"), dict):
            config.onebot = OneBotConfig(
                **{key: value for key, value in data["onebot"].items() if key in OneBotConfig.__dataclass_fields__}
            )
        if isinstance(data.get("qqbot"), dict):
            config.qqbot = QQBotConfig(
                **{key: value for key, value in data["qqbot"].items() if key in QQBotConfig.__dataclass_fields__}
            )
        if isinstance(data.get("qmsg"), dict):
            config.qmsg = QmsgConfig(
                **{key: value for key, value in data["qmsg"].items() if key in QmsgConfig.__dataclass_fields__}
            )
        if isinstance(data.get("pushplus"), dict):
            config.pushplus = PushPlusConfig(
                **{key: value for key, value in data["pushplus"].items() if key in PushPlusConfig.__dataclass_fields__}
            )
        if isinstance(data.get("webhook"), dict):
            config.webhook = WebhookConfig(
                **{key: value for key, value in data["webhook"].items() if key in WebhookConfig.__dataclass_fields__}
            )
        if isinstance(data.get("serverchan"), dict):
            config.serverchan = ServerChanConfig(
                **{key: value for key, value in data["serverchan"].items() if key in ServerChanConfig.__dataclass_fields__}
            )
        # 允许把 token / sendkey 写在顶层，配置更直观
        for key in ("token", "pushplus_token"):
            if data.get(key) and not config.pushplus.token:
                config.pushplus.token = str(data[key])
        for key in ("sendkey", "serverchan_key"):
            if data.get(key) and not config.serverchan.sendkey:
                config.serverchan.sendkey = str(data[key])
        # 允许把 target_qq 写在顶层，配置更直观
        target = data.get("target_qq")
        if target not in (None, "") and not config.onebot.target_id:
            config.onebot.target_id = int(target)
        if target not in (None, "") and not config.qmsg.target_qq:
            config.qmsg.target_qq = int(target)
        # 目标群号也可以写在顶层 target_group
        group = data.get("target_group")
        if group not in (None, "") and not config.qmsg.target_group:
            config.qmsg.target_group = int(group)
        return config

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_config(path: str | Path | None = None) -> QQConfig:
    target = Path(path).expanduser() if path else default_config_path()
    if not target.exists():
        raise QQNotifyError(
            f"找不到 QQ 通知配置：{target}\n"
            "请先运行：python qq_notify.py --configure，"
            "或复制 qq_config.example.json 并填写。"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QQNotifyError(f"读取 QQ 配置失败：{target}：{exc}") from exc
    if not isinstance(payload, dict):
        raise QQNotifyError("QQ 配置文件必须是 JSON 对象")
    return QQConfig.from_dict(payload)


def save_config(config: QQConfig, path: str | Path | None = None) -> Path:
    target = Path(path).expanduser() if path else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


_PROXY_OVERRIDE = ""


def _proxy_url() -> str:
    """Telegram 在国内需要代理：优先 EVE_ALERT_PROXY，其次 HTTPS_PROXY / ALL_PROXY。"""
    if _PROXY_OVERRIDE:
        return _PROXY_OVERRIDE
    for key in ("EVE_ALERT_PROXY", "TELEGRAM_PROXY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(key, "").strip()
        if value:
            if "://" not in value:
                value = "http://" + value
            return value
    return ""


def _open(request, timeout: float):
    proxy = _proxy_url()
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def _http_json(
    url: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 15.0,
    form: bool = False,
) -> dict[str, Any]:
    if form:
        data = urllib.parse.urlencode(payload or {}).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
    else:
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/json; charset=utf-8")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _open(request, timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise QQNotifyError(f"HTTP {exc.code}：{detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise QQNotifyError(f"连接通知服务失败：{exc.reason}") from exc
    except TimeoutError as exc:
        raise QQNotifyError("通知服务响应超时") from exc
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise QQNotifyError(f"通知服务返回的不是 JSON：{body[:300]!r}") from exc
    if not isinstance(result, dict):
        raise QQNotifyError(f"通知服务返回格式异常：{body[:300]!r}")
    return result


def _http_multipart(url, fields, files, *, timeout=30.0):
    """最简 multipart/form-data，只为 Telegram sendPhoto 用。"""
    crlf = chr(13) + chr(10)
    boundary = "----EveAlert" + uuid.uuid4().hex
    chunks = []
    for key, value in fields.items():
        chunks.append(("--" + boundary + crlf
                       + 'Content-Disposition: form-data; name="' + key + '"' + crlf + crlf
                       + str(value) + crlf).encode("utf-8"))
    for key, (filename, data, ctype) in files.items():
        head = ("--" + boundary + crlf
                + 'Content-Disposition: form-data; name="' + key + '"; filename="' + filename + '"' + crlf
                + "Content-Type: " + ctype + crlf + crlf)
        chunks.append(head.encode("utf-8") + data + crlf.encode("utf-8"))
    chunks.append(("--" + boundary + "--" + crlf).encode("utf-8"))
    request = urllib.request.Request(url, data=b"".join(chunks), method="POST")
    request.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    try:
        with _open(request, timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise QQNotifyError("HTTP " + str(exc.code) + "：" + detail[:500]) from exc
    except urllib.error.URLError as exc:
        raise QQNotifyError("连接通知服务失败：" + str(exc.reason)) from exc
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise QQNotifyError("通知服务返回的不是 JSON：" + repr(body[:300])) from exc
    return result if isinstance(result, dict) else {}


class QQNotifier:
    """按配置把文字 / 截图发到 QQ。"""

    def __init__(self, config: QQConfig):
        self.config = config

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> "QQNotifier":
        return cls(load_config(path))

    def _ensure_enabled(self) -> None:
        if not self.config.enabled:
            raise QQNotifyError("QQ 通知未启用，请在 qq_config.json 中设置 enabled: true")

    def send_text(self, text: str) -> None:
        self._ensure_enabled()
        channel = self.config.channel
        if channel in ("qqbot", "qq-bot", "qq_bot", "official"):
            self._send_qqbot_text(text)
        elif channel == "onebot":
            self._send_onebot_text(text)
        elif channel == "qmsg":
            self._send_qmsg_text(text)
        elif channel == "pushplus":
            self._send_pushplus_text(text)
        elif channel in ("serverchan", "server"):
            self._send_serverchan_text(text)
        elif channel in ("webhook", "wecom", "dingtalk", "feishu", "telegram"):
            self._send_webhook_text(text)
        elif channel in ("qqwindow", "qq-window", "window"):
            self._send_window_text(text)
        else:
            raise QQNotifyError(
                f"未知的通知通道：{channel}；可用 qqbot / onebot / qmsg / pushplus / serverchan"
            )

    def send_image(self, image_path: str | Path, caption: str = "") -> None:
        self._ensure_enabled()
        path = Path(image_path)
        channel = self.config.channel
        if channel in ("qqbot", "qq-bot", "qq_bot", "official"):
            if not path.exists():
                raise QQNotifyError(f"图片不存在：{path}")
            self._send_qqbot_image(path, caption)
            return
        if channel in ("qqwindow", "qq-window", "window"):
            if not path.exists():
                raise QQNotifyError(f"图片不存在：{path}")
            self._send_window_image(path, caption)
            return
        if channel in ("webhook", "wecom", "dingtalk", "feishu", "telegram"):
            if path.exists():
                self._send_webhook_image(path, caption)
            else:
                self.send_text(caption or "（截图丢了）")
            return
        if channel != "onebot":
            # 微信 / qmsg 通道发不了图，改成带截图文案的文字消息，别让报警丢掉
            text = caption or ""
            if path.exists():
                text = f"{text}\n（截图已存本机：{path}）".strip()
            self.send_text(text)
            return
        if not path.exists():
            raise QQNotifyError(f"图片不存在：{path}")
        self._send_onebot_image(path, caption)

    # ----- 官方 QQ 机器人 -------------------------------------------------
    _TOKEN_CACHE: dict[str, tuple[str, float]] = {}

    @staticmethod
    def _im_connect_paths() -> tuple[Path, Path]:
        home = Path(os.getenv("DSH_HOME") or (Path.home() / ".dsh"))
        return home / "dsh-im-connect" / "channels.json", home / ".credentials.yaml"

    @staticmethod
    def _secret_of(path: Path, ref: str) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise QQNotifyError(f"读取凭据文件失败：{path}：{exc}") from exc
        match = re.search(rf"^\s*{re.escape(ref)}:\s*(\S+)\s*$", text, re.MULTILINE)
        return match.group(1) if match else ""

    def _qqbot_resolve(self) -> tuple[str, str, str]:
        """返回 (app_id, owner_openid, app_secret)；配置里留空的部分自动从 dsh-im-connect 读。"""
        cfg = self.config.qqbot
        app_id = str(cfg.app_id).strip()
        openid = str(cfg.owner_openid).strip()
        secret = str(cfg.app_secret).strip()
        if cfg.auto and not (app_id and openid and secret):
            channels_path, creds_path = self._im_connect_paths()
            try:
                data = json.loads(channels_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise QQNotifyError(
                    f"自动读取 dsh-im-connect 配置失败：{channels_path}：{exc}"
                ) from exc
            for channel in data.get("channels", {}).values():
                if channel.get("platform") != "qq":
                    continue
                inner = channel.get("config", {}) or {}
                app_id = app_id or str(inner.get("appId") or "").strip()
                openid = openid or str(inner.get("ownerOpenId") or "").strip()
                if not secret:
                    ref = str(inner.get("appSecretRef") or cfg.secret_ref).strip()
                    secret = self._secret_of(creds_path, ref) if ref else ""
                break
        need = [("appId", app_id), ("appSecret", secret)]
        if not str(cfg.group_openid or "").strip():
            need.insert(1, ("ownerOpenId", openid))
        missing = [name for name, value in need if not value]
        if missing:
            raise QQNotifyError(
                "官方 QQ 机器人配置不全，缺：" + "、".join(missing)
                + "。请在 qq_config.json 的 qqbot 段里写明，或确认 dsh-im-connect 里有 qq 通道。"
            )
        return app_id, openid, secret

    def _qqbot_token(self, app_id: str, secret: str) -> str:
        now = time.time()
        cached = QQNotifier._TOKEN_CACHE.get(app_id)
        if cached and cached[1] - 60 > now:
            return cached[0]
        result = _http_json(
            self.config.qqbot.token_url,
            {"appId": app_id, "clientSecret": secret},
            timeout=self.config.qqbot.timeout,
        )
        token = str(result.get("access_token") or "")
        if not token:
            raise QQNotifyError(f"取 QQ access_token 失败：{result}")
        try:
            ttl = float(result.get("expires_in") or 300)
        except (TypeError, ValueError):
            ttl = 300.0
        QQNotifier._TOKEN_CACHE[app_id] = (token, now + ttl)
        return token

    def _qqbot_post(self, app_id: str, openid: str, secret: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        token = self._qqbot_token(app_id, secret)
        url = self.config.qqbot.api_base.rstrip("/") + path
        headers = {"Authorization": f"QQBot {token}", "X-Union-Appid": app_id}
        try:
            return _http_json(url, payload, headers=headers, timeout=self.config.qqbot.timeout)
        except QQNotifyError:
            # token 可能刚好过期，清掉缓存让下次重取
            QQNotifier._TOKEN_CACHE.pop(app_id, None)
            raise

    def _qqbot_target(self, openid: str) -> str:
        """发到群还是私聊：配置了 group_openid 就发群。"""
        grp = str(self.config.qqbot.group_openid or "").strip()
        return f"/v2/groups/{grp}" if grp else f"/v2/users/{openid}"

    def _send_qqbot_text(self, text: str) -> None:
        app_id, openid, secret = self._qqbot_resolve()
        body = str(text)
        if self.config.at_all:
            body = "<@all> " + body
        payload = {"content": body[:1800], "msg_type": 0, "msg_seq": 1}
        self._qqbot_post(app_id, openid, secret, f"{self._qqbot_target(openid)}/messages", payload)

    def _send_qqbot_image(self, path: Path, caption: str) -> None:
        app_id, openid, secret = self._qqbot_resolve()
        note = f"{caption}\n截图在本机：{path}".strip()
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            upload = self._qqbot_post(
                app_id,
                openid,
                secret,
                f"{self._qqbot_target(openid)}/files",
                {"file_type": 1, "file_data": encoded, "srv_send_msg": False},
            )
        except QQNotifyError as exc:
            self._send_qqbot_text(f"{note}\n（截图上传失败：{exc}）")
            return
        file_info = str(upload.get("file_info") or upload.get("file_uuid") or "")
        if not file_info:
            self._send_qqbot_text(f"{note}\n（截图上传没拿到 file_info）")
            return
        payload = {
            "content": str(caption or "")[:1800],
            "msg_type": 7,
            "media": {"file_info": file_info},
            "msg_seq": 1,
        }
        try:
            self._qqbot_post(app_id, openid, secret, f"{self._qqbot_target(openid)}/messages", payload)
        except QQNotifyError:
            self._send_qqbot_text(note)

    # ----- OneBot 11 ------------------------------------------------------
    def _onebot_endpoint(self) -> str:
        base = self.config.onebot.url.strip().rstrip("/")
        if not base:
            raise QQNotifyError("onebot.url 不能为空")
        action = "send_group_msg" if self.config.onebot.target_type == "group" else "send_private_msg"
        if base.endswith(action):
            return base
        return f"{base}/{action}"

    def _onebot_payload(self, message: list[dict[str, Any]]) -> dict[str, Any]:
        target = int(self.config.onebot.target_id)
        if target <= 0:
            raise QQNotifyError("onebot.target_id 必须是接收消息的 QQ 号（或群号）")
        if self.config.onebot.target_type == "group":
            return {"group_id": target, "message": message}
        return {"user_id": target, "message": message}

    def _onebot_post(self, payload: dict[str, Any]) -> None:
        headers = {}
        token = self.config.onebot.access_token.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        result = _http_json(
            self._onebot_endpoint(),
            payload,
            headers=headers,
            timeout=self.config.onebot.timeout,
        )
        status = str(result.get("status", "")).lower()
        retcode = result.get("retcode")
        if status and status not in ("ok", "async"):
            raise QQNotifyError(f"OneBot 发送失败：{result}")
        if retcode not in (0, None):
            raise QQNotifyError(f"OneBot 发送失败：{result}")

    def _onebot_message(self, text: str) -> list[dict[str, Any]]:
        """群消息带 @全体成员 时，先放一个 at=all 分段。"""
        message: list[dict[str, Any]] = []
        if self.config.at_all and self.config.onebot.target_type == "group":
            message.append({"type": "at", "data": {"qq": "all"}})
            message.append({"type": "text", "data": {"text": " "}})
        message.append({"type": "text", "data": {"text": str(text)}})
        return message

    def _send_onebot_text(self, text: str) -> None:
        self._onebot_post(self._onebot_payload(self._onebot_message(text)))

    def _send_onebot_image(self, path: Path, caption: str) -> None:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        message: list[dict[str, Any]] = []
        if self.config.at_all and self.config.onebot.target_type == "group":
            message += [
                {"type": "at", "data": {"qq": "all"}},
                {"type": "text", "data": {"text": " "}},
            ]
        message.append({"type": "image", "data": {"file": f"base64://{encoded}"}})
        if caption:
            message.append({"type": "text", "data": {"text": str(caption)}})
        self._onebot_post(self._onebot_payload(message))

    # ----- Qmsg 酱 --------------------------------------------------------
    def _send_qmsg_text(self, text: str) -> None:
        key = self.config.qmsg.key.strip()
        if not key:
            raise QQNotifyError("qmsg.key 不能为空")
        endpoint = self.config.qmsg.api.strip().rstrip("/") + "/" + key
        payload: dict[str, Any] = {"msg": str(text)}
        if self.config.qmsg.target_group:
            # Qmsg v3：带 group 就发到该群（需先在控制台完成群绑定）
            payload["group"] = int(self.config.qmsg.target_group)
        elif self.config.qmsg.target_qq:
            payload["qq"] = int(self.config.qmsg.target_qq)
        result = _http_json(endpoint, payload, timeout=self.config.qmsg.timeout, form=True)
        if not (result.get("success") is True or result.get("code") in (0, "0")):
            raise QQNotifyError(f"Qmsg 发送失败：{result}")

    # ----- PushPlus（微信）-------------------------------------------------
    def _send_pushplus_text(self, text: str) -> None:
        token = self.config.pushplus.token.strip()
        if not token:
            raise QQNotifyError("pushplus.token 不能为空（在 pushplus.plus 扫码获取）")
        title = (text or "EVE 本地预警").strip().splitlines()[0][:60] or "EVE 本地预警"
        payload: dict[str, Any] = {
            "token": token,
            "title": title,
            "content": str(text),
            "template": "txt",
        }
        topic = self.config.pushplus.topic.strip()
        if topic:
            payload["topic"] = topic
        result = _http_json(self.config.pushplus.api, payload, timeout=self.config.pushplus.timeout)
        if str(result.get("code")) != "200":
            raise QQNotifyError(f"PushPlus 发送失败：{result}")

    # ----- Server酱（微信）------------------------------------------------
    # ----- 直接操作已登录的 QQ 窗口（粘贴 + 回车）-------------------------
    def _send_window_text(self, text):
        import qq_window

        qq_window.send_text(text)

    def _send_window_image(self, path, caption=""):
        import qq_window

        qq_window.send_image(path, caption)

    # ----- 群机器人（企业微信 / 钉钉 / 飞书 / Telegram）-------------------
    def _webhook_kind(self):
        cfg = self.config.webhook
        kind = (cfg.kind or "").lower().strip()
        if kind not in ("wecom", "dingtalk", "feishu", "telegram"):
            channel = str(self.config.channel).lower()
            kind = channel if channel in ("wecom", "dingtalk", "feishu", "telegram") else "wecom"
        return cfg, kind

    def _telegram_target(self, cfg):
        token = (cfg.token or "").strip()
        chat = str(cfg.chat_id).strip()
        if not token or not chat:
            raise QQNotifyError("Telegram 通道需要 webhook.token 与 webhook.chat_id")
        return token, chat

    def _send_webhook_text(self, text):
        global _PROXY_OVERRIDE
        cfg, kind = self._webhook_kind()
        _PROXY_OVERRIDE = cfg.proxy.strip()
        if kind == "telegram":
            token, chat = self._telegram_target(cfg)
            _http_json(
                "https://api.telegram.org/bot" + token + "/sendMessage",
                {"chat_id": chat, "text": text, "disable_web_page_preview": True},
                timeout=cfg.timeout,
            )
            return
        url = cfg.url.strip()
        if not url:
            raise QQNotifyError(kind + " 通道需要 webhook.url（群机器人地址）")
        if kind == "feishu":
            payload = {"msg_type": "text", "content": {"text": text}}
        else:
            payload = {"msgtype": "text", "text": {"content": text}}
        _http_json(url, payload, timeout=cfg.timeout)

    def _send_webhook_image(self, path, caption=""):
        global _PROXY_OVERRIDE
        cfg, kind = self._webhook_kind()
        _PROXY_OVERRIDE = cfg.proxy.strip()
        data = path.read_bytes()
        if kind == "telegram":
            token, chat = self._telegram_target(cfg)
            fields = {"chat_id": chat}
            if caption:
                fields["caption"] = caption[:1000]
            _http_multipart(
                "https://api.telegram.org/bot" + token + "/sendPhoto",
                fields,
                {"photo": (path.name, data, "image/png")},
                timeout=max(cfg.timeout, 30.0),
            )
            return
        if kind == "wecom":
            if len(data) > 2 * 1024 * 1024:
                self._send_webhook_text(caption + "（截图超过 2MB，未发送）")
                return
            if caption:
                self._send_webhook_text(caption)
            _http_json(
                cfg.url.strip(),
                {"msgtype": "image",
                 "image": {"base64": base64.b64encode(data).decode("ascii"),
                           "md5": hashlib.md5(data).hexdigest()}},
                timeout=cfg.timeout,
            )
            return
        self._send_webhook_text(caption + "（该通道发不了图片，截图已存本机）")

    def _send_serverchan_text(self, text: str) -> None:
        sendkey = self.config.serverchan.sendkey.strip()
        if not sendkey:
            raise QQNotifyError("serverchan.sendkey 不能为空（在 sct.ftqq.com 获取）")
        title = (text or "EVE 本地预警").strip().splitlines()[0][:60] or "EVE 本地预警"
        endpoint = f"{self.config.serverchan.api.strip().rstrip('/')}/{sendkey}.send"
        result = _http_json(
            endpoint,
            {"title": title, "desp": str(text)},
            timeout=self.config.serverchan.timeout,
            form=True,
        )
        if result.get("code") not in (0, "0", None):
            raise QQNotifyError(f"Server酱 发送失败：{result}")

def send_to_qq(
    text: str = "",
    image_path: str | Path | None = None,
    *,
    config_path: str | Path | None = None,
) -> None:
    """一次性发送入口：加载配置，发文字和/或图片。"""
    notifier = QQNotifier.from_file(config_path)
    if image_path is not None:
        notifier.send_image(image_path, caption=text)
    elif text:
        notifier.send_text(text)


# ---------------------------------------------------------------------------
# 命令行配置 / 测试
# ---------------------------------------------------------------------------
def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def configure_interactive(path: str | Path | None = None) -> Path:
    target = Path(path).expanduser() if path else default_config_path()
    print("配置通知通道（直接回车使用括号中的默认值）")
    channel = _ask("通道 qqbot / onebot / qmsg / pushplus / serverchan", "qqbot").lower()
    config = QQConfig(enabled=True, channel=channel)
    if channel in ("qqbot", "qq-bot", "qq_bot", "official"):
        config.channel = "qqbot"
        print("官方 QQ 机器人：默认自动使用 dsh-im-connect 里的机器人配置（已装就什么都不用填）")
        config.qqbot.auto = _ask("自动读取 dsh-im-connect 配置 true / false", "true").lower() != "false"
        if not config.qqbot.auto:
            config.qqbot.app_id = _ask("机器人 appId", "")
            config.qqbot.owner_openid = _ask("接收者 ownerOpenId", "")
            config.qqbot.app_secret = _ask("机器人 appSecret", "")
    elif channel == "onebot":
        config.onebot.url = _ask("OneBot HTTP 地址", "http://127.0.0.1:3000")
        config.onebot.target_type = _ask("发送到 private / group", "private")
        config.onebot.target_id = int(_ask("接收 QQ 号（群号）", "0") or 0)
        config.onebot.access_token = _ask("Access Token（没有就留空）", "")
    elif channel == "qmsg":
        config.qmsg.key = _ask("Qmsg key", "")
        config.qmsg.target_group = int(_ask("发送到哪个 QQ 群（群号，留 0 则发私聊）", "0") or 0)
        if not config.qmsg.target_group:
            config.qmsg.target_qq = int(_ask("接收 QQ 号", "0") or 0)
    elif channel == "pushplus":
        config.pushplus.token = _ask("PushPlus token", "")
    elif channel in ("serverchan", "server"):
        config.serverchan.sendkey = _ask("Server酱 SendKey", "")
    else:
        raise QQNotifyError(
            f"未知通道：{channel}；可用 qqbot / onebot / qmsg / pushplus / serverchan"
        )
    saved = save_config(config, target)
    print(f"已写入：{saved}")
    return saved


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="发送通知（OneBot / Qmsg / PushPlus / Server酱）")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--configure", action="store_true", help="交互式配置")
    parser.add_argument("--test", metavar="TEXT", help="发送一条测试文字")
    parser.add_argument("--send-image", metavar="PATH", help="发送图片")
    parser.add_argument("--text", default="", help="随图片发送的说明")
    args = parser.parse_args(argv)

    try:
        if args.configure:
            configure_interactive(args.config)
            return 0
        if args.send_image:
            send_to_qq(args.text, args.send_image, config_path=args.config)
            print("已发送图片")
            return 0
        if args.test is not None:
            send_to_qq(args.test or "雷电模拟器通知测试", config_path=args.config)
            print("已发送测试消息")
            return 0
        parser.print_help()
        return 2
    except QQNotifyError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

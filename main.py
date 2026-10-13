"""GLM + Kimi + Codex + MiMo 用量悬浮小组件 + 番茄工作闹钟 — Acrylic 竖向状态轨贴靠屏幕左/右缘

Win11 Acrylic 真毛玻璃状态轨：GLM 深蓝几何晶体、Kimi 绿色星芒、Codex 洋红
终端符、MiMo 橙色气泡火花、番茄沙漏五个图标槽位的圆环分别表示三源额度、
Codex 周预算、MiMo 套餐剩余和当前番茄阶段剩余。拖到任意屏松手就近贴边
（左/右缘皆可，跨屏生效），悬停从停靠侧对缘展开详情面板，空闲时自动缩成
停靠侧 6px 水位式番茄进度条（满格齐顶、随剩余时间自上而下回落）。
"""

import ctypes
import ipaddress
import json
import math
import os
import socket
import sys
import threading
import time
import traceback
import tkinter as tk
import winreg
import winsound
from datetime import datetime, time as dtime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
from winotify import Notification

# pythonw 在无控制台环境（任务计划程序 / 开机自启）下 sys.stdout/stderr 为 None，
# 此时 print() 会抛 AttributeError 导致进程崩溃；从终端启动时又可能因终端关闭
# 使管道失效（BrokenPipeError）。统一以 Tee 同时写 dashboard.log 与原输出，
# 写入永不抛异常，保证任何启动方式下运行期日志（API 错误 / 异常 traceback）
# 都完整落盘，便于故障定位。
_LOG_PATH = Path(__file__).resolve().parent / "dashboard.log"


class _Tee:
    """把 write 扇出到多个流，单个流失效不影响其余、也不向上抛异常。"""

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass


_log_file = open(_LOG_PATH, "a", encoding="utf-8", buffering=1)  # 行缓冲
sys.stdout = _Tee(sys.stdout, _log_file)
sys.stderr = _Tee(sys.stderr, _log_file)

REFRESH_INTERVAL = 300      # 有会话活跃时的刷新间隔：5 分钟
IDLE_REFRESH_INTERVAL = 600  # 全部源空闲（无会话活动）时降频到 10 分钟
BACKOFF_BASE = 60           # 429 退避起步秒数，连续触发翻倍
BACKOFF_CAP = 900           # 退避上限 15 分钟
DEAD_AFTER = 3              # 连续多少次抓取无响应 → 摘掉该源图标（成功一次即恢复）
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"  # 项目本地独立配置（脱离 cc 的兜底）
# ZCode 的 API 配置（最高优先级，自动跟随其更新）：
#   cli/config.json  记录当前选中供应商 model.providerId
#   v2/config.json   存各供应商的 apiKey / baseURL（provider[<id>].options）
ZCODE_CLI_CONFIG_PATH = Path.home() / ".zcode" / "cli" / "config.json"
ZCODE_PROVIDERS_PATH = Path.home() / ".zcode" / "v2" / "config.json"
ICON_PATH = Path(__file__).resolve().parent / "tomato.ico"  # AUMID 应用图标
ICON_PNG = Path(__file__).resolve().parent / "tomato.png"   # toast 内联图标
UI_STATE_PATH = Path(__file__).resolve().parent / "ui_state.json"  # 停靠位置与侧别记忆
AUMID = "GlmDashboard"  # 应用模型 ID（系统通知来源标识）

# 开机自启：写入 HKCU Run 项，登录后用 pythonw 无窗口启动本脚本
AUTOSTART_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "GlmDashboard"

# 番茄钟作息（v1.16）：锚定工作日作息表——9:00-11:30 / 13:30-18:00 两个时段内
# 连续跑「45 分钟工作 + 15 分钟休息」，时段尾自然截断（上午/下午各留一段 30 分钟
# 收尾工作）；时段外不计时（待机/午休/下班/假日）。节假日与调休补班由
# chinese-calendar 判定（见 _is_workday），无需手动配置。
POMODORO_WORK_MIN = 45
POMODORO_REST_MIN = 15
WORK_SESSIONS = ((dtime(9, 0), dtime(11, 30)), (dtime(13, 30), dtime(18, 0)))

# ── Win32 常量与结构体 ────────────────────────────────────────────────
WS_EX_LAYERED = 0x80000
GWL_EXSTYLE = -20
ULW_ALPHA = 0x02
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class _BMPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_ulong),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", ctypes.c_ulong),
        ("biSizeImage", ctypes.c_ulong),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_ulong),
        ("biClrImportant", ctypes.c_ulong),
    ]


# ── Acrylic 毛玻璃（Win11 DWM）─────────────────────────────────────────
# 组合：SetWindowCompositionAttribute(ACCENT_ENABLE_ACRYLICBLURBEHIND) 提供
# 系统级模糊背景，DWMWA_WINDOW_CORNER_PREFERENCE 裁剪连续圆角；内容仍由
# UpdateLayeredWindow 逐像素绘制，叠在玻璃之上。老系统 API 失败时回退
# PIL 自绘卡片底（glass=False）。
_WCA_ACCENT_POLICY = 19
_ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
CORNER_RADIUS = 12  # 窗口圆角半径（SetWindowRgn 裁剪，与 PIL 描边半径一致）


class _ACCENT_POLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_ulong),  # ABGR
        ("AnimationId", ctypes.c_int),
    ]


class _WCA_DATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


def _set_acrylic(hwnd, r=28, g=28, b=30, a=110):
    """给窗口开启 Acrylic 模糊背景 + 暗色 tint，返回是否成功"""
    color = (a << 24) | (b << 16) | (g << 8) | r
    policy = _ACCENT_POLICY(_ACCENT_ENABLE_ACRYLICBLURBEHIND, 0, color, 0)
    data = _WCA_DATA(
        _WCA_ACCENT_POLICY,
        ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p),
        ctypes.sizeof(policy),
    )
    return bool(ctypes.windll.user32.SetWindowCompositionAttribute(
        ctypes.c_void_p(hwnd), ctypes.byref(data)))


def _unset_acrylic(hwnd):
    """关闭组合属性特效（ACCENT_DISABLED）。贴边隐藏时必须调用：
    Acrylic 模糊按窗口 region 生效而非按内容 alpha，隐藏态若保持开启，
    会在停靠侧对面的邻屏上浮现一块暗色玻璃矩形。"""
    policy = _ACCENT_POLICY(0, 0, 0, 0)  # ACCENT_DISABLED
    data = _WCA_DATA(
        _WCA_ACCENT_POLICY,
        ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p),
        ctypes.sizeof(policy),
    )
    return bool(ctypes.windll.user32.SetWindowCompositionAttribute(
        ctypes.c_void_p(hwnd), ctypes.byref(data)))


def _set_round_corners(hwnd, w, h, radius=CORNER_RADIUS):
    """用 SetWindowRgn 裁剪圆角（只作用于分层位图内容与命中测试）。
    注意：region 裁不到 SetWindowCompositionAttribute 绘制的 Acrylic 模糊底
    （模糊底按窗口矩形方形绘制），必须配合 _set_dwm_round 才能得到圆角玻璃。"""
    rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1,
                                                 radius * 2, radius * 2)
    if not rgn:
        return False
    ok = ctypes.windll.user32.SetWindowRgn(ctypes.c_void_p(hwnd), rgn, True)
    if not ok:
        ctypes.windll.gdi32.DeleteObject(rgn)
    return bool(ok)


def _clear_window_region(hwnd):
    """清除硬像素 region，让分层位图自身的 alpha 边缘负责抗锯齿。"""
    return bool(ctypes.windll.user32.SetWindowRgn(ctypes.c_void_p(hwnd), None, True))


def _set_dwm_round(hwnd, pref=2):
    """DWMWA_WINDOW_CORNER_PREFERENCE：让 DWM 把 Acrylic 模糊底一并裁成圆角
    （只有它管得到组合属性绘制的玻璃形状）。pref: 2=DWMWCP_ROUND，
    1=DWMWCP_DONOTROUND。注意此属性会让 DWM 按窗口矩形描一圈边框——
    隐藏态窗口身体伸在邻屏上，必须 DONOTROUND，否则邻屏浮出整卡轮廓线"""
    return bool(ctypes.windll.dwmapi.DwmSetWindowAttribute(
        ctypes.c_void_p(hwnd), 33, ctypes.byref(ctypes.c_int(pref)), 4))


# ── 配置 ──────────────────────────────────────────────────────────────
def _read_zcode_config():
    """读取 ZCode 当前生效的 API 配置，返回 (base_url, token)。

    每次调用都现读 ZCode 的两个配置文件，因此 ZCode 里换 key / 换供应商后
    仪表盘无需任何手动同步，最迟一个刷新周期（5 分钟）自动生效。

    活动供应商取不到可用 key 时（如切换到 OAuth 类套餐，token 存在
    ZCode 加密凭据库中读不到），退而取配置里任一已启用且带 key 的供应商
    保持仪表盘可用；再不行返回 ("", "") 交由后续来源兜底。
    """
    try:
        with open(ZCODE_PROVIDERS_PATH, encoding="utf-8") as f:
            providers = json.load(f).get("provider", {})
    except (json.JSONDecodeError, OSError):
        return "", ""

    try:
        with open(ZCODE_CLI_CONFIG_PATH, encoding="utf-8") as f:
            active_id = json.load(f).get("model", {}).get("providerId", "")
    except (json.JSONDecodeError, OSError):
        active_id = ""

    def _usable(pid):
        options = (providers.get(pid) or {}).get("options", {})
        return options.get("baseURL", ""), options.get("apiKey", "")

    base_url, token = _usable(active_id)
    if base_url and token:
        return base_url, token

    for pid, spec in providers.items():
        if pid == active_id or not spec.get("enabled"):
            continue
        base_url, token = _usable(pid)
        if base_url and token:
            print(f"ZCode 活动供应商 {active_id or '(未指定)'} 无可用 API key，改用 {pid}")
            return base_url, token
    return "", ""


def read_raw_config(skip_local_config=False):
    """读取原始 API 配置（未裁剪 base_url），优先级：
    ZCode 当前配置（自动跟随更新） > 项目 config.json > 环境变量 >
    ~/.claude/settings.json（兜底，兼容 cc）

    skip_local_config=True 时跳过项目 config.json，仅从其余来源读取——
    供 setup_config.py 同步外部最新配置，避免「读出旧 config.json 再写回」
    的自我循环。"""
    # 0. ZCode 当前生效的 API（最高优先级，随其配置文件实时更新）
    base_url, token = _read_zcode_config()

    # 1. 项目本地 config.json（ZCode 不可用时的独立兜底）
    if (not base_url or not token) and not skip_local_config and CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            base_url = base_url or cfg.get("base_url", "")
            token = token or cfg.get("token", "")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"config.json 读取失败: {exc}")

    # 2. 环境变量
    if not base_url or not token:
        base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL", "")
        token = token or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")

    # 3. ~/.claude/settings.json（兜底）
    if (not base_url or not token) and SETTINGS_PATH.exists():
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            env = json.load(f).get("env", {})
        base_url = base_url or env.get("ANTHROPIC_BASE_URL", "")
        token = token or env.get("ANTHROPIC_AUTH_TOKEN", "")

    return base_url, token


def load_config():
    """读取 API 配置，按 cc cli 方式从 base_url 提取 base_domain（scheme://host），
    返回 (base_domain, token) 或 (None, None)"""
    base_url, token = read_raw_config()
    if not base_url or not token:
        return None, None
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return None, None
    return f"{parsed.scheme}://{parsed.netloc}", token


# ── API ───────────────────────────────────────────────────────────────
# 用量接口支持的平台域名，与 cc cli 的 glm-plan-usage 插件（query-usage.mjs）保持一致：
# api.z.ai（国际）/ open.bigmodel.cn / dev.bigmodel.cn（智谱）
SUPPORTED_USAGE_HOSTS = ("api.z.ai", "open.bigmodel.cn", "dev.bigmodel.cn")


class _NoRedirect(HTTPRedirectHandler):
    """拒绝跟随 HTTP 重定向：用量接口固定 200 JSON，任何 3xx 都按异常处理"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


def _validated_usage_url(base_domain):
    """SSRF 防护：仅允许 https + 用量接口白名单域名；DNS 解析后逐 IP 阻断
    私网 / 环回 / 链路本地 / 保留地址。校验失败返回 None，不发请求。"""
    parsed = urlparse(base_domain)
    if parsed.scheme != "https" or parsed.hostname not in SUPPORTED_USAGE_HOSTS:
        return None
    try:
        infos = socket.getaddrinfo(
            parsed.hostname, 443, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror:
        return None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return None
    return f"https://{parsed.hostname}/api/monitor/usage/quota/limit"


def fetch_usage():
    """调用已配置 API 获取短期与周 Token 用量百分比，返回 dict 或 None。

    调用方式（域名解析 / 请求头 / 平台识别）对齐 cc cli 的 query-usage.mjs：
    从 ANTHROPIC_BASE_URL 取 scheme://host 作为 base_domain，校验为已知 GLM 平台后，
    请求 {base_domain}/api/monitor/usage/quota/limit。
    """
    base_domain, token = load_config()
    if not base_domain:
        return None

    host = urlparse(base_domain).hostname or ""
    if host not in SUPPORTED_USAGE_HOSTS:
        print(f"不支持的用量接口域名: {host}（仅支持 z.ai / open.bigmodel.cn / dev.bigmodel.cn）")
        return None

    url = _validated_usage_url(base_domain)
    if not url:
        print(f"用量接口 URL 校验失败: {host}（域名白名单 / DNS 解析未通过）")
        return None

    req = Request(url, headers={
        "Authorization": token,
        "Accept-Language": "en-US,en",
        "Content-Type": "application/json",
    })

    try:
        with _OPENER.open(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            _gate_reset("glm")
            data = body.get("data") or body
            if not data.get("limits"):
                # HTTP 200 但业务层失败（如 key 过期返回
                # {"code":1000,"msg":"Authentication Failed","success":false}）：
                # 不抛异常、走静默 None，但必须留痕，否则排查时日志一片空白
                print(f"GLM 用量接口无额度数据: {body.get('msg') or str(body)[:150]}")
                return None
            token_limits = [
                item for item in data.get("limits", [])
                if item.get("type") == "TOKENS_LIMIT"
            ]
            if token_limits:
                # unit=3 是 5 小时短期窗口，unit=6 是周窗口。不要用重置时间推断：
                # 短期窗口未启用时接口可能省略 nextResetTime。
                short_term_limit = next(
                    (item for item in token_limits if item.get("unit") == 3), None
                )
                weekly_limit = next(
                    (item for item in token_limits if item.get("unit") == 6), None
                )
                if not short_term_limit and not weekly_limit:
                    return None
                return {
                    "short_term_percentage": (
                        float(short_term_limit.get("percentage", 0))
                        if short_term_limit else None
                    ),
                    # 短期窗口的重置点（epoch 秒），供黄色倒计时条使用。
                    "short_term_reset_ts": (
                        float(short_term_limit.get("nextResetTime") or 0) / 1000
                        if short_term_limit else 0
                    ),
                    "weekly_percentage": (
                        float(weekly_limit.get("percentage", 0))
                        if weekly_limit else None
                    ),
                }
    except (URLError, OSError, json.JSONDecodeError, KeyError) as exc:
        # OSError 兜住读响应阶段的裸 TimeoutError / socket 错误
        #（urlopen 只把连接阶段超时包成 URLError，读阶段不包装）
        _gate_report("glm", exc)
        print(f"API 错误: {exc}")

    return None


# ── Kimi 用量（本地 daemon）────────────────────────────────────────────
# Kimi Code CLI 常驻一个本地 HTTP 服务（kap-server）：端口动态写在
# ~/.kimi-code/server/instances/*.json（取心跳最新），鉴权 token 在
# ~/.kimi-code/server.token。用量端点返回 limit5h / limit7d / monthTotal，
# 每项含 usedRatio（已用比例 0~1）与 resetAt（ISO-8601 UTC）。
KIMI_HOME = Path.home() / ".kimi-code"
KIMI_INSTANCES_DIR = KIMI_HOME / "server" / "instances"
KIMI_SERVER_TOKEN = KIMI_HOME / "server.token"


def _kimi_server_targets():
    """枚举本地 Kimi daemon 的 (url, token)，按实例心跳新旧排序。"""
    try:
        token = KIMI_SERVER_TOKEN.read_text(encoding="utf-8").strip()
    except OSError:
        return []
    if not token:
        return []
    instances = []
    try:
        for f in KIMI_INSTANCES_DIR.glob("*.json"):
            try:
                lock = json.loads(f.read_text(encoding="utf-8"))
                instances.append(
                    (lock.get("heartbeat_at", 0), lock.get("host", "127.0.0.1"),
                     lock.get("port"))
                )
            except (json.JSONDecodeError, OSError):
                continue
    except OSError:
        return []
    targets = []
    for _, host, port in sorted(instances, reverse=True):
        if isinstance(port, int) and 0 < port < 65536:
            targets.append((f"http://{host}:{port}/api/v1/oauth/usage", token))
    return targets


def fetch_kimi_usage():
    """读取本地 Kimi daemon 的短期窗口用量，返回 dict 或 None。

    daemon 未在跑（Kimi CLI 没启动过 / 已退出）时所有实例都连不上，
    返回 None，界面按无数据处理（空轨道 + "--"），不伪造读数。
    """
    for url, token in _kimi_server_targets():
        req = Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept-Language": "en-US,en",
        })
        try:
            with _OPENER.open(req, timeout=3) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (URLError, OSError, json.JSONDecodeError) as exc:
            _gate_report("kimi", exc)
            continue
        data = body.get("data") or {}
        if data.get("kind") != "ok":
            continue
        _gate_reset("kimi")
        quota = data.get("quota") or {}
        usages = quota.get("usages") or {}
        limit5h = usages.get("limit5h") or {}
        used = limit5h.get("usedRatio")
        if used is None:
            continue
        reset_ts = 0.0
        reset_at = limit5h.get("resetAt")
        if reset_at:
            try:
                from datetime import timezone
                reset_ts = datetime.fromisoformat(
                    reset_at.replace("Z", "+00:00")
                ).astimezone(timezone.utc).timestamp()
            except ValueError:
                reset_ts = 0.0
        monthly_used = (usages.get("monthTotal") or {}).get("usedRatio")
        return {
            "short_term_percentage": float(used) * 100,
            "short_term_reset_ts": reset_ts,
            "monthly_percentage": (
                float(monthly_used) * 100 if monthly_used is not None else None
            ),
        }
    return None


# ── Codex 用量（第三方中转的 GPT 订阅）────────────────────────────────
# Codex CLI 经 UAPI 网关中转：base_url 在 ~/.codex/config.toml 的
# [model_providers.<model_provider>].base_url（供应商名跟顶层 model_provider 走，
# 不写死 "OpenAI"），key 在 ~/.codex/auth.json 的 OPENAI_API_KEY（Bearer 鉴权——
# 与 GLM 的裸 token 头不同，勿复用）。GET {base}/v1/usage 返回
# subscription{weekly/monthly_limit_usd, weekly_usage_usd, weekly_window_start,
# daily_limit_usd, expires_at} / mode / unit=USD / usage.today{cost}。
# 额度是 USD 计价、没有 5h 窗口：环语义取周额度剩余（对齐全卡"剩余递减"），
# daily_limit=0 且 mode=unrestricted 表示无日限额——今日已用只进 hover 文本，
# 没有分母的比例一律不画（伪造读数）。
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
CODEX_USAGE_HOSTS = ("uapi.ccwu.cc",)   # 中转站用量接口白名单（沿用 GLM 的 SSRF 策略）
CODEX_USAGE_MAX_BYTES = 65536           # 响应体上限：正常几十 KB，防异常流撑爆内存


def _discover_codex_profiles():
    """发现本机 Codex 账号目录：默认 CODEX_HOME 为 ("") 主账号，
    每个带 auth.json 的 ~/.codex-<slug> 目录追加一个 (slug, 目录) 账号
    （对齐 Codenotch 的多账号约定：默认在前，其余按字母序，位置固定不互换）。"""
    profiles = [("", CODEX_HOME)]
    seen = {CODEX_HOME.resolve()} if CODEX_HOME.exists() else set()
    try:
        candidates = sorted(Path.home().glob(".codex-*"))
    except OSError:
        return profiles
    for d in candidates:
        try:
            if not d.is_dir() or not (d / "auth.json").exists():
                continue
            if d.resolve() in seen:
                continue
        except OSError:
            continue
        profiles.append((d.name[len(".codex-"):], d))
    return profiles


CODEX_PROFILES = _discover_codex_profiles()

# ── 429 退避门控 ─────────────────────────────────────────────────────
# 每个源独立的退避状态：连续 429 时 60s 起步指数翻倍、封顶 15 分钟，
# 退避截止前该源的刷新直接跳过，不再空发注定被拒的请求。
_fetch_gates = {}
_gates_lock = threading.Lock()


def _gate_allows(source):
    with _gates_lock:
        return time.time() >= _fetch_gates.get(source, {}).get("until", 0)


def _gate_report(source, exc):
    """抓取返回后登记结果：HTTPError 429 升一级退避，其他结果保持不变。
    成功响应由调用方显式调用 _gate_reset 清零。"""
    if not isinstance(exc, HTTPError) or exc.code != 429:
        return
    with _gates_lock:
        gate = _fetch_gates.setdefault(source, {"until": 0, "delay": BACKOFF_BASE})
        gate["until"] = time.time() + gate["delay"]
        gate["delay"] = min(gate["delay"] * 2, BACKOFF_CAP)
    print(f"{source} 触发 429，退避 {gate['delay']} 秒")


def _gate_reset(source):
    with _gates_lock:
        _fetch_gates.pop(source, None)


def _codex_usage_url(home=CODEX_HOME):
    """从指定 Codex 账号目录的 CLI 配置解析 (usage_url, api_key)；解析不出返回 (None, None)。

    URL 安全校验沿用 GLM 源策略：https + 域名白名单 + DNS 解析后逐 IP 阻断
    私网/环回/链路本地/保留地址；_OPENER 已禁重定向，Bearer key 不会被带往
    其他主机。"""
    try:
        key = json.loads((home / "auth.json").read_text(encoding="utf-8")).get(
            "OPENAI_API_KEY", "")
    except (json.JSONDecodeError, OSError):
        return None, None
    key = (key or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    if not key:
        return None, None

    import tomllib
    try:
        with open(home / "config.toml", "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None, None
    provider = (cfg.get("model_providers") or {}).get(cfg.get("model_provider", ""))
    host = urlparse((provider or {}).get("base_url", "")).hostname or ""
    if host not in CODEX_USAGE_HOSTS:
        if host:
            print(f"Codex 用量域名不在白名单，跳过: {host}")
        return None, None

    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return None, None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return None, None
    return f"https://{host}/v1/usage", key


def _codex_weekly_reset_ts(window_start):
    """由周窗口起点推算重置时刻（epoch 秒）；推不出或明显不合理返回 0。

    服务端只给 weekly_window_start（本周一 00:00 +08:00），重置点 = 起点 + 7 天
    属客户端推算：起点在未来（乱数据）、或 now 已越过推算点而服务端窗口还没
    滚动（数据陈旧），都不推算——返回 0 让上层显示等待而非编一个倒计时。
    aware 时间全程保留原始偏移、按 UTC 比较，不做本地时区假设。"""
    if not window_start:
        return 0.0
    try:
        start = datetime.fromisoformat(window_start)
    except (TypeError, ValueError):
        return 0.0
    start_ts = start.timestamp()  # naive 视作 UTC，避免隐式本地时区假设
    reset_ts = start_ts + 7 * 86400
    now = time.time()
    if start_ts > now or now >= reset_ts:
        return 0.0
    return reset_ts


def fetch_codex_usage(home=CODEX_HOME, source="codex"):
    """读取指定 Codex 账号的中转订阅周/月 USD 额度，返回 dict 或 None。

    未配置 / 域名不认识 / key 失效 / 网络失败统一返回 None，界面按无数据
    处理（虚线空环 + "--"），沿用既有的"不伪造读数"约定。"""
    url, key = _codex_usage_url(home)
    if not url:
        return None
    req = Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept-Language": "en-US,en",
    })
    try:
        with _OPENER.open(req, timeout=10) as resp:
            raw = resp.read(CODEX_USAGE_MAX_BYTES + 1)
        if len(raw) > CODEX_USAGE_MAX_BYTES:
            print("Codex 用量响应超过大小上限，已丢弃")
            return None
        _gate_reset(source)
        body = json.loads(raw.decode("utf-8"))
        sub = body.get("subscription") or {}
        weekly_limit = sub.get("weekly_limit_usd") or 0
        weekly_used = sub.get("weekly_usage_usd")
        if weekly_limit <= 0 or weekly_used is None:
            return None
        monthly_limit = sub.get("monthly_limit_usd") or 0
        monthly_used = sub.get("monthly_usage_usd")
        today_cost = (body.get("usage") or {}).get("today") or {}
        return {
            "weekly_remaining": max(0.0, min(
                100.0, (weekly_limit - weekly_used) / weekly_limit * 100)),
            "weekly_reset_ts": _codex_weekly_reset_ts(
                sub.get("weekly_window_start")),
            "weekly_used_usd": float(weekly_used),
            "weekly_limit_usd": float(weekly_limit),
            "monthly_remaining": (
                max(0.0, min(100.0, (monthly_limit - monthly_used)
                             / monthly_limit * 100))
                if monthly_limit > 0 and monthly_used is not None else None),
            "today_cost_usd": (
                float(today_cost["cost"])
                if isinstance(today_cost, dict)
                and today_cost.get("cost") is not None else None),
            "unrestricted": (body.get("mode") == "unrestricted"
                             and not sub.get("daily_limit_usd")),
        }
    except (URLError, OSError, json.JSONDecodeError, ValueError,
            TypeError) as exc:
        _gate_report(source, exc)
        print(f"Codex 用量错误: {exc}")
        return None


# ── MiMo 用量（config.json 可配置端点）──────────────────────────────
# MiMo 没有「装好即可读」的额度接口：Token Plan 网关（mimocode.jsonc 里的
# tp- key）只有 /v1/models 这类模型接口，账号级 /user/usage 又要小米
# 账号会话 cookie（MiMo Desktop 运行时该库被独占锁住，读不到）。因此这一
# 源照抄 GLM 的「配置驱动」思路，端点与凭证由使用者写进 config.json：
#
#   "mimo": {
#     "url":   "https://…/usage",     # 必填，https（见 _validated_mimo_url）
#     "token": "…",                   # 选填，凭证原文
#     "auth":  "bearer",              # bearer(默认) / header / cookie
#     "header": "Authorization"       # 选填，自定义头名，auth=header 时常用
#   }
#
# 响应里按字段名浅层找读数（≤4 层，命中即停）：remaining* 视为剩余、
# percent / usedRatio / used* 视为已用；数值 ≤1 按比例 ×100，>1 按百分数。
# 重置点取 reset* / nextResetTime / expire*，兼容 epoch 秒 / 毫秒 / ISO8601。
# 未配置、域名不通、鉴权失败统一返回 None——界面走虚线空环 + "--"，
# 与 Kimi daemon 不在跑时同一语义，绝不伪造读数。
MIMO_MAX_BYTES = 65536   # 响应体上限（沿用 Codex 的 64KB 约束）


def _mimo_config():
    """读取 config.json 的 mimo 段，返回 (url, headers)；未配置返回 (None, None)。

    每次刷新都现读文件，改完 config.json 最迟一个刷新周期生效，与 ZCode
    配置「自动跟随」同一习惯。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            mimo = json.load(f).get("mimo") or {}
    except (OSError, json.JSONDecodeError):
        return None, None
    url = str(mimo.get("url") or "").strip()
    token = str(mimo.get("token") or "").strip()
    if not url:
        return None, None
    headers = {"Accept": "application/json", "Accept-Language": "en-US,en"}
    if token:
        mode = str(mimo.get("auth") or "bearer").lower()
        name = str(mimo.get("header") or "Authorization")
        if mode == "header":          # 自定义头直接放原文（如 X-Api-Key: sk-…）
            headers[name] = token
        elif mode == "cookie":        # 手工粘贴的 Cookie 串
            headers["Cookie"] = token
        else:                         # bearer（默认）
            headers[name] = f"Bearer {token}"
    return url, headers


def _validated_mimo_url(url):
    """SSRF 防护：仅允许 https，DNS 解析后逐 IP 阻断私网 / 环回 / 链路
    本地 / 保留 / 组播地址。端点由使用者配置，故不做硬编码域名白名单
    （白名单会让功能在改配置后立刻失效），其余策略与 GLM / Codex 一致。"""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    try:
        infos = socket.getaddrinfo(
            parsed.hostname, 443, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror:
        return None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return None
    return url


def _mimo_find(payload, keys, depth=0):
    """在 JSON 里按字段名有限深度（≤4 层）查找，返回首个命中值。

    只看 dict 的 key 名与 list 元素，不做模糊匹配——响应结构由使用者的
    端点决定，宁可找不到也不猜一个数字出来。"""
    if depth > 4:
        return None
    if isinstance(payload, dict):
        for key in keys:
            if key in payload and payload[key] is not None:
                return payload[key]
        for value in payload.values():
            found = _mimo_find(value, keys, depth + 1)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _mimo_find(item, keys, depth + 1)
            if found is not None:
                return found
    return None


def _mimo_number(value):
    """把接口给的比例/百分数/带 % 的字符串统一成 0~100 的数值；解析失败返回 None。

    ≤1 视为比例（0.62 → 62），>1 视为百分数——usedRatio 与 percent 两种
    常见写法都覆盖，避免再按字段名分支。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().rstrip("%").strip()
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value * 100 if 0 <= value <= 1 else value


def _mimo_reset_ts(value):
    """重置时刻 → epoch 秒：支持秒 / 毫秒 / ISO-8601；解析不出返回 0（等待同步）。"""
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:      # 毫秒
            ts /= 1000
        # 远古（<2000 年）与未来（>2100 年）都当垃圾数据，不编倒计时
        return ts if 946684800 < ts < 4102444800 else 0.0
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            return _mimo_reset_ts(datetime.fromisoformat(text).timestamp())
        except ValueError:
            return 0.0
    return 0.0


_MIMO_USED_KEYS = ("usedRatio", "used_ratio", "usedPercent", "used_percent",
                   "percent", "usage", "used", "consumed")
_MIMO_REMAIN_KEYS = ("remaining", "remainingPercent", "remaining_percent",
                     "left", "remain", "balance")


def fetch_mimo_usage():
    """调用 config.json 里配置的 MiMo 用量端点，返回 dict 或 None。

    返回 {"remaining": 0~100, "reset_ts": epoch 秒, "plan": 套餐名|None,
          "monthly_remaining": 0~100|None}。未配置 / 校验失败 / 鉴权失败 /
    结构里读不出比例，一律返回 None（虚线空环，不伪造读数）。"""
    url, headers = _mimo_config()
    if not url:
        return None
    url = _validated_mimo_url(url)
    if not url:
        print("MiMo 用量端点未通过校验（需 https + 公网地址）")
        return None

    try:
        with _OPENER.open(Request(url, headers=headers), timeout=10) as resp:
            raw = resp.read(MIMO_MAX_BYTES + 1)
    except (URLError, OSError) as exc:
        _gate_report("mimo", exc)
        print(f"MiMo 用量错误: {exc}")
        return None
    if len(raw) > MIMO_MAX_BYTES:
        print("MiMo 用量响应超过大小上限，已丢弃")
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"MiMo 用量响应不是 JSON: {exc}")
        return None
    _gate_reset("mimo")

    remaining = _mimo_number(_mimo_find(body, _mimo_REMAIN_KEYS))
    if remaining is None:
        used = _mimo_number(_mimo_find(body, _MIMO_USED_KEYS))
        if used is not None:
            remaining = 100 - used
    if remaining is None:
        # 业务层失败（HTTP 200 但 code!=0 / 空 data）要留痕，否则日志一片空白
        print(f"MiMo 用量接口无额度数据: {str(body)[:150]}")
        return None
    remaining = max(0.0, min(100.0, remaining))

    plan = _mimo_find(body, ("plan", "planName", "plan_name", "title",
                             "subscription", "packageName"))
    if not isinstance(plan, str) or not plan.strip():
        plan = None
    monthly = _mimo_number(_mimo_find(
        body, ("monthly", "monthlyRemaining", "monthly_remaining",
               "monthRemaining", "month_remaining", "monthPercent")))
    return {
        "remaining": remaining,
        "reset_ts": _mimo_reset_ts(_mimo_find(
            body, ("reset_ts", "resetAt", "reset_at", "resetDate",
                   "reset_date", "nextResetTime", "next_reset_time",
                   "expires_at", "expire_at", "expireAt"))),
        "plan": plan,
        "monthly_remaining": (max(0.0, min(100.0, monthly))
                              if monthly is not None else None),
    }


# ── 会话活动检测（闲忙状态）───────────────────────────────────────────
# 借鉴 Codenotch 的会话状态可视化：各源忙闲不看 API，看本机 CLI 的会话文件
# 写入信号——rollout / event jsonl 持续写入即「忙」，停写但进程还在即
# 「阻塞等待输入」，进程消失或长期静默即「空闲」。只做本地只读 stat，
# 不读文件内容，不碰会话数据本身。
ACTIVITY_BUSY_WINDOW = 90          # 会话文件多少秒内有写入 → 忙
ACTIVITY_BLOCKED_WINDOW = 15 * 60  # 停写后多久内进程仍在 → 阻塞等待
ACTIVITY_SCAN_INTERVAL = 2.0       # 后台扫描周期（秒）


def _activity_watchers():
    """每个额度源的活动信号：(会话文件 glob 目录列表, 进程映像名关键字)。

    - GLM：Claude Code 走 GLM 中转，会话写在 ~/.claude/projects/**/*.jsonl
    - Kimi：Kimi Code daemon 的会话事件流 server/events/session_*.jsonl
    - Codex：各账号目录 sessions/**/rollout-*.jsonl（slug 账号各自独立）
    """
    watchers = {
        "glm": ([Path.home() / ".claude" / "projects"], ("claude",)),
        "kimi": ([KIMI_HOME / "server" / "events"], ("kimi",)),
    }
    for slug, home in CODEX_PROFILES:
        key = "codex" if not slug else f"codex:{slug}"
        watchers[key] = ([home / "sessions"], ("codex",))
    return watchers


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_proc_cache = (0.0, frozenset())


def _running_process_names():
    """当前进程映像名小写集合（Toolhelp32 快照，ctypes 直读无子进程开销），
    结果缓存 5 秒——活动扫描周期 2 秒，进程生灭不需要更高精度。"""
    global _proc_cache
    now = time.time()
    if now - _proc_cache[0] < 5:
        return _proc_cache[1]
    names = set()
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot and snapshot != ctypes.c_void_p(-1).value:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        # 必须显式用 W 版：缺省解析到 ANSI 的 Process32First，宽字符字段会
        # 读出两两拼错的乱码进程名（'n','o' 拼成 U+6F6E），匹配永远落空
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                names.add(entry.szExeFile.lower())
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        kernel32.CloseHandle(snapshot)
    _proc_cache = (now, frozenset(names))
    return _proc_cache[1]


def _newest_mtime(dirs, suffix):
    """目录树内指定后缀文件的最新 mtime（epoch 秒）；无文件返回 0。"""
    newest = 0.0
    for root in dirs:
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    if not name.endswith(suffix):
                        continue
                    try:
                        mtime = os.path.getmtime(os.path.join(dirpath, name))
                    except OSError:
                        continue
                    if mtime > newest:
                        newest = mtime
        except OSError:
            continue
    return newest


def scan_activity_states():
    """扫描全部源的活动状态，返回 {源 key: "busy" | "blocked" | "idle"}。

    busy：会话文件 90 秒内有写入（agent 正在跑）；
    blocked：停写未超 15 分钟且对应 CLI 进程仍在（大概率在等你输入）；
    idle：其余。进程名按关键字子串匹配（如 "codex" 命中 codex.exe）。
    """
    now = time.time()
    states = {}
    procs = None
    for key, (dirs, proc_keywords) in _activity_watchers().items():
        age = now - _newest_mtime(dirs, ".jsonl")
        if age <= ACTIVITY_BUSY_WINDOW:
            states[key] = "busy"
            continue
        if age <= ACTIVITY_BLOCKED_WINDOW:
            if procs is None:
                procs = _running_process_names()
            if any(kw in name for kw in proc_keywords for name in procs):
                states[key] = "blocked"
                continue
        states[key] = "idle"
    return states


# ── 番茄钟作息推导 ────────────────────────────────────────────────────
_workday_cache = {}


def _is_workday(day):
    """中国工作日判定：法定节假日休息、调休周末补班（chinese-calendar 数据）。
    库缺失 / 日期超出其数据范围时退回「周一~周五」近似，保证可用。"""
    if day not in _workday_cache:
        try:
            from chinese_calendar import is_workday
            _workday_cache[day] = bool(is_workday(day))
        except Exception:
            _workday_cache[day] = day.weekday() < 5
    return _workday_cache[day]


def _off(label, next_time):
    """非工作时段状态：倒计时区直接显示下次开工时刻（HH:MM 编码进 MM:SS 绘制）"""
    return {"stage": "off", "label": label,
            "remaining": next_time.hour * 60 + next_time.minute,
            "progress": 0, "next_time": next_time.strftime("%H:%M")}


def _pomo_state(now=None):
    """从墙钟时间推导番茄钟状态（对齐 WORK_SESSIONS 作息表）。

    每秒现算而非自由累计计时，因此任意时刻启动即对位、睡眠/挂起后自动恢复，
    阶段边界永远精确落在作息表的整分上，不随运行时长漂移。时段尾的自然截断
    （如上午 11:00-11:30 只剩 30 分钟）会收紧该阶段时长，使倒计时递减到 0
    恰好落在午休/下班边界，不产生突兀跳变。

    返回 dict：
      stage     "work" / "rest" / "off"（非工作时段）
      label     显示词：工作/休息，或 off 时的 待机/午休/下班/假日
      remaining 剩余秒（off 时为下次开工 HH:MM 的编码值）
      progress  阶段剩余比例 0~1（与倒计时数字同向递减；off 恒 0）
      next_time off 时下一个时段开始时刻 "HH:MM"
    """
    now = now or datetime.now()
    if not _is_workday(now.date()):
        return _off("假日", dtime(9, 0))

    t = now.time()
    (am_start, am_end), (pm_start, pm_end) = WORK_SESSIONS
    if t < am_start:
        return _off("待机", am_start)
    if am_end <= t < pm_start:
        return _off("午休", pm_start)
    if t >= pm_end:
        return _off("下班", dtime(9, 0))

    session_start, session_end = (am_start, am_end) if t < am_end else (pm_start, pm_end)
    to_sec = lambda tt: tt.hour * 3600 + tt.minute * 60 + tt.second
    session_len = to_sec(session_end) - to_sec(session_start)

    elapsed = (now - datetime.combine(now.date(), session_start)).total_seconds()
    cycle_sec = (POMODORO_WORK_MIN + POMODORO_REST_MIN) * 60
    cycle_idx, in_cycle = divmod(elapsed, cycle_sec)
    if in_cycle < POMODORO_WORK_MIN * 60:
        stage, stage_total = "work", POMODORO_WORK_MIN * 60
        stage_offset = cycle_idx * cycle_sec
    else:
        stage, stage_total = "rest", POMODORO_REST_MIN * 60
        stage_offset = cycle_idx * cycle_sec + POMODORO_WORK_MIN * 60

    # 时段尾截断：收尾阶段时长按距时段结束收紧（见 docstring）
    total = min(stage_total, session_len - stage_offset)
    remain = total - (elapsed - stage_offset)
    return {
        "stage": stage,
        "label": "工作" if stage == "work" else "休息",
        "remaining": math.ceil(remain),
        # 进度按标准阶段时长（45/15 分钟）归一：进度条读数永远等于
        # 剩余分钟数 ÷ 满阶段分钟数，与倒计时严格同比例；时段尾截断段
        # （30 分钟收尾）从 67% 起步而非满格，避免「条显 82% 但只剩 24 分钟」
        "progress": remain / stage_total,
        "next_time": "",
    }


# ── 通知 ──────────────────────────────────────────────────────────────
def register_aumid():
    """注册应用 AUMID 到注册表，让系统通知显示番茄图标和应用名「GLM 仪表盘」（幂等）"""
    key_path = f"Software\\Classes\\AppUserModelId\\{AUMID}"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "GLM 仪表盘")
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(ICON_PATH))
    except OSError as exc:
        print(f"AUMID 注册失败: {exc}")


def notify_windows(title, message):
    """通过 winotify 弹出 WinRT toast（番茄图标 + 应用名「GLM 仪表盘」）"""
    try:
        Notification(
            app_id=AUMID,
            title=title,
            msg=message,
            icon=str(ICON_PNG),
            duration="short",
        ).show()
    except Exception as exc:
        print(f"通知发送失败: {exc}")


# ── 开机自启 ──────────────────────────────────────────────────────────
def _autostart_command():
    """自启命令：用 pythonw 无窗口启动本脚本绝对路径"""
    pyw = Path(sys.executable).with_name("pythonw.exe")
    if not pyw.exists():  # 兜底：个别环境无独立 pythonw
        pyw = Path(sys.executable)
    return f'"{pyw}" "{Path(__file__).resolve()}"'


def is_autostart_enabled():
    """当前是否已注册开机自启，且命令与本次启动路径一致"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_KEY) as k:
            return winreg.QueryValueEx(k, AUTOSTART_NAME)[0] == _autostart_command()
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_autostart(enable):
    """开启/关闭开机自启（HKCU Run 项），返回是否成功"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_command())
            else:
                winreg.DeleteValue(k, AUTOSTART_NAME)
        return True
    except FileNotFoundError:
        return not enable  # 关闭时本就不存在，视为成功
    except OSError as exc:
        print(f"自启设置失败: {exc}")
        return False


# ── 图标生成 ──────────────────────────────────────────────────────────
# 字体缓存：同一字号只加载一次，避免每秒重复 truetype 造成 GDI 字体对象阶梯增长
_FONT_CACHE = {}


def _load_font(candidates, size, weight=None):
    """按 (字体候选, 字号, 字重) 缓存 PIL 字体对象。
    weight 用于可变字体（如思源黑体 NotoSansSC-VF）设定 wght 轴；
    非可变字体设置失败时静默忽略（如 msyhbd 本身就是粗体）。"""
    key = (candidates, size, weight)
    if key not in _FONT_CACHE:
        font = None
        for _fname in candidates:
            try:
                font = ImageFont.truetype(_fname, size)
                if weight:
                    try:
                        font.set_variation_by_axes([weight])
                    except Exception:
                        pass
                break
            except Exception:
                continue
        _FONT_CACHE[key] = font or ImageFont.load_default()
    return _FONT_CACHE[key]


# ── UI 规格（紧凑状态轨：品牌图标 + 短期额度 / 周预算 / 番茄阶段圆环）──
WIDGET_W = 44
RAIL_RADIUS = WIDGET_W // 2
# 槽位规格单一来源：key 列表 + 槽距推导中心 y，其余（hover 命中分界 /
# 百分比文字顶 / 总高）全部由它推导——加槽只改这一处，不再五处同步。
# Codex 多账号（~/.codex-<slug>）各占一槽，位置固定不互换。
# MiMo 排在各 LLM 源之后、番茄钟之前：上段全是额度源，下段是时间源。
SLOT_LAYOUT = (["glm", "kimi"]
               + ["codex" if not slug else f"codex:{slug}"
                  for slug, _ in CODEX_PROFILES]
               + ["mimo", "pomo"])


def _slot_specs(keys):
    """槽位 key 列表 → ((key, 中心 y), ...)：槽距 60、首槽中心 28，
    布局推导的单一来源——可见槽位变化（源被摘掉/恢复）时用它重排。"""
    return tuple((key, 28 + 60 * i) for i, key in enumerate(keys))


def _widget_h(count):
    return 60 * count + 12


SLOT_SPECS = _slot_specs(SLOT_LAYOUT)
WIDGET_H = _widget_h(len(SLOT_LAYOUT))
SLOT_KEYS = tuple(key for key, _ in SLOT_SPECS)
SS = 4                          # 超采样倍率：先画 4 倍大图再 LANCZOS 缩小抗锯齿
CARD_BG = (28, 28, 30, 190)     # #1C1C1E @75%，iOS secondarySystemBackground(dark)
HAIRLINE = (255, 255, 255, 26)  # 发丝描边 / 分隔线
TOP_LIGHT = (255, 255, 255, 38)  # 顶部内高光（伪玻璃上光）
RAIL_BG = (5, 5, 7, 250)
RAIL_HAIRLINE = (255, 255, 255, 12)
TEXT_SUB = (255, 255, 255, 185)
TEXT_DIM = (255, 255, 255, 100)
BAR_TRACK = (255, 255, 255, 36)
GREEN = (48, 209, 88, 255)      # iOS systemGreen：Token ≥40%
ORANGE = (255, 159, 10, 255)    # iOS systemOrange：15–40%
RED = (255, 69, 58, 255)        # iOS systemRed：<15%（数字同步染红）
GLM_BLUE = (18, 105, 203, 255)  # GLM 主色：深蓝
KIMI_GREEN = (48, 209, 88, 255) # Kimi 主色：绿色
CODEX_MAGENTA = (214, 51, 132, 255)  # Codex 主色：洋红（色相 320–335° 是全卡唯一空闲区间，避开蓝/绿/紫/teal 与告警三色）
CODEX_DISC = (192, 38, 110, 255)      # Codex 底盘：洋红深一档
MIMO_ORANGE = (255, 105, 0, 255)      # MiMo 主色：小米橙 #FF6900（与 iOS 告警橙 #FF9F0A 相邻但更红）
MIMO_DISC = (214, 84, 0, 255)         # MiMo 底盘：深一档，压住与额度告警橙的相似度
FOCUS_COLOR = (191, 90, 242, 255)   # Apple 专注紫：工作阶段
REST_COLOR = (100, 210, 255, 255)   # teal：休息阶段
OFF_COLOR = (142, 142, 147, 255)    # iOS systemGray：非工作时段（待机/午休/下班/假日）
YELLOW = (255, 214, 10, 255)        # iOS systemYellow：窗口重置倒计时填充

NUM_FONT = ("seguisb.ttf",)   # Segoe UI Semibold（详情面板数值）
BOLD_FONT = ("segoeuib.ttf",)  # Segoe UI Bold（TOKEN 标签）
# 中文：思源黑体可变字体（NotoSansSC-VF，wght=700），缺失时回退微软雅黑/黑体
ZH_FONT = ("NotoSansSC-VF.ttf", "msyhbd.ttc", "msyh.ttc", "simhei.ttf")
ZH_WEIGHT = 700

# 贴边隐藏 / 详情面板交互
EDGE_STRIP = 6      # 贴边隐藏后露出的细边宽度（px）
HIDE_DELAY = 1.2    # 鼠标离开后多少秒滑入停靠侧屏缘
PANEL_DELAY = 0.3   # 悬停多少秒展开对应项的详情卡片
PANEL_SWITCH_DELAY = 0.15
PANEL_CLOSE_DELAY = 0.3
PANEL_W, PANEL_H = 168, 64
PANEL_GAP = 8
BOOT_GRACE = 5.0    # 启动后的宽限期，期间不自动隐藏（先亮个相）


def _remaining_color(remaining):
    """Token 剩余分级色：≥40% 绿 / 15–40% 橙 / <15% 红（iOS 系统色）"""
    if remaining >= 40:
        return GREEN
    if remaining >= 15:
        return ORANGE
    return RED


def _stage_color(stage):
    """番茄钟阶段色：工作=专注紫、休息=teal、非工作时段=灰（刻意避开电量三色）"""
    if stage == "work":
        return FOCUS_COLOR
    if stage == "rest":
        return REST_COLOR
    return OFF_COLOR


def _draw_tracked(d, cx, y_top, text, font, tracking, fill):
    """带字距的逐字符绘制（模拟 SF Micro 小标签），整串水平居中于 cx"""
    total = sum(font.getlength(ch) for ch in text) + tracking * (len(text) - 1)
    x = cx - total / 2
    for ch in text:
        d.text((x, y_top), ch, font=font, fill=fill)
        x += font.getlength(ch) + tracking


def _lift(color, k):
    """向白色提亮 k∈[0,1]，用于弧头发光核心。"""
    r, g, b = color[:3]
    return (round(r + (255 - r) * k),
            round(g + (255 - g) * k),
            round(b + (255 - b) * k), 255)


def _draw_ring(d, img, cx, cy, radius, stroke, frac, color, no_data=False):
    """软光晕状态环（macOS 呼吸灯质感）：亮环芯 + 双层高斯柔光。

    进度弧不再画前后两个圆头端点——尾部以 smoothstep alpha 渐隐、
    融入暗色轨道，头部只剩一颗向白提亮的发光核心（彗星画法），接近
    满环时整圈无缝闭合成完整圆环。no_data 仍用虚线轨道，避免与耗尽
    态混淆。光晕与清晰环同源：环层两级高斯模糊后垫底、清晰层叠上，
    任意壁纸下都有柔和的发光轮廓而不改变额度语义。"""
    s = SS
    margin = 6 * s                         # 光晕层外边距（≥2.5×最大模糊半径）
    half = radius + margin
    size = int(half * 2)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dl = ImageDraw.Draw(layer)
    lc = half                              # 层内中心坐标
    ring_bbox = [lc - radius, lc - radius, lc + radius, lc + radius]

    if no_data:
        for start in range(-90, 270, 52):
            dl.arc(ring_bbox, start, start + 28, fill=OFF_COLOR, width=stroke)
    else:
        dl.ellipse(ring_bbox, outline=BAR_TRACK, width=stroke)
        frac = max(0.0, min(1.0, frac))
        if 0.001 < frac < 0.999:
            start = -90.0
            sweep = 360.0 * frac
            fade = min(85.0, sweep * 0.6)     # 尾部渐隐弧长（°）
            n = max(4, int(fade / 3))         # 渐隐段数：每段约 3°，平滑无缝
            for i in range(n):
                a0 = start + fade * i / n
                a1 = start + fade * (i + 1) / n
                t = (i + 0.5) / n
                alpha = int(255 * t * t * (3 - 2 * t))   # smoothstep 渐入
                dl.arc(ring_bbox, a0, a1 + 0.6,
                       fill=color[:3] + (alpha,), width=stroke)
            # 实心主体 + 头部发光核心：唯一的“亮端”，光晕下读作光点而非端点
            dl.arc(ring_bbox, start + fade - 0.6, start + sweep,
                   fill=color, width=stroke)
            ang = math.radians(start + sweep)
            hx, hy = lc + radius * math.cos(ang), lc + radius * math.sin(ang)
            cap_r = stroke / 2
            dl.ellipse([hx - cap_r, hy - cap_r, hx + cap_r, hy + cap_r],
                       fill=color)
            core_r = stroke * 0.34
            dl.ellipse([hx - core_r, hy - core_r, hx + core_r, hy + core_r],
                       fill=_lift(color, 0.55))
        elif frac >= 0.999:
            dl.ellipse(ring_bbox, outline=color, width=stroke)

    left, top = int(cx - half), int(cy - half)
    halo = layer.filter(ImageFilter.GaussianBlur(2.4 * s))       # 宽柔光
    halo.putalpha(halo.getchannel("A")
                  .point(lambda v: min(255, round(v * 1.7))))
    img.alpha_composite(halo, (left, top))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(1.0 * s)),
                        (left, top))                             # 近距辉光
    img.alpha_composite(layer, (left, top))                      # 清晰环芯


def _draw_glm_mark(d, cx, cy, size):
    """GLM 的深蓝晶体标记：角形轮廓配合两道切面。"""
    half = size / 2
    points = [
        (cx, cy - half), (cx + half * 0.82, cy - half * 0.22),
        (cx + half * 0.46, cy + half), (cx - half * 0.63, cy + half * 0.42),
        (cx - half * 0.78, cy - half * 0.36),
    ]
    width = max(2, round(size * 0.17))
    d.line(points + [points[0]], fill=(219, 237, 255, 255), width=width, joint="curve")
    d.line([points[0], (cx + half * 0.46, cy + half)],
           fill=(147, 199, 250, 255), width=width)
    d.line([(cx - half * 0.78, cy - half * 0.36),
            (cx + half * 0.82, cy - half * 0.22)],
           fill=(147, 199, 250, 255), width=max(1, width - 1))


def _draw_kimi_mark(d, cx, cy, size):
    """Kimi 的绿色星芒标记：固定八角，远看仍有清晰轮廓。"""
    points = []
    for i in range(16):
        angle = math.radians(-90 + i * 22.5)
        radius = size * (0.48 if i % 2 == 0 else 0.21)
        points.append((cx + math.cos(angle) * radius,
                       cy + math.sin(angle) * radius))
    d.polygon(points, fill=(224, 255, 229, 255))
    d.ellipse([cx - size * 0.12, cy - size * 0.12,
               cx + size * 0.12, cy + size * 0.12], fill=(46, 130, 76, 255))


def _draw_codex_mark(d, cx, cy, size):
    """Codex 的终端提示符标记：❯ 折线箭头 + 短横光标，洋红底盘反白。

    12px 下可辨识优先于品牌还原（圆桌结论：OpenAI 花结交织在此尺寸是物理
    极限），与 GLM 晶体 / Kimi 星芒 / 番茄沙漏同为高对比简洁剪影。"""
    width = max(2, round(size * 0.18))
    hw, hh = size * 0.26, size * 0.36
    d.line([(cx - hw - size * 0.12, cy - hh), (cx - hw + size * 0.16, cy),
            (cx - hw - size * 0.12, cy + hh)],
           fill=(255, 255, 255, 255), width=width, joint="curve")
    d.line([(cx + size * 0.05, cy + hh * 0.8), (cx + size * 0.42, cy + hh * 0.8)],
           fill=(255, 255, 255, 255), width=width)


def _draw_mimo_mark(d, cx, cy, size):
    """MiMo 的橙色圆盘剪影：白色对话气泡 + 右上四角火花。

    与 GLM 晶体 / Kimi 星芒 / Codex ❯_ / 番茄沙漏同属高对比简洁剪影——
    12px 下可辨识优先于品牌还原：气泡是实心白块（远看轮廓唯一，不会与
    星芒的尖角混淆），火花压在右上角补足「AI 助手」语义，两块之间留出
    约 0.3×半径的缺口，缩图后仍读得出是两个元素。"""
    h = size / 2
    white = (255, 255, 255, 255)
    # 气泡：偏左下的圆角矩形，尾巴又宽又短——缩到 12px 后「细长尖尾」
    # 会读成一面旗而不是对话气泡，宽尾才是这个尺寸下的稳定轮廓
    bl, bt = cx - h * 0.92, cy - h * 0.58
    br, bb = cx + h * 0.10, cy + h * 0.28
    d.rounded_rectangle([bl, bt, br, bb], radius=h * 0.28, fill=white)
    d.polygon([(cx - h * 0.72, bb - h * 0.12),
               (cx - h * 0.84, bb + h * 0.46),
               (cx - h * 0.06, bb - h * 0.12)], fill=white)
    # 火花：四角星（外尖锐、内收腰），落在右上缺口之外
    sx, sy = cx + h * 0.78, cy - h * 0.62
    ro, ri = h * 0.36, h * 0.11
    points = []
    for i in range(8):
        angle = math.radians(-90 + i * 45)
        r = ro if i % 2 == 0 else ri
        points.append((sx + math.cos(angle) * r, sy + math.sin(angle) * r))
    d.polygon(points, fill=white)


def _draw_pomodoro_mark(d, cx, cy, size, color):
    """番茄阶段使用简洁沙漏，工作/休息/非工作只变色，不改变图形语义。"""
    half_w, half_h = size * 0.32, size * 0.44
    width = max(2, round(size * 0.15))
    d.line([(cx - half_w, cy - half_h), (cx + half_w, cy - half_h)],
           fill=color, width=width)
    d.line([(cx - half_w, cy + half_h), (cx + half_w, cy + half_h)],
           fill=color, width=width)
    d.line([(cx - half_w * 0.8, cy - half_h), (cx + half_w * 0.72, cy + half_h)],
           fill=color, width=width)
    d.line([(cx + half_w * 0.8, cy - half_h), (cx - half_w * 0.72, cy + half_h)],
           fill=color, width=width)


def _draw_icon_disc(d, cx, cy, radius, color):
    """状态环中央的品牌色底盘，让小尺寸图标保持足够识别度。"""
    d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=color)


def _draw_quota_badge(d, cx, cy, radius, remaining):
    """只在额度告警时绘制小点，平台环保持自身的品牌主色。"""
    if remaining is None or remaining >= 40:
        return
    badge_r = max(3, radius * 0.16)
    color = _remaining_color(remaining)
    x, y = cx + radius * 0.64, cy + radius * 0.64
    d.ellipse([x - badge_r - 1, y - badge_r - 1,
               x + badge_r + 1, y + badge_r + 1], fill=RAIL_BG)
    d.ellipse([x - badge_r, y - badge_r, x + badge_r, y + badge_r], fill=color)


def _draw_activity_overlay(img, cx, cy, radius, state, now):
    """会话闲忙可视化 v1.33：busy 由「环内细弧旋转」改为**呼吸灯**——
    整环冷白柔光以 2.4s 周期正弦起伏，环芯亮度与光晕强度同步呼吸，
    待机指示灯观感；blocked 保持环外琥珀脉冲（1.6s），同样走柔光
    管线。相位取墙钟，帧率无关。叠加在额度环之上，不改变额度语义。"""
    s = SS
    if state == "busy":
        color, r, stroke, period = (222, 238, 255), radius, 2.2 * s, 2.4
    elif state == "blocked":
        color, r, stroke, period = ORANGE[:3], radius + 3.0 * s, 1.6 * s, 1.6
    else:
        return
    amp = 0.5 + 0.5 * math.sin(2 * math.pi * now / period)
    core_alpha = int(40 + (100 if state == "busy" else 110) * amp)
    boost = 0.7 + (2.6 if state == "busy" else 2.4) * amp

    margin = 6 * s
    half = r + margin
    size = int(half * 2)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dl = ImageDraw.Draw(layer)
    lc = half
    dl.ellipse([lc - r, lc - r, lc + r, lc + r],
               outline=color + (core_alpha,), width=max(1, round(stroke)))
    left, top = int(cx - half), int(cy - half)
    glow = layer.filter(ImageFilter.GaussianBlur(2.0 * s))
    glow.putalpha(glow.getchannel("A")
                  .point(lambda v: min(255, round(v * boost))))
    img.alpha_composite(glow, (left, top))
    img.alpha_composite(layer, (left, top))


def create_widget_image(values, pomo, dim, activity=None, glass=False,
                        slots=None):
    """生成窄幅状态轨：GLM、Kimi、Codex（可多账号）、MiMo 与番茄阶段图标圆环。

    主轨只呈现各源最关键的一格读数（GLM/Kimi 短期额度、Codex 周预算、
    MiMo 套餐剩余、当前番茄阶段）；重置、周/月额度和精确读数保留在 hover
    详情面板中。每个图标下方显示对应剩余比例。activity 给出各额度源的
    会话闲忙态叠加。
    slots 为当前可见槽位（默认全量）：被判无响应的源由调用方整体摘掉，
    轨道高度随可见槽数收缩。"""
    keys = tuple(slots) if slots is not None else SLOT_KEYS
    specs = _slot_specs(keys)
    height = _widget_h(len(keys))
    s = SS
    cw, ch = WIDGET_W * s, height * s
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=RAIL_RADIUS * s,
                        fill=RAIL_BG, outline=RAIL_HAIRLINE, width=2)

    cx = cw / 2
    radius, stroke = 15 * s, 3 * s
    glyph_size = 12 * s
    activity = activity or {}
    now = time.time()
    pct_font = _load_font(NUM_FONT, 11 * s)

    def draw_percentage(y, value):
        text = "--" if value is None else f"{max(0, min(100, value)):.0f}%"
        color = TEXT_DIM if value is None else TEXT_SUB
        bbox = d.textbbox((0, 0), text, font=pct_font)
        d.text((cx - (bbox[2] - bbox[0]) / 2 - bbox[0], y * s - bbox[1]),
               text, font=pct_font, fill=color)

    def draw_no_data_dash(cy):
        d.line([(cx - glyph_size * 0.28, cy), (cx + glyph_size * 0.28, cy)],
               fill=OFF_COLOR, width=max(2, s))

    for i, key in enumerate(keys):
        cy = specs[i][1] * s
        if key == "pomo":
            stage_color = _stage_color(pomo["stage"])
            if dim:
                stage_color = stage_color[:3] + (int(255 - 145 * dim),)
            _draw_ring(d, img, cx, cy, radius, stroke,
                       pomo["progress"], stage_color)
            _draw_icon_disc(d, cx, cy, glyph_size * 0.75, stage_color)
            _draw_pomodoro_mark(d, cx, cy, glyph_size, (248, 248, 250, 255))
            draw_percentage(specs[i][1] + 19, pomo["progress"] * 100)
            continue

        remaining = values.get(key)
        if key == "glm":
            ring_color, disc_color = GLM_BLUE, GLM_BLUE
        elif key == "kimi":
            ring_color, disc_color = KIMI_GREEN, KIMI_GREEN
        elif key == "mimo":
            ring_color, disc_color = MIMO_ORANGE, MIMO_DISC
        else:  # codex 及各 slug 账号
            ring_color, disc_color = CODEX_MAGENTA, CODEX_DISC
        _draw_ring(d, img, cx, cy, radius, stroke,
                   0 if remaining is None else remaining / 100,
                   ring_color, no_data=remaining is None)
        if remaining is None:
            if key != "glm":  # GLM 加载态默认满电，无数据横线只用于其余源
                draw_no_data_dash(cy)
            else:
                _draw_icon_disc(d, cx, cy, glyph_size * 0.75, disc_color)
                _draw_glm_mark(d, cx, cy, glyph_size)
        else:
            _draw_icon_disc(d, cx, cy, glyph_size * 0.75, disc_color)
            if key == "glm":
                _draw_glm_mark(d, cx, cy, glyph_size)
            elif key == "kimi":
                _draw_kimi_mark(d, cx, cy, glyph_size)
            elif key == "mimo":
                _draw_mimo_mark(d, cx, cy, glyph_size)
            else:
                _draw_codex_mark(d, cx, cy, glyph_size)
        _draw_quota_badge(d, cx, cy, radius, remaining)
        _draw_activity_overlay(img, cx, cy, radius, activity.get(key), now)
        draw_percentage(specs[i][1] + 19, remaining)

    return img.resize((WIDGET_W, height), Image.LANCZOS)


def _create_strip_image(progress=0.0, side="right", height=WIDGET_H):
    """贴边隐藏时的细边：窗口只露出停靠侧的 EDGE_STRIP 像素（其余伸在
    屏外/邻屏，由 region 裁掉），可见区画一条竖向番茄进度条——水位式：
    满格时水位齐顶，随剩余时间自上而下回落，落到空即时间到。颜色按
    剩余比例分级：>50% 绿 / 20–50% 黄 / <20% 红，一眼看出还能撑多久。
    side 为停靠侧："right" 时贴屏右缘、细条画在窗口左侧；"left" 镜像。
    height 跟随当前可见槽数（摘牌后的矮轨细边同步变矮）。"""
    s = SS
    img = Image.new("RGBA", (WIDGET_W * s, height * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if side == "left":
        x0, x1 = (WIDGET_W - 4.5) * s, (WIDGET_W - 1.5) * s
    else:
        x0, x1 = 1.5 * s, 4.5 * s
    y0, y1 = 8 * s, (height - 8) * s
    d.rounded_rectangle([x0, y0, x1, y1], radius=1.5 * s,
                        fill=(255, 255, 255, 36))
    if progress > 0.01:
        color = GREEN if progress > 0.5 else (YELLOW if progress > 0.2 else RED)
        fill_h = max((y1 - y0) * progress, x1 - x0)
        d.rounded_rectangle([x0, y1 - fill_h, x1, y1], radius=1.5 * s,
                            fill=color[:3] + (220,))
    return img.resize((WIDGET_W, height), Image.LANCZOS)


def create_detail_image(info, target, glass=False):
    """为一个状态项生成紧凑详情卡片。"""
    s = SS
    cw, ch = PANEL_W * s, PANEL_H * s
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=12 * s,
                        fill=(24, 24, 27, 232), outline=HAIRLINE, width=2)
    d.line([(14 * s, 2 * s), ((PANEL_W - 14) * s, 2 * s)],
           fill=TOP_LIGHT, width=s)

    clock_mode = info.get("reset_clock")  # 重置时间显示：False 倒计时 / True 时刻

    def reset_text(ts):
        if not ts:
            return "等待同步"
        if clock_mode:
            return f"重置 {datetime.fromtimestamp(ts).strftime('%H:%M')}"
        left = max(0, int(ts - time.time()))
        hours, remainder = divmod(left, 3600)
        return f"重置 {hours}h{remainder // 60:02d}m"

    if target == "glm":
        value = info["glm_remaining"]
        title, accent = "GLM 用量", GLM_BLUE
        line_one = f"周额度 {info['glm_weekly']:.0f}%"
        line_two = reset_text(info["glm_reset_ts"])
        meta_x = 84
    elif target == "kimi":
        value = info["kimi_remaining"]
        title, accent = "Kimi 用量", KIMI_GREEN
        monthly = info["kimi_monthly"]
        line_one = "月额度 --" if monthly is None else f"月额度 {monthly:.0f}%"
        line_two = reset_text(info["kimi_reset_ts"])
        meta_x = 84
    elif target.startswith("codex"):
        slug = target.split(":", 1)[1] if ":" in target else ""
        value = info["codex_remaining"]
        title = "Codex 用量" if not slug else f"Codex·{slug}"
        accent = CODEX_MAGENTA
        # 中转额度是 USD 计价：主数值是周预算剩余比例，配行给绝对值与今日消耗。
        # 无日限（daily_limit=0 + unrestricted）没有分母，绝不折算成比例，只以
        # 文字标注；月额度放窗口 tooltip。meta 起点比其他卡左移（数值列最宽
        # "100%" 也不到 64px，不冲突），两行绝对值文本才放得下。
        meta_x = 70
        line_one = (f"周 ${info['codex_weekly_used']:.1f}"
                    f"/${info['codex_weekly_limit']:.0f}")
        today = info.get("codex_today_cost")
        ts = info.get("codex_reset_ts") or 0
        if not ts:
            reset = "等待窗口"
        elif clock_mode:
            reset = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        else:
            days, rem = divmod(max(0, int(ts - time.time())), 86400)
            reset = f"{days}天{rem // 3600}h"
        line_two = (f"{reset} · 今日 ${today:.1f}"
                    if today is not None else reset)
    elif target == "mimo":
        value = info["mimo_remaining"]
        title, accent = "MiMo 用量", MIMO_ORANGE
        monthly = info["mimo_monthly"]
        if monthly is not None:
            line_one = f"月额度 {monthly:.0f}%"
        else:  # 没有长周期额度就把套餐名放这行，端点没给则如实写「套餐 --」
            line_one = f"套餐 {info['mimo_plan'] or '--'}"
        line_two = reset_text(info["mimo_reset_ts"])
        meta_x = 84
    else:
        value = info["pomo_progress"] * 100
        title, accent = "番茄阶段", _stage_color(info["pomo_stage"])
        line_one = info["pomo_label"]
        line_two = info["pomo_detail"]
        meta_x = 84

    value_text = "--" if value is None else f"{max(0, min(100, value)):.0f}%"
    title_font = _load_font(ZH_FONT, 10 * s, ZH_WEIGHT)
    value_font = _load_font(NUM_FONT, 22 * s)
    meta_font = _load_font(ZH_FONT, 9 * s, ZH_WEIGHT)
    d.text((12 * s, 8 * s), title, font=title_font, fill=TEXT_SUB)
    d.text((12 * s, 25 * s), value_text, font=value_font,
           fill=TEXT_DIM if value is None else accent)
    d.text((meta_x * s, 23 * s), line_one, font=meta_font, fill=TEXT_SUB)
    d.text((meta_x * s, 40 * s), line_two, font=meta_font, fill=TEXT_DIM)

    if info.get("pinned"):
        # 钉住标记：右上角小圆点（再点一次面板解除）
        r = 2.5 * s
        d.ellipse([cw - 12 * s - r, 9 * s - r, cw - 12 * s + r, 9 * s + r],
                  fill=accent)

    return img.resize((PANEL_W, PANEL_H), Image.LANCZOS)


# ── 苹果风右键菜单（自绘，替代系统 tk.Menu）───────────────────────────
MENU_ITEM_H = 26    # 菜单项行高
MENU_SEP_H = 9      # 分隔线区高度
MENU_PAD_X = 12     # 文字左右留白
MENU_PAD_Y = 6      # 卡片上下内边距
MENU_SHADOW = 8     # 投影边距（窗口比卡片大一圈）


def create_menu_image(labels, hover=-1):
    """macOS 风右键菜单：圆角暗色卡片 + 发丝描边 + 顶部内高光 + 柔和投影，
    悬停项为苹果蓝圆角高亮条；labels 中 None 表示分隔线。
    返回 (图像, (卡片宽, 卡片高, 阴影边距))。"""
    s = SS
    font = _load_font(ZH_FONT, 10 * s, ZH_WEIGHT)
    text_w = max((font.getlength(t) for t in labels if t), default=0)
    w = math.ceil(text_w / s) + 2 * MENU_PAD_X + 14  # 文字 + 留白 + 右侧余量
    rows_h = sum(MENU_SEP_H if t is None else MENU_ITEM_H for t in labels)
    h = rows_h + 2 * MENU_PAD_Y
    cw, ch = (w + 2 * MENU_SHADOW) * s, (h + 2 * MENU_SHADOW) * s
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))

    # 柔和投影：偏移深色圆角矩形 + 高斯模糊，垫在卡片之下
    sh = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    x0, y0 = MENU_SHADOW * s, MENU_SHADOW * s
    x1, y1 = x0 + w * s - 1, y0 + h * s - 1
    ImageDraw.Draw(sh).rounded_rectangle(
        [x0, y0 + 2 * s, x1, y1 + 2 * s], radius=10 * s, fill=(0, 0, 0, 110))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(3 * s)))

    d = ImageDraw.Draw(img)
    d.rounded_rectangle([x0, y0, x1, y1], radius=10 * s,
                        fill=(26, 26, 30, 242), outline=HAIRLINE, width=2)
    d.line([(x0 + 10 * s, y0 + 2 * s), (x1 - 10 * s, y0 + 2 * s)],
           fill=TOP_LIGHT, width=s)

    ty = y0 + MENU_PAD_Y * s
    for i, text in enumerate(labels):
        if text is None:
            cy = ty + MENU_SEP_H * s // 2
            d.line([(x0 + 10 * s, cy), (x1 - 10 * s, cy)],
                   fill=HAIRLINE, width=max(1, s // 2))
            ty += MENU_SEP_H * s
            continue
        if i == hover:
            d.rounded_rectangle([x0 + 4 * s, ty, x1 - 4 * s, ty + MENU_ITEM_H * s],
                                radius=6 * s, fill=(10, 132, 255, 255))
        bbox = d.textbbox((0, 0), text, font=font)
        d.text((x0 + MENU_PAD_X * s,
                ty + (MENU_ITEM_H * s - (bbox[3] - bbox[1])) / 2 - bbox[1]),
               text, font=font, fill=(245, 245, 247, 255))
        ty += MENU_ITEM_H * s

    out_w, out_h = w + 2 * MENU_SHADOW, h + 2 * MENU_SHADOW
    return img.resize((out_w, out_h), Image.LANCZOS), (w, h, MENU_SHADOW)


# ── Win32 分层窗口渲染 ────────────────────────────────────────────────
def _update_layered_window(hwnd, img):
    """将 PIL RGBA 图像渲染到 Win32 分层窗口（逐像素 Alpha 透明）"""
    w, h = img.size

    # PIL RGBA → Win32 premultiplied BGRA。multiply 即 c*a/255（C 层逐像素），
    # 替代 Python 逐字节循环——此前每帧约 10 万字节的纯 Python 循环是卡顿主因
    r, g, b, a = img.split()
    bgra = Image.merge("RGBA", (
        ImageChops.multiply(b, a),
        ImageChops.multiply(g, a),
        ImageChops.multiply(r, a),
        a,
    )).tobytes()

    # 创建 DIB Section
    bmi = _BMPINFOHEADER()
    bmi.biSize = ctypes.sizeof(_BMPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h  # 自顶向下
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0

    ppvBits = ctypes.c_void_p()
    hdc = ctypes.windll.user32.GetDC(None)
    hBitmap = ctypes.windll.gdi32.CreateDIBSection(
        hdc, ctypes.byref(bmi), 0, ctypes.byref(ppvBits), None, 0,
    )
    ctypes.windll.user32.ReleaseDC(None, hdc)
    if not hBitmap:
        return

    # 拷贝图像数据
    ctypes.memmove(ppvBits, bgra, len(bgra))

    # 创建兼容 DC
    hdcMem = ctypes.windll.gdi32.CreateCompatibleDC(None)
    oldBmp = ctypes.windll.gdi32.SelectObject(hdcMem, hBitmap)

    # 混合参数
    blend = _BLENDFUNCTION()
    blend.BlendOp = AC_SRC_OVER
    blend.BlendFlags = 0
    blend.SourceConstantAlpha = 255
    blend.AlphaFormat = AC_SRC_ALPHA

    ptSrc = _POINT(0, 0)
    size = _SIZE(w, h)

    ctypes.windll.user32.UpdateLayeredWindow(
        hwnd, None, None, ctypes.byref(size),
        hdcMem, ctypes.byref(ptSrc), 0,
        ctypes.byref(blend), ULW_ALPHA,
    )

    # 清理
    ctypes.windll.gdi32.SelectObject(hdcMem, oldBmp)
    ctypes.windll.gdi32.DeleteObject(hBitmap)
    ctypes.windll.gdi32.DeleteDC(hdcMem)


# ── 多屏显示器感知 ───────────────────────────────────────────────────
# 贴边停靠/自动隐藏的边沿必须取「窗口当前所在显示器」的竖边，而非主屏宽度：
# 卡片拖到右侧副屏后其坐标普遍大于主屏宽度，若仍按主屏判定，隐藏条件恒真、
# 滑入目标却落在主屏边缘，卡片会在两块屏之间来回跳（v1.22 及之前的行为）。

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", ctypes.c_long * 4),  # left, top, right, bottom
        ("rcWork", ctypes.c_long * 4),
        ("dwFlags", ctypes.c_uint32),
    ]


def _hmon_rect(hmon):
    """HMONITOR → (left, top, right, bottom)；失败返回 None"""
    if not hmon:
        return None
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if not ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    return tuple(mi.rcMonitor)


def monitor_rect_of_hwnd(hwnd):
    """窗口所在显示器矩形；窗口在屏外时取最近屏兜底"""
    user32 = ctypes.windll.user32
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    hmon = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), 2)  # NEAREST
    return _hmon_rect(hmon)


def monitor_rect_at_point(px, py):
    """坐标所在显示器矩形；不在任何屏上（如记忆位置所在屏已拔掉）返回 None"""
    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.restype = ctypes.c_void_p
    hmon = user32.MonitorFromPoint(_POINT(int(px), int(py)), 0)  # NULL
    return _hmon_rect(hmon)


# ── 悬浮窗口 ─────────────────────────────────────────────────────────
class GLMWidget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)       # 无边框
        self.root.attributes("-topmost", True)  # 置顶

        # 窗口尺寸 & 位置：优先恢复上次停靠位置与侧别（ui_state.json，所在屏
        # 已拔则丢弃），首次启动贴「鼠标所在屏」右缘（悬空 8px、竖直居中）
        self._win_w, self._win_h = WIDGET_W, WIDGET_H
        persisted = self._load_state()
        x, y, self._side = self._restore_pos(persisted)
        if x is None:
            rect = (monitor_rect_at_point(*self.root.winfo_pointerxy())
                    or (0, 0, self.root.winfo_screenwidth(),
                        self.root.winfo_screenheight()))
            x = rect[2] - self._win_w - 8
            y = rect[1] + (rect[3] - rect[1] - self._win_h) // 2
        self.root.geometry(f"{self._win_w}x{self._win_h}+{x}+{y}")

        # 确保窗口已创建，再设置分层窗口
        self.root.update_idletasks()
        # 注意：3.13+ 新版 Tk 的 winfo_id() 返回 TkChild 子窗口，分层窗口必须挂顶层
        self._hwnd = self._toplevel_hwnd(int(self.root.winfo_id()))

        # 右键菜单：自绘 macOS 风分层窗口（create_menu_image），非系统菜单
        self._menu_win = None       # 菜单窗口（lazy 创建）
        self._menu_hwnd = None
        self._menu_items = []       # 当前菜单文案（None = 分隔线）
        self._menu_cmds = []        # 与文案对应的命令
        self._menu_hover = -1       # 当前悬停项索引
        self._menu_geo = None       # (卡片宽, 卡片高, 阴影边距)
        self._menu_armed = False    # 点击外关闭预备：右键松开后才武装，
                                    # 否则按住右键 >150ms 菜单刚开就被自己关掉
        self.root.bind("<Button-3>", self._popup_menu)

        # 拖拽 + 点击刷新：按下记位置，松开时位移小于阈值视为点击槽位
        self.root.bind("<Button-1>", self._drag_start)
        self.root.bind("<B1-Motion>", self._drag_move)
        self.root.bind("<ButtonRelease-1>", self._drag_end)
        self._drag_x = self._drag_y = 0
        self._press_xy = None       # 按下时的屏幕坐标（点击判定用）
        self._dragging = False

        # 贴边隐藏 + hover 详情面板状态
        self._hidden = False          # 当前是否缩成停靠侧细边
        self._sliding_out = False     # 细边→整卡滑出动画进行中（期间保持细边视觉）
        self._visual_mode = None      # 当前窗口形态 ("strip", side)/("full",)（幂等切换用）
        self._panel = None            # 详情面板窗口（lazy 创建）
        self._panel_visible = False
        self._panel_target = None
        self._panel_pinned = False    # 点击详情卡钉住（Keep open），再点解除
        self._panel_animating = False  # 详情卡弹出动画进行中
        self._hover_target = None
        self._hover_since = None      # 指针进入当前项的时刻（面板延迟用）
        self._panel_leave_since = None
        self._leave_since = None      # 指针离开的时刻（隐藏延迟用）
        self._born = time.time()      # 启动宽限期（BOOT_GRACE 内不自动隐藏）
        self._last_refresh = "--"
        self._peek_until = 0.0        # 会话结束提醒：临时滑出截止时刻

        # 持久化状态（ui_state.json）：位置 / 开关 / 告警带 / 各源最后读数
        self._keep_open = bool(persisted.get("keep_open"))
        self._reset_clock = bool(persisted.get("reset_clock"))
        self._alert_bands = persisted.get("alert_bands", {})
        self._state_dirty = False
        self._state_saved_at = 0.0

        # 状态：GLM（短期 Token 剩余 + 周额度剩余 + 短期窗口重置点）+ Kimi
        # + Codex（可多账号，slug → 读数 dict）+ 番茄钟。
        # 启动先用上次落盘的读数（跨启动保留最后读数，对齐 Codenotch UsageStore），
        # 首个刷新周期（5 分钟）内被真实数据替换；reset_ts 已过期的不恢复。
        saved = persisted.get("readings", {})
        now = time.time()
        glm_saved = saved.get("glm") or {}
        self._token_remaining = glm_saved.get("remaining", 100)
        self._weekly_remaining = glm_saved.get("weekly", 100.0)
        self._quota_reset_ts = (glm_saved.get("reset_ts", 0)
                                if glm_saved.get("reset_ts", 0) > now else 0.0)
        kimi_saved = saved.get("kimi") or {}
        self._kimi_remaining = kimi_saved.get("remaining")
        self._kimi_reset_ts = (kimi_saved.get("reset_ts", 0)
                               if kimi_saved.get("reset_ts", 0) > now else 0.0)
        self._kimi_monthly_remaining = kimi_saved.get("monthly")
        codex_saved = saved.get("codex") or {}
        self._codex = {}
        for slug, _home in CODEX_PROFILES:
            c = codex_saved.get(slug) or {}
            reset_ts = c.get("reset_ts", 0)
            self._codex[slug] = {
                "remaining": c.get("remaining"),
                "reset_ts": reset_ts if reset_ts and reset_ts > now else 0.0,
                "weekly": tuple(c.get("weekly", (0.0, 0.0))),
                "today_cost": c.get("today_cost"),
                "monthly": c.get("monthly"),
                "unrestricted": bool(c.get("unrestricted", False)),
            }
        mimo_saved = saved.get("mimo") or {}
        self._mimo_remaining = mimo_saved.get("remaining")
        self._mimo_reset_ts = (mimo_saved.get("reset_ts", 0)
                               if mimo_saved.get("reset_ts", 0) > now else 0.0)
        self._mimo_monthly_remaining = mimo_saved.get("monthly")
        self._mimo_plan = mimo_saved.get("plan")
        self._pomo = _pomo_state()
        self._dim = 0.0           # 番茄钟闪烁调光系数 0~1（正弦淡出）
        self._blinking = False
        # 会话闲忙状态：{源 key: busy/blocked/idle}，后台线程 2 秒一扫；
        # 忙→闲跳变触发「会话结束」提醒（滑出 5 秒 + 提示音）
        self._activity = {}
        self._activity_anim = False  # 闲忙叠加动画循环是否在跑
        # 数值缓动动画：_disp 为当前显示值，_targets 为目标值，
        # 每次刷新后 30fps 指数趋近（ease-out 观感），收敛即停，平时零开销
        self._disp = {}
        self._targets = {}
        self._animating = False
        self._refresh_timer = None  # 全局唯一全源刷新链（_schedule 维护）
        # 源死活状态：连续 DEAD_AFTER 次抓取无响应的源进 _dead，图标（整个
        # 槽位）从状态轨摘除；周期刷新对其照常探测，成功一次即复活挂回
        self._dead = set()
        self._fail_counts = {}

        # 主轨先以 PIL 渲染一帧；局部详情卡片另行启用 Acrylic。
        self._glass = False
        self._render()
        self._setup_layered()
        if self._glass:
            self._render()

    @staticmethod
    def _toplevel_hwnd(hwnd):
        """沿 GetParent 向上找到真正的顶层窗口句柄。

        Python 3.13+ 的新版 Tk 中，Tk().winfo_id() 返回的是 TkChild 子窗口，
        而非顶层；Win32 分层窗口必须挂在顶层窗口上才能被合成显示，
        否则窗口会全透明不可见（进程在跑但屏幕看不到）。
        """
        user32 = ctypes.windll.user32
        parent = user32.GetParent(hwnd)
        while parent:
            hwnd = parent
            parent = user32.GetParent(hwnd)
        return hwnd

    # Win32 分层窗口 -----------------------------------------------
    def _setup_layered(self):
        """建立分层主轨，让自绘 alpha 边缘保留平滑的半圆端帽。"""
        ex = ctypes.windll.user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        self._glass = False
        _clear_window_region(self._hwnd)
        _set_dwm_round(self._hwnd, 1)  # 不让系统默认小圆角覆盖自绘胶囊外形

    # 拖拽 ----------------------------------------------------------
    def _drag_start(self, event):
        self._drag_x, self._drag_y = event.x, event.y
        self._press_xy = (event.x_root, event.y_root)
        self._dragging = True
        self._dismiss_menu()
        if self._panel_visible:
            self._hide_panel()

    def _drag_move(self, event):
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _drag_end(self, event):
        self._dragging = False
        # 按下-松开位移小于 5px 视为点击而非拖拽：点击槽位只刷新对应源
        if self._press_xy is not None:
            dx = event.x_root - self._press_xy[0]
            dy = event.y_root - self._press_xy[1]
            self._press_xy = None
            if dx * dx + dy * dy < 25:
                self._refresh_source(self._slot_at(event.y))
                return
        if self._hidden or self._sliding_out:
            # 拽着细边拖动后松手：就地恢复整卡视觉并按落点归位，
            # 否则 6px 细条会悬在屏中间既唤不出也不贴边
            self._hidden = False
            self._sliding_out = False
            self._render()
        else:
            # 松手时就近更新停靠侧（决定详情面板展开方向与下次贴哪边）
            rect = self._monitor_rect()
            if rect:
                x = self.root.winfo_x()
                self._side = ("left"
                              if x - rect[0] <= rect[2] - (x + self._win_w)
                              else "right")
        self._save_state()

    def _monitor_rect(self):
        """卡片所在屏矩形。锚点取停靠侧可见的窗口内缘而非窗口矩形：隐藏态
        窗口身体伸在停靠侧对面的邻屏上，按矩形取「最近屏」会误判成邻屏，
        边沿判定与滑入目标就全按邻屏算——卡片贴主屏缘时永远唤不出来的根因。
        右停靠可见的是窗口左缘（x+3），左停靠镜像取窗口右缘（x+w-3）"""
        x, y = self.root.winfo_x(), self.root.winfo_y()
        anchor_x = x + self._win_w - 3 if self._side == "left" else x + 3
        return (monitor_rect_at_point(anchor_x, y + self._win_h // 2)
                or monitor_rect_of_hwnd(self._hwnd))

    # 状态持久化（位置 + 停靠侧 + 开关 + 告警带 + 最后读数）------------------
    def _load_state(self):
        """读取 ui_state.json 全部持久化字段；文件缺失/损坏返回空 dict。"""
        try:
            with open(UI_STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
            return state if isinstance(state, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, throttle=False):
        """落盘持久化状态（隐藏/滑出中态位置归一为展开位）。
        throttle=True 时 60 秒内最多写一次（刷新成功后的读数保存走此路径）。"""
        now = time.time()
        if throttle:
            if now - self._state_saved_at < 60:
                self._state_dirty = True
                return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if self._hidden or self._sliding_out:
            rect = self._monitor_rect()
            if rect:
                x = (rect[0] + 8 if self._side == "left"
                     else rect[2] - self._win_w - 8)
        state = {
            "x": x, "y": y,
            "side": self._side,
            "keep_open": self._keep_open,
            "reset_clock": self._reset_clock,
            "alert_bands": self._alert_bands,
            "readings": {
                "glm": {
                    "remaining": self._token_remaining,
                    "weekly": self._weekly_remaining,
                    "reset_ts": self._quota_reset_ts,
                },
                "kimi": {
                    "remaining": self._kimi_remaining,
                    "reset_ts": self._kimi_reset_ts,
                    "monthly": self._kimi_monthly_remaining,
                },
                "codex": {
                    slug: {
                        "remaining": c["remaining"],
                        "reset_ts": c["reset_ts"],
                        "weekly": list(c["weekly"]),
                        "today_cost": c["today_cost"],
                        "monthly": c["monthly"],
                        "unrestricted": c["unrestricted"],
                    }
                    for slug, c in self._codex.items()
                },
                "mimo": {
                    "remaining": self._mimo_remaining,
                    "reset_ts": self._mimo_reset_ts,
                    "monthly": self._mimo_monthly_remaining,
                    "plan": self._mimo_plan,
                },
            },
        }
        try:
            # 固定写项目内 ui_state.json；resolve 后显式断言仍在项目目录，
            # 把「路径由 __file__ 派生、无外部输入」的隐式约束变成运行时保证
            path = UI_STATE_PATH.resolve()
            project_dir = Path(__file__).resolve().parent
            if project_dir not in path.parents:
                raise OSError("状态文件越出项目目录，拒绝写入")
            path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            self._state_saved_at = now
            self._state_dirty = False
        except OSError:
            pass  # 状态写失败不影响运行

    def _restore_pos(self, persisted):
        """读取上次位置与停靠侧；所在屏已不在线（拔掉/改布局）则返回
        (None, None, "right") 走默认。旧版 ui_state.json 没有 side 字段，
        按右停靠兼容（v1.24 及之前只有右缘贴边）"""
        try:
            x, y = int(persisted["x"]), int(persisted["y"])
        except (KeyError, TypeError, ValueError):
            return None, None, "right"
        side = persisted.get("side", "right")
        if side not in ("left", "right"):
            side = "right"
        # 用窗口中心点校验：记忆位置必须落在一块当前在线的屏上
        if monitor_rect_at_point(x + self._win_w // 2, y + self._win_h // 2):
            return x, y, side
        return None, None, "right"

    # 贴边隐藏 + hover 详情面板 --------------------------------------
    def _visible_keys(self):
        """当前状态轨实际渲染的槽位：被判无响应的源整体摘除，番茄钟恒在。"""
        return [k for k in SLOT_KEYS if k == "pomo" or k not in self._dead]

    def _layout(self):
        """(可见 key 列表, (key, 中心 y) 规格)——hover 命中 / 面板锚点 /
        图像绘制共用同一份动态布局。"""
        keys = self._visible_keys()
        return keys, _slot_specs(keys)

    def _relayout(self):
        """图标摘/挂后重排：窗口高度随可见槽数变化，竖直中心保持不变
        （贴边细边态同样适用——region 高度随 _win_h 重建）。"""
        h = _widget_h(len(self._visible_keys()))
        if h == self._win_h:
            self._render()
            return
        old_h = self._win_h
        self._win_h = h
        x, y = self.root.winfo_x(), self.root.winfo_y()
        y += (old_h - h) // 2
        rect = self._monitor_rect()
        if rect:
            y = max(rect[1], min(y, rect[3] - h))
        self._visual_mode = None  # 高度变了：strip region / Acrylic 按新高度重建
        self.root.geometry(f"{self._win_w}x{h}+{x}+{y}")
        self._render()

    def _note_fetch_result(self, key, ok):
        """UI 线程登记一次抓取结果：连续 DEAD_AFTER 次无响应摘掉该源图标
        （槽位整体移出状态轨），一旦重新成功立即复位挂回。周期刷新对摘掉
        的源照常抓取，API 恢复（重新部署 / 换 key）后图标自动回归。"""
        if ok:
            self._fail_counts.pop(key, None)
            if key in self._dead:
                self._dead.discard(key)
                print(f"{key} 恢复响应，图标已重新挂载")
                self._relayout()
            return
        n = self._fail_counts.get(key, 0) + 1
        self._fail_counts[key] = n
        if n >= DEAD_AFTER and key not in self._dead:
            self._dead.add(key)
            print(f"{key} 连续 {n} 次抓取无响应，暂时摘掉图标（恢复后自动回归）")
            if self._panel_visible and self._panel_target == key:
                self._hide_panel()
            self._relayout()

    def _slot_at(self, local_y):
        """返回纵向状态轨中被悬停的独立项（分界由当前可见槽位推导）。"""
        if local_y < 0 or local_y >= self._win_h:
            return None
        keys, specs = self._layout()
        breaks = tuple((a[1] + b[1]) // 2 for a, b in zip(specs, specs[1:]))
        for (key, _), brk in zip(specs, breaks):
            if local_y < brk:
                return key
        return keys[-1]

    def _poll_hover(self):
        """150ms 轮询驱动贴边隐藏与详情面板的状态机（自愈：任何一轮异常
        只记日志，链条照常续跑——此前回调里一处未捕获异常就会让整条
        after 链永久中断，hover/贴边全部失灵且无任何痕迹）"""
        try:
            self._poll_hover_once()
        except Exception:
            traceback.print_exc()
        finally:
            self.root.after(150, self._poll_hover)

    def _poll_hover_once(self):
        """单轮 hover 状态机（调度由 _poll_hover 负责）"""
        px, py = self.root.winfo_pointerxy()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        w, h = self._win_w, self._win_h
        # 边沿取「窗口当前所在屏」：拖到哪块屏就贴哪块屏，不再拽回主屏
        rect = self._monitor_rect()
        if rect:
            ml, sw = rect[0], rect[2]
        else:  # 查询失败兜底：退回老的主屏判定
            ml, sw = 0, self.root.winfo_screenwidth()
        now = time.time()

        # 就近贴边判定：卡片左/右缘进入本屏对应竖边 24px 内即可停靠；
        # 两边都够到（屏比卡片宽不了多少）时取更近的一边
        near_l = x <= ml + 24
        near_r = x + w >= sw - 24

        # 停靠某侧屏缘时，卡片到该屏缘之间的 8px 悬空带也算「在卡片上」：
        # 鼠标贴屏幕边缘悬停（正是唤出卡片的自然位置）恰好落在这条带里，
        # 若不算，1.2s 后会被判「离开」缩边、随后又触发滑出，卡片反复弹跳
        left_bound = ml if near_l else x
        right_bound = sw if near_r else x + w
        over_card = left_bound <= px <= right_bound and y <= py <= y + h
        over_rail = x <= px <= x + w and y <= py <= y + h
        hover_target = self._slot_at(py - y) if over_rail else None
        over_panel = False
        if self._panel_visible and self._panel:
            qx, qy = self._panel.winfo_x(), self._panel.winfo_y()
            over_panel = qx <= px <= qx + PANEL_W and qy <= py <= qy + PANEL_H

        if self._hidden:
            # 细边状态：指针触及停靠侧屏缘热区（本屏内 16px）即滑出。热区
            # 严格限制在本屏之内：窗口隐藏时身体伸在对面邻屏上，若热区越过
            # 屏缘，鼠标在邻屏边缘移动就会把卡片拽出来再缩回，两屏间弹跳。
            # 拖拽期间不唤出（松手时 _drag_end 统一恢复整卡）
            if self._side == "left":
                in_zone = ml <= px < ml + 16
                reveal_x = ml + 8
            else:
                in_zone = sw - 16 <= px < sw
                reveal_x = sw - w - 8
            peek = now < self._peek_until  # 会话结束提醒：临时滑出期间视同触及
            if (peek or (in_zone and not self._dragging
                         and y - 20 <= py <= y + h + 20)):
                self._hidden = False
                self._sliding_out = True
                self._slide_to(reveal_x, on_done=self._finish_reveal)
        elif not self._dragging:
            if over_card or over_panel or self._keep_open or self._panel_pinned:
                self._leave_since = None
            elif now < self._peek_until:
                self._leave_since = None  # 提醒滑出期间不立刻缩回
            else:
                # 靠近本屏任一竖边即自动隐藏：原地缩成细边（region 裁窄 +
                # 关 Acrylic），窗口不再越出本屏可见内容，邻屏上无残影
                if (near_l or near_r) and now - self._born > BOOT_GRACE:
                    if near_l and (not near_r or x - ml <= sw - (x + w)):
                        self._side, hide_x = "left", ml - w + EDGE_STRIP
                    else:
                        self._side, hide_x = "right", sw - EDGE_STRIP
                    if self._leave_since is None:
                        self._leave_since = now
                    elif now - self._leave_since >= HIDE_DELAY:
                        self._leave_since = None
                        self._hidden = True
                        self._render()  # 先换细边视觉，再滑向边缘
                        self._slide_to(hide_x)

            if hover_target:
                self._panel_leave_since = None
                if self._panel_pinned and hover_target == self._panel_target:
                    pass  # 钉住的正是这个槽位：保持钉住态
                else:
                    if self._panel_pinned:
                        # 钉住不妨碍看其他槽位：hover 新槽位时钉住让位、详情卡切换
                        self._panel_pinned = False
                    if hover_target != self._hover_target:
                        self._hover_target = hover_target
                        self._hover_since = now
                    elif self._panel_visible:
                        if (hover_target != self._panel_target
                                and self._hover_since is not None
                                and now - self._hover_since >= PANEL_SWITCH_DELAY):
                            self._show_panel(hover_target)
                    elif (self._hover_since is not None
                            and now - self._hover_since >= PANEL_DELAY):
                        self._show_panel(hover_target)
            elif over_panel:
                self._hover_target = None
                self._hover_since = None
                self._panel_leave_since = None
            else:
                self._hover_target = None
                self._hover_since = None
                if self._panel_visible and not self._panel_pinned:
                    if self._panel_leave_since is None:
                        self._panel_leave_since = now
                    elif now - self._panel_leave_since >= PANEL_CLOSE_DELAY:
                        self._hide_panel()

        # 自绘菜单的点击外关闭：焦点未抢到（Win 前台焦点限制）时 FocusOut
        # 不一定触发，这里按物理按键 + 指针位置兜底。武装逻辑：菜单弹出时
        # 右键往往还按着（Button-3 在按下时触发），必须先检测到一次全松开，
        # 之后的「按下」才算一次新的点击，否则菜单刚开就被自己关掉（闪退感）
        if self._menu_win and self._menu_win.winfo_ismapped():
            pressed = (ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000
                       or ctypes.windll.user32.GetAsyncKeyState(0x02) & 0x8000)
            if not pressed:
                self._menu_armed = True
            elif self._menu_armed:
                mx0, my0 = self._menu_win.winfo_rootx(), self._menu_win.winfo_rooty()
                mw, mh = self._menu_win.winfo_width(), self._menu_win.winfo_height()
                if not (mx0 <= px <= mx0 + mw and my0 <= py <= my0 + mh):
                    self._dismiss_menu()

    def _slide_to(self, target_x, steps=10, on_done=None):
        """水平滑动到目标 x（ease-out cubic），贴边滑入/滑出与面板共用；
        on_done 在动画结束后回调（滑出完成后恢复整卡视觉用）"""
        start_x = self.root.winfo_x()
        y = self.root.winfo_y()

        def step(i):
            t = (i + 1) / steps
            e = 1 - (1 - t) ** 3
            self.root.geometry(f"+{round(start_x + (target_x - start_x) * e)}+{y}")
            if i + 1 < steps:
                self.root.after(16, lambda: step(i + 1))
            elif on_done:
                self.root.after(16, on_done)

        step(0)

    # 细边/整卡窗口形态 ------------------------------------------------
    def _apply_visual_mode(self, strip):
        """细边与整卡两种窗口形态切换（按需幂等）：
        - 细边：region 裁出停靠侧的 EDGE_STRIP 竖条（右停靠裁左缘、
          左停靠裁右缘）——窗口矩形虽仍为整卡大小，region 外不参与命中
          测试，伸在邻屏上的透明部分不挡鼠标；同时关 Acrylic（模糊按
          region 生效，开着会在邻屏浮出暗色玻璃块）
        - 整卡：清除硬 region，交给分层位图的 alpha 边缘抗锯齿"""
        mode = ("strip", self._side) if strip else ("full",)
        if mode == self._visual_mode:
            return
        self._visual_mode = mode
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        if strip:
            rgn_x = 0 if self._side == "right" else self._win_w - EDGE_STRIP
            rgn = gdi32.CreateRoundRectRgn(
                rgn_x, 0, rgn_x + EDGE_STRIP + 1, self._win_h + 1, 6, 6)
            if not user32.SetWindowRgn(ctypes.c_void_p(self._hwnd), rgn, True):
                gdi32.DeleteObject(rgn)
            _set_dwm_round(self._hwnd, 1)  # DONOTROUND：撤掉按窗口矩形的描边
            if self._glass:
                _unset_acrylic(self._hwnd)
        else:
            _clear_window_region(self._hwnd)
            if self._glass:
                _set_acrylic(self._hwnd)
                _set_dwm_round(self._hwnd)  # 玻璃底圆角：region 裁不到它

    def _finish_reveal(self):
        """滑出动画结束：恢复整卡 region / Acrylic 并渲染完整卡片。
        动画期间窗口尚在边缘、身体伸向邻屏，必须保持细边（透明）视觉，
        整卡图像到位后才能亮出"""
        self._sliding_out = False
        self._render()

    def _ensure_panel(self):
        """惰性创建单项详情卡片（分层窗口 + Acrylic）。"""
        if self._panel:
            return
        p = tk.Toplevel(self.root)
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        p.geometry(f"{PANEL_W}x{PANEL_H}+0+0")
        p.update_idletasks()
        hwnd = self._toplevel_hwnd(int(p.winfo_id()))
        ex = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        self._panel = p
        self._panel_hwnd = hwnd
        # 点击详情卡：钉住（Keep open）/ 解除钉住，钉住后不随鼠标离开收起
        p.bind("<Button-1>", lambda _e: self._toggle_panel_pin())
        # 与主卡片相同的顺序约束：先建立 UpdateLayeredWindow 渲染通道，
        # 再叠加 Acrylic / 圆角，否则渲染通道不生效
        blank = Image.new("RGBA", (PANEL_W, PANEL_H), (0, 0, 0, 0))
        _update_layered_window(hwnd, blank)
        self._panel_glass = _set_acrylic(hwnd) and _set_round_corners(
            hwnd, PANEL_W, PANEL_H)
        if self._panel_glass:
            _set_dwm_round(hwnd)
        p.withdraw()

    def _panel_position(self, target):
        """将局部详情卡片对齐至对应图标，并限制在当前屏幕可见区。
        展开方向随停靠侧镜像：右停靠默认从卡片左缘展开，左停靠默认右缘；
        默认侧放不下时回退到另一侧。"""
        card_x, card_y = self.root.winfo_x(), self.root.winfo_y()
        rect = self._monitor_rect()
        if not rect:
            rect = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        if self._side == "left":
            target_x = card_x + self._win_w + PANEL_GAP
            if target_x + PANEL_W > rect[2] - PANEL_GAP:
                target_x = card_x - PANEL_W - PANEL_GAP
        else:
            target_x = card_x - PANEL_W - PANEL_GAP
            if target_x < rect[0] + PANEL_GAP:
                target_x = card_x + self._win_w + PANEL_GAP
        target_y = card_y + dict(self._layout()[1]).get(target, 28) - PANEL_H // 2
        target_x = max(rect[0] + PANEL_GAP, min(target_x, rect[2] - PANEL_W - PANEL_GAP))
        target_y = max(rect[1] + PANEL_GAP, min(target_y, rect[3] - PANEL_H - PANEL_GAP))
        return target_x, target_y

    def _show_panel(self, target):
        """展开或切换对应项的局部详情卡片：macOS popover 式从被悬停槽位的
        圆环位置直接弹出（0.82→1.0 缩放 + 淡入，锚定槽位中心），不再从
        屏幕右侧滑入。"""
        self._ensure_panel()
        was_visible = self._panel_visible
        self._panel_target = target
        self._panel_visible = True
        full = self._panel_image()
        target_x, target_y = self._panel_position(target)
        p = self._panel

        # 锚点：面板贴轨一侧的边缘 + 槽位中心高度，缩放从该点长出
        #（与停靠侧无关：面板落在轨道哪侧就锚哪侧，左/右停靠均自然）
        anchor_right = target_x < self.root.winfo_x()  # 面板在轨道左侧锚右缘
        anchor_x = target_x + PANEL_W if anchor_right else target_x
        anchor_y = target_y + PANEL_H / 2

        if not was_visible:
            p.geometry(f"+{target_x}+{target_y}")
            p.deiconify()
            # 面板压到主卡片之下，滑出时不遮挡卡片
            ctypes.windll.user32.SetWindowPos(
                ctypes.c_void_p(self._panel_hwnd), ctypes.c_void_p(self._hwnd),
                0, 0, 0, 0, 0x1 | 0x2)  # SWP_NOSIZE | SWP_NOMOVE

        self._panel_animating = True

        def step(i, steps=7):
            if not self._panel_visible or self._panel_target != target:
                self._panel_animating = False
                return
            t = (i + 1) / steps
            e = 1 - (1 - t) ** 3
            k = 0.82 + 0.18 * e
            w, h = max(1, round(PANEL_W * k)), max(1, round(PANEL_H * k))
            frame = full.resize((w, h), Image.LANCZOS)
            if e < 0.999:
                r, g, b, a = frame.split()
                a = a.point(lambda v: int(v * e))
                frame = Image.merge("RGBA", (r, g, b, a))
            x = round(anchor_x - w) if anchor_right else round(anchor_x)
            y = round(anchor_y - h / 2)
            p.geometry(f"+{x}+{y}")
            _update_layered_window(self._panel_hwnd, frame)
            if i + 1 < steps:
                self.root.after(16, lambda: step(i + 1))
            else:
                self._panel_animating = False
                p.geometry(f"+{target_x}+{target_y}")
                self._render_panel()

        step(0)

    def _toggle_panel_pin(self):
        """点击详情卡切换钉住态：钉住后不随鼠标离开收起，再点解除并立即收起。"""
        if not self._panel_visible:
            return
        self._panel_pinned = not self._panel_pinned
        if self._panel_pinned:
            self._render_panel()  # 画出钉住标记
        else:
            self._hide_panel()

    def _hide_panel(self):
        self._panel_visible = False
        self._panel_target = None
        self._panel_pinned = False
        # _hover_since 与 _hover_target 必须同生同灭：只清前者的话，
        # 「面板弹出→按下拖拽(_drag_start 收面板)→松手指针仍停在原槽位」
        # 会让 hover_target == _hover_target 而 _hover_since 为 None，
        # _poll_hover_once 里 now - None 每 150ms 炸一次 TypeError
        self._hover_target = None
        self._hover_since = None
        self._panel_leave_since = None
        if self._panel:
            self._panel.withdraw()

    def _panel_image(self):
        """构建当前详情目标的完整尺寸卡片图像（精确数值用目标值而非动画中间值）。"""
        p = self._pomo
        if p["stage"] == "off":
            detail = f"下次开工 {p['next_time']}"
        else:
            m, s = divmod(max(0, p["remaining"]), 60)
            detail = f"{m:02d}:{s:02d}"
        target = self._panel_target
        codex_slug = target.split(":", 1)[1] if target.startswith("codex") and ":" in target else ""
        codex = self._codex.get(codex_slug, {}) if target.startswith("codex") else {}
        info = {
            "glm_remaining": self._token_remaining,
            "glm_reset_ts": self._quota_reset_ts,
            "glm_weekly": self._weekly_remaining,
            "kimi_remaining": self._kimi_remaining,
            "kimi_reset_ts": self._kimi_reset_ts,
            "kimi_monthly": self._kimi_monthly_remaining,
            "codex_remaining": codex.get("remaining"),
            "codex_weekly_used": (codex.get("weekly") or (0.0, 0.0))[0],
            "codex_weekly_limit": (codex.get("weekly") or (0.0, 0.0))[1],
            "codex_reset_ts": codex.get("reset_ts", 0),
            "codex_today_cost": codex.get("today_cost"),
            "mimo_remaining": self._mimo_remaining,
            "mimo_reset_ts": self._mimo_reset_ts,
            "mimo_monthly": self._mimo_monthly_remaining,
            "mimo_plan": self._mimo_plan,
            "pomo_label": p["label"],
            "pomo_detail": detail,
            "pomo_progress": p["progress"],
            "pomo_stage": p["stage"],
            "last_refresh": self._last_refresh,
            "reset_clock": self._reset_clock,
            "pinned": self._panel_pinned,
        }
        return create_detail_image(info, target, glass=self._panel_glass)

    def _render_panel(self):
        """重绘详情面板内容；弹出/切换动画进行中不干扰（动画结束帧自会落地）。"""
        if not self._panel_visible or not self._panel or not self._panel_target:
            return
        if getattr(self, "_panel_animating", False):
            return
        _update_layered_window(self._panel_hwnd, self._panel_image())

    def _render(self):
        disp = self._disp
        values = {
            "glm": disp.get("glm", self._token_remaining),
            "kimi": (disp.get("kimi", self._kimi_remaining)
                     if self._kimi_remaining is not None else None),
            "mimo": (disp.get("mimo", self._mimo_remaining)
                     if self._mimo_remaining is not None else None),
        }
        for slug, c in self._codex.items():
            key = "codex" if not slug else f"codex:{slug}"
            values[key] = (disp.get(key, c["remaining"])
                           if c["remaining"] is not None else None)
        if self._hidden or self._sliding_out:
            img = _create_strip_image(self._pomo["progress"], side=self._side,
                                      height=self._win_h)
        else:
            img = create_widget_image(values, self._pomo, self._dim,
                                      activity=self._activity, glass=self._glass,
                                      slots=self._visible_keys())
        # 细边/整卡窗口形态（region + Acrylic）随图像一起切换，幂等
        self._apply_visual_mode(self._hidden or self._sliding_out)
        _update_layered_window(self._hwnd, img)
        p = self._pomo
        if p["stage"] == "off":
            detail = f"{p['label']}，下次开工 {p['next_time']}"
        else:
            m, s = divmod(max(0, p["remaining"]), 60)
            detail = f"{p['label']} {m:02d}:{s:02d}"
        parts = []
        if "glm" in self._dead:
            parts.append("GLM: API 无响应，图标已暂时摘除（恢复后自动回归）")
        else:
            parts.append(f"GLM 短期剩余: {self._token_remaining:.0f}%")
            if self._quota_reset_ts:
                left = max(0, self._quota_reset_ts - time.time())
                h, rem = divmod(int(left), 3600)
                parts[0] += f"（{h}h{rem // 60:02d}m 后重置）"
            parts.append(f"GLM 周额度剩余: {self._weekly_remaining:.0f}%")
        if "kimi" in self._dead:
            parts.append("Kimi: 无响应，图标已暂时摘除（恢复后自动回归）")
        elif self._kimi_remaining is None:
            parts.append("Kimi: 无数据（未检测到 Kimi 本地服务）")
        else:
            kp = f"Kimi 短期剩余: {self._kimi_remaining:.0f}%"
            if self._kimi_reset_ts:
                left = max(0, self._kimi_reset_ts - time.time())
                h, rem = divmod(int(left), 3600)
                kp += f"（{h}h{rem // 60:02d}m 后重置）"
            parts.append(kp)
            if self._kimi_monthly_remaining is not None:
                parts.append(f"Kimi 月额度剩余: {self._kimi_monthly_remaining:.0f}%")
        for slug, c in self._codex.items():
            name = "Codex" if not slug else f"Codex·{slug}"
            if self._codex_key(slug) in self._dead:
                parts.append(f"{name}: 无响应，图标已暂时摘除（恢复后自动回归）")
                continue
            if c["remaining"] is None:
                parts.append(f"{name}: 无数据（未配置中转 / 密钥失效 / 网络失败）")
                continue
            cp = (f"{name} 周预算剩余: {c['remaining']:.0f}%"
                  f"（${c['weekly'][0]:.1f}/${c['weekly'][1]:.0f}）")
            if c["reset_ts"]:
                left = max(0, c["reset_ts"] - time.time())
                days, rem = divmod(int(left), 86400)
                cp += f"（预计 {days}天{rem // 3600}h 后重置）"
            parts.append(cp)
            if c["today_cost"] is not None:
                daily = ("（无日限额）" if c["unrestricted"] else "")
                parts.append(f"{name} 今日已用: ${c['today_cost']:.2f}{daily}")
            if c["monthly"] is not None:
                parts.append(f"{name} 月额度剩余: {c['monthly']:.0f}%")
        if "mimo" in self._dead:
            parts.append("MiMo: 无响应，图标已暂时摘除（恢复后自动回归）")
        elif self._mimo_remaining is None:
            parts.append("MiMo: 无数据（config.json 未配 mimo 段 / 端点不可达）")
        else:
            mp = f"MiMo 套餐剩余: {self._mimo_remaining:.0f}%"
            if self._mimo_plan:
                mp += f"（{self._mimo_plan}）"
            if self._mimo_reset_ts:
                left = max(0, self._mimo_reset_ts - time.time())
                h, rem = divmod(int(left), 3600)
                mp += f"（{h}h{rem // 60:02d}m 后重置）"
            parts.append(mp)
            if self._mimo_monthly_remaining is not None:
                parts.append(f"MiMo 月额度剩余: {self._mimo_monthly_remaining:.0f}%")
        parts.append(f"番茄钟 {detail}")
        self.root.tooltip_text = " | ".join(parts)
        if self._panel_visible:
            self._render_panel()

    # 数值缓动动画 ------------------------------------------------
    def _set_targets(self, **kw):
        """登记动画目标值并启动 30fps 缓动循环（已在运行则只更新目标）"""
        self._targets.update(kw)
        if not self._animating:
            self._animating = True
            self._animate_tick()

    def _animate_tick(self):
        """指数趋近（ease-out 观感）：每帧向目标靠拢 22%，收敛后吸附并停止"""
        for key, tgt in list(self._targets.items()):
            cur = self._disp.get(key, tgt)
            nxt = cur + (tgt - cur) * 0.22
            if abs(tgt - nxt) < 0.2:
                nxt = tgt
                del self._targets[key]
            self._disp[key] = nxt
        self._render()
        if self._targets:
            self.root.after(33, self._animate_tick)
        else:
            self._animating = False

    # 番茄钟 -------------------------------------------------------
    def _pomo_tick(self):
        """番茄钟每秒循环（自愈守护，同 _poll_hover）"""
        try:
            self._pomo_tick_once()
        except Exception:
            traceback.print_exc()
        finally:
            self.root.after(1000, self._pomo_tick)

    def _pomo_tick_once(self):
        """每秒从墙钟推导番茄钟状态；阶段跃迁（工作↔休息、时段开关）时通知+闪烁"""
        old, self._pomo = self._pomo, _pomo_state()
        if old["stage"] != self._pomo["stage"]:
            self._on_stage_edge(old, self._pomo)
            self._start_blink()
        self._render()

    def _on_stage_edge(self, old, new):
        """阶段跃迁沿的通知分发（每条沿只触发一次，启动首帧不通知）"""
        if old["stage"] == "work" and new["stage"] == "rest":
            notify_windows("休息时间到", "45 分钟工作完成，休息 15 分钟～放松一下！")
        elif old["stage"] == "rest" and new["stage"] == "work":
            notify_windows("工作时间到", "休息结束，开始下一个 45 分钟工作周期！")
        elif new["stage"] == "work":  # 非工作时段 → 工作：上/下午开工
            part = "上午" if datetime.now().hour < 12 else "下午"
            notify_windows(f"{part}开工", f"{part}工作时段开始，45 分钟工作周期启动！")
        elif new["stage"] == "off":  # 工作 → 非工作时段：午休 / 下班
            if new["label"] == "午休":
                notify_windows("上午结束", "午休时间，13:30 继续～")
            elif new["label"] == "下班":
                notify_windows("今日工作结束", "下班啦，明天 9:00 见！")

    def _start_blink(self):
        """阶段切换闪烁：6 秒内 3 次正弦调光（0.1s/帧），替代生硬开关式闪烁"""
        self._blink_t = 0
        if not self._blinking:
            self._blinking = True
            self._blink_step()

    def _blink_step(self):
        if self._blink_t >= 60:
            self._dim = 0.0
            self._blinking = False
            self._render()
            return
        self._blink_t += 1
        self._dim = abs(math.sin(math.pi * self._blink_t / 20))
        self._render()
        self.root.after(100, self._blink_step)

    # Token 数据刷新 -------------------------------------------------------
    @staticmethod
    def _codex_key(slug):
        return "codex" if not slug else f"codex:{slug}"

    def _spawn_fetch(self, source, fetch, apply):
        """单源独立线程拉取、独立落地；429 退避期内的源直接跳过。
        不负责下一轮调度（由 _do_refresh / _refresh_source 统一经 _schedule）。"""
        if not _gate_allows(source):
            return

        def run():
            try:
                result = fetch()
            except Exception:
                # 抓取线程里的未捕获异常（如向已失效的管道 print）不能杀死
                # 该源的落地回调，否则对应数据源永远停在初始值
                traceback.print_exc()
                result = None
            self.root.after(0, lambda: apply(result))
        threading.Thread(target=run, daemon=True).start()

    def _do_refresh(self):
        """全源刷新：各自独立线程拉取（任一源超时不拖累其余源 UI 更新）。"""
        self._refresh_source("glm", schedule=False)
        self._refresh_source("kimi", schedule=False)
        self._refresh_source("mimo", schedule=False)
        for slug, home in CODEX_PROFILES:
            self._refresh_source(self._codex_key(slug), schedule=False)
        self._schedule()

    def _refresh_source(self, key, schedule=True):
        """点击槽位刷新对应源（借鉴 Codenotch 点环重拉该 provider）。
        pomo 槽无远程数据源，仅重绘。"""
        if key == "glm":
            self._spawn_fetch("glm", fetch_usage, self._apply_glm_usage)
        elif key == "kimi":
            self._spawn_fetch("kimi", fetch_kimi_usage, self._apply_kimi_usage)
        elif key == "mimo":
            self._spawn_fetch("mimo", fetch_mimo_usage, self._apply_mimo_usage)
        elif key and key.startswith("codex"):
            slug = key.split(":", 1)[1] if ":" in key else ""
            home = dict(CODEX_PROFILES).get(slug)
            if home is None:
                return
            self._spawn_fetch(
                key,
                lambda: fetch_codex_usage(home, key),
                lambda result, s=slug: self._apply_codex_usage(result, s))
        elif key == "pomo":
            self._render()
        else:
            return
        if schedule:
            self._schedule()

    def _check_alert(self, key, name, remaining):
        """额度阈值告警（借鉴 Codenotch）：已用比例跨越 80% / 100% 时通知，
        每次跨越只提醒一次；用量明显回落（窗口滚动/额度回满）后自动复位，
        下次跨越再提醒。告警带随 ui_state.json 持久化。"""
        if remaining is None:
            return
        used = 100 - remaining
        crossed = 100 if used >= 99.5 else (80 if used >= 80 else 0)
        band = self._alert_bands.get(key, 0)
        if crossed > band:
            self._alert_bands[key] = crossed
            if crossed >= 100:
                notify_windows(f"{name} 额度已用尽",
                               "限额已达 100%，等窗口重置后再冲～")
            else:
                notify_windows(f"{name} 额度告急",
                               f"已用超过 80%，剩余 {remaining:.0f}%，注意节奏～")
            self._save_state(throttle=True)
        elif used < 72 and band:
            self._alert_bands[key] = crossed
            self._save_state(throttle=True)

    def _apply_glm_usage(self, result):
        """UI 线程应用 GLM 抓取结果：短期额度平滑过渡，详情值直接更新。"""
        self._last_refresh = datetime.now().strftime("%H:%M:%S")
        self._note_fetch_result("glm", bool(result))
        if result:
            short_term_pct = result.get("short_term_percentage")
            if short_term_pct is not None:
                self._token_remaining = min(max(100 - short_term_pct, 0), 100)
                self._set_targets(glm=self._token_remaining)
                self._quota_reset_ts = result.get("short_term_reset_ts", 0)
                self._check_alert("glm", "GLM 短期", self._token_remaining)

            weekly_pct = result.get("weekly_percentage")
            if weekly_pct is not None:
                self._weekly_remaining = min(max(100 - weekly_pct, 0), 100)
            self._save_state(throttle=True)
        self._render()

    def _apply_kimi_usage(self, kimi):
        """UI 线程应用 Kimi 抓取结果；daemon 不在跑时不清空既有读数，
        从未有过数据则保持 None（空轨道）。"""
        self._last_refresh = datetime.now().strftime("%H:%M:%S")
        self._note_fetch_result("kimi", bool(kimi))
        if kimi:
            self._kimi_remaining = min(
                max(100 - kimi["short_term_percentage"], 0), 100)
            self._set_targets(kimi=self._kimi_remaining)
            self._kimi_reset_ts = kimi["short_term_reset_ts"]
            monthly_pct = kimi.get("monthly_percentage")
            if monthly_pct is not None:
                self._kimi_monthly_remaining = min(
                    max(100 - monthly_pct, 0), 100)
            self._check_alert("kimi", "Kimi 短期", self._kimi_remaining)
            self._save_state(throttle=True)
        self._render()

    def _apply_mimo_usage(self, mimo):
        """UI 线程应用 MiMo 抓取结果；端点未配置 / 不可达时保持既有读数，
        从未有过数据则保持 None（虚线空环，不伪造读数）。"""
        self._last_refresh = datetime.now().strftime("%H:%M:%S")
        self._note_fetch_result("mimo", bool(mimo))
        if mimo:
            self._mimo_remaining = mimo["remaining"]
            self._set_targets(mimo=self._mimo_remaining)
            self._mimo_reset_ts = mimo["reset_ts"]
            self._mimo_monthly_remaining = mimo["monthly_remaining"]
            self._mimo_plan = mimo["plan"]
            self._check_alert("mimo", "MiMo 套餐", self._mimo_remaining)
            self._save_state(throttle=True)
        self._render()

    def _apply_codex_usage(self, codex, slug=""):
        """UI 线程应用 Codex 抓取结果；失败时保持既有读数，从未有过数据
        则保持 None（虚线空环，不伪造读数）。slug 区分多账号。"""
        self._last_refresh = datetime.now().strftime("%H:%M:%S")
        self._note_fetch_result(self._codex_key(slug), bool(codex))
        if codex:
            c = self._codex.setdefault(slug, {})
            c["remaining"] = codex["weekly_remaining"]
            self._set_targets(**{self._codex_key(slug): c["remaining"]})
            c["reset_ts"] = codex["weekly_reset_ts"]
            c["weekly"] = (codex["weekly_used_usd"], codex["weekly_limit_usd"])
            c["today_cost"] = codex["today_cost_usd"]
            c["monthly"] = codex["monthly_remaining"]
            c["unrestricted"] = codex["unrestricted"]
            name = "Codex 周预算" if not slug else f"Codex·{slug} 周预算"
            self._check_alert(self._codex_key(slug), name, c["remaining"])
            self._save_state(throttle=True)
        self._render()

    def _schedule(self):
        """调度下一轮全源刷新：有会话活跃（busy/blocked）按 5 分钟，
        全部源空闲时降频到 10 分钟（对齐 Codenotch 的空闲降频策略）。
        全局只保留一条刷新链：点击单源刷新重置周期，不叠加并行链。"""
        if self._refresh_timer is not None:
            try:
                self.root.after_cancel(self._refresh_timer)
            except Exception:
                pass
        active = any(state in ("busy", "blocked")
                     for state in self._activity.values())
        interval = REFRESH_INTERVAL if active else IDLE_REFRESH_INTERVAL
        self._refresh_timer = self.root.after(interval * 1000, self._do_refresh)

    # 会话闲忙状态 -------------------------------------------------------
    def _activity_loop(self):
        """后台线程：每 2 秒扫描各源会话文件信号，结果回 UI 线程落地。"""
        while True:
            try:
                states = scan_activity_states()
                self.root.after(0, lambda s=states: self._apply_activity(s))
            except Exception as exc:
                print(f"活动扫描异常: {exc}")
            time.sleep(ACTIVITY_SCAN_INTERVAL)

    def _apply_activity(self, states):
        """UI 线程应用闲忙状态；busy→非忙跳变触发会话结束提醒
        （主轨临时滑出 5 秒 + 系统提示音，对齐 Codenotch 的结束即提醒）。
        首次扫描只建立基线，不把既有会话当成「刚结束」。"""
        if not self._activity:
            self._activity = states
        else:
            names = {"glm": "GLM", "kimi": "Kimi"}
            for key, new in states.items():
                old = self._activity.get(key)
                self._activity[key] = new
                if old == "busy" and new != "busy":
                    name = names.get(key)
                    if name is None:
                        slug = key.split(":", 1)[1] if ":" in key else ""
                        name = "Codex" if not slug else f"Codex·{slug}"
                    self._on_session_end(name, waiting=(new == "blocked"))
        self._ensure_activity_anim()
        self._render()

    def _on_session_end(self, name, waiting):
        """会话离开忙态：临时滑出主轨 5 秒 + 提示音 + toast 说明。"""
        self._peek_until = time.time() + 5
        try:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except RuntimeError:
            pass
        if waiting:
            notify_windows(f"{name} 在等你", "会话暂停，可能在等待你的输入～")
        else:
            notify_windows(f"{name} 会话结束", "agent 已停止工作，去看看结果吧～")

    def _ensure_activity_anim(self):
        """闲忙叠加动画：有源忙/阻塞时 100–120ms 一帧驱动呼吸灯柔光，
        全部空闲即停；相位取墙钟，帧率无关。"""
        if self._activity_anim:
            return
        if not any(state in ("busy", "blocked") for state in self._activity.values()):
            return
        self._activity_anim = True
        self._activity_anim_tick()

    def _activity_anim_tick(self):
        states = set(self._activity.values())
        if not states & {"busy", "blocked"}:
            self._activity_anim = False
            return
        self._render()
        delay = 100 if "busy" in states else 120
        self.root.after(delay, self._activity_anim_tick)

    # 显示开关 ---------------------------------------------------------
    def _ensure_menu_win(self):
        """惰性创建自绘菜单窗口（分层窗口，PIL 逐像素 alpha + 投影）。"""
        if self._menu_win:
            return
        m = tk.Toplevel(self.root)
        m.overrideredirect(True)
        m.attributes("-topmost", True)
        m.geometry("10x10+0+0")
        m.update_idletasks()
        hwnd = self._toplevel_hwnd(int(m.winfo_id()))
        ex = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        _clear_window_region(hwnd)
        _set_dwm_round(hwnd, 1)
        self._menu_win = m
        self._menu_hwnd = hwnd
        # 与主卡片/详情卡相同的顺序约束：先建立 UpdateLayeredWindow 渲染通道
        blank = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
        _update_layered_window(hwnd, blank)
        m.bind("<Motion>", self._menu_motion)
        m.bind("<Button-1>", self._menu_click)
        m.bind("<Escape>", lambda _e: self._dismiss_menu())
        m.bind("<FocusOut>", lambda _e: self._dismiss_menu())
        m.withdraw()

    def _menu_index_at(self, y):
        """窗口局部 y → 菜单项索引；命中分隔线或空白返回 -1。"""
        if not self._menu_geo:
            return -1
        ty = MENU_SHADOW + MENU_PAD_Y
        for i, text in enumerate(self._menu_items):
            h = MENU_SEP_H if text is None else MENU_ITEM_H
            if ty <= y < ty + h:
                return i if text is not None else -1
            ty += h
        return -1

    def _menu_motion(self, event):
        idx = self._menu_index_at(event.y)
        if idx != self._menu_hover:
            self._menu_hover = idx
            img, _ = create_menu_image(self._menu_items, hover=idx)
            _update_layered_window(self._menu_hwnd, img)

    def _menu_click(self, event):
        idx = self._menu_index_at(event.y)
        if idx < 0:
            return
        cmd = self._menu_cmds[idx]
        self._dismiss_menu()
        if cmd:
            cmd()

    def _dismiss_menu(self):
        if self._menu_win:
            self._menu_hover = -1
            self._menu_armed = False
            self._menu_win.withdraw()

    def _popup_menu(self, event):
        """右键菜单：开关项文案随状态生成；弹出在插件与详情卡左侧
        （水平错开不遮挡二者），垂直方向随点击点、夹取在屏幕可见区内。"""
        self._ensure_menu_win()
        self._menu_items = [
            "立即刷新",
            "恢复隐藏" if self._keep_open else "保持展开",
            "重置时间显示倒计时" if self._reset_clock else "重置时间显示时刻",
            "开机自启（点击切换）",
            None,
            "退出",
        ]
        self._menu_cmds = [
            self._do_refresh,
            self._toggle_keep_open,
            self._toggle_reset_clock,
            self._toggle_autostart,
            None,
            self._quit,
        ]
        self._menu_hover = -1
        self._menu_armed = False  # 重新武装：本次右键松开前不关菜单
        img, geo = create_menu_image(self._menu_items)
        self._menu_geo = geo
        card_w, card_h, shadow = geo
        win_w, win_h = img.size
        rect = (self._monitor_rect()
                or (0, 0, self.root.winfo_screenwidth(),
                    self.root.winfo_screenheight()))
        # 卡片右缘贴到插件（或详情卡）左缘 - PANEL_GAP；窗口左缘再让出阴影
        x = self.root.winfo_x() - card_w - PANEL_GAP
        if self._panel_visible and self._panel:
            x = min(x, self._panel.winfo_x() - card_w - PANEL_GAP)
        x = max(rect[0] + 4 + shadow, x) - shadow
        y = event.y_root - shadow - MENU_PAD_Y - 2
        y = max(rect[1] + 4, min(y, rect[3] - win_h - 8))
        m = self._menu_win
        m.geometry(f"{win_w}x{win_h}+{x}+{y}")
        m.deiconify()
        _update_layered_window(self._menu_hwnd, img)
        m.focus_set()

    def _toggle_keep_open(self):
        """保持展开（Codenotch 的 Keep open）：开启后不再贴边自动隐藏。"""
        self._keep_open = not self._keep_open
        self._save_state()
        notify_windows("保持展开已开启" if self._keep_open else "保持展开已关闭",
                       "状态轨不再自动隐藏" if self._keep_open
                       else "空闲时将重新自动贴边隐藏")

    def _toggle_reset_clock(self):
        """重置时间显示模式：倒计时（3h05m 后重置）⇄ 时刻（14:30 重置）。"""
        self._reset_clock = not self._reset_clock
        self._save_state()
        if self._panel_visible:
            self._render_panel()

    # 开机自启 -------------------------------------------------------
    def _toggle_autostart(self):
        was_on = is_autostart_enabled()
        if set_autostart(not was_on):
            if was_on:
                notify_windows("开机自启已关闭", "不再开机自动启动仪表盘")
            else:
                notify_windows("开机自启已开启", "登录 Windows 后将自动启动仪表盘")
        else:
            notify_windows("开机自启设置失败", "请检查注册表写权限后重试")

    # 退出 -----------------------------------------------------------
    def _quit(self):
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] exit: 用户菜单退出")  # 区分正常退出与被外部杀死
        self._save_state()
        if self._menu_win:
            self._menu_win.destroy()
        if self._panel:
            self._panel.destroy()
        self.root.quit()
        self.root.destroy()

    # 启动 -----------------------------------------------------------
    def run(self):
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] start: pid={os.getpid()}")  # 会话分界，便于事后定位闪退点
        register_aumid()                          # 注册通知应用 ID
        self.root.after(500, self._do_refresh)    # Token 刷新
        self.root.after(1000, self._pomo_tick)    # 番茄钟启动
        self.root.after(150, self._poll_hover)    # 贴边隐藏 / 详情面板轮询
        threading.Thread(target=self._activity_loop, daemon=True).start()  # 会话闲忙扫描
        self.root.mainloop()


def _ensure_single_instance():
    """命名互斥锁保证全局只运行一个实例；已有实例时静默退出。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.restype = ctypes.c_void_p
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    mutex = create_mutex(None, False, "Global\\GlmDashboard_SingleInstance")
    # CreateMutex 返回 0 为失败；名称已存在时 last error == ERROR_ALREADY_EXISTS(183)
    if not mutex or ctypes.get_last_error() == 183:
        sys.exit(0)
    return mutex


if __name__ == "__main__":
    _single_mutex = _ensure_single_instance()  # 持有引用，进程退出前互斥锁不释放

    # --- presence_lock: 摄像头人脸识别离座锁屏(作为守护线程嵌入, 不再单独挂进程) ---
    try:
        _presence_dir = r"E:\GIT_Space\safety-precautions"
        if _presence_dir not in sys.path:
            sys.path.insert(0, _presence_dir)
        import presence_lock as _presence
        threading.Thread(target=_presence.run_service, kwargs={"camera": None},
                         name="presence-lock", daemon=True).start()
    except Exception:
        traceback.print_exc()  # 锁屏服务失败不影响 dashboard 本体
    # ---------------------------------------------------------------------------

    GLMWidget().run()

"""GLM + Kimi 用量悬浮小组件 + 番茄工作闹钟 — Acrylic 毛玻璃竖版侧栏贴靠屏幕右缘

Win11 Acrylic 真毛玻璃卡片：上段三列用量——TOKEN 竖排标签、GLM 三竖条（短期剩余 /
5h 窗口重置倒计时 / 周剩余）、KIMI 三竖条（短期剩余 / 5h 窗口重置倒计时 /
月剩余，读 Kimi Code CLI 本地 daemon）；下段番茄钟倒计时 + 细进度条。
空闲自动缩成右缘微光细边，悬停展开详情面板；数值变化带缓动动效。
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
import tkinter as tk
import winreg
from datetime import datetime, time as dtime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image, ImageChops, ImageDraw, ImageFont
from winotify import Notification

# pythonw 在无控制台环境（任务计划程序 / 开机自启）下 sys.stdout/stderr 为 None，
# 此时 print() 会抛 AttributeError 导致进程崩溃；重定向到项目内 dashboard.log，
# 既规避崩溃，又保留运行期日志（API 错误 / 未捕获异常 traceback）便于故障定位。
_LOG_PATH = Path(__file__).resolve().parent / "dashboard.log"
if sys.stdout is None:
    sys.stdout = open(_LOG_PATH, "a", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(_LOG_PATH, "a", encoding="utf-8")

REFRESH_INTERVAL = 300  # Token 刷新：5 分钟
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"  # 项目本地独立配置（脱离 cc 的兜底）
# ZCode 的 API 配置（最高优先级，自动跟随其更新）：
#   cli/config.json  记录当前选中供应商 model.providerId
#   v2/config.json   存各供应商的 apiKey / baseURL（provider[<id>].options）
ZCODE_CLI_CONFIG_PATH = Path.home() / ".zcode" / "cli" / "config.json"
ZCODE_PROVIDERS_PATH = Path.home() / ".zcode" / "v2" / "config.json"
ICON_PATH = Path(__file__).resolve().parent / "tomato.ico"  # AUMID 应用图标
ICON_PNG = Path(__file__).resolve().parent / "tomato.png"   # toast 内联图标
UI_STATE_PATH = Path(__file__).resolve().parent / "ui_state.json"  # 停靠位置记忆
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
    会在右侧邻屏上浮现一块暗色玻璃矩形。"""
    policy = _ACCENT_POLICY(0, 0, 0, 0)  # ACCENT_DISABLED
    data = _WCA_DATA(
        _WCA_ACCENT_POLICY,
        ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p),
        ctypes.sizeof(policy),
    )
    return bool(ctypes.windll.user32.SetWindowCompositionAttribute(
        ctypes.c_void_p(hwnd), ctypes.byref(data)))


def _set_round_corners(hwnd, w, h):
    """用 SetWindowRgn 裁剪圆角（只作用于分层位图内容与命中测试）。
    注意：region 裁不到 SetWindowCompositionAttribute 绘制的 Acrylic 模糊底
    （模糊底按窗口矩形方形绘制），必须配合 _set_dwm_round 才能得到圆角玻璃。"""
    rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1,
                                                 CORNER_RADIUS * 2,
                                                 CORNER_RADIUS * 2)
    if not rgn:
        return False
    ok = ctypes.windll.user32.SetWindowRgn(ctypes.c_void_p(hwnd), rgn, True)
    if not ok:
        ctypes.windll.gdi32.DeleteObject(rgn)
    return bool(ok)


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
            data = body.get("data") or body
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
        except (URLError, OSError, json.JSONDecodeError):
            continue
        data = body.get("data") or {}
        if data.get("kind") != "ok":
            continue
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


# ── UI 规格（Apple 风格竖版侧栏，与 cc cli 研讨定稿：iOS 暗色系统色 + 发丝线）──
WIDGET_W, WIDGET_H = 140, 192   # 最终窗口尺寸（逻辑像素）；v1.21 加宽容纳三列
SS = 3                          # 超采样倍率：先画 3 倍大图再 LANCZOS 缩小抗锯齿
CARD_BG = (28, 28, 30, 190)     # #1C1C1E @75%，iOS secondarySystemBackground(dark)
HAIRLINE = (255, 255, 255, 26)  # 发丝描边 / 分隔线
TOP_LIGHT = (255, 255, 255, 38)  # 顶部内高光（伪玻璃上光）
TEXT_MAIN = (255, 255, 255, 255)
TEXT_SUB = (255, 255, 255, 185)
TEXT_DIM = (255, 255, 255, 100)
BAR_TRACK = (255, 255, 255, 36)
GREEN = (48, 209, 88, 255)      # iOS systemGreen：Token ≥40%
ORANGE = (255, 159, 10, 255)    # iOS systemOrange：15–40%
RED = (255, 69, 58, 255)        # iOS systemRed：<15%（数字同步染红）
FOCUS_COLOR = (191, 90, 242, 255)   # Apple 专注紫：工作阶段
REST_COLOR = (100, 210, 255, 255)   # teal：休息阶段
OFF_COLOR = (142, 142, 147, 255)    # iOS systemGray：非工作时段（待机/午休/下班/假日）
YELLOW = (255, 214, 10, 255)        # iOS systemYellow：窗口重置倒计时填充

NUM_FONT = ("seguisb.ttf",)   # Segoe UI Semibold（数字，等宽步进不抖）
BOLD_FONT = ("segoeuib.ttf",)  # Segoe UI Bold（TOKEN 标签）
# 中文：思源黑体可变字体（NotoSansSC-VF，wght=700），缺失时回退微软雅黑/黑体
ZH_FONT = ("NotoSansSC-VF.ttf", "msyhbd.ttc", "msyh.ttc", "simhei.ttf")
ZH_WEIGHT = 700

# 贴边隐藏 / 详情面板交互
EDGE_STRIP = 6      # 贴边隐藏后露出的细边宽度（px）
HIDE_DELAY = 1.2    # 鼠标离开后多少秒滑入右缘
PANEL_DELAY = 0.4   # 悬停多少秒展开详情面板
PANEL_W = 200       # 详情面板宽度
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


def _draw_tabular_timer(d, cx, cy, remaining_sec, font, dim):
    """等宽步进逐字绘制 MM:SS（伪 tabular，秒针跳动零抖动），冒号每秒呼吸。
    dim∈[0,1]：阶段切换闪烁时整体柔和淡出（正弦调光）"""
    m, s = divmod(min(remaining_sec, 99 * 60 + 59), 60)
    text = f"{m:02d}:{s:02d}"
    digit_w = font.getlength("0")
    colon_w = font.getlength(":")
    total = 4 * digit_w + colon_w
    colon_alpha = 150 if remaining_sec % 2 else 255  # 冒号每秒呼吸
    fade = 1 - 0.73 * dim
    x = cx - total / 2
    for ch in text:
        cell = colon_w if ch == ":" else digit_w
        alpha = int((colon_alpha if ch == ":" else 255) * fade)
        bbox = d.textbbox((0, 0), ch, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(
            (x + (cell - w) / 2 - bbox[0], cy - h / 2 - bbox[1]),
            ch, font=font, fill=(255, 255, 255, alpha),
        )
        x += cell


def _draw_capsule(d, cx, y, length, height, frac, color, track=BAR_TRACK):
    """横向细胶囊进度条：轨道 + 按比例填充（iOS 锁屏电量条形态）"""
    x = cx - length / 2
    d.rounded_rectangle([x, y, x + length, y + height],
                        radius=int(height / 2), fill=track)
    if frac > 0.01:
        fill_w = max(length * frac, height)
        d.rounded_rectangle([x, y, x + fill_w, y + height],
                            radius=int(height / 2), fill=color)


def _draw_vertical_capsule(d, x, y, width, height, frac, color, track=BAR_TRACK):
    """竖向细胶囊进度条：填充自下向上，用于弱化呈现周额度已用比例。"""
    d.rounded_rectangle([x, y, x + width, y + height],
                        radius=int(width / 2), fill=track)
    if frac > 0.01:
        fill_h = max(height * frac, width)
        d.rounded_rectangle([x, y + height - fill_h, x + width, y + height],
                            radius=int(width / 2), fill=color)


def _draw_column_header(d, cx, title, pct, s):
    """列顶单行小标题 + 短期剩余小数字（如 GLM 99）：无数据时数字显示 --"""
    title_font = _load_font(BOLD_FONT, 9 * s)
    num_font = _load_font(NUM_FONT, 12 * s)
    num = "--" if pct is None else f"{int(pct)}"
    color = TEXT_DIM if pct is None else (
        RED if pct < 15 else TEXT_MAIN
    )
    tw = title_font.getlength(title)
    nw = num_font.getlength(num)
    gap = 3.5 * s
    x = cx - (tw + gap + nw) / 2
    baseline = 25 * s
    d.text((x, baseline), title, font=title_font, fill=TEXT_SUB, anchor="ls")
    d.text((x + tw + gap, baseline), num, font=num_font, fill=color, anchor="ls")


def _pulse_white(color, pulse):
    """按脉冲幅度向白色混合填充色（整分心跳用，峰值混 60% 白）"""
    if pulse <= 0:
        return color
    return tuple(int(c + (255 - c) * 0.6 * pulse) for c in color[:3]) + (color[3],)


def create_widget_image(glm, kimi, pomo, dim, glass=False):
    """生成竖版悬浮窗图像：三列用量（TOKEN 竖排标签 / GLM 三竖条 / KIMI 三竖条）
    + 番茄钟。glm/kimi 为视图 dict（remaining / reset_frac / pulse，glm 另有 weekly、
    kimi 另有 monthly），kimi["remaining"] 为 None 表示无数据（daemon 未跑）。
    glass=True 时卡片底由 DWM Acrylic 提供，只画内容与发丝线；False 回退 PIL 自绘卡片。"""
    s = SS
    cw, ch = WIDGET_W * s, WIDGET_H * s
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if glass:
        # Acrylic 提供模糊底，外形由 SetWindowRgn 圆角裁剪（CORNER_RADIUS），
        # 只补一条贴合的发丝描边与顶部内高光
        d.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=CORNER_RADIUS * s,
                            outline=HAIRLINE, width=2)
        d.line([(16 * s, 2 * s), ((WIDGET_W - 16) * s, 2 * s)],
               fill=TOP_LIGHT, width=s)
    else:
        # 深色圆角卡片 + 发丝描边 + 顶部内高光（无真模糊时的伪玻璃补偿）
        d.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=28 * s, fill=CARD_BG,
                            outline=HAIRLINE, width=2)
        d.line([(24 * s, 2 * s), ((WIDGET_W - 24) * s, 2 * s)], fill=TOP_LIGHT, width=s)

    bar_top, bar_h, bar_w = 34, 54, 5

    # ── 第一列：TOKEN 竖排标签（纯标识，不承载数据）──
    tok_font = _load_font(BOLD_FONT, 8 * s)
    for i, ch_ in enumerate("TOKEN"):
        bbox = d.textbbox((0, 0), ch_, font=tok_font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        y = (32 + i * 12) * s
        d.text((15 * s - w / 2 - bbox[0], y - bbox[1]), ch_,
               font=tok_font, fill=TEXT_DIM)

    # 列间竖发丝线（TOKEN 标签列与数据列分组）
    d.line([(26 * s, 22 * s), (26 * s, 90 * s)], fill=HAIRLINE, width=s)

    # ── 第二列：GLM 三竖条（短期剩余 / 5h 重置倒计时 / 周剩余）──
    _draw_column_header(d, 56 * s, "GLM", glm["remaining"], s)
    glm_remaining = min(max(glm["remaining"], 0), 100)
    bar_x = [48, 56, 64]
    for x, (frac, color) in zip(bar_x, (
        (glm_remaining / 100, _remaining_color(glm_remaining)),
        (glm["reset_frac"], _pulse_white(YELLOW, glm["pulse"] * (1 - dim))),
        (min(max(glm["weekly"], 0), 100) / 100, RED),
    )):
        _draw_vertical_capsule(d, (x - bar_w / 2) * s, bar_top * s,
                               bar_w * s, bar_h * s,
                               max(0.0, min(1.0, frac)), color)

    # ── 第三列：KIMI 三竖条（短期剩余 / 5h 重置倒计时 / 月剩余）──
    _draw_column_header(d, 110 * s, "KIMI", kimi["remaining"], s)
    if kimi["remaining"] is None:
        kimi_bars = ((0.0, OFF_COLOR), (0.0, OFF_COLOR), (0.0, OFF_COLOR))
    else:
        kimi_remaining = min(max(kimi["remaining"], 0), 100)
        monthly = kimi.get("monthly")
        kimi_bars = (
            (kimi_remaining / 100, _remaining_color(kimi_remaining)),
            (kimi["reset_frac"], _pulse_white(YELLOW, kimi["pulse"] * (1 - dim))),
            (min(max(monthly, 0), 100) / 100 if monthly is not None else 0.0, RED),
        )
    for x, (frac, color) in zip((102, 110, 118), kimi_bars):
        _draw_vertical_capsule(d, (x - bar_w / 2) * s, bar_top * s,
                               bar_w * s, bar_h * s,
                               max(0.0, min(1.0, frac)), color)

    # 分隔发丝线（上段用量 / 下段番茄钟）
    d.line([(22 * s, 95 * s), ((WIDGET_W - 22) * s, 95 * s)], fill=HAIRLINE, width=s)

    # ── 下段：阶段点 + 中文阶段词（整组居中）──
    cx = cw / 2
    stage = pomo["stage"]
    stage_color = _stage_color(stage)
    stage_zh = pomo["label"]
    zh_font = _load_font(ZH_FONT, 14 * s, ZH_WEIGHT)
    dot_r = 3 * s
    dot_gap = 6 * s
    group_w = dot_r * 2 + dot_gap + zh_font.getlength(stage_zh)
    gx = cx - group_w / 2
    row_cy = 111 * s
    d.ellipse([gx, row_cy - dot_r, gx + dot_r * 2, row_cy + dot_r],
              fill=stage_color[:3] + (int(255 - 155 * dim),))
    bbox = d.textbbox((0, 0), stage_zh, font=zh_font)
    d.text((gx + dot_r * 2 + dot_gap - bbox[0],
            row_cy - (bbox[3] - bbox[1]) / 2 - bbox[1]),
           stage_zh, font=zh_font,
           fill=(255, 255, 255, int(TEXT_SUB[3] - (TEXT_SUB[3] - 70) * dim)))

    # 倒计时（等宽步进 + 冒号呼吸；off 时 remaining 即下次开工 HH:MM 折算秒）
    _draw_tabular_timer(d, cx, 139 * s, pomo["remaining"],
                        _load_font(NUM_FONT, 20 * s), dim)

    # 阶段胶囊进度条（随秒缩减，「活着」的最低调表达）
    bar_color = stage_color[:3] + (int(255 - 195 * dim),)
    _draw_capsule(d, cx, 160 * s, (WIDGET_W - 2 * 22) * s, 5 * s,
                  pomo["progress"], bar_color)

    return img.resize((WIDGET_W, WIDGET_H), Image.LANCZOS)


def _create_strip_image(progress=0.0):
    """贴边隐藏时的细边：窗口只露出左侧 EDGE_STRIP 像素（其余在屏外），
    可见区画一条竖向番茄进度条——顶部锚定、底部边界随剩余时间向上收，
    像水从底部流走，流到底即空（时间到）。颜色按剩余比例分级：
    >50% 绿 / 20–50% 黄 / <20% 红，一眼看出还能撑多久。"""
    s = SS
    img = Image.new("RGBA", (WIDGET_W * s, WIDGET_H * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0, x1 = 1.5 * s, 4.5 * s
    y0, y1 = 8 * s, (WIDGET_H - 8) * s
    d.rounded_rectangle([x0, y0, x1, y1], radius=1.5 * s,
                        fill=(255, 255, 255, 36))
    if progress > 0.01:
        color = GREEN if progress > 0.5 else (YELLOW if progress > 0.2 else RED)
        fill_h = max((y1 - y0) * progress, x1 - x0)
        d.rounded_rectangle([x0, y0, x1, y0 + fill_h], radius=1.5 * s,
                            fill=color[:3] + (220,))
    return img.resize((WIDGET_W, WIDGET_H), Image.LANCZOS)


def create_detail_image(info, glass=False):
    """详情面板图像（hover 主卡片时向左展开）。

    info 为 dict：
      glm_remaining / glm_reset_left(秒, None 无) / glm_weekly
      kimi_remaining(None 无数据) / kimi_reset_left / kimi_monthly
      pomo_label / pomo_detail / last_refresh("HH:MM:SS" 或 "--")
    """
    s = SS
    cw, ch = PANEL_W * s, WIDGET_H * s
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if glass:
        d.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=CORNER_RADIUS * s,
                            outline=HAIRLINE, width=2)
        d.line([(16 * s, 2 * s), ((PANEL_W - 16) * s, 2 * s)],
               fill=TOP_LIGHT, width=s)
    else:
        d.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=28 * s, fill=CARD_BG,
                            outline=HAIRLINE, width=2)
        d.line([(24 * s, 2 * s), ((PANEL_W - 24) * s, 2 * s)],
               fill=TOP_LIGHT, width=s)

    title_font = _load_font(BOLD_FONT, 9 * s)
    text_font = _load_font(ZH_FONT, 10 * s, ZH_WEIGHT)
    val_font = _load_font(NUM_FONT, 10 * s)

    def fmt_reset(left):
        if left is None:
            return ""
        h, rem = divmod(max(0, int(left)), 3600)
        return f"（{h}h{rem // 60:02d}m 后重置）"

    pad_x = 16 * s
    y = 20 * s

    def section(title):
        nonlocal y
        # Segoe 无中文字形：中文标题（番茄钟）改用中文字体
        font = title_font if title.isascii() else _load_font(ZH_FONT, 9 * s, ZH_WEIGHT)
        _draw_tracked(d, pad_x + font.getlength(title) / 2, y,
                      title, font, 1 * s, TEXT_DIM)
        y += 16 * s

    def line(label, value, color=TEXT_SUB):
        nonlocal y
        d.text((pad_x, y), label, font=text_font, fill=TEXT_SUB)
        lw = text_font.getlength(label)
        # 值拆成数字主体（Segoe）+ 中文后缀（如「（4h10m 后重置）」，中文字体）
        main, sep, suffix = value.partition("（")
        if main.isascii():
            d.text((pad_x + lw + 2 * s, y - 1), main, font=val_font, fill=color)
            if sep:
                mw = val_font.getlength(main)
                d.text((pad_x + lw + 2 * s + mw, y), sep + suffix,
                       font=text_font, fill=TEXT_DIM)
        else:  # 值含中文（如「下次开工 13:30」），整体用中文字体
            d.text((pad_x + lw + 2 * s, y), value, font=text_font, fill=color)
        y += 18 * s

    def pct_text(v):
        return "--" if v is None else f"{v:.0f}%"

    def pct_color(v):
        return TEXT_DIM if v is None else _remaining_color(v)

    section("GLM")
    line("短期剩余", pct_text(info["glm_remaining"]) + fmt_reset(info["glm_reset_left"]),
         pct_color(info["glm_remaining"]))
    line("周额度剩余", pct_text(info["glm_weekly"]), pct_color(info["glm_weekly"]))
    y += 8 * s
    section("KIMI")
    line("短期剩余", pct_text(info["kimi_remaining"]) + fmt_reset(info["kimi_reset_left"]),
         pct_color(info["kimi_remaining"]))
    line("月额度剩余", pct_text(info["kimi_monthly"]), pct_color(info["kimi_monthly"]))
    y += 8 * s
    section("番茄钟")
    line(info["pomo_label"], info["pomo_detail"])
    line("上次刷新", info["last_refresh"])

    return img.resize((PANEL_W, WIDGET_H), Image.LANCZOS)


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
# 贴边停靠/自动隐藏的边沿必须取「窗口当前所在显示器」的右缘，而非主屏宽度：
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

        # 窗口尺寸 & 位置：优先恢复上次停靠位置（ui_state.json，所在屏已拔则丢弃），
        # 首次启动贴「鼠标所在屏」右缘（悬空 8px、竖直居中），不再写死主屏
        self._win_w, self._win_h = WIDGET_W, WIDGET_H
        x, y = self._restore_pos()
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

        # 右键菜单
        self._menu = tk.Menu(self.root, tearoff=0)
        self._menu.add_command(label="立即刷新", command=self._do_refresh)
        self._menu.add_command(label="开机自启（点击切换）", command=self._toggle_autostart)
        self._menu.add_separator()
        self._menu.add_command(label="退出", command=self._quit)
        self.root.bind("<Button-3>", lambda e: self._menu.tk_popup(e.x_root, e.y_root))

        # 拖拽
        self.root.bind("<Button-1>", self._drag_start)
        self.root.bind("<B1-Motion>", self._drag_move)
        self.root.bind("<ButtonRelease-1>", self._drag_end)
        self._drag_x = self._drag_y = 0
        self._dragging = False

        # 贴边隐藏 + hover 详情面板状态
        self._hidden = False          # 当前是否缩成右缘细边
        self._sliding_out = False     # 细边→整卡滑出动画进行中（期间保持细边视觉）
        self._visual_mode = None      # 当前窗口形态 "strip"/"full"（幂等切换用）
        self._panel = None            # 详情面板窗口（lazy 创建）
        self._panel_visible = False
        self._hover_since = None      # 指针进入卡片的时刻（面板延迟用）
        self._leave_since = None      # 指针离开的时刻（隐藏延迟用）
        self._born = time.time()      # 启动宽限期（BOOT_GRACE 内不自动隐藏）
        self._last_refresh = "--"

        # 状态：GLM（短期 Token 剩余 + 周额度剩余 + 短期窗口重置点）+ Kimi + 番茄钟
        self._token_remaining = 100  # 加载态显示满电，API 返回后更新
        self._weekly_remaining = 100.0  # 周额度剩余比例，GLM 第三竖条
        self._quota_reset_ts = 0.0   # 短期窗口重置时刻（epoch 秒），0 = 尚无数据
        self._quota_span = 5 * 3600  # 窗口满刻度：初始按 5h，每次拉取按实际跨度上调
        self._kimi_remaining = None  # Kimi 短期剩余；None = 无数据（daemon 未跑）
        self._kimi_reset_ts = 0.0
        self._kimi_span = 5 * 3600
        self._kimi_monthly_remaining = None  # Kimi 月额度剩余；None = 无数据
        self._pomo = _pomo_state()
        self._dim = 0.0           # 番茄钟闪烁调光系数 0~1（正弦淡出）
        self._blinking = False
        # 数值缓动动画：_disp 为当前显示值，_targets 为目标值，
        # 每次刷新后 30fps 指数趋近（ease-out 观感），收敛即停，平时零开销
        self._disp = {}
        self._targets = {}
        self._animating = False

        # 显示初始状态（先用 PIL 卡片底渲染一帧，再开启 Acrylic——
        # 分层窗口先建立 UpdateLayeredWindow 渲染通道后叠加模糊才生效）
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
        """将窗口设为分层窗口（WS_EX_LAYERED），并尝试开启 Acrylic 毛玻璃
        与 DWM 圆角；失败（老系统）时回退 PIL 自绘卡片底"""
        ex = ctypes.windll.user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        self._glass = (_set_acrylic(self._hwnd) and _set_round_corners(
            self._hwnd, self._win_w, self._win_h))
        if self._glass:
            _set_dwm_round(self._hwnd)  # 玻璃底圆角（region 裁不到组合属性底）
        if not self._glass:
            print("Acrylic/DWM 圆角不可用，回退 PIL 自绘卡片")

    # 拖拽 ----------------------------------------------------------
    def _drag_start(self, event):
        self._drag_x, self._drag_y = event.x, event.y
        self._dragging = True
        if self._panel_visible:
            self._hide_panel()

    def _drag_move(self, event):
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _drag_end(self, _event):
        self._dragging = False
        self._save_pos()

    def _monitor_rect(self):
        """卡片所在屏矩形。锚点取窗口左缘内侧而非窗口矩形：隐藏态窗口
        身体伸在右侧邻屏上，按矩形取「最近屏」会误判成邻屏，边沿判定与
        滑入目标就全按邻屏右缘算——卡片贴主屏右缘时永远唤不出来的根因"""
        x, y = self.root.winfo_x(), self.root.winfo_y()
        return (monitor_rect_at_point(x + 3, y + self._win_h // 2)
                or monitor_rect_of_hwnd(self._hwnd))

    # 位置记忆 --------------------------------------------------------
    def _save_pos(self):
        """记住当前停靠位置（隐藏/滑出中态归一为展开位），重启后原位回归"""
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if self._hidden or self._sliding_out:
            rect = self._monitor_rect()
            if rect:
                x = rect[2] - self._win_w - 8
        try:
            with open(UI_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"x": x, "y": y}, f)
        except OSError:
            pass  # 位置记忆写失败不影响运行

    def _restore_pos(self):
        """读取上次位置；所在屏已不在线（拔掉/改布局）则返回 (None, None) 走默认"""
        try:
            with open(UI_STATE_PATH, encoding="utf-8") as f:
                pos = json.load(f)
            x, y = int(pos["x"]), int(pos["y"])
        except (OSError, KeyError, TypeError, ValueError):
            return None, None
        # 用窗口中心点校验：记忆位置必须落在一块当前在线的屏上
        if monitor_rect_at_point(x + self._win_w // 2, y + self._win_h // 2):
            return x, y
        return None, None

    # 贴边隐藏 + hover 详情面板 --------------------------------------
    def _poll_hover(self):
        """150ms 轮询指针位置，驱动贴边隐藏与详情面板的状态机"""
        px, py = self.root.winfo_pointerxy()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        w, h = self._win_w, self._win_h
        # 边沿取「窗口当前所在屏」的右缘：拖到哪块屏就贴哪块屏，不再拽回主屏
        rect = self._monitor_rect()
        if rect:
            sw = rect[2]
        else:  # 查询失败兜底：退回老的主屏判定
            sw = self.root.winfo_screenwidth()
        now = time.time()

        # 停靠右缘时，卡片右界到屏幕边缘之间的 8px 悬空带也算「在卡片上」：
        # 鼠标贴屏幕右缘悬停（正是唤出卡片的自然位置）恰好落在这条带里，
        # 若不算，1.2s 后会被判「离开」缩边、随后又触发滑出，卡片反复弹跳
        right = sw if x + w >= sw - 24 else x + w
        over_card = x <= px <= right and y <= py <= y + h
        over_panel = False
        if self._panel_visible and self._panel:
            qx = self._panel.winfo_x()
            over_panel = qx <= px <= qx + PANEL_W and y <= py <= y + h

        if self._hidden:
            # 细边状态：指针触及细边（本屏右缘附近）即滑出。热区严格限制在
            # 本屏之内（px < sw）：窗口隐藏时身体伸在右侧邻屏上，若热区越过
            # 屏缘（旧版 sw+8），鼠标在邻屏左缘移动就会把卡片拽出来再缩回，
            # 造成卡片在两屏间反复弹跳
            if sw - 16 <= px < sw and y - 20 <= py <= y + h + 20:
                self._hidden = False
                self._sliding_out = True
                self._slide_to(sw - w - 8, on_done=self._finish_reveal)
        elif not self._dragging:
            if over_card or over_panel:
                self._leave_since = None
                if over_card and not self._panel_visible:
                    if self._hover_since is None:
                        self._hover_since = now
                    elif now - self._hover_since >= PANEL_DELAY:
                        self._show_panel()
            else:
                self._hover_since = None
                if self._panel_visible:
                    self._hide_panel()
                # 停靠当前屏右缘即自动隐藏：原地缩成细边（region 裁窄 +
                # 关 Acrylic），窗口不再越出屏缘，邻屏上无残影
                if x + w >= sw - 24 and now - self._born > BOOT_GRACE:
                    if self._leave_since is None:
                        self._leave_since = now
                    elif now - self._leave_since >= HIDE_DELAY:
                        self._leave_since = None
                        self._hidden = True
                        self._render()  # 先换细边视觉，再滑向边缘
                        self._slide_to(sw - EDGE_STRIP)
        self.root.after(150, self._poll_hover)

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
        - 细边：region 裁成 EDGE_STRIP 竖条——窗口矩形虽仍为整卡大小，
          region 外不参与命中测试，伸在邻屏上的透明部分不挡鼠标；
          同时关 Acrylic（模糊按 region 生效，开着会在邻屏浮出暗色玻璃块）
        - 整卡：恢复圆角 region；曾成功开玻璃（_glass）的再重新开启"""
        mode = "strip" if strip else "full"
        if mode == self._visual_mode:
            return
        self._visual_mode = mode
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        if strip:
            rgn = gdi32.CreateRoundRectRgn(
                0, 0, EDGE_STRIP + 1, self._win_h + 1, 6, 6)
            if not user32.SetWindowRgn(ctypes.c_void_p(self._hwnd), rgn, True):
                gdi32.DeleteObject(rgn)
            _set_dwm_round(self._hwnd, 1)  # DONOTROUND：撤掉按窗口矩形的描边
            if self._glass:
                _unset_acrylic(self._hwnd)
        else:
            rgn = gdi32.CreateRoundRectRgn(
                0, 0, self._win_w + 1, self._win_h + 1,
                CORNER_RADIUS * 2, CORNER_RADIUS * 2)
            if not user32.SetWindowRgn(ctypes.c_void_p(self._hwnd), rgn, True):
                gdi32.DeleteObject(rgn)
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
        """惰性创建详情面板窗口（分层窗口 + Acrylic，与主卡片同材质）"""
        if self._panel:
            return
        p = tk.Toplevel(self.root)
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        p.geometry(f"{PANEL_W}x{self._win_h}+0+0")
        p.update_idletasks()
        hwnd = self._toplevel_hwnd(int(p.winfo_id()))
        ex = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        self._panel = p
        self._panel_hwnd = hwnd
        # 与主卡片相同的顺序约束：先建立 UpdateLayeredWindow 渲染通道，
        # 再叠加 Acrylic / 圆角，否则渲染通道不生效
        blank = Image.new("RGBA", (PANEL_W, self._win_h), (0, 0, 0, 0))
        _update_layered_window(hwnd, blank)
        self._panel_glass = _set_acrylic(hwnd) and _set_round_corners(
            hwnd, PANEL_W, self._win_h)
        if self._panel_glass:
            _set_dwm_round(hwnd)
        p.withdraw()

    def _show_panel(self):
        """展开详情面板：从主卡片左缘滑出，z 序压在卡片下方（像是卡片背后长出来）"""
        self._ensure_panel()
        self._render_panel()
        card_x, y = self.root.winfo_x(), self.root.winfo_y()
        target_x = card_x - PANEL_W - 6
        p = self._panel
        p.geometry(f"+{card_x - 24}+{y}")
        p.deiconify()
        # 面板压到主卡片之下，滑出时不遮挡卡片
        ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(self._panel_hwnd), ctypes.c_void_p(self._hwnd),
            0, 0, 0, 0, 0x1 | 0x2)  # SWP_NOSIZE | SWP_NOMOVE
        self._panel_visible = True
        start_x = card_x - 24

        def step(i, steps=10):
            t = (i + 1) / steps
            e = 1 - (1 - t) ** 3
            p.geometry(f"+{round(start_x + (target_x - start_x) * e)}+{y}")
            if i + 1 < steps:
                self.root.after(16, lambda: step(i + 1))

        step(0)

    def _hide_panel(self):
        self._panel_visible = False
        self._hover_since = None
        if self._panel:
            self._panel.withdraw()

    def _render_panel(self):
        """重绘详情面板内容（精确数值用目标值而非动画中间值）"""
        if not self._panel_visible or not self._panel:
            return
        p = self._pomo
        if p["stage"] == "off":
            detail = f"下次开工 {p['next_time']}"
        else:
            m, s = divmod(max(0, p["remaining"]), 60)
            detail = f"{m:02d}:{s:02d}"
        info = {
            "glm_remaining": self._token_remaining,
            "glm_reset_left": (self._quota_reset_ts - time.time()
                               if self._quota_reset_ts else None),
            "glm_weekly": self._weekly_remaining,
            "kimi_remaining": self._kimi_remaining,
            "kimi_reset_left": (self._kimi_reset_ts - time.time()
                                if self._kimi_reset_ts else None),
            "kimi_monthly": self._kimi_monthly_remaining,
            "pomo_label": p["label"],
            "pomo_detail": detail,
            "last_refresh": self._last_refresh,
        }
        _update_layered_window(self._panel_hwnd,
                               create_detail_image(info, glass=self._panel_glass))

    # 统一渲染（GLM/Kimi 三列 + 番茄钟）--------------------------------
    @staticmethod
    def _reset_frac(reset_ts, span):
        """窗口重置倒计时的剩余比例（满 = 刚重置，空 = 即将重置回满）"""
        if not reset_ts:
            return 0
        left = reset_ts - time.time()
        return max(0.0, min(1.0, left / span))

    @staticmethod
    def _reset_pulse(reset_ts):
        """黄条整分心跳幅度（1→0）：每分钟第 0 秒达峰、6 秒内线性衰减到 0。

        5h 满刻度的位移每分钟仅 0.2px 不可见，用颜色节律代替位移传达「在走」；
        无重置数据（黄条为空轨道）时不跳。番茄钟闪烁（dim）期间不叠加。"""
        if not reset_ts:
            return 0
        return max(0.0, 1 - datetime.now().second / 6)

    def _render(self):
        disp = self._disp
        glm_view = {
            "remaining": disp.get("glm", self._token_remaining),
            "reset_frac": self._reset_frac(self._quota_reset_ts, self._quota_span),
            "pulse": self._reset_pulse(self._quota_reset_ts),
            "weekly": disp.get("glm_weekly", self._weekly_remaining),
        }
        kimi_view = {
            "remaining": (disp.get("kimi", self._kimi_remaining)
                          if self._kimi_remaining is not None else None),
            "reset_frac": self._reset_frac(self._kimi_reset_ts, self._kimi_span),
            "pulse": self._reset_pulse(self._kimi_reset_ts),
            "monthly": (disp.get("kimi_monthly", self._kimi_monthly_remaining)
                        if self._kimi_monthly_remaining is not None else None),
        }
        if self._hidden or self._sliding_out:
            img = _create_strip_image(self._pomo["progress"])
        else:
            img = create_widget_image(glm_view, kimi_view, self._pomo, self._dim,
                                      glass=self._glass)
        # 细边/整卡窗口形态（region + Acrylic）随图像一起切换，幂等
        self._apply_visual_mode(self._hidden or self._sliding_out)
        _update_layered_window(self._hwnd, img)
        p = self._pomo
        if p["stage"] == "off":
            detail = f"{p['label']}，下次开工 {p['next_time']}"
        else:
            m, s = divmod(max(0, p["remaining"]), 60)
            detail = f"{p['label']} {m:02d}:{s:02d}"
        parts = [f"GLM 短期剩余: {self._token_remaining:.0f}%"]
        if self._quota_reset_ts:
            left = max(0, self._quota_reset_ts - time.time())
            h, rem = divmod(int(left), 3600)
            parts[0] += f"（{h}h{rem // 60:02d}m 后重置）"
        parts.append(f"GLM 周额度剩余: {self._weekly_remaining:.0f}%")
        if self._kimi_remaining is None:
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
        """每秒从墙钟推导番茄钟状态；阶段跃迁（工作↔休息、时段开关）时通知+闪烁"""
        old, self._pomo = self._pomo, _pomo_state()
        if old["stage"] != self._pomo["stage"]:
            self._on_stage_edge(old, self._pomo)
            self._start_blink()
        self._render()
        self.root.after(1000, self._pomo_tick)

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
    def _do_refresh(self):
        def _fetch():
            result = fetch_usage()
            kimi = fetch_kimi_usage()
            self.root.after(0, lambda: self._apply_usage(result, kimi))

        threading.Thread(target=_fetch, daemon=True).start()
        self._schedule()

    def _apply_usage(self, result, kimi):
        """UI 线程应用抓取结果：更新目标值并触发缓动动画与满刻度校准"""
        self._last_refresh = datetime.now().strftime("%H:%M:%S")
        if result:
            short_term_pct = result.get("short_term_percentage")
            if short_term_pct is not None:
                self._token_remaining = min(max(100 - short_term_pct, 0), 100)
                self._set_targets(glm=self._token_remaining)
                reset_ts = result.get("short_term_reset_ts", 0)
                self._quota_reset_ts = reset_ts
                if reset_ts:
                    # 满刻度校准：同一窗口内跨度只会递减，取历史最大即窗口刚重置后的值；
                    # 窗口重置后跨度重新变大，max 自然跟上，无需状态机。
                    span = reset_ts - time.time()
                    if span > 0:
                        self._quota_span = max(self._quota_span, span)

            weekly_pct = result.get("weekly_percentage")
            if weekly_pct is not None:
                self._weekly_remaining = min(max(100 - weekly_pct, 0), 100)
                self._set_targets(glm_weekly=self._weekly_remaining)

        if kimi:
            self._kimi_remaining = min(
                max(100 - kimi["short_term_percentage"], 0), 100)
            self._set_targets(kimi=self._kimi_remaining)
            self._kimi_reset_ts = kimi["short_term_reset_ts"]
            if self._kimi_reset_ts:
                span = self._kimi_reset_ts - time.time()
                if span > 0:
                    self._kimi_span = max(self._kimi_span, span)
            monthly_pct = kimi.get("monthly_percentage")
            if monthly_pct is not None:
                self._kimi_monthly_remaining = min(
                    max(100 - monthly_pct, 0), 100)
                self._set_targets(kimi_monthly=self._kimi_monthly_remaining)
        # daemon 不在跑时不清空既有读数；从未有过数据则保持 None（空轨道）
        self._render()

    def _schedule(self):
        self.root.after(REFRESH_INTERVAL * 1000, self._do_refresh)

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
        self._save_pos()
        if self._panel:
            self._panel.destroy()
        self.root.quit()
        self.root.destroy()

    # 启动 -----------------------------------------------------------
    def run(self):
        register_aumid()                          # 注册通知应用 ID
        self.root.after(500, self._do_refresh)    # Token 刷新
        self.root.after(1000, self._pomo_tick)    # 番茄钟启动
        self.root.after(150, self._poll_hover)    # 贴边隐藏 / 详情面板轮询
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
    GLMWidget().run()

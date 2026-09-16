"""GLM 套餐用量悬浮小组件 + 番茄工作闹钟 — 竖版侧栏贴靠屏幕右缘（Win32 分层窗口）

Apple 风格竖版卡片：上段 Token 剩余百分比 + 胶囊进度条，
下段番茄钟倒计时（50 分钟工作 ↔ 10 分钟休息，自动循环）+ 细进度条。
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

from PIL import Image, ImageDraw, ImageFont
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
    except (URLError, json.JSONDecodeError, KeyError) as exc:
        print(f"API 错误: {exc}")

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
        "progress": remain / total,  # 剩余比例：与倒计时、Token 条同向递减，全卡统一
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


def _load_font(candidates, size):
    """按 (字体候选, 字号) 缓存 PIL 字体对象"""
    key = (candidates, size)
    if key not in _FONT_CACHE:
        font = None
        for _fname in candidates:
            try:
                font = ImageFont.truetype(_fname, size)
                break
            except Exception:
                continue
        _FONT_CACHE[key] = font or ImageFont.load_default()
    return _FONT_CACHE[key]


# ── UI 规格（Apple 风格竖版侧栏，与 cc cli 研讨定稿：iOS 暗色系统色 + 发丝线）──
WIDGET_W, WIDGET_H = 84, 192    # 最终窗口尺寸（逻辑像素）
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
WHITE_TRACK = (255, 255, 255, 150)  # 窗口倒计时的亮白轨道（衬托黄填充，空段清晰可读）

NUM_FONT = ("seguisb.ttf",)   # Segoe UI Semibold（数字）
BOLD_FONT = ("segoeuib.ttf",)  # Segoe UI Bold（TOKEN 标签）
ZH_FONT = ("msyhbd.ttc", "msyh.ttc", "simhei.ttf")  # 微软雅黑粗体（中文）


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
    """等宽步进逐字绘制 MM:SS（伪 tabular，秒针跳动零抖动），冒号每秒呼吸"""
    m, s = divmod(min(remaining_sec, 99 * 60 + 59), 60)
    text = f"{m:02d}:{s:02d}"
    digit_w = font.getlength("0")
    colon_w = font.getlength(":")
    total = 4 * digit_w + colon_w
    colon_alpha = 150 if remaining_sec % 2 else 255  # 冒号每秒呼吸
    x = cx - total / 2
    for ch in text:
        cell = colon_w if ch == ":" else digit_w
        alpha = (colon_alpha if ch == ":" else 255) if not dim else 70
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


def create_widget_image(token_remaining, quota_frac, weekly_remaining, pomo, dim):
    """生成竖版悬浮窗图像：短期 Token%、周用量竖条与番茄钟。
    pomo 为 _pomo_state() 的返回 dict；off 时倒计时区改显下次开工时刻 HH:MM。"""
    s = SS
    cw, ch = WIDGET_W * s, WIDGET_H * s
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cw / 2
    content_w = (WIDGET_W - 2 * 14) * s  # 左右内边距 14 → 内容宽 56

    # 深色圆角卡片 + 发丝描边 + 顶部内高光（无真模糊时的伪玻璃补偿）
    d.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=28 * s, fill=CARD_BG,
                        outline=HAIRLINE, width=2)
    d.line([(24 * s, 2 * s), ((WIDGET_W - 24) * s, 2 * s)], fill=TOP_LIGHT, width=s)

    # 右侧周额度：红色底部锚定，剩余量下降时上沿从上向下收缩。
    weekly_remaining = min(max(weekly_remaining, 0), 100)
    _draw_vertical_capsule(d, (WIDGET_W - 11) * s, 18 * s, 4 * s, 68 * s,
                           weekly_remaining / 100, RED)

    # ── 上段：TOKEN 标签 + 剩余大数字 ──
    _draw_tracked(d, cx, 18 * s, "TOKEN", _load_font(BOLD_FONT, 14 * s),
                  int(1 * s), TEXT_SUB)

    remaining = min(max(token_remaining, 0), 100)
    num_font = _load_font(NUM_FONT, 26 * s)
    pct_font = _load_font(NUM_FONT, 13 * s)
    num = f"{int(remaining)}"
    num_w = num_font.getlength(num)
    pct_w = pct_font.getlength("%")
    gap = 2 * s
    x = cx - (num_w + gap + pct_w) / 2
    baseline = 34 * s + num_font.getmetrics()[0]
    num_color = RED if remaining < 15 else TEXT_MAIN  # 告急时数字同步染红
    d.text((x, baseline), num, font=num_font, fill=num_color, anchor="ls")
    d.text((x + num_w + gap, baseline), "%", font=pct_font,
           fill=num_color[:3] + (170,), anchor="ls")

    _draw_capsule(d, cx, 74 * s, content_w, 5 * s,
                  remaining / 100, _remaining_color(remaining))

    # 窗口重置倒计时细条（黄填充 = 距重置剩余时间，亮白轨道；满=刚重置回满，
    # 空=即将重置，用于判断「该冲用量还是该省着用」）
    reset_bar = YELLOW if not dim else (255, 214, 10, 100)
    _draw_capsule(d, cx, 84 * s, content_w, 3 * s,
                  max(0.0, min(1.0, quota_frac)), reset_bar, track=WHITE_TRACK)

    # 分隔发丝线（比内容再内缩 8px，不通栏）
    d.line([(22 * s, 95 * s), ((WIDGET_W - 22) * s, 95 * s)], fill=HAIRLINE, width=s)

    # ── 下段：阶段点 + 中文阶段词（整组居中）──
    stage = pomo["stage"]
    stage_color = _stage_color(stage)
    stage_zh = pomo["label"]
    zh_font = _load_font(ZH_FONT, 14 * s)
    dot_r = 3 * s
    dot_gap = 6 * s
    group_w = dot_r * 2 + dot_gap + zh_font.getlength(stage_zh)
    gx = cx - group_w / 2
    row_cy = 111 * s
    d.ellipse([gx, row_cy - dot_r, gx + dot_r * 2, row_cy + dot_r],
              fill=stage_color[:3] + (100 if dim else 255,))
    bbox = d.textbbox((0, 0), stage_zh, font=zh_font)
    d.text((gx + dot_r * 2 + dot_gap - bbox[0],
            row_cy - (bbox[3] - bbox[1]) / 2 - bbox[1]),
           stage_zh, font=zh_font,
           fill=(255, 255, 255, 70 if dim else TEXT_SUB[3]))

    # 倒计时（等宽步进 + 冒号呼吸；off 时 remaining 即下次开工 HH:MM 折算秒）
    _draw_tabular_timer(d, cx, 139 * s, pomo["remaining"],
                        _load_font(NUM_FONT, 20 * s), dim)

    # 阶段胶囊进度条（随秒缩减，「活着」的最低调表达）
    bar_color = (255, 255, 255, 60) if dim else stage_color
    _draw_capsule(d, cx, 160 * s, content_w, 5 * s, pomo["progress"], bar_color)

    return img.resize((WIDGET_W, WIDGET_H), Image.LANCZOS)


# ── Win32 分层窗口渲染 ────────────────────────────────────────────────
def _update_layered_window(hwnd, img):
    """将 PIL RGBA 图像渲染到 Win32 分层窗口（逐像素 Alpha 透明）"""
    w, h = img.size

    # PIL RGBA → Win32 premultiplied BGRA
    raw = img.tobytes()
    n = len(raw)
    bgra = bytearray(n)
    for i in range(0, n, 4):
        r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        bgra[i] = int(b * a / 255)
        bgra[i + 1] = int(g * a / 255)
        bgra[i + 2] = int(r * a / 255)
        bgra[i + 3] = a

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
    ctypes.memmove(ppvBits, bytes(bgra), len(bgra))

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


# ── 悬浮窗口 ─────────────────────────────────────────────────────────
class GLMWidget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)       # 无边框
        self.root.attributes("-topmost", True)  # 置顶

        # 窗口尺寸 & 默认位置（贴靠屏幕右缘，悬空 8px、竖直居中）
        self._win_w, self._win_h = WIDGET_W, WIDGET_H
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = sw - self._win_w - 8
        y = (sh - self._win_h) // 2
        self.root.geometry(f"{self._win_w}x{self._win_h}+{x}+{y}")

        # 确保窗口已创建，再设置分层窗口
        self.root.update_idletasks()
        # 注意：3.13+ 新版 Tk 的 winfo_id() 返回 TkChild 子窗口，分层窗口必须挂顶层
        self._hwnd = self._toplevel_hwnd(int(self.root.winfo_id()))
        self._setup_layered()

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
        self._drag_x = self._drag_y = 0

        # 状态：短期 Token 剩余 + 周额度剩余 + 短期窗口重置点 + 番茄钟
        self._token_remaining = 100  # 加载态显示满电，API 返回后更新
        self._weekly_remaining = 100.0  # 周额度剩余比例，右侧红色竖条
        self._quota_reset_ts = 0.0   # 短期窗口重置时刻（epoch 秒），0 = 尚无数据
        self._quota_span = 5 * 3600  # 窗口满刻度：初始按 5h，每次拉取按实际跨度上调
        self._pomo = _pomo_state()
        self._dim = False  # 番茄钟闪烁（灭）标志

        # 显示初始状态
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
        """将窗口设为分层窗口（WS_EX_LAYERED）"""
        ex = ctypes.windll.user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)

    # 拖拽 ----------------------------------------------------------
    def _drag_start(self, event):
        self._drag_x, self._drag_y = event.x, event.y

    def _drag_move(self, event):
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    # 统一渲染（Token + 窗口重置 + 番茄钟）--------------------------
    def _render(self):
        quota_frac = self._quota_reset_frac()
        img = create_widget_image(
            self._token_remaining, quota_frac, self._weekly_remaining, self._pomo, self._dim
        )
        _update_layered_window(self._hwnd, img)
        p = self._pomo
        if p["stage"] == "off":
            detail = f"{p['label']}，下次开工 {p['next_time']}"
        else:
            m, s = divmod(max(0, p["remaining"]), 60)
            detail = f"{p['label']} {m:02d}:{s:02d}"
        reset = ""
        if self._quota_reset_ts:
            left = max(0, self._quota_reset_ts - time.time())
            h, rem = divmod(int(left), 3600)
            reset = f" | 窗口 {h}h{rem // 60:02d}m 后重置"
        self.root.tooltip_text = (
            f"GLM 短期 Token 剩余: {self._token_remaining:.0f}%{reset}"
            f" | 周额度剩余: {self._weekly_remaining:.0f}% | 番茄钟 {detail}"
        )

    def _quota_reset_frac(self):
        """短期窗口重置倒计时的剩余比例（满 = 刚重置，空 = 即将重置回满）"""
        if not self._quota_reset_ts:
            return 0
        left = self._quota_reset_ts - time.time()
        return max(0.0, min(1.0, left / self._quota_span))

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
        self._blink_left = 12  # 12 × 0.5s = 6 秒闪烁
        self._blink_step()

    def _blink_step(self):
        if self._blink_left <= 0:
            self._dim = False
            self._render()
            return
        self._dim = not self._dim
        self._blink_left -= 1
        self._render()
        self.root.after(500, self._blink_step)

    # Token 数据刷新 -------------------------------------------------------
    def _do_refresh(self):
        def _fetch():
            result = fetch_usage()
            if result:
                short_term_pct = result.get("short_term_percentage")
                if short_term_pct is not None:
                    self._token_remaining = min(max(100 - short_term_pct, 0), 100)
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
            self.root.after(0, self._render)

        threading.Thread(target=_fetch, daemon=True).start()
        self._schedule()

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
        self.root.quit()
        self.root.destroy()

    # 启动 -----------------------------------------------------------
    def run(self):
        register_aumid()                          # 注册通知应用 ID
        self.root.after(500, self._do_refresh)    # Token 刷新
        self.root.after(1000, self._pomo_tick)    # 番茄钟启动
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

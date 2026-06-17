"""GLM 套餐用量悬浮小组件 + 番茄工作闹钟 — 右下角置顶悬浮（Win32 分层窗口）

上行：电池图标显示 Token 剩余百分比
下行：番茄钟倒计时（45 分钟工作 ↔ 5 分钟休息，自动循环）
"""

import ctypes
import json
import os
import sys
import threading
import tkinter as tk
import winreg
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont
from winotify import Notification

# pythonw 在无控制台环境（任务计划程序 / 开机自启）下 sys.stdout/stderr 为 None，
# 此时 print() 会抛 AttributeError 导致进程崩溃，重定向到 devnull 规避。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

REFRESH_INTERVAL = 300  # Token 刷新：5 分钟
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
ICON_PATH = Path(__file__).resolve().parent / "tomato.ico"  # AUMID 应用图标
ICON_PNG = Path(__file__).resolve().parent / "tomato.png"   # toast 内联图标
AUMID = "GlmDashboard"  # 应用模型 ID（系统通知来源标识）

# 番茄钟配置：45 分钟工作 ↔ 5 分钟休息，自动循环
POMODORO_WORK_MIN = 45
POMODORO_REST_MIN = 5

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
def load_config():
    """从环境变量或 ~/.claude/settings.json 读取 API 配置"""
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")

    if (not base_url or not token) and SETTINGS_PATH.exists():
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            env = json.load(f).get("env", {})
        base_url = base_url or env.get("ANTHROPIC_BASE_URL", "")
        token = token or env.get("ANTHROPIC_AUTH_TOKEN", "")

    if not base_url or not token:
        return None, None

    base_domain = base_url.split("/api/anthropic")[0] if "/api/anthropic" in base_url else base_url
    return base_domain, token


# ── API ───────────────────────────────────────────────────────────────
def fetch_usage():
    """调用 GLM API 获取 Token 用量百分比，返回 dict 或 None"""
    base_domain, token = load_config()
    if not base_domain:
        return None

    url = f"{base_domain}/api/monitor/usage/quota/limit"
    req = Request(url, headers={
        "Authorization": token,
        "Accept-Language": "zh-CN,zh",
        "Content-Type": "application/json",
    })

    try:
        with urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            data = body.get("data") or body
            for item in data.get("limits", []):
                if item.get("type") == "TOKENS_LIMIT":
                    return {"percentage": float(item.get("percentage", 0))}
    except (URLError, json.JSONDecodeError, KeyError) as exc:
        print(f"API 错误: {exc}")

    return None


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


# ── 图标生成 ──────────────────────────────────────────────────────────
def _remaining_color(remaining):
    """根据剩余百分比返回颜色：>40% 绿，20-40% 黄，<20% 红"""
    if remaining > 40:
        return (76, 175, 80)
    if remaining > 20:
        return (255, 193, 7)
    return (244, 67, 54)


def _pomo_label(stage, remaining_sec):
    """番茄钟显示文字，如 '工作 42:30'"""
    stage_zh = "工作" if stage == "work" else "休息"
    m, s = divmod(max(0, remaining_sec), 60)
    return f"{stage_zh} {m:02d}:{s:02d}"


def _draw_battery(d, cw, half_h, remaining):
    """在高度 half_h 的区域内垂直居中绘制电池图标 + 百分比"""
    color = _remaining_color(remaining)
    body_w, body_h = 210, 44
    cap_w, cap_h = 8, 18

    total_w = body_w + cap_w
    bx1 = (cw - total_w) // 2
    by1 = (half_h - body_h) // 2
    bx2 = bx1 + body_w
    by2 = by1 + body_h

    # 正极凸起
    d.rounded_rectangle(
        [bx2, half_h // 2 - cap_h // 2, bx2 + cap_w, half_h // 2 + cap_h // 2],
        radius=2, fill=(180, 180, 180, 255),
    )
    # 外壳
    d.rounded_rectangle(
        [bx1, by1, bx2, by2], radius=5,
        outline=(220, 220, 220, 255), width=2,
    )

    # 填充条
    inner = 4
    ix1, iy1 = bx1 + inner, by1 + inner
    ix2, iy2 = bx2 - inner, by2 - inner
    fill_w = (ix2 - ix1) * min(remaining, 100) / 100

    if fill_w > 1:
        d.rounded_rectangle(
            [ix1, iy1, ix1 + fill_w, iy2],
            radius=3, fill=(*color, 255),
        )
    else:
        d.rectangle([ix1, iy1, ix1 + 2, iy2], fill=(*color, 255))

    # 百分比文字（居中在电池内部）
    try:
        font = ImageFont.truetype("arialbd.ttf", 28)
    except Exception:
        font = ImageFont.load_default()
    label = f"{int(remaining)}%"
    bbox = d.textbbox((0, 0), label, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    tx = (bx1 + bx2 - text_w) // 2 - bbox[0]
    ty = (by1 + by2 - text_h) // 2 - bbox[1]
    d.text((tx, ty), label, fill=(255, 255, 255, 255), font=font)


def create_widget_image(token_remaining, pomo_stage, pomo_remaining_sec, dim):
    """生成悬浮窗图像：上行电池+Token%，下行番茄钟倒计时（RGBA 逐像素透明圆角）"""
    cw, ch = 288, 172
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角深色背景卡片（整体）
    d.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=30, fill=(40, 40, 40, 255))

    # 中间细分隔线
    d.line([24, ch // 2, cw - 24, ch // 2], fill=(70, 70, 70, 255), width=1)

    half_h = ch // 2

    # 上半：电池图标 + Token%
    _draw_battery(d, cw, half_h, token_remaining)

    # 下半：番茄钟倒计时（需支持中文，依次尝试微软雅黑/黑体/宋体）
    font = None
    for _fname in ("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "simsun.ttc"):
        try:
            font = ImageFont.truetype(_fname, 34)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    label = _pomo_label(pomo_stage, pomo_remaining_sec)
    bbox = d.textbbox((0, 0), label, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    tx = (cw - text_w) // 2 - bbox[0]
    ty = half_h + (half_h - text_h) // 2 - bbox[1]

    # 文字颜色：休息=绿、工作=白；闪烁(dim)时切到背景灰，造成闪烁
    bright = (120, 220, 140) if pomo_stage == "rest" else (255, 255, 255)
    text_color = (60, 60, 60) if dim else bright
    d.text((tx, ty), label, fill=(*text_color, 255), font=font)

    # 应用 92% 不透明度（整体半透明玻璃效果）
    alpha = img.getchannel("A")
    alpha = alpha.point(lambda a: int(a * 0.92))
    img.putalpha(alpha)

    # 缩小 42%
    img = img.resize((int(cw * 0.42), int(ch * 0.42)), Image.LANCZOS)

    return img


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

        # 窗口尺寸 & 初始位置（右下角）—— 高度加大以容纳上下两行
        self._win_w, self._win_h = 121, 73
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = sw - self._win_w - 15
        y = sh - self._win_h - 55
        self.root.geometry(f"{self._win_w}x{self._win_h}+{x}+{y}")

        # 确保窗口已创建，再设置分层窗口
        self.root.update_idletasks()
        self._hwnd = int(self.root.winfo_id())
        self._setup_layered()

        # 右键菜单
        self._menu = tk.Menu(self.root, tearoff=0)
        self._menu.add_command(label="立即刷新", command=self._do_refresh)
        self._menu.add_separator()
        self._menu.add_command(label="退出", command=self._quit)
        self.root.bind("<Button-3>", lambda e: self._menu.tk_popup(e.x_root, e.y_root))

        # 拖拽
        self.root.bind("<Button-1>", self._drag_start)
        self.root.bind("<B1-Motion>", self._drag_move)
        self._drag_x = self._drag_y = 0

        # 状态：Token 剩余 + 番茄钟
        self._token_remaining = 100  # 加载态显示满电，API 返回后更新
        self._pomo_stage = "work"
        self._pomo_remaining = POMODORO_WORK_MIN * 60
        self._dim = False  # 番茄钟闪烁（灭）标志

        # 显示初始状态
        self._render()

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

    # 统一渲染（Token + 番茄钟）------------------------------------
    def _render(self):
        img = create_widget_image(
            self._token_remaining, self._pomo_stage, self._pomo_remaining, self._dim
        )
        _update_layered_window(self._hwnd, img)
        stage_zh = "工作" if self._pomo_stage == "work" else "休息"
        m, s = divmod(max(0, self._pomo_remaining), 60)
        self.root.tooltip_text = (
            f"GLM Token 剩余: {self._token_remaining:.0f}% | 番茄钟 {stage_zh} {m:02d}:{s:02d}"
        )

    # 番茄钟 -------------------------------------------------------
    def _pomo_tick(self):
        """每秒推进倒计时，归零时切换阶段并通知/闪烁"""
        self._pomo_remaining -= 1
        if self._pomo_remaining < 0:
            self._switch_stage()
        self._render()
        self.root.after(1000, self._pomo_tick)

    def _switch_stage(self):
        if self._pomo_stage == "work":
            notify_windows("休息时间到", "45 分钟工作完成，休息 5 分钟～放松一下！")
            self._pomo_stage = "rest"
            self._pomo_remaining = POMODORO_REST_MIN * 60
        else:
            notify_windows("工作时间到", "休息结束，开始下一个 45 分钟工作周期！")
            self._pomo_stage = "work"
            self._pomo_remaining = POMODORO_WORK_MIN * 60
        self._start_blink()

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
            pct = result["percentage"] if result else 0
            self._token_remaining = max(0, 100 - pct)
            self.root.after(0, self._render)

        threading.Thread(target=_fetch, daemon=True).start()
        self._schedule()

    def _schedule(self):
        self.root.after(REFRESH_INTERVAL * 1000, self._do_refresh)

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


if __name__ == "__main__":
    GLMWidget().run()

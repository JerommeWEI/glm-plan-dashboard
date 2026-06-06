"""GLM 套餐用量悬浮小组件 — 右下角置顶显示 Token 剩余量（Win32 分层窗口）"""

import ctypes
import json
import os
import threading
import tkinter as tk
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont

REFRESH_INTERVAL = 300  # 5 分钟
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

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


# ── 图标生成 ──────────────────────────────────────────────────────────
def _remaining_color(remaining):
    """根据剩余百分比返回颜色：>40% 绿，20-40% 黄，<20% 红"""
    if remaining > 40:
        return (76, 175, 80)
    if remaining > 20:
        return (255, 193, 7)
    return (244, 67, 54)


def create_widget_image(remaining):
    """生成悬浮窗口用的电池图像（RGBA，真正的逐像素透明圆角）"""
    cw, ch = 192, 78
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = _remaining_color(remaining)

    # 圆角深色背景卡片
    d.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=30, fill=(40, 40, 40, 255))

    # 电池主体尺寸
    body_w, body_h = 140, 40
    cap_w, cap_h = 8, 18

    # 电池居中（包含正极凸起的宽度）
    total_w = body_w + cap_w
    bx1 = (cw - total_w) // 2
    by1 = (ch - body_h) // 2
    bx2 = bx1 + body_w
    by2 = by1 + body_h

    # 正极凸起
    d.rounded_rectangle(
        [bx2, ch // 2 - cap_h // 2, bx2 + cap_w, ch // 2 + cap_h // 2],
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

    # 应用 80% 不透明度（整体半透明玻璃效果）
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

        # 窗口尺寸 & 初始位置（右下角）
        self._win_w, self._win_h = 81, 33
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

        # 显示加载状态
        self._update_image(0)

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

    # UI 更新 -------------------------------------------------------
    def _update_image(self, usage_pct):
        remaining = max(0, 100 - usage_pct)
        img = create_widget_image(remaining)
        _update_layered_window(self._hwnd, img)
        self.root.tooltip_text = f"GLM Token 剩余: {remaining:.1f}%（已用 {usage_pct:.1f}%）"

    # 数据刷新 -------------------------------------------------------
    def _do_refresh(self):
        def _fetch():
            result = fetch_usage()
            pct = result["percentage"] if result else 0
            self.root.after(0, lambda: self._update_image(pct))

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
        self.root.after(500, self._do_refresh)
        self.root.mainloop()


if __name__ == "__main__":
    GLMWidget().run()

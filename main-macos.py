"""GLM 套餐用量菜单栏应用 — macOS 版本，顶部菜单栏显示 Token 剩余量"""

import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import rumps
from PIL import Image, ImageDraw, ImageFont

REFRESH_INTERVAL = 300  # 5 分钟
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


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


def create_menu_bar_icon(remaining):
    """生成菜单栏用的电池图标（小尺寸，约 40×20 像素）"""
    # macOS 菜单栏图标通常很小
    width, height = 48, 24
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = _remaining_color(remaining)

    # 电池主体尺寸
    body_w, body_h = 36, 14
    cap_w, cap_h = 4, 8

    # 电池居中（包含正极凸起的宽度）
    total_w = body_w + cap_w
    bx1 = (width - total_w) // 2
    by1 = (height - body_h) // 2
    bx2 = bx1 + body_w
    by2 = by1 + body_h

    # 正极凸起
    d.rounded_rectangle(
        [bx2, height // 2 - cap_h // 2, bx2 + cap_w, height // 2 + cap_h // 2],
        radius=1, fill=(180, 180, 180, 255),
    )
    # 外壳
    d.rounded_rectangle(
        [bx1, by1, bx2, by2], radius=2,
        outline=(220, 220, 220, 255), width=1,
    )

    # 填充条
    inner = 2
    ix1, iy1 = bx1 + inner, by1 + inner
    ix2, iy2 = bx2 - inner, by2 - inner
    fill_w = (ix2 - ix1) * min(remaining, 100) / 100

    if fill_w > 0.5:
        d.rounded_rectangle(
            [ix1, iy1, ix1 + fill_w, iy2],
            radius=1, fill=(*color, 255),
        )
    else:
        d.rectangle([ix1, iy1, ix1 + 1, iy2], fill=(*color, 255))

    # 百分比文字（很小的字体，可选）
    try:
        font = ImageFont.truetype("Arial Bold", 10)
    except Exception:
        font = ImageFont.load_default()
    label = f"{int(remaining)}"
    bbox = d.textbbox((0, 0), label, font=font)
    text_w = bbox[2] - bbox[0]
    tx = (bx1 + bx2 - text_w) // 2 - bbox[0]
    ty = (by1 + by2) // 2 - bbox[1] - 1
    d.text((tx, ty), label, fill=(255, 255, 255, 255), font=font)

    # 保存为临时文件
    icon_path = Path.home() / ".claude" / "glm-battery-icon.png"
    img.save(icon_path)
    return str(icon_path)


# ── 菜单栏应用 ─────────────────────────────────────────────────────────
class GLMMenuBarApp(rumps.App):
    def __init__(self):
        super().__init__("⚡", quit_button=None)
        self.icon = None
        self.usage_pct = 0
        self.refresh_item = rumps.MenuItem("刷新", callback=self.manual_refresh)
        self.menu = [
            self.refresh_item,
            rumps.separator,
            rumps.MenuItem("退出", callback=self.quit_app)
        ]
        # 启动时立即刷新一次
        self.refresh_usage()

    def refresh_usage(self):
        """后台获取用量数据并更新 UI"""
        def _fetch():
            result = fetch_usage()
            pct = result["percentage"] if result else 0
            # 在主线程更新 UI
            self._update_ui(pct)

        threading.Thread(target=_fetch, daemon=True).start()

    def _update_ui(self, usage_pct):
        """更新菜单栏图标和标题"""
        self.usage_pct = usage_pct
        remaining = max(0, 100 - usage_pct)

        # 更新标题
        self.title = f"{int(remaining)}%"

        # 更新图标
        icon_path = create_menu_bar_icon(remaining)
        self.icon = rumps.Image(icon_path)

        # 更新菜单项提示
        self.refresh_item.title = f"刷新 (剩余: {remaining:.1f}%)"

    def manual_refresh(self, _):
        """手动刷新按钮回调"""
        self.refresh_item.title = "刷新中..."
        self.refresh_usage()

    def quit_app(self, _):
        """退出应用"""
        rumps.quit_application()

    def run(self):
        """启动应用，设置定时刷新"""
        # 每 5 分钟自动刷新
        def _schedule_loop():
            while True:
                time.sleep(REFRESH_INTERVAL)
                self.refresh_usage()

        threading.Thread(target=_schedule_loop, daemon=True).start()
        super().run()


if __name__ == "__main__":
    app = GLMMenuBarApp()
    app.run()

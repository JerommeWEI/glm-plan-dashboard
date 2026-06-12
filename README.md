# GLM Plan Dashboard v1.4

GLM 套餐用量悬浮小组件，在 Windows 桌面右下角置顶显示 Token 剩余量。

## 功能

- 电池图标实时显示 Token 剩余百分比
- 颜色随余量变化：绿色（>40%）→ 黄色（20%-40%）→ 红色（<20%）
- 每 5 分钟自动刷新数据
- 右键菜单：立即刷新 / 退出
- 可拖拽移动位置
- 圆角半透明悬浮窗口
- 支持开机自启动（Windows 任务计划程序，登录后启动）

## 实施方案

- **语言**: Python 3
- **GUI**: tkinter（无边框置顶窗口 + Win32 分层窗口实现逐像素透明）
- **图像**: Pillow（PIL）绘制电池图标，支持圆角和抗锯齿
- **API**: 通过 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 环境变量或 `~/.claude/settings.json` 读取配置，调用 GLM 配额接口获取用量
- **刷新**: 后台线程请求 API，主线程更新 UI，5 分钟轮询

## 安装与运行

### Windows

```bash
pip install -r requirements.txt
python main.py
```

或直接双击 `start.bat`（无控制台窗口启动）。

### macOS

```bash
pip3 install -r requirements-macos.txt
python3 main-macos.py
```

或直接双击 `start-macos.command`。

**macOS 版本特性：**
- 在顶部菜单栏显示电池图标
- 点击图标显示菜单（刷新/退出）
- 每 5 分钟自动刷新数据
- 颜色随余量变化：绿色（>40%）→ 黄色（20%-40%）→ 红色（<20%）

## 更新日志

### v1.4
- 电池图标尺寸调整：长度（水平）加长 50%、宽度（垂直）加宽 10%，百分比文字保持居中
- 新增开机自启动支持：修复 `pythonw` 在无控制台环境（任务计划程序 / 开机自启）下 `sys.stdout` 为 `None` 导致 `print` 崩溃的问题，输出重定向至 `os.devnull`

### v1.3
- 使用 Win32 分层窗口（`UpdateLayeredWindow`）替代 `transparentcolor` 方案，实现真正的逐像素 Alpha 透明
- 圆角背景现在具有平滑的抗锯齿边缘，不再有毛边或倒角
- 背景卡片圆角半径增大（radius=30），外观更圆润
- 调整整体透明度为 92% 不透明度，背景更扎实

### v1.2
- 百分比文字嵌入电池图标内部，取代原有的右侧文字布局
- 电池图标上下左右居中于背景卡片
- 背景卡片高度增加 30%、宽度缩小 20%，整体比例更紧凑
- 整体放大 20%，提升可读性
- 百分比文字使用加粗字体（Arial Bold）

### v1.1
- 使用 `pythonw` 替代 `python` 启动脚本，隐藏 CMD 黑窗口

### v1.0
- 初始版本：GLM 套餐用量悬浮小组件

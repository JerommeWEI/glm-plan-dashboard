"""把当前生效的 API 配置固化到项目本地 config.json，让仪表盘脱离 Claude Code 独立运行。

读取来源（刻意跳过已有 config.json，避免「读出旧值再写回」的自我循环）：
    环境变量 > ~/.claude/settings.json

用法：python setup_config.py   （可重复执行，每次从外部配置同步最新 token）
"""
import json
from pathlib import Path

from main import read_raw_config

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

base_url, token = read_raw_config(skip_local_config=True)
if not base_url or not token:
    print("未检测到 API 配置（环境变量与 ~/.claude/settings.json 均为空）。")
    print("请先在 Claude Code 中登录 GLM，或手动编辑 config.json 填入 base_url 与 token。")
    raise SystemExit(1)

CONFIG_PATH.write_text(
    json.dumps({"base_url": base_url, "token": token}, indent=2, ensure_ascii=False),
    encoding="utf-8",
)
print(f"已写入 {CONFIG_PATH}")
print(f"  base_url = {base_url}")
print(f"  token    = {'*' * 6}{token[-4:]}（脱敏）")
print("仪表盘现已可脱离 Claude Code 独立运行（双击 start.bat，或启动后右键开启「开机自启」）。")

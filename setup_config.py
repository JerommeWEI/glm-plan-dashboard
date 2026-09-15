"""把当前生效的 API 配置固化到项目本地 config.json，作为 ZCode 不可用时的兜底。

正常情况下无需运行本脚本：仪表盘每次刷新都直接读 ZCode 的配置文件
（~/.zcode/cli/config.json + ~/.zcode/v2/config.json），ZCode 里更新 API 后
最迟 5 分钟自动跟随。config.json 仅在 ZCode 配置缺失时兜底。

读取来源（刻意跳过已有 config.json，避免「读出旧值再写回」的自我循环）：
    ZCode 当前配置 > 环境变量 > ~/.claude/settings.json

用法：python setup_config.py   （可重复执行，每次从外部配置同步最新 key）
"""
import json
from pathlib import Path

from main import read_raw_config

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

base_url, token = read_raw_config(skip_local_config=True)
if not base_url or not token:
    print("未检测到 API 配置（ZCode 配置、环境变量与 ~/.claude/settings.json 均为空）。")
    print("请先在 ZCode 中配置 GLM API key，或手动编辑 config.json 填入 base_url 与 token。")
    raise SystemExit(1)

CONFIG_PATH.write_text(
    json.dumps({"base_url": base_url, "token": token}, indent=2, ensure_ascii=False),
    encoding="utf-8",
)
print(f"已写入 {CONFIG_PATH}")
print(f"  base_url = {base_url}")
print(f"  token    = {'*' * 6}{token[-4:]}（脱敏）")
print("说明：仪表盘运行时优先实时读取 ZCode 配置（自动跟随更新），此文件仅作兜底。")

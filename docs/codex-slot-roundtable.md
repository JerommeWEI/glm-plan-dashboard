# Codex 第 4 槽方案 · 圆桌纪要（2026-09-21）

参会：Codex（gpt-5.6-sol）、DeepSeek（dsh headless）、Kimi（kimi -p）。Claude Code 因本机模型配置报 `unrecognized_model: glm-5.3` 缺席。
预览图（图标候选对比）：会话存档，未入库。

## 数据源（已实测打通）

- base：`~/.codex/config.toml` 顶层 `model_provider` → `[model_providers.<name>].base_url`（现为 https://uapi.ccwu.cc）
- key：`~/.codex/auth.json` 的 `OPENAI_API_KEY`，**Bearer** 鉴权（GLM 是裸 token，勿复用）
- `GET {base}/v1/usage` → `subscription{weekly_limit_usd=300, weekly_usage_usd, weekly_window_start(本周一00:00+08:00), monthly_limit_usd=1200, monthly_usage_usd, daily_limit_usd=0, expires_at}`、`mode=unrestricted`、`unit=USD`、`usage.today{cost,requests}`。**无 5h 窗口，USD 计价。**

## 三方一致（直接采纳）

1. **环语义 = 周额度剩余比例** `max(0, 1 - weekly_usage/weekly_limit)`，对齐全卡"剩余递减"；hover 明示"本周剩余"避免误读 5h；详情给 `$15.6/$300`。接受周环长期 90%+ 高位（如实反映，不硬造动感）。
2. **daily_limit=0/unrestricted 绝不画环、不画 ∞**：今日已用只进 hover 文本（`今日 $x · 无上限`）。
3. **周重置 = window_start+7d 推算，标注"预计"**：aware datetime 保留 +08:00、UTC 比较；跨周期判定以 window_start 变化为准而非本地时钟；`now≥推算点` 不本地置满，触发重轮询、显示"等待刷新"；顺修 `reset_text` >24h 显示"Nd HHh"。
4. **安全与降级**：复用 GLM 的 SSRF 校验（https + 白名单加 `uapi.ccwu.cc` + 私网/环回阻断 + 禁重定向 + 响应体上限 64KB）；tomllib 必须 rb 模式；provider 名从顶层 `model_provider` 读，不写死；日志只打 key 指纹；错误分四级文案（未配置 / 密钥失效(401 退避 30min) / 网络失败 / 200 但无周限）。
5. **每源独立线程落地**：现有 `_do_refresh` 顺序 fetch，Codex 一次超时会拖慢 GLM/Kimi 的 UI 更新——拆 `_apply_codex` 按源 `root.after(0,...)` 生效。

## 分歧与裁决

- **布局**：Codex/Kimi 认可 307 等间距；DS 反对（~25px/槽死空白，1080p 占 28% 屏高），推荐 **H=252、槽距 60、槽心 (28,88,148,208)** + Kimi↔Codex 间 1px 分隔线（5h 会话组 | 周预算组 | 番茄）。→ 采纳 DS（预览图即此布局）。DS 附带：合并 SLOT_KEYS/CENTERS/BREAKS/PERCENT_TOPS 为单一 SLOT_SPECS（现状加槽要同步 5 处）；面板高度不动。
- **图标配色**：Codex/Kimi 推银白 #D6DCE5 / #E8E8E8（OpenAI 黑白品牌）；DS 反对银灰（撞无数据虚线灰 OFF_COLOR），推洋红/覆盆子 #B5179E~#C0266E。→ 预览图按 形状(花结/终端符)×配色(银白/洋红) 出 4 候选 A–D，**待用户挑选**。三方共识：12px 下 OpenAI 花结交织是物理极限，只画简化剪影（六瓣+中心孔，petal_r .19S/dist .335S/孔 .155S）；GPT 字标否决（糊+语言不一致）。
- DS 独有建议（可选）：环外 1.5px 时间基准刻度（elapsed/7d，装饰性对照）；读数三态（新鲜/陈旧降饱和+上次成功时刻/TTL 15min 转虚线）；补 `draw_percentage` 低量染色（注释承诺、实现缺失）。

## 实现顺序（DS）

① 数据源函数 `fetch_codex_usage()`（含校验/降级）→ ② SLOT_SPECS 合并 + 布局常量 → ③ create_widget_image 第 4 槽 + hover 详情分支 → ④ 图标定稿上色。

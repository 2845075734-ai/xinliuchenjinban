# 🌊 心流插件 (Heartflow Plugin)

智能群聊主动回复系统 — 让你的 Bot 像真人一样"读懂"聊天氛围

![version](https://img.shields.io/badge/version-2.0.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![platform](https://img.shields.io/badge/platform-AstrBot-purple)

---

## ✨ 功能特性

- 🧠 **多维度评分判断** — 基于相关性、意愿度、社交性、时效性、连续性五个维度综合评估
- 🌊 **沉浸模式** — Bot 决定回复后自动进入沉浸模式，持续参与对话
- 🤖 **小模型决策** — 使用轻量级模型判断是否回复，降低开销
- 🎭 **人设感知** — 自动获取并精简角色设定，让判断更贴合 Bot 性格
- ⏸️ **智能暂停** — Bot 回复前自动暂停判断，防止连续多条回复
- 💾 **缓存优化** — 精简提示词缓存，减少重复调用

---

## 🛠️ 安装

**方式一：AstrBot 插件市场**

在 AstrBot 管理面板 → 插件市场 → 搜索「xinliuchenjinba」→ 安装

**方式二：手动安装**

```bash
cd AstrBot/data/plugins
git clone https://github.com/2845075734-ai/xinliuchenjinban
重启 AstrBot 即可。

⚙️ 配置说明
基础配置
enable_heartflow
(bool, 默认 false) — 是否启用心流插件
judge_provider_name
(string) — 小模型提供商名称
reply_threshold
(float, 默认 0.6) — 回复触发阈值，范围 0.0~1.0
评分权重配置
每个维度分数范围 0-10，最终加权计算综合得分：

judge_relevance
(默认 0.25) — 相关性：消息与 Bot 角色的关联程度
judge_willingness
(默认 0.20) — 意愿度：Bot 参与对话的意愿
judge_social
(默认 0.20) — 社交性：回复是否符合社交礼仪
judge_timing
(默认 0.15) — 时效性：当前时机是否适合插入
judge_continuity
(默认 0.20) — 连续性：是否直接回应上一句
权重之和应为 1.0，不为 1.0 时插件会自动归一化。

高级配置
context_messages_count
(int, 默认 5) — 上下文消息数量，范围 1-50
judge_context_count
(int, 默认 5) — 判断参考消息数量，范围 1-50
judge_max_retries
(int, 默认 3) — 小模型调用重试次数，范围 0-10
immersive_mode_duration
(int, 默认 30) — 沉浸模式持续时间(秒)，范围 0-300
judgment_paused_timeout
(int, 默认 60) — 判断暂停超时保护(秒)，范围 5-300
judge_include_reasoning
(bool, 默认 true) — 是否输出推理过程
whitelist_enabled
(bool, 默认 false) — 是否启用白名单
chat_whitelist
(list) — 白名单群聊 ID 列表
🌊 沉浸模式详解
触发条件： 当 Bot 判断应该回复时，自动激活

模式行为： 使用更严格的判断规则，避免过度参与闲聊

倒计时刷新： Bot 每次回复后，倒计时自动刷新

退出机制： 倒计时结束后自动退出

沉浸模式下会回复的情况：

消息直接 @ 了机器人
消息明确向机器人提问
机器人发起的对话的延续
消息与机器人刚聊的话题直接相关
群聊冷场，机器人能活跃气氛
沉浸模式下不会回复的情况：

用户之间的正常闲聊
用户之间的问答互动
简单的表情包、语气词
与机器人无关的日常对话
🛡️ 智能暂停机制
为防止 Bot 连续发送多条回复：

Bot 判定回复后，暂停判断
Bot 发送消息后，恢复判断
超时保护（默认60秒）防止永久暂停
🎮 管理命令
/heartflow
— 查看当前心流状态（所有人可用）
/heartflow_reset
— 重置当前群聊心流状态（管理员可用）
💡 使用建议
阈值调节 — 默认 0.6 适合大多数场景。回复太频繁就调高，回复太少就调低

沉浸时长 — 建议 30-60 秒。太短会频繁进出，太长可能过度活跃

小模型选择 — 判断逻辑不需要太强的模型，轻量级即可

白名单 — 建议仅在需要 Bot 主动参与的群聊中启用

❓ 常见问题
Q: Bot 完全不主动回复怎么办？

确认
enable_heartflow
为 true，检查
judge_provider_name
是否正确，尝试调低
reply_threshold
至 0.4

Q: Bot 回复太频繁怎么办？

调高
reply_threshold
至 0.75，增大
immersive_mode_duration

Q: 沉浸模式一直不触发？

确保 Bot 有正常响应能力，检查白名单设置，查看日志

📄 License
MIT License

用 ❤️ 为 AstrBot 社区打造

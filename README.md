
✨ 功能特性

- 🧠 多维度评分判断 — 基于相关性、意愿度、社交性、时效性、连续性五个维度综合评估
- 🌊 沉浸模式 — Bot 决定回复后自动进入沉浸模式，持续参与对话
- 🤖 小模型决策 — 使用轻量级模型判断是否回复，降低开销
- 🛡️ 防零分机制 — 评分范围1-10分，避免模型返回0分影响判断
- ⏸️ 智能暂停 — Bot 回复前自动暂停判断，防止连续多条回复
- 📊 消息缓冲 — 智能消息记录与去重，保证上下文连贯
- ✏️ 自定义提示词 — 支持自定义评判提示词，灵活控制判断逻辑

---

🛠️ 安装

方式一：AstrBot 插件市场

在 AstrBot 管理面板 → 插件市场 → 搜索「xinliuchenjinban」→ 安装

方式二：手动安装

    cd AstrBot/data/plugins
    git clone https://github.com/2845075734-ai/xinliuchenjinban
    重启 AstrBot 即可。

---

⚙️ 配置说明

基础配置

    enable_heartflow (bool, 默认 false) — 是否启用心流插件
    judge_provider_name (string, 默认 "") — 小模型提供商名称
    reply_threshold (float, 默认 0.6) — 回复触发阈值，范围 0.0~1.0

评分权重配置

每个维度分数范围 1-10分（避免0分），最终加权计算综合得分：

    judge_relevance (默认 0.25) — 相关性：消息与 Bot 角色的关联程度
    judge_willingness (默认 0.20) — 意愿度：Bot 参与对话的意愿
    judge_social (默认 0.20) — 社交性：回复是否符合社交礼仪
    judge_timing (默认 0.15) — 时效性：当前时机是否适合插入
    judge_continuity (默认 0.20) — 连续性：是否直接回应上一句

注意：权重之和应为 1.0，不为 1.0 时插件会自动归一化。

高级配置

    context_messages_count (int, 默认 5, 范围 1-50) — 上下文消息数量
    judge_context_count (int, 默认 5, 范围 1-50) — 判断参考消息数量
    judge_max_retries (int, 默认 3, 范围 0-10) — 小模型调用重试次数
    immersive_mode_duration (int, 默认 30, 范围 0-300) — 沉浸模式持续时间(秒)
    judgment_paused_timeout (int, 默认 60, 范围 5-300) — 判断暂停超时保护(秒)
    judge_include_reasoning (bool, 默认 true) — 是否输出推理过程
    whitelist_enabled (bool, 默认 false) — 是否启用白名单
    chat_whitelist (list, 默认 []) — 白名单群聊 ID 列表

---

✏️ 自定义评判提示词

你可以在配置中设置 judge_prompt 来完全自定义评判逻辑。留空则使用内置默认提示词。

必须包含的占位符

在自定义提示词中，你需要使用以下占位符（用 {} 包裹）：

    {score_min} — 最低分数 (示例: 1)
    {score_max} — 最高分数 (示例: 10)
    {mid} — 中间分数 (示例: 5)
    {message} — 当前待判断的消息内容 (示例: "今天天气真好")
    {sender_name} — 消息发送者的名称 (示例: "用户A")
    {chat_id} — 群聊标识符 (示例: "group_12345")
    {minutes_since_reply} — 距离上次回复的分钟数 (示例: 5)
    {threshold} — 回复触发阈值 (示例: 0.6)
    {chat_context} — 当前群聊上下文信息 (示例: "活跃度:高 | 回复率:5.2%")
    {chat_flow} — 对话流向分析 (示例: "闲聊")
    {last_bot_reply} — Bot上一次的回复内容 (示例: "你好！")
    {reasoning_part} — 推理输出部分（内部使用）

返回格式要求

你的提示词必须要求模型返回 JSON 格式，且包含以下字段：

    {
      "relevance": 7,
      "willingness": 6,
      "social": 8,
      "timing": 5,
      "continuity": 7,
      "reasoning": "消息是关于编程的提问，Bot可以回答"
    }

字段说明：

- relevance ~ continuity：五个评分维度，值为 {score_min} 到 {score_max} 之间的数字
- reasoning：简短的原因说明（可选，由 judge_include_reasoning 配置控制）

完整示例：简化版提示词

    你是消息判断助手。

    ## 待判断消息
    [{sender_name}] {message}

    ## 判断规则
    - 如果消息是提问（包含?或吗等），各维度给高分（{mid}-{score_max}）
    - 如果消息是陈述句，各维度给低分（{score_min}-{mid}）
    - 所有分数必须在 {score_min} 到 {score_max} 之间，不能为0

    ## 返回格式
    严格返回JSON：
    {{"relevance": 分数, "willingness": 分数, "social": 分数, "timing": 分数, "continuity": 分数, "reasoning": "原因"}}

完整示例：角色扮演场景（傲娇角色）

    你是傲娇机器人的决策系统，判断是否应该回复。

    ## 性格特点
    - 表面上不想理人，实际上很热心
    - 喜欢被需要的感觉
    - 对技术问题特别感兴趣

    ## 评分标准 ({score_min}-{score_max}分)

    ### 高分情况 ({mid}-{score_max})
    - 有人问技术问题 → relevance: 9
    - 有人求助 → willingness: 8
    - 群里在聊技术话题 → social: 8

    ### 低分情况 ({score_min}-{mid})
    - 纯闲聊 → relevance: 3
    - 有人在秀恩爱 → social: 2
    - 刚回复过 → timing: 2

    ## 当前状态
    - 上次回复: {minutes_since_reply}分钟前
    - 上次Bot回复: {last_bot_reply}

    ## 待判断消息
    [{sender_name}] {message}

    ## 返回格式
    分数范围 {score_min}-{score_max}，不能为0。
    {{"relevance": 分, "willingness": 分, "social": 分, "timing": 分, "continuity": 分, "reasoning": "傲娇分析"}}

提示词编写技巧

1. 明确分数范围：在提示词中多次强调"所有分数必须在 X 到 Y 之间，不能为0"
2. 给出具体示例：告诉模型什么情况给高分，什么情况给低分
3. 利用上下文：使用 {chat_context} 和 {chat_flow} 让模型了解群聊氛围
4. 保持简洁：提示词过长会增加小模型的处理负担
5. 测试调试：使用 /heartflow_debug 查看消息记录，配合日志调试

---

📊 评分系统详解

评分范围

- 分数范围：1-10分（最低1分，避免0分）
- 默认分数：5分（解析失败或异常时使用）
- 中间值：5.5分

五个评分维度

    relevance     相关性    消息与Bot的关联程度
    willingness   意愿度    Bot参与对话的意愿
    social        社交性    回复是否符合社交礼仪
    timing        时效性    当前时机是否适合插入
    continuity    连续性    是否直接回应上一句

阈值计算

    配置阈值: 0.6 (0-1范围)
    绝对阈值: 1 + (10-1) × 0.6 = 6.4分

    即: 5个维度加权平均分需达到 6.4分 才触发回复

防零分机制

    第1层 | 提示词引导 | 明确告知模型"最低1分，不能为0"
    第2层 | 安全提取 | safe_extract_score() 捕获并修正0分
    第3层 | 范围限制 | clamp_score() 最终兜底

---

🌊 沉浸模式详解

触发与退出

    触发条件: Bot判断应该回复时，自动激活
    持续时间: 配置的 immersive_mode_duration (默认30秒)
    倒计时刷新: Bot每次回复后，倒计时自动刷新
    退出机制: 倒计时结束后自动退出

沉浸模式下的判断逻辑

会回复的情况：

1. 消息直接 @ 了机器人
2. 消息明确向机器人提问
3. 机器人发起的对话的延续（如机器人刚问了问题，现在有人回答）
4. 消息内容与机器人刚刚的话题直接相关
5. 群聊中出现了冷场，机器人的回复能自然活跃气氛

不会回复的情况：

- 两个用户之间的正常闲聊
- 用户之间的问答互动
- 简单的表情包、语气词
- 与机器人无关的日常对话

---

⏸️ 智能暂停机制

为防止 Bot 连续发送多条回复：

    Bot判定回复 → 暂停判断 → Bot发送消息 → 恢复判断
                ↓
        超时保护(默认60秒)
                ↓
            自动恢复

---

📋 管理命令

    /heartflow          — 查看当前心流状态（所有人可用）
    /heartflow_debug    — 显示最近20条消息记录（调试用）
    /heartflow_clear    — 清除当前群的消息缓冲区
    /heartflow_reset    — 完全重置当前群的心流状态（管理员可用）
    /heartflow_help     — 显示所有命令的帮助说明

命令示例

    /heartflow          # 查看状态
    /heartflow_debug    # 查看消息记录
    /heartflow_clear    # 清除缓冲区
    /heartflow_reset    # 重置状态（管理员）
    /heartflow_help     # 查看帮助

---

💡 使用建议

    场景              建议
    阈值调节          默认 0.6 适合大多数场景。回复太频繁就调高，回复太少就调低
    沉浸时长          建议 30-60 秒。太短会频繁进出，太长可能过度活跃
    小模型选择        判断逻辑不需要太强的模型，轻量级即可
    白名单            建议仅在需要 Bot 主动参与的群聊中启用
    自定义提示词      简洁优先，明确分数范围，给出高分/低分示例

---

❓ 常见问题

Q: Bot 完全不主动回复怎么办？

1. 确认 enable_heartflow 为 true
2. 检查 judge_provider_name 是否正确配置
3. 尝试调低 reply_threshold 至 0.4
4. 使用 /heartflow_debug 查看消息是否被正常记录

Q: Bot 回复太频繁怎么办？

1. 调高 reply_threshold 至 0.75-0.85
2. 增大 immersive_mode_duration 至 60-120秒
3. 降低评分权重中的 willingness 值

Q: 沉浸模式一直不触发？

1. 确保 Bot 有正常响应能力
2. 检查白名单设置（如启用了白名单）
3. 查看日志中是否有错误信息

Q: 评分出现0分怎么办？

本版本已内置防零分机制：

- 评分范围为1-10分
- 即使模型返回0分，也会自动修正为1分
- 异常情况下使用5分作为默认值

Q: 如何查看心流判断的详细日志？

1. 使用 /heartflow_debug 查看消息记录
2. 使用 /heartflow 查看当前状态
3. 在 AstrBot 日志中搜索 [心流] 关键字

Q: 自定义提示词不生效？

1. 确保提示词中包含所有必要的占位符（见上方占位符列表）
2. 检查提示词格式是否正确（JSON中的花括号需要双写 {{}} ）
3. 查看日志是否有"提示词模板格式化失败"的错误

心流插件 (Heartflow Plugin)
<p align="center"> <img src="https://img.shields.io/badge/version-2.0.0-blue" alt="version"> <img src="https://img.shields.io/badge/license-MIT-green" alt="license"> <img src="https://img.shields.io/badge/platform-AstrBot-purple" alt="platform"> <img src="https://img.shields.io/badge/python-3.10+-yellow" alt="python"> </p> <p align="center"> <b>🌊 智能群聊主动回复系统 — 让你的 Bot 像真人一样"读懂"聊天氛围</b> </p>
✨ 功能特性
特性	说明
🧠 多维度评分判断	基于相关性、意愿度、社交性、时效性、连续性五个维度综合评估
🌊 沉浸模式	Bot 决定回复后自动进入沉浸模式，持续参与对话并实时刷新倒计时
🤖 小模型决策	使用轻量级模型判断是否回复，降低开销，响应更快
🎭 人设感知	自动获取并精简角色设定，让判断更贴合 Bot 性格
⏸️ 智能暂停	Bot 回复前自动暂停判断，防止连续多条回复
📊 灵活配置	评分权重、阈值、沉浸时长等均可自定义
🔒 白名单模式	支持仅在指定群聊中启用
💾 缓存优化	精简提示词缓存，减少重复调用
📋 工作流程
群聊消息 ──▶ 心流判断系统 ──▶ 是否回复？
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
                 ✅ 回复                ❌ 沉默
                    │                       │
            ┌───────┴───────┐               │
            ▼               ▼               │
      激活沉浸模式    暂停判断保护           │
            │               │               │
            ▼               ▼               ▼
      Bot 发送回复    恢复判断引擎    继续监听消息
            │
            ▼
      刷新沉浸倒计时
🛠️ 安装
方式一：通过 AstrBot 插件市场安装
在 AstrBot 管理面板 → 插件市场 → 搜索「心流」→ 安装
方式二：手动安装
cd AstrBot/data/plugins
git clone https://github.com/your-username/heartflow-plugin.git heartflow
重启 AstrBot 后即可在插件列表中看到。

⚙️ 配置说明
基础配置
配置项	类型	默认值	说明
enable_heartflow
bool	
false
是否启用心流插件
judge_provider_name
string	
""
小模型提供商名称（需在 AstrBot 中已配置）
reply_threshold
float	
0.6
回复触发阈值（0.0 ~ 1.0）
评分权重
每个维度的分数范围为
0-10
，最终加权计算综合得分：

权重项	默认值	说明
judge_relevance
0.25	相关性 — 消息与 Bot 角色/话题的关联程度
judge_willingness
0.20	意愿度 — Bot 是否有参与对话的"意愿"
judge_social
0.20	社交性 — 回复是否符合社交礼仪
judge_timing
0.15	时效性 — 当前时机是否适合插入对话
judge_continuity
0.20	连续性 — 是否是对上一句的直接回应
⚠️ 权重之和应为
1.0
，若不为
1.0
插件会自动归一化。

高级配置
配置项	类型	默认值	范围	说明
context_messages_count
int	5	1-50	上下文消息数量
judge_context_count
int	5	1-50	判断时参考的消息数量
judge_max_retries
int	3	0-10	小模型调用失败重试次数
immersive_mode_duration
int	30	0-300	沉浸模式持续时间（秒）
judgment_paused_timeout
int	60	5-300	判断暂停超时保护（秒）
judge_include_reasoning
bool	
true
—	是否让小模型输出推理过程
whitelist_enabled
bool	
false
—	是否启用群聊白名单
chat_whitelist
list	
[]
—	白名单群聊 ID 列表
🌊 沉浸模式详解
沉浸模式是心流插件的核心机制之一：

触发条件：当 Bot 判断应该回复时，自动激活沉浸模式
模式行为：使用更严格的判断规则，避免 Bot 过度参与闲聊
倒计时刷新：Bot 每次回复后，沉浸模式倒计时自动刷新
退出机制：倒计时结束后自动退出沉浸模式，恢复正常判断逻辑
沉浸模式下的回复规则
✅ 会回复的情况：

消息直接 @ 了机器人
消息明确向机器人提问
机器人发起的对话的延续（如有人回答了机器人的问题）
消息与机器人刚聊的话题直接相关
群聊冷场，机器人能自然活跃气氛
❌ 不会回复的情况：

用户之间的正常闲聊
用户之间的问答互动
简单的表情包、语气词
与机器人无关的日常对话
🛡️ 智能暂停机制
为防止 Bot 连续发送多条回复，插件实现了判断暂停机制：

Bot 判定回复 ──▶ 暂停判断 ──▶ Bot 发送消息 ──▶ 恢复判断
                    │
                    ├── 超时保护（默认60秒）
                    └── 自动恢复
当 Bot 已决定回复但尚未发送时，暂停小模型判断
Bot 回复发送后自动恢复
超时保护防止极端情况下判断永久暂停
🎮 管理命令
命令	权限	说明
/heartflow
所有人	查看当前心流状态（活跃度、回复率、沉浸模式状态等）
/heartflow_reset
管理员	重置当前群聊的心流状态
📁 项目结构
heartflow/
├── main.py              # 插件主文件
├── metadata.yaml        # 插件元数据
└── README.md            # 说明文档
🔧 依赖
AstrBot >= 1.0.0
Python >= 3.10
一个已配置的小模型提供商（用于判断逻辑）
💡 使用建议
阈值调节：默认阈值
0.6
适合大多数场景。若 Bot 回复过于频繁，可调高至
0.7-0.8
；若回复过少，可调低至
0.4-0.5

沉浸时长：建议设置为
30-60
秒。太短会频繁进出沉浸模式，太长可能导致 Bot 过度活跃

小模型选择：判断逻辑不需要太强的模型，轻量级模型即可胜任，推荐使用成本更低的模型

白名单：建议仅在需要 Bot 主动参与的群聊中启用，避免在所有群聊中触发

❓ 常见问题
<details> <summary><b>Q: Bot 完全不主动回复怎么办？</b></summary>
确认
enable_heartflow
已设为
true
检查
judge_provider_name
是否正确配置
尝试调低
reply_threshold
至
0.4
观察效果
查看日志中是否有
[心流]
相关输出
</details> <details> <summary><b>Q: Bot 回复太频繁怎么办？</b></summary>
调高
reply_threshold
（如
0.75
）
增大
immersive_mode_duration
减少沉浸模式触发频率
调整评分权重，降低
willingness
和
social
的权重
</details> <details> <summary><b>Q: 沉浸模式一直不触发？</b></summary>
确保 Bot 有正常响应消息的能力
检查是否设置了白名单但当前群不在列表中
查看日志确认判断流程是否正常执行
</details>
📄 License
MIT License

<p align="center">用 ❤️ 为 AstrBot 社区打造</p>

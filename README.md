心流插件 - 智能群聊主动回复系统
基于小模型判断是否应该主动参与群聊对话的高性能AstrBot插件。通过模拟人类在群聊中的参与模式，实现自然、适时的机器人主动回复。

✨ 功能特性
🎯 智能判断模式
正常模式：基于多维评分系统（相关性、意愿度、社交性、时机、连续性）综合判断
沉浸模式：机器人主动参与后，进入更积极的回复状态
智能跳过：自动识别指令、@他人消息、空消息等不需判断的情况
🧠 高性能优化
预编译正则表达式提升解析效率
多级JSON提取容错机制
消息缓冲区与定时清理
提示词智能压缩与缓存
⚙️ 精细化控制
可配置的评分权重与阈值
沉浸模式持续时间控制
判断暂停机制防止重复回复
白名单过滤与权限管理
📊 状态监控
实时心流状态查看
消息统计与回复率分析
沉浸模式状态跟踪
一键重置功能
📋 系统要求
Python 3.8+
AstrBot 框架
可用的小模型提供商（通过AstrBot配置）
🚀 安装与配置
1. 安装插件
将插件代码放入AstrBot的插件目录中。

2. 基础配置
在AstrBot配置中设置以下参数：

heartflow:
  enable_heartflow: false          # 启用心流插件
  judge_provider_name: ""          # 判断用的小模型提供商ID
  reply_threshold: 0.6             # 回复阈值 (0.0-1.0)
  context_messages_count: 5        # 上下文消息数量
  judge_context_count: 5           # 判断上下文数量
  judge_max_retries: 3             # JSON解析最大重试次数
  immersive_mode_duration: 30      # 沉浸模式持续时间(秒)
  judgment_paused_timeout: 60      # 判断暂停超时(秒)
  whitelist_enabled: false         # 启用白名单过滤
  chat_whitelist: []               # 群聊白名单
  judge_include_reasoning: true    # 包含判断理由
  
  # 评分权重 (总和应为1.0)
  judge_relevance: 0.25            # 相关性权重
  judge_willingness: 0.2           # 意愿度权重
  judge_social: 0.2                # 社交性权重
  judge_timing: 0.15               # 时机权重
  judge_continuity: 0.2            # 连续性权重
3. 启用心流
设置
enable_heartflow: true
即可启用心流功能。

🎮 使用说明
自动工作流程
群聊消息到达 → 心流插件接收
判断消息是否需要处理（跳过指令、@他人等）
根据当前模式（正常/沉浸）调用小模型判断
如果达到阈值，触发机器人回复
进入沉浸模式，保持积极参与状态
管理员命令
/heartflow
- 查看当前心流状态
/heartflow_reset
- 重置当前群聊的心流状态
状态解读
使用
/heartflow
命令可以查看：

上次回复时间
消息统计与回复率
当前阈值设置
沉浸模式状态
判断暂停状态
⚙️ 配置参数详解
核心参数
参数	类型	默认值	说明
enable_heartflow
bool	
false
是否启用心流插件
judge_provider_name
str	
""
用于判断的小模型提供商ID
reply_threshold
float	
0.6
综合评分达到此值才回复 (0.0-1.0)
性能参数
参数	类型	默认值	说明
context_messages_count
int	
5
分析对话流时使用的消息数量
judge_context_count
int	
5
发送给小模型判断的历史消息数量
judge_max_retries
int	
3
JSON解析失败时的最大重试次数
immersive_mode_duration
int	
30
沉浸模式持续时间 (秒)
judgment_paused_timeout
int	
60
判断暂停超时时间 (秒)
评分权重
参数	默认值	说明
judge_relevance
0.25
消息与机器人角色的相关性
judge_willingness
0.2
机器人回复的意愿度
judge_social
0.2
社交因素（如对话氛围）
judge_timing
0.15
回复时机是否合适
judge_continuity
0.2
对话连续性（直接回应上一句）
过滤参数
参数	类型	默认值	说明
whitelist_enabled
bool	
false
是否启用群聊白名单
chat_whitelist
list	
[]
启用心流的群聊ID列表
🏗️ 架构设计
核心组件
消息缓冲区 - 存储原始群聊消息
状态管理器 - 跟踪各群聊的心流状态
判断引擎 - 调用小模型进行回复判断
沉浸模式 - 管理积极参与状态
缓存系统 - 优化提示词处理
工作流程
消息到达 → 预处理 → 模式判断 → 小模型评估 → 评分计算 → 是否回复 → 状态更新
线程安全
使用
threading.RLock
保证多线程环境下的状态一致性。

🔧 技术细节
JSON提取机制
插件使用多级提取策略，确保从小模型返回的文本中稳定提取JSON：

直接解析完整文本
从代码块中提取
匹配最外层花括号
查找所有花括号并尝试解析
消息去重
相同发送者在5秒内的相同内容消息会被自动过滤。

内存管理
消息缓冲区自动清理1小时前的旧消息
系统提示词缓存24小时过期
定时清理机制防止内存泄漏
❓ 常见问题
Q: 为什么机器人没有主动回复？
A: 请检查：

enable_heartflow
是否设置为
true
judge_provider_name
是否配置了可用的小模型提供商
当前群聊是否在白名单中（如果启用了白名单）
回复阈值设置是否过高
Q: 如何调整回复的积极程度？
A: 调整
reply_threshold
参数：

降低阈值（如0.4）：机器人更积极回复
提高阈值（如0.8）：机器人更保守回复
Q: 沉浸模式是什么？
A: 当机器人主动回复后，会进入沉浸模式，在此期间：

回复判断更加积极
持续时间可配置
防止机器人过于频繁回复的暂停机制
Q: 判断暂停机制的作用？
A: 防止机器人连发多条消息。当机器人判定要回复时，会暂停判断，直到回复发出后才恢复。

📄 许可证
本插件为AstrBot生态系统的一部分，请遵循AstrBot的相关许可协议。

心流插件 - 让机器人在群聊中自然参与，就像人类一样思考何时该说话，何时该沉默。
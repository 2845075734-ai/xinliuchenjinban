"""
心流插件 - 智能群聊主动回复系统
基于小模型判断是否应该主动参与群聊对话
支持动态阈值：活跃时少说话，冷淡时多说话
支持评分模式和沉浸模式下：Bot 未实际回复时暂停判断，超时自动恢复
"""

import json
import re
import time
import datetime
import traceback
import threading
from collections import deque
from typing import Dict, Optional, List, Any, Set, Tuple
from dataclasses import dataclass, field

import astrbot.api.star as star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import logger
from astrbot.api.message_components import Plain, At


# ==================== 预编译正则 ====================

RE_CODE_BLOCK = re.compile(
    r"^(?:```(?:json)?|~~~(?:json)?|`{3,}(?:json)?)\s*\n?(.*?)\n?\s*(?:```|~~~|`{3,})$",
    re.DOTALL | re.IGNORECASE
)
RE_BRACE = re.compile(r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}", re.DOTALL)
RE_ALL_BRACES = re.compile(r"\{.*\}", re.DOTALL)
RE_XML_TAG = re.compile(r"<[^>]+>")
RE_URL = re.compile(r"https?://\S+")


# ==================== 评分配置 ====================

SCORE_MIN = 1.0
SCORE_MAX = 10.0
SCORE_DEFAULT = 5.0


# ==================== 内置默认评判提示词 ====================

DEFAULT_JUDGE_PROMPT = """你是群聊机器人决策系统，判断机器人是否应该回复消息。

## 重要规则
- 所有评分必须在 {score_min} 到 {score_max} 之间。
- 不要给出0分，最低分为 {score_min} 分。
- 即使完全不相关，也至少给 {score_min} 分。

## 评分维度

### 1. relevance 内容相关度
消息内容是否适合机器人参与讨论。

低分情况：
- 表情包、语气词、哈哈、嗯
- 完全无关的私人话题
- 群内专属梗

高分情况：
- 消息涉及机器人能回答的话题
- 消息是提问句
- 消息有信息价值，机器人可以补充有用内容

### 2. willingness 回复意愿
机器人是否想回复。

低分情况：
- 负面情绪宣泄，介入不合适
- 两人私密对话
- 机器人刚回复过，不应频繁插话

高分情况：
- 消息有趣或有深度
- 消息是求助类
- 机器人较久未回复，有参与意愿

### 3. social 社交适宜性
回复是否符合群聊氛围。

低分情况：
- 群内严肃讨论
- 管理员发言
- 两人正在对话，强行插入不合适

高分情况：
- 群内氛围活跃，多人参与
- 消息是对群体说的
- 机器人回复能活跃气氛

### 4. timing 时机恰当性
回复时机是否合适。

低分情况：
- 距上次回复不足1分钟
- 发送者可能还有后续补充
- 多人快速聊天中

高分情况：
- 距上次回复已过3分钟以上
- 消息刚发出，适合快速回应
- 群内长时间无人回复，适合打破沉默

### 5. continuity 对话连贯性
与对话历史的逻辑关联度。

低分情况：
- 突然转换话题
- 机器人已收尾的对话
- 消息是对其他用户的回复

高分情况：
- 消息是对机器人上一条回复的直接回应
- 消息延续机器人开启的话题
- 对话链路清晰

## 待判断消息
[{sender_name}] {message}

## 当前状态
- 群聊ID: {chat_id}
- 上次回复: {minutes_since_reply}分钟前
- 状态: {chat_context}
- 对话流: {chat_flow}
- 上次Bot回复: {last_bot_reply}
- 活跃度: {activity_desc} ({activity_level:.0%})

## 综合判断
阈值: {threshold}
分数范围: {score_min}-{score_max}分

## 返回格式
严格返回 JSON：
{{"relevance": 分数, "willingness": 分数, "social": 分数, "timing": 分数, "continuity": 分数{reasoning_part}}}
"""


# ==================== 数据类定义 ====================

@dataclass
class JudgeResult:
    relevance: float = SCORE_DEFAULT
    willingness: float = SCORE_DEFAULT
    social: float = SCORE_DEFAULT
    timing: float = SCORE_DEFAULT
    continuity: float = SCORE_DEFAULT
    reasoning: str = ""
    should_reply: bool = False
    confidence: float = 0.0
    overall_score: float = 0.0
    related_messages: List[str] = field(default_factory=list)

    def __post_init__(self):
        for attr in ("relevance", "willingness", "social", "timing", "continuity"):
            setattr(self, attr, clamp_score(getattr(self, attr)))


@dataclass
class RawMessage:
    sender_name: str
    sender_id: str
    content: str
    normalized_content: str
    timestamp: float
    is_bot: bool = False


@dataclass
class ChatState:
    last_reply_time: float = 0.0
    last_reset_date: str = ""
    total_messages: int = 0
    total_replies: int = 0
    last_cleanup_time: float = 0.0
    immersive_mode_end_time: float = 0.0

    # 判断暂停状态
    judgment_paused: bool = False
    judgment_paused_at: float = 0.0

    # 心流触发后，等待 Bot 实际回复
    pending_heartflow_reply: bool = False
    pending_heartflow_mode: str = ""

    last_recorded_message: str = ""

    # 活跃度缓存
    cached_activity_level: float = 0.0
    activity_cache_time: float = 0.0


# ==================== 工具函数 ====================

def clamp_score(
    v: Any,
    min_val: float = SCORE_MIN,
    max_val: float = SCORE_MAX,
    default: float = SCORE_DEFAULT
) -> float:
    try:
        val = float(v)
        if val <= 0:
            return default
        return max(min_val, min(max_val, val))
    except (TypeError, ValueError):
        return default


def safe_extract_score(
    data: Dict[str, Any],
    key: str,
    min_val: float = SCORE_MIN,
    max_val: float = SCORE_MAX
) -> float:
    raw_value = data.get(key)
    if raw_value is None or raw_value == "":
        return SCORE_DEFAULT

    try:
        val = float(raw_value)
    except (TypeError, ValueError):
        return SCORE_DEFAULT

    if val <= 0:
        return min_val

    return max(min_val, min(max_val, val))


def _extract_json(text: str) -> Dict[str, Any]:
    if not text or not (text := text.strip()):
        raise ValueError("输入文本为空")

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    if m := RE_CODE_BLOCK.search(text):
        try:
            result = json.loads(m.group(1).strip())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    if m := RE_BRACE.search(text):
        try:
            result = json.loads(m.group())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    for candidate in reversed(RE_ALL_BRACES.findall(text)):
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue

    raise ValueError(f"无法从文本中提取有效 JSON: {text[:100]}...")


def _validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    v = dict(config or {})

    v["reply_threshold"] = clamp_score(v.get("reply_threshold", 0.6), 0.0, 1.0, 0.6)

    limits = {
        "context_messages_count": (1, 50, 5),
        "judge_context_count": (1, 50, 5),
        "judge_max_retries": (0, 10, 3),
        "immersive_mode_duration": (0, 300, 30),
        "judgment_paused_timeout": (5, 300, 60),
        "dynamic_activity_window": (60, 600, 300),
    }

    for k, (mn, mx, df) in limits.items():
        v[k] = max(mn, min(mx, int(v.get(k, df))))

    for k in [
        "judge_relevance",
        "judge_willingness",
        "judge_social",
        "judge_timing",
        "judge_continuity",
    ]:
        v[k] = clamp_score(v.get(k, 0.2), 0.0, 1.0, 0.2)

    v["enable_dynamic_threshold"] = bool(v.get("enable_dynamic_threshold", False))
    v["dynamic_threshold_max"] = max(0.0, min(1.0, float(v.get("dynamic_threshold_max", 0.85))))
    v["dynamic_threshold_min"] = max(0.0, min(1.0, float(v.get("dynamic_threshold_min", 0.35))))

    if v["dynamic_threshold_min"] > v["dynamic_threshold_max"]:
        logger.warning(
            f"[心流] 配置警告: dynamic_threshold_min({v['dynamic_threshold_min']}) "
            f"> dynamic_threshold_max({v['dynamic_threshold_max']})，已自动调整。"
        )
        v["dynamic_threshold_min"] = max(0.0, v["dynamic_threshold_max"] - 0.1)

    try:
        weights_str = v.get("dynamic_activity_weights", "0.4,0.3,0.3")
        weights = [float(w.strip()) for w in weights_str.split(",")]
        if len(weights) == 3 and all(0 <= w <= 1 for w in weights):
            total = sum(weights)
            v["dynamic_activity_weights"] = [w / total for w in weights] if total > 0 else [0.4, 0.3, 0.3]
        else:
            v["dynamic_activity_weights"] = [0.4, 0.3, 0.3]
    except Exception:
        v["dynamic_activity_weights"] = [0.4, 0.3, 0.3]

    if "chat_whitelist" in v and not isinstance(v["chat_whitelist"], list):
        v["chat_whitelist"] = []

    if "judge_prompt" in v and not isinstance(v["judge_prompt"], str):
        v["judge_prompt"] = ""

    return v


# ==================== 主插件类 ====================

class HeartflowPlugin(star.Star):
    _lock = threading.RLock()

    def __init__(self, context: star.Context, config: Dict[str, Any]):
        super().__init__(context)
        self.config = _validate_config(config)

        self.judge_provider_name: str = self.config.get("judge_provider_name", "")
        self.reply_threshold: float = self.config.get("reply_threshold", 0.6)
        self.context_messages_count: int = self.config.get("context_messages_count", 5)
        self.judge_context_count: int = self.config.get("judge_context_count", self.context_messages_count)
        self.whitelist_enabled: bool = self.config.get("whitelist_enabled", False)
        self._whitelist_set: Set[str] = set(self.config.get("chat_whitelist", []))
        self.judge_include_reasoning: bool = self.config.get("judge_include_reasoning", True)
        self.judge_max_retries: int = self.config.get("judge_max_retries", 3)
        self.immersive_mode_duration: int = self.config.get("immersive_mode_duration", 30)
        self.judgment_paused_timeout: int = self.config.get("judgment_paused_timeout", 60)
        self.judge_prompt: str = self.config.get("judge_prompt", "")

        self.enable_dynamic_threshold: bool = self.config.get("enable_dynamic_threshold", False)
        self.dynamic_threshold_max: float = self.config.get("dynamic_threshold_max", 0.85)
        self.dynamic_threshold_min: float = self.config.get("dynamic_threshold_min", 0.35)
        self.dynamic_activity_window: int = self.config.get("dynamic_activity_window", 300)
        self.dynamic_activity_weights: List[float] = self.config.get("dynamic_activity_weights", [0.4, 0.3, 0.3])

        self.score_min: float = SCORE_MIN
        self.score_max: float = SCORE_MAX
        self.score_default: float = SCORE_DEFAULT

        self.chat_states: Dict[str, ChatState] = {}
        self._raw_msg_buffer: Dict[str, deque[RawMessage]] = {}
        self._raw_msg_buffer_size: int = max(self.context_messages_count, self.judge_context_count) * 4

        self.weights = {
            "relevance": self.config.get("judge_relevance", 0.25),
            "willingness": self.config.get("judge_willingness", 0.2),
            "social": self.config.get("judge_social", 0.2),
            "timing": self.config.get("judge_timing", 0.15),
            "continuity": self.config.get("judge_continuity", 0.2),
        }

        w_sum = sum(self.weights.values())
        if w_sum <= 0:
            self.weights = {
                "relevance": 0.25,
                "willingness": 0.2,
                "social": 0.2,
                "timing": 0.15,
                "continuity": 0.2,
            }
        elif abs(w_sum - 1.0) > 1e-6:
            logger.warning(f"[心流] 判断权重和不为1，当前和为{w_sum:.4f}，自动归一化")
            self.weights = {k: v / w_sum for k, v in self.weights.items()}

        self._buffer_cleanup_interval: float = 3600.0
        self.absolute_threshold = self.score_min + (self.score_max - self.score_min) * self.reply_threshold

        logger.info(
            f"[心流] 心流插件已初始化完成 | "
            f"评分范围: {self.score_min}-{self.score_max}分 | "
            f"基础阈值: {self.reply_threshold} | "
            f"动态阈值: {'启用' if self.enable_dynamic_threshold else '关闭'} "
            f"[{self.dynamic_threshold_min:.2f}-{self.dynamic_threshold_max:.2f}] | "
            f"沉浸模式持续时间: {self.immersive_mode_duration}秒 | "
            f"暂停超时: {self.judgment_paused_timeout}秒"
        )

    # ==================== 状态管理 ====================

    def _get_chat_state(self, chat_id: str) -> ChatState:
        with self._lock:
            return self.chat_states.setdefault(chat_id, ChatState())

    def _is_judgment_paused(self, chat_id: str) -> bool:
        state = self._get_chat_state(chat_id)

        if not state.judgment_paused:
            return False

        if state.judgment_paused_at > 0 and (time.time() - state.judgment_paused_at) > self.judgment_paused_timeout:
            logger.warning(
                f"[心流] ⚠️ 判断暂停超时（>{self.judgment_paused_timeout}秒），自动恢复判断: {chat_id} "
                f"| 模式: {state.pending_heartflow_mode or 'unknown'}"
            )
            state.judgment_paused = False
            state.judgment_paused_at = 0.0
            state.pending_heartflow_reply = False
            state.pending_heartflow_mode = ""
            return False

        return True

    def _set_judgment_paused(self, chat_id: str, paused: bool = True) -> None:
        state = self._get_chat_state(chat_id)

        if paused:
            state.judgment_paused = True
            state.judgment_paused_at = time.time()
            logger.debug(
                f"[心流] ⏸️ 已暂停小模型判断: {chat_id} "
                f"| 模式: {state.pending_heartflow_mode or 'unknown'}"
            )
        else:
            state.judgment_paused = False
            state.judgment_paused_at = 0.0
            state.pending_heartflow_reply = False
            state.pending_heartflow_mode = ""
            logger.debug(f"[心流] ▶️ 已恢复小模型判断: {chat_id}")

    def _get_minutes_since_last_reply(self, chat_id: str) -> int:
        t = self._get_chat_state(chat_id).last_reply_time
        return 999 if t == 0 else max(0, int((time.time() - t) / 60))

    # ==================== 消息过滤 ====================

    def _should_process_message(self, event: AstrMessageEvent) -> bool:
        if not self.config.get("enable_heartflow", False):
            return False

        if self.whitelist_enabled and event.unified_msg_origin not in self._whitelist_set:
            return False

        if event.get_sender_id() == event.get_self_id():
            return False

        if not (event.message_str and event.message_str.strip()):
            return False

        if event.is_at_or_wake_command:
            logger.debug("[心流] 消息已主动唤醒bot，跳过心流判断")
            return False

        try:
            for component in event.message_obj.message:
                if isinstance(component, At):
                    at_qq = str(component.qq)
                    if at_qq != event.get_self_id():
                        logger.debug(f"[心流] 消息@了其他用户({at_qq})，跳过心流判断")
                        return False
        except Exception as e:
            logger.debug(f"[心流] 解析At组件异常，按非@消息处理: {e}")

        msg_str = event.message_str.strip()
        if msg_str.startswith(("/", "#", "!", "！")):
            logger.debug(f"[心流] 消息为指令，跳过心流判断: {msg_str[:20]}...")
            return False

        return True

    # ==================== 消息清理与记录 ====================

    def _clean_message_content(self, text: str, max_len: int = 200) -> str:
        if not text:
            return ""
        text = RE_XML_TAG.sub("", text)
        text = RE_URL.sub("[链接]", text)
        text = text.strip()
        return (text[:max_len] + "...") if len(text) > max_len else text

    def _normalize_for_dedup(self, text: str) -> str:
        if not text:
            return ""
        text = RE_XML_TAG.sub("", text)
        text = RE_URL.sub("[链接]", text)
        return text.strip().lower()

    def _record_raw_message(self, event: AstrMessageEvent, is_bot: bool = False) -> bool:
        umo = event.unified_msg_origin

        with self._lock:
            buf = self._raw_msg_buffer.setdefault(umo, deque(maxlen=self._raw_msg_buffer_size))

            raw_content = event.message_str if not is_bot else (event.message_str or "")
            normalized = self._normalize_for_dedup(raw_content)

            if not normalized:
                return False

            sender_id = "bot" if is_bot else str(event.get_sender_id())

            if buf:
                last_msg = buf[-1]
                time_diff = time.time() - last_msg.timestamp

                if (
                    last_msg.sender_id == sender_id
                    and last_msg.normalized_content == normalized
                    and time_diff < 5
                ):
                    return False

            new_msg = RawMessage(
                sender_name="bot" if is_bot else event.get_sender_name(),
                sender_id=sender_id,
                content=raw_content,
                normalized_content=normalized,
                timestamp=time.time(),
                is_bot=is_bot,
            )

            buf.append(new_msg)

            state = self._get_chat_state(umo)
            state.last_recorded_message = normalized

            return True

    def _record_bot_reply_manual(self, umo: str, content: str) -> bool:
        if not content or not content.strip():
            return False

        normalized = self._normalize_for_dedup(content)
        if not normalized:
            return False

        with self._lock:
            buf = self._raw_msg_buffer.setdefault(umo, deque(maxlen=self._raw_msg_buffer_size))

            if buf:
                last_msg = buf[-1]
                if (
                    last_msg.is_bot
                    and last_msg.normalized_content == normalized
                    and (time.time() - last_msg.timestamp) < 5
                ):
                    return False

            new_msg = RawMessage(
                sender_name="bot",
                sender_id="bot",
                content=content,
                normalized_content=normalized,
                timestamp=time.time(),
                is_bot=True,
            )

            buf.append(new_msg)

            state = self._get_chat_state(umo)
            state.last_recorded_message = normalized

            return True

    def _get_raw_buffer(self, umo: str) -> List[RawMessage]:
        with self._lock:
            return list(self._raw_msg_buffer.get(umo, []))

    def _cleanup_old_messages(self, umo: str) -> None:
        now = time.time()
        state = self._get_chat_state(umo)

        if now - state.last_cleanup_time < self._buffer_cleanup_interval or umo not in self._raw_msg_buffer:
            return

        state.last_cleanup_time = now
        cutoff = now - 3600

        self._raw_msg_buffer[umo] = deque(
            (m for m in self._raw_msg_buffer[umo] if m.timestamp >= cutoff),
            maxlen=self._raw_msg_buffer_size,
        )

    # ==================== 活跃度计算 ====================

    def _calculate_activity_level(self, chat_id: str) -> float:
        state = self._get_chat_state(chat_id)
        now = time.time()

        if (now - state.activity_cache_time) < 5.0 and state.activity_cache_time > 0:
            return state.cached_activity_level

        msgs = self._get_raw_buffer(chat_id)
        window_start = now - self.dynamic_activity_window

        recent_user_msgs = [
            m for m in msgs
            if m.timestamp >= window_start and not m.is_bot
        ]

        if not recent_user_msgs:
            state.cached_activity_level = 0.0
            state.activity_cache_time = now
            return 0.0

        w_freq, w_interval, w_diversity = self.dynamic_activity_weights

        msg_count = len(recent_user_msgs)
        window_minutes = self.dynamic_activity_window / 60.0
        msgs_per_minute = msg_count / window_minutes
        freq_score = min(1.0, msgs_per_minute / 10.0)

        if len(recent_user_msgs) >= 2:
            intervals = [
                recent_user_msgs[i].timestamp - recent_user_msgs[i - 1].timestamp
                for i in range(1, len(recent_user_msgs))
            ]
            avg_interval = sum(intervals) / len(intervals)

            if avg_interval <= 5:
                interval_score = 1.0
            elif avg_interval >= 120:
                interval_score = 0.0
            else:
                interval_score = 1.0 - (avg_interval - 5) / 115.0
        else:
            interval_score = 0.0

        unique_senders = set(m.sender_id for m in recent_user_msgs)
        diversity_score = min(1.0, len(unique_senders) / 5.0)

        activity = (
            freq_score * w_freq
            + interval_score * w_interval
            + diversity_score * w_diversity
        )

        activity = round(max(0.0, min(1.0, activity)), 3)

        state.cached_activity_level = activity
        state.activity_cache_time = now

        return activity

    def _get_activity_description(self, activity: float) -> str:
        if activity >= 0.7:
            return "🔥非常活跃"
        elif activity >= 0.4:
            return "💬一般活跃"
        elif activity >= 0.15:
            return "😴较冷清"
        else:
            return "🥶几乎没人说话"

    def _get_dynamic_threshold(self, chat_id: str) -> Tuple[float, float, float]:
        if not self.enable_dynamic_threshold:
            return self.reply_threshold, 0.0, self.absolute_threshold

        activity = self._calculate_activity_level(chat_id)

        base = self.reply_threshold
        offset_range = self.dynamic_threshold_max - self.dynamic_threshold_min
        normalized_activity = (activity - 0.5) * 2

        smoothed = normalized_activity * (1 + 0.3 * abs(normalized_activity))
        smoothed = max(-1.0, min(1.0, smoothed))

        dynamic_threshold = base + smoothed * (offset_range / 2)
        dynamic_threshold = max(self.dynamic_threshold_min, min(self.dynamic_threshold_max, dynamic_threshold))
        dynamic_threshold = round(dynamic_threshold, 3)

        absolute_threshold = self.score_min + (self.score_max - self.score_min) * dynamic_threshold

        return dynamic_threshold, activity, absolute_threshold

    # ==================== 沉浸模式 ====================

    def _is_immersive_mode_active(self, chat_id: str) -> bool:
        if self.immersive_mode_duration <= 0:
            return False

        state = self._get_chat_state(chat_id)

        if state.immersive_mode_end_time == 0:
            return False

        if time.time() > state.immersive_mode_end_time:
            state.immersive_mode_end_time = 0
            return False

        return True

    def _get_immersive_mode_remaining(self, chat_id: str) -> int:
        state = self._get_chat_state(chat_id)

        if state.immersive_mode_end_time == 0:
            return 0

        remaining = int(state.immersive_mode_end_time - time.time())
        return max(0, remaining)

    def _activate_immersive_mode(self, chat_id: str) -> None:
        if self.immersive_mode_duration <= 0:
            return

        state = self._get_chat_state(chat_id)
        state.immersive_mode_end_time = time.time() + self.immersive_mode_duration

        logger.info(
            f"[心流] 🌊 沉浸模式已激活: {chat_id}, "
            f"持续 {self.immersive_mode_duration}秒"
        )

    def _refresh_immersive_mode(self, chat_id: str) -> None:
        if self.immersive_mode_duration <= 0:
            return

        state = self._get_chat_state(chat_id)

        if state.immersive_mode_end_time > 0:
            state.immersive_mode_end_time = time.time() + self.immersive_mode_duration
            logger.debug(f"[心流] 🔄 沉浸模式倒计时已刷新: {chat_id}")

    def _format_chat_history_for_immersive_mode(self, event: AstrMessageEvent) -> str:
        msgs = self._get_raw_buffer(event.unified_msg_origin)

        if not msgs:
            return "暂无聊天记录"

        history = []

        for msg in msgs[-20:]:
            prefix = "🤖 Bot" if msg.is_bot else f"👤 {msg.sender_name}"
            content = self._clean_message_content(msg.content, 150)
            history.append(f"{prefix}: {content}")

        return "\n".join(history) if history else "暂无聊天记录"

    # ==================== 上下文构建 ====================

    def _get_raw_history_for_context(self, event: AstrMessageEvent) -> List[RawMessage]:
        msgs = self._get_raw_buffer(event.unified_msg_origin)

        if msgs and not msgs[-1].is_bot and msgs[-1].content == event.message_str:
            msgs = msgs[:-1]

        return msgs[-self.judge_context_count:]

    def _build_optimized_contexts(self, raw_msgs: List[RawMessage]) -> List[Dict[str, str]]:
        contexts = []

        for msg in raw_msgs:
            content = self._clean_message_content(msg.content, 200)

            if not content:
                continue

            contexts.append({
                "role": "assistant" if msg.is_bot else "user",
                "content": content,
            })

        contexts.reverse()
        return contexts

    def _analyze_conversation_flow(self, event: AstrMessageEvent) -> str:
        msgs = self._get_raw_buffer(event.unified_msg_origin)[-self.context_messages_count:]

        if len(msgs) < 2:
            return "历史较短。"

        flow = []
        last, prev = msgs[-1], msgs[-2]

        if not last.is_bot and not prev.is_bot and any(q in prev.content for q in ("?", "？", "吗")):
            flow.append("用户似乎在回答前一条提问")

        if not flow:
            keywords = {"游戏", "工作", "吃", "学习", "代码", "问题", "群", "机器人"}
            topics = {kw for m in msgs for kw in keywords if kw in m.content}
            flow.append(f"话题: {','.join(topics)}" if topics else "闲聊")

        return " | ".join(flow)

    def _build_chat_context(self, event: AstrMessageEvent) -> str:
        s = self._get_chat_state(event.unified_msg_origin)

        rate = s.total_replies / max(1, s.total_messages) * 100
        mode = "沉浸" if self._is_immersive_mode_active(event.unified_msg_origin) else "正常"

        dynamic_threshold, activity, _ = self._get_dynamic_threshold(event.unified_msg_origin)
        activity_desc = self._get_activity_description(activity)

        return (
            f"活跃度:{activity_desc}({activity:.0%}) | "
            f"回复率:{rate:.1f}% | "
            f"模式:{mode} | "
            f"阈值:{dynamic_threshold:.2f} | "
            f"{datetime.datetime.now().strftime('%H:%M')}"
        )

    def _get_last_bot_reply(self, event: AstrMessageEvent) -> Optional[str]:
        for m in reversed(self._get_raw_buffer(event.unified_msg_origin)):
            if m.is_bot and m.content.strip():
                return m.content
        return None

    # ==================== 核心判断逻辑 ====================

    async def judge_with_tiny_model(self, event: AstrMessageEvent) -> JudgeResult:
        provider = self.context.get_provider_by_id(self.judge_provider_name)

        if not provider:
            logger.warning(f"[心流] 提供商未配置或不存在: {self.judge_provider_name}")
            return JudgeResult(should_reply=False, reasoning="提供商未配置或不存在")

        prompt_template = self.judge_prompt if self.judge_prompt.strip() else DEFAULT_JUDGE_PROMPT

        reasoning_part = ', "reasoning": "简短分析原因"' if self.judge_include_reasoning else ""
        mid_score = (self.score_min + self.score_max) / 2

        dynamic_threshold, activity, _ = self._get_dynamic_threshold(event.unified_msg_origin)
        activity_desc = self._get_activity_description(activity)

        try:
            judge_prompt = prompt_template.format(
                chat_id=event.unified_msg_origin,
                minutes_since_reply=self._get_minutes_since_last_reply(event.unified_msg_origin),
                chat_context=self._build_chat_context(event),
                chat_flow=self._analyze_conversation_flow(event),
                last_bot_reply=self._get_last_bot_reply(event) or "无",
                sender_name=event.get_sender_name(),
                message=event.message_str,
                threshold=dynamic_threshold,
                reasoning_part=reasoning_part,
                score_min=int(self.score_min),
                score_max=int(self.score_max),
                mid=int(mid_score),
                activity_level=activity,
                activity_desc=activity_desc,
            )
        except KeyError as e:
            logger.error(f"[心流] 提示词模板格式化失败，缺少键: {e}，使用默认提示词")
            judge_prompt = DEFAULT_JUDGE_PROMPT.format(
                chat_id=event.unified_msg_origin,
                minutes_since_reply=self._get_minutes_since_last_reply(event.unified_msg_origin),
                chat_context=self._build_chat_context(event),
                chat_flow=self._analyze_conversation_flow(event),
                last_bot_reply=self._get_last_bot_reply(event) or "无",
                sender_name=event.get_sender_name(),
                message=event.message_str,
                threshold=dynamic_threshold,
                reasoning_part=reasoning_part,
                score_min=int(self.score_min),
                score_max=int(self.score_max),
                mid=int(mid_score),
                activity_level=activity,
                activity_desc=activity_desc,
            )

        system_inst = (
            f"你是群聊机器人决策系统，严格返回JSON，不要输出其他内容。"
            f"所有分数必须在{int(self.score_min)}到{int(self.score_max)}之间，不能为0。"
        )

        contexts = self._build_optimized_contexts(self._get_raw_history_for_context(event))

        for attempt in range(self.judge_max_retries + 1):
            try:
                resp = await provider.text_chat(
                    prompt=judge_prompt,
                    contexts=contexts,
                    system=system_inst,
                    image_urls=[],
                )

                data = _extract_json(resp.completion_text.strip())

                scores = {
                    "relevance": safe_extract_score(data, "relevance", self.score_min, self.score_max),
                    "willingness": safe_extract_score(data, "willingness", self.score_min, self.score_max),
                    "social": safe_extract_score(data, "social", self.score_min, self.score_max),
                    "timing": safe_extract_score(data, "timing", self.score_min, self.score_max),
                    "continuity": safe_extract_score(data, "continuity", self.score_min, self.score_max),
                }

                normalized_scores = {
                    k: (v - self.score_min) / (self.score_max - self.score_min)
                    for k, v in scores.items()
                }

                overall = sum(normalized_scores[k] * self.weights[k] for k in self.weights)
                should_reply = overall >= dynamic_threshold
                avg_absolute_score = sum(scores.values()) / len(scores)

                logger.info(
                    f"[心流] 📊 评分模式: {overall:.3f}/{dynamic_threshold:.2f} "
                    f"(均分:{avg_absolute_score:.1f}分) | "
                    f"{'✅触发' if should_reply else '❌跳过'} | "
                    f"活跃度:{activity_desc}({activity:.0%}) | "
                    f"R:{scores['relevance']:.0f} "
                    f"W:{scores['willingness']:.0f} "
                    f"S:{scores['social']:.0f} "
                    f"T:{scores['timing']:.0f} "
                    f"C:{scores['continuity']:.0f}"
                )

                return JudgeResult(
                    **scores,
                    reasoning=data.get("reasoning", ""),
                    should_reply=should_reply,
                    confidence=overall,
                    overall_score=overall,
                )

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(
                    f"[心流] 评分模式JSON解析失败 "
                    f"(尝试 {attempt + 1}/{self.judge_max_retries + 1}): {e}"
                )
                if attempt == self.judge_max_retries:
                    return JudgeResult(should_reply=False, reasoning=f"JSON解析失败: {str(e)}")

            except Exception as e:
                logger.error(f"[心流] 评分模式判断异常: {e}")
                return JudgeResult(should_reply=False, reasoning=f"异常: {str(e)}")

        return JudgeResult(should_reply=False, reasoning="未知错误")

    async def judge_immersive_mode(self, event: AstrMessageEvent) -> JudgeResult:
        provider = self.context.get_provider_by_id(self.judge_provider_name)

        if not provider:
            return JudgeResult(should_reply=False, reasoning="提供商未配置或不存在")

        chat_history = self._format_chat_history_for_immersive_mode(event)
        remaining = self._get_immersive_mode_remaining(event.unified_msg_origin)

        _, activity, _ = self._get_dynamic_threshold(event.unified_msg_origin)
        activity_desc = self._get_activity_description(activity)

        system_inst = "你是严格的群聊机器人判断系统，严格返回JSON，不要输出其他内容。"
        immersive_reasoning_part = ', "reasoning": "简短原因"' if self.judge_include_reasoning else ""

        judge_prompt = f"""
## 沉浸模式判断规则

你的默认倾向是 **不回复**。只有在以下情况之一时才回复：

1. 消息直接 @ 了机器人或提到机器人名字
2. 消息明确向机器人提问
3. 消息是机器人发起的对话的延续
4. 消息内容与机器人刚的话题直接相关，且机器人回复能明显推进对话
5. 群聊中出现冷场，机器人的回复能自然活跃气氛

## 不应该回复的情况

- 两个用户之间的正常闲聊
- 用户之间的问答
- 简单表情、语气词
- 即使 Bot 最近活跃，也不应强行插入每条消息

## 当前状态

- 模式: 沉浸模式
- 沉浸模式剩余: {remaining}秒
- 群聊活跃度: {activity_desc} ({activity:.0%})

## 聊天记录

{chat_history}

## 待判断消息

[{event.get_sender_name()}] {event.message_str}

## 严格判断

根据上述规则严格判断。不要因为“沉浸模式”就倾向于回复。沉默也是好的选择。

严格返回 JSON：
{{"should_reply": true或false{immersive_reasoning_part}}}
"""

        for attempt in range(self.judge_max_retries + 1):
            try:
                resp = await provider.text_chat(
                    prompt=judge_prompt,
                    contexts=[],
                    system=system_inst,
                    image_urls=[],
                )

                data = _extract_json(resp.completion_text.strip())
                should_reply = bool(data.get("should_reply", False))

                logger.info(
                    f"[心流] 🌊 沉浸模式: {'✅触发' if should_reply else '❌跳过'} | "
                    f"剩余{remaining}秒 | "
                    f"活跃度:{activity_desc}({activity:.0%}) | "
                    f"{data.get('reasoning', '')}"
                )

                return JudgeResult(
                    should_reply=should_reply,
                    reasoning=data.get("reasoning", ""),
                    confidence=1.0 if should_reply else 0.0,
                    overall_score=1.0 if should_reply else 0.0,
                )

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(
                    f"[心流] 沉浸模式JSON解析失败 "
                    f"(尝试 {attempt + 1}/{self.judge_max_retries + 1}): {e}"
                )
                if attempt == self.judge_max_retries:
                    return JudgeResult(should_reply=False, reasoning="JSON解析失败")

            except Exception as e:
                logger.error(f"[心流] 沉浸模式判断异常: {e}")
                return JudgeResult(should_reply=False, reasoning=f"异常: {str(e)}")

        return JudgeResult(should_reply=False, reasoning="未知错误")

    # ==================== 事件处理器 ====================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self._should_process_message(event):
            return

        umo = event.unified_msg_origin
        state = self._get_chat_state(umo)

        if self._is_judgment_paused(umo):
            logger.debug(
                f"[心流] ⏸️ 判断已暂停，仅记录消息: "
                f"[{event.get_sender_name()}] {event.message_str[:30]}"
            )
            self._record_raw_message(event, is_bot=False)
            self._cleanup_old_messages(umo)
            state.total_messages += 1
            return

        self._record_raw_message(event, is_bot=False)
        self._cleanup_old_messages(umo)

        try:
            is_immersive = self._is_immersive_mode_active(umo)

            if is_immersive:
                res = await self.judge_immersive_mode(event)
            else:
                res = await self.judge_with_tiny_model(event)

            if res.should_reply:
                event.is_at_or_wake_command = True
                event.set_extra("heartflow_triggered", True)

                mode_name = "immersive" if is_immersive else "score"
                event.set_extra("heartflow_mode", mode_name)

                state.pending_heartflow_reply = True
                state.pending_heartflow_mode = mode_name

                self._set_judgment_paused(umo, paused=True)
                self._activate_immersive_mode(umo)

                logger.info(
                    f"[心流] 🎯 心流触发回复: {event.message_str[:50]}... "
                    f"| 模式: {'🌊沉浸模式' if is_immersive else '📊评分模式'} "
                    f"| 已暂停判断，等待 Bot 实际回复"
                )

            state.total_messages += 1

        except Exception as e:
            logger.error(f"[心流] 处理异常: {e}\n{traceback.format_exc()}")

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        if not self.config.get("enable_heartflow"):
            return

        umo = event.unified_msg_origin
        state = self._get_chat_state(umo)

        bot_reply_content = None
        has_reply = False

        try:
            res = event.get_result()
            if res and getattr(res, "chain", None):
                texts = []
                for component in res.chain:
                    if isinstance(component, Plain) and component.text.strip():
                        texts.append(component.text.strip())

                if texts:
                    bot_reply_content = "\n".join(texts)
                    has_reply = True

        except Exception as e:
            logger.debug(f"[心流] 获取回复结果异常: {e}")

        if has_reply and bot_reply_content:
            self._record_bot_reply_manual(umo, bot_reply_content)

        if event.get_extra("heartflow_triggered"):
            mode_name = (
                event.get_extra("heartflow_mode")
                or state.pending_heartflow_mode
                or "unknown"
            )

            mode_display = "🌊沉浸模式" if mode_name == "immersive" else "📊评分模式"

            if has_reply:
                state.last_reply_time = time.time()
                state.total_replies += 1

                if self._is_immersive_mode_active(umo):
                    self._refresh_immersive_mode(umo)
                else:
                    self._activate_immersive_mode(umo)

                self._set_judgment_paused(umo, paused=False)

                logger.info(
                    f"[心流] ✅ Bot回复完成，恢复小模型判断: {umo} "
                    f"| 模式: {mode_display}"
                )

            else:
                logger.warning(
                    f"[心流] ⚠️ 心流已触发，但未检测到 Bot 实际回复，继续暂停判断: {umo} "
                    f"| 模式: {mode_display} "
                    f"| 将在 {self.judgment_paused_timeout} 秒后自动恢复"
                )

            return

        elif event.is_at_or_wake_command:
            if has_reply:
                state.last_reply_time = time.time()

                if not self._is_immersive_mode_active(umo):
                    self._activate_immersive_mode(umo)
                else:
                    self._refresh_immersive_mode(umo)

                return

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        if not event.get_extra("heartflow_triggered"):
            return

        if req and hasattr(req, "system_prompt"):
            req.system_prompt = (req.system_prompt or "") + "\n（注意：本次是你主动参与群聊的，请自然切入。）"

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        if not event.get_extra("heartflow_triggered"):
            return

    # ==================== 管理员命令 ====================

    @filter.command("heartflow", help="查看心流插件运行状态")
    async def heartflow_status(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        s = self._get_chat_state(umo)

        rate = (s.total_replies / max(1, s.total_messages) * 100) if s.total_messages > 0 else 0.0
        is_immersive = self._is_immersive_mode_active(umo)
        remaining = self._get_immersive_mode_remaining(umo)
        is_paused = self._is_judgment_paused(umo)

        immersive_info = f"✅激活 ({remaining}秒)" if is_immersive else "❌未激活"
        paused_info = "⏸️ 已暂停" if is_paused else "▶️ 正常"
        prompt_info = "✅自定义" if self.judge_prompt.strip() else "📄内置默认"

        buffer_size = len(self._raw_msg_buffer.get(umo, []))

        dynamic_threshold, activity, absolute_threshold = self._get_dynamic_threshold(umo)
        activity_desc = self._get_activity_description(activity)

        if self.enable_dynamic_threshold:
            dynamic_info = f"✅启用 [{self.dynamic_threshold_min:.2f}-{self.dynamic_threshold_max:.2f}]"
            current_threshold_info = f"当前: {dynamic_threshold:.2f} (绝对值: {absolute_threshold:.1f}分)"
        else:
            dynamic_info = "❌关闭"
            current_threshold_info = f"固定值: {self.reply_threshold:.2f} (绝对值: {self.absolute_threshold:.1f}分)"

        pending_info = "✅是" if s.pending_heartflow_reply else "❌否"
        pending_mode = s.pending_heartflow_mode or "无"

        event.set_result(event.plain_result(f"""
🔮 **心流状态**
━━━━━━━━━━━━━━━━━━━━
📊 **统计信息**
• 上次回复: {self._get_minutes_since_last_reply(umo)}分钟前
• 消息总数: {s.total_messages}
• 实际回复总数: {s.total_replies} ({rate:.1f}%)
• 缓冲区: {buffer_size}条消息

⚙️ **配置信息**
• 评分范围: {int(self.score_min)}-{int(self.score_max)}分
• 基础阈值: {self.reply_threshold}
• 沉浸持续: {self.immersive_mode_duration}秒
• 暂停超时: {self.judgment_paused_timeout}秒
• 评判提示词: {prompt_info}

🎚️ **动态阈值**
• 开关: {dynamic_info}
• {current_threshold_info}

📊 **当前活跃度**
• 状态: {activity_desc} ({activity:.0%})
• 计算窗口: {self.dynamic_activity_window}秒

🌊 **当前模式**
• 沉浸模式: {immersive_info}
• 判断状态: {paused_info}
• 等待Bot回复: {pending_info}
• 等待模式: {pending_mode}
━━━━━━━━━━━━━━━━━━━━
💡 提示: 使用 /heartflow_debug 查看消息记录
"""))

    @filter.command("heartflow_debug", help="查看最近的消息记录（调试用）")
    async def heartflow_debug(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        msgs = self._get_raw_buffer(umo)

        if not msgs:
            event.set_result(event.plain_result("📭 缓冲区为空，暂无消息记录"))
            return

        lines = ["📋 **最近消息记录**（最多显示20条）\n━━━━━━━━━━━━━━"]

        display_msgs = msgs[-20:]

        for msg in display_msgs:
            prefix = "🤖" if msg.is_bot else "👤"
            time_str = datetime.datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S")
            content = msg.content[:60] + "..." if len(msg.content) > 60 else msg.content

            if not content:
                content = "[空消息]"

            lines.append(f"{prefix} [{time_str}] {msg.sender_name}: {content}")

        lines.append("━━━━━━━━━━")
        lines.append(f"📊 共 {len(msgs)} 条记录，显示最近 {len(display_msgs)} 条")

        event.set_result(event.plain_result("\n".join(lines)))

    @filter.command("heartflow_clear", help="清除当前群的消息缓冲区")
    async def heartflow_clear_buffer(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin

        with self._lock:
            if umo in self._raw_msg_buffer:
                count = len(self._raw_msg_buffer[umo])
                self._raw_msg_buffer[umo].clear()

                event.set_result(event.plain_result(
                    f"✅ 已清除 {count} 条缓冲消息\n"
                    f"💡 心流判断将从零开始积累上下文"
                ))
            else:
                event.set_result(event.plain_result("📭 缓冲区为空，无需清除"))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("heartflow_reset", help="重置当前群的所有心流状态（管理员）")
    async def heartflow_reset(self, event: AstrMessageEvent):
        cid = event.unified_msg_origin

        state = self._get_chat_state(cid)
        old_messages = state.total_messages
        old_replies = state.total_replies
        buffer_count = len(self._raw_msg_buffer.get(cid, []))

        with self._lock:
            self.chat_states.pop(cid, None)
            self._raw_msg_buffer.pop(cid, None)

        event.set_result(event.plain_result(
            f"✅ **心流状态已完全重置**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🗑️ 已清除:\n"
            f"• 消息统计: {old_messages}条\n"
            f"• 回复统计: {old_replies}条\n"
            f"• 缓冲区: {buffer_count}条\n"
            f"• 沉浸模式: 已关闭\n"
            f"• 判断暂停: 已恢复\n"
            f"━━━━━━━━\n"
            f"💡 心流将从零开始重新计数"
        ))

    @filter.command("heartflow_help", help="显示心流插件所有命令说明")
    async def heartflow_help(self, event: AstrMessageEvent):
        help_text = """
🔮 **心流插件 - 命令帮助**
━━━━━━━━━━━━━━

📋 **可用命令**

• `/heartflow`
  查看心流插件的运行状态、统计数据和当前配置

• `/heartflow_debug`
 显示最近20条消息记录，用于调试心流判断逻辑

• `/heartflow_clear`
 清除当前群的消息缓冲区

• `/heartflow_reset` 🔒管理员
  完全重置心流状态

• `/heartflow_help`
  显示本帮助信息

━━━━━━

📖 **功能说明**

心流插件会智能判断是否应该主动回复群聊消息。

评分模式基于5个维度：
• 内容相关度
• 回复意愿
• 社交适宜性
• 时机恰当性
• 对话连贯性

当评分达到阈值时，会自动触发 Bot 回复并进入沉浸模式。

🌊 **沉浸模式**

沉浸模式下使用简化判断逻辑：
直接判断是否应该回复，而不是五维评分。

⏸️ **暂停保护**

评分模式和沉浸模式下，只要 Bot 判定要回复：
• 会先暂停小模型判断
• 等待 Bot 实际回复
• Bot 回复成功后恢复判断
• 如果 Bot 没有实际回复，会在配置的超时时间后自动恢复

🎚️ **动态阈值**

开启后，阈值会根据群聊活跃度自动调整：
• 非常活跃 → 阈值升高 → 更克制
• 群聊冷清 → 阈值降低 → 更主动

━━━━━━━━━━━━━━
"""
        event.set_result(event.plain_result(help_text))
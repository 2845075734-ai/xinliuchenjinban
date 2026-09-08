"""
心流插件 - 智能群聊主动回复系统 v4.27+
支持动态阈值 / 评分模式 / 沉浸模式 / 防抖 / 群聊独立配置
"""

import json
import re
import time
import datetime
import traceback
import threading
import asyncio
from collections import deque
from typing import Dict, Optional, List, Any, Set, Tuple
from dataclasses import dataclass, field

import astrbot.api.star as star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import logger
from astrbot.api.message_components import Plain, At

try:
    from astrbot.core.agent.message import TextPart
except ImportError:
    TextPart = None
    logger.warning("[心流] 当前版本不支持 TextPart，群聊上下文注入将降级为追加 system_prompt")

# ============================================================
# 正则
# ============================================================
RE_CODE_BLOCK = re.compile(
    r"^(?:```(?:json)?|~~~(?:json)?|`{3,}(?:json)?)\s*\n?(.*?)\n?\s*(?:```|~~~|`{3,})$",
    re.DOTALL | re.IGNORECASE,
)
RE_BRACE = re.compile(r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}", re.DOTALL)
RE_ALL_BRACES = re.compile(r"\{.*\}", re.DOTALL)
RE_XML_TAG = re.compile(r"<[^>]+>")
RE_URL = re.compile(r"https?://\S+")

# ============================================================
# 评分常量
# ============================================================
SCORE_MIN = 1.0
SCORE_MAX = 10.0
SCORE_DEFAULT = 5.0

# ============================================================
# 默认评判提示词模板（不修改）
# ============================================================
DEFAULT_JUDGE_PROMPT = """你是群聊机器人决策系统，判断机器人是否应该回复消息。

## ⚠️ 最高优先级：关注机器人自己的发言
对话历史（contexts）中，role 为 assistant 的消息是【机器人自己】之前说的话，role 为 user 的消息是群友说的话（带 [昵称] 前缀）。
你必须按以下步骤思考：
1. 先回顾机器人最近说了什么（见下方"上次Bot回复"，以及对话历史中的 assistant 消息）
2. 判断当前消息是否与机器人的发言存在关联：追问、回应、赞同、反驳、补充、顺着话题继续
3. 如果当前消息是在回应机器人说过的话，continuity 必须给高分

## 评分规则
- 所有评分必须在 {score_min} 到 {score_max} 之间。
- 不要给出0分，最低分为 {score_min} 分。
- 即使完全不相关，也至少给 {score_min} 分。

## 评分维度

### 1. relevance 内容相关度
消息内容是否适合机器人参与讨论。
低分情况：表情包、语气词、哈哈、嗯、完全无关的私人话题、群内专属梗
高分情况：涉及机器人能回答的话题、提问句、有信息价值

### 2. willingness 回复意愿
机器人是否想回复。
低分情况：负面情绪宣泄、两人私密对话、机器人刚回复过
高分情况：消息有趣或有深度、求助类、机器人较久未回复

### 3. social 社交适宜性
回复是否符合群聊氛围。
低分情况：严肃讨论、管理员发言、两人正在对话强行插入
高分情况：氛围活跃多人参与、对群体说的、能活跃气氛

### 4. timing 时机恰当性
回复时机是否合适。
低分情况：距上次回复不足1分钟、发送者可能还有后续、多人快速聊天
高分情况：距上次已过3分钟以上、消息刚发出、长时间无人回复

### 5. continuity 对话连贯性（重点维度）
当前消息与【机器人自己的发言】的逻辑关联度。
低分情况：突然转换话题、已收尾的对话、明显是对其他群友回复
高分情况：对机器人回复的回应/追问/反驳、延续机器人话题、提及机器人说过的内容

## 待判断消息
[{sender_name}] {message}

## 当前状态
- 群聊ID: {chat_id}
- 上次回复: {minutes_since_reply}分钟前
- 状态: {chat_context}
- 对话流: {chat_flow}
- ⭐ 上次Bot回复: {last_bot_reply}
- 活跃度: {activity_desc} ({activity_level:.0%})

## 综合判断
阈值: {threshold}
分数范围: {score_min}-{score_max}分

## 返回格式
严格返回 JSON，不要输出任何其他内容：
{{"relevance": 分数, "willingness": 分数, "social": 分数, "timing": 分数, "continuity": 分数{reasoning_part}}}
"""

# ============================================================
# 数据类
# ============================================================
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
    total_messages: int = 0
    total_replies: int = 0
    last_cleanup_time: float = 0.0
    immersive_mode_end_time: float = 0.0
    judgment_paused: bool = False
    judgment_paused_at: float = 0.0
    pending_heartflow_reply: bool = False
    pending_heartflow_mode: str = ""
    last_recorded_message: str = ""
    cached_activity_level: float = 0.0
    activity_cache_time: float = 0.0


# ============================================================
# 工具函数
# ============================================================
def clamp_score(v: Any, min_val: float = SCORE_MIN, max_val: float = SCORE_MAX,
                default: float = SCORE_DEFAULT) -> float:
    try:
        val = float(v)
        return default if val <= 0 else max(min_val, min(max_val, val))
    except (TypeError, ValueError):
        return default


def safe_extract_score(data: Dict[str, Any], key: str,
                       min_val: float = SCORE_MIN, max_val: float = SCORE_MAX) -> float:
    raw = data.get(key)
    if raw is None or raw == "":
        return SCORE_DEFAULT
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return SCORE_DEFAULT
    return min_val if val <= 0 else max(min_val, min(max_val, val))


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

    for k in ("judge_relevance", "judge_willingness", "judge_social",
              "judge_timing", "judge_continuity"):
        v[k] = clamp_score(v.get(k, 0.2), 0.0, 1.0, 0.2)

    v["enable_dynamic_threshold"] = bool(v.get("enable_dynamic_threshold", False))
    v["dynamic_threshold_max"] = max(0.0, min(1.0, float(v.get("dynamic_threshold_max", 0.85))))
    v["dynamic_threshold_min"] = max(0.0, min(1.0, float(v.get("dynamic_threshold_min", 0.35))))
    if v["dynamic_threshold_min"] > v["dynamic_threshold_max"]:
        v["dynamic_threshold_min"] = max(0.0, v["dynamic_threshold_max"] - 0.1)

    try:
        raw_w = v.get("dynamic_activity_weights", "0.4,0.3,0.3")
        weights = [float(w.strip()) for w in raw_w.split(",")] if isinstance(raw_w, str) else [float(w) for w in raw_w] if isinstance(raw_w, list) else [0.4, 0.3, 0.3]
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

    v["enable_debounce"] = bool(v.get("enable_debounce", True))
    v["debounce_delay"] = max(0.5, min(10.0, float(v.get("debounce_delay", 2.0))))
    v["max_debounce_wait"] = max(1.0, min(15.0, float(v.get("max_debounce_wait", 5.0))))
    v["enable_detailed_log"] = bool(v.get("enable_detailed_log", False))
    return v
# ============================================================
# 插件主类
# ============================================================
class HeartflowPlugin(star.Star):
    _lock = threading.RLock()

    def __init__(self, context: star.Context, config: Dict[str, Any]):
        super().__init__(context)
        self.config = _validate_config(config)

        # 基础配置
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

        # 动态阈值全局配置
        self.enable_dynamic_threshold: bool = self.config.get("enable_dynamic_threshold", False)
        self.dynamic_threshold_max: float = self.config.get("dynamic_threshold_max", 0.85)
        self.dynamic_threshold_min: float = self.config.get("dynamic_threshold_min", 0.35)
        self.dynamic_activity_window: int = self.config.get("dynamic_activity_window", 300)
        self.dynamic_activity_weights: List[float] = self.config.get("dynamic_activity_weights", [0.4, 0.3, 0.3])

        # 独立群聊阈值配置解析 (支持 template_list)
        self.group_threshold_configs: Dict[str, Dict[str, float]] = {}
        for item in self.config.get("group_threshold_configs", []):
            chat_id = str(item.get("chat_id", "")).strip()
            if chat_id:
                g_reply = clamp_score(item.get("reply_threshold", self.reply_threshold), 0.0, 1.0, self.reply_threshold)
                g_max = clamp_score(item.get("dynamic_threshold_max", self.dynamic_threshold_max), 0.0, 1.0, self.dynamic_threshold_max)
                g_min = clamp_score(item.get("dynamic_threshold_min", self.dynamic_threshold_min), 0.0, 1.0, self.dynamic_threshold_min)
                if g_min > g_max:
                    g_min = max(0.0, g_max - 0.1)
                self.group_threshold_configs[chat_id] = {
                    "reply_threshold": g_reply,
                    "dynamic_threshold_max": g_max,
                    "dynamic_threshold_min": g_min,
                }

        # 评分常量与权重
        self.score_min, self.score_max, self.score_default = SCORE_MIN, SCORE_MAX, SCORE_DEFAULT
        self.weights = {
            "relevance": self.config.get("judge_relevance", 0.25),
            "willingness": self.config.get("judge_willingness", 0.2),
            "social": self.config.get("judge_social", 0.2),
            "timing": self.config.get("judge_timing", 0.15),
            "continuity": self.config.get("judge_continuity", 0.2),
        }
        w_sum = sum(self.weights.values())
        if w_sum <= 0 or abs(w_sum - 1.0) > 1e-6:
            self.weights = {k: v / w_sum for k, v in self.weights.items()} if w_sum > 0 else {"relevance": 0.25, "willingness": 0.2, "social": 0.2, "timing": 0.15, "continuity": 0.2}

        # 状态与缓冲
        self.chat_states: Dict[str, ChatState] = {}
        self._raw_msg_buffer: Dict[str, deque[RawMessage]] = {}
        self._raw_msg_buffer_size: int = max(self.context_messages_count, self.judge_context_count) * 4
        self._pause_restore_tasks: Dict[str, asyncio.Task] = {}
        self._buffer_cleanup_interval: float = 3600.0
        self.absolute_threshold = self.score_min + (self.score_max - self.score_min) * self.reply_threshold

        # 防抖与日志
        self.enable_debounce: bool = self.config.get("enable_debounce", True)
        self.debounce_delay: float = float(self.config.get("debounce_delay", 2.0))
        self.max_debounce_wait: float = float(self.config.get("max_debounce_wait", 5.0))
        self._debounce_states: Dict[str, Dict[str, Any]] = {}
        self.enable_detailed_log: bool = self.config.get("enable_detailed_log", False)

        logger.info(f"[心流] 初始化完成 | 基础阈值: {self.reply_threshold} | 独立配置群数: {len(self.group_threshold_configs)} | 防抖: {'开' if self.enable_debounce else '关'}")

    def _log_detail(self, msg: str) -> None:
        if self.enable_detailed_log:
            logger.info(msg)

    # ============================================================
    # 状态与任务管理
    # ============================================================
    def _get_chat_state(self, chat_id: str) -> ChatState:
        with self._lock:
            return self.chat_states.setdefault(chat_id, ChatState())

    def _cancel_pause_restore_task(self, chat_id: str) -> None:
        task = self._pause_restore_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    def _schedule_pause_restore_task(self, chat_id: str) -> None:
        self._cancel_pause_restore_task(chat_id)
        try:
            loop = asyncio.get_running_loop()
            self._pause_restore_tasks[chat_id] = loop.create_task(self._delayed_restore_judgment(chat_id))
        except RuntimeError:
            pass

    async def _delayed_restore_judgment(self, chat_id: str) -> None:
        try:
            await asyncio.sleep(self.judgment_paused_timeout)
            state = self._get_chat_state(chat_id)
            if state.judgment_paused and state.pending_heartflow_reply:
                logger.warning(f"[心流] ⚠️ 判断暂停后台超时，自动恢复: {chat_id}")
                self._set_judgment_paused(chat_id, paused=False)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[心流] 暂停恢复异常: {e}")

    def _is_judgment_paused(self, chat_id: str) -> bool:
        state = self._get_chat_state(chat_id)
        if not state.judgment_paused:
            return False
        if state.judgment_paused_at > 0 and (time.time() - state.judgment_paused_at) > self.judgment_paused_timeout:
            self._set_judgment_paused(chat_id, paused=False)
            return False
        return True

    def _set_judgment_paused(self, chat_id: str, paused: bool = True) -> None:
        state = self._get_chat_state(chat_id)
        if paused:
            state.judgment_paused = True
            state.judgment_paused_at = time.time()
            self._schedule_pause_restore_task(chat_id)
            self._log_detail(f"[心流] ⏸️ 暂停小模型判断: {chat_id}")
        else:
            state.judgment_paused = False
            state.judgment_paused_at = 0.0
            state.pending_heartflow_reply = False
            state.pending_heartflow_mode = ""
            self._cancel_pause_restore_task(chat_id)
            self._log_detail(f"[心流] ▶️ 恢复小模型判断: {chat_id}")

    def _get_minutes_since_last_reply(self, chat_id: str) -> int:
        t = self._get_chat_state(chat_id).last_reply_time
        return 999 if t == 0 else max(0, int((time.time() - t) / 60))

    # ============================================================
    # 消息过滤与缓冲
    # ============================================================
    def _should_process_message(self, event: AstrMessageEvent) -> bool:
        if not self.config.get("enable_heartflow", False): return False
        if self.whitelist_enabled and event.unified_msg_origin not in self._whitelist_set: return False
        if event.get_sender_id() == event.get_self_id(): return False
        if not (event.message_str and event.message_str.strip()): return False

        if event.is_at_or_wake_command:
            umo = event.unified_msg_origin
            debounce_state = self._debounce_states.get(umo)
            if debounce_state and debounce_state.get("active"):
                debounce_state["cancelled"] = True
            return False

        try:
            for component in event.message_obj.message:
                if isinstance(component, At) and str(component.qq) != event.get_self_id():
                    return False
        except Exception:
            pass

        if event.message_str.strip().startswith(("/", "#", "!", "！")):
            return False
        return True

    def _clean_message_content(self, text: str, max_len: int = 200) -> str:
        if not text: return ""
        text = RE_XML_TAG.sub("", text)
        text = RE_URL.sub("[链接]", text).strip()
        return (text[:max_len] + "...") if len(text) > max_len else text

    def _normalize_for_dedup(self, text: str) -> str:
        if not text: return ""
        return RE_URL.sub("[链接]", RE_XML_TAG.sub("", text)).strip().lower()

    def _record_raw_message(self, event: AstrMessageEvent, is_bot: bool = False) -> bool:
        umo = event.unified_msg_origin
        with self._lock:
            buf = self._raw_msg_buffer.setdefault(umo, deque(maxlen=self._raw_msg_buffer_size))
            raw_content = event.message_str if not is_bot else (event.message_str or "")
            normalized = self._normalize_for_dedup(raw_content)
            if not normalized: return False
            sender_id = "bot" if is_bot else str(event.get_sender_id())
            if buf and buf[-1].sender_id == sender_id and buf[-1].normalized_content == normalized and (time.time() - buf[-1].timestamp) < 5:
                return False
            buf.append(RawMessage(
                sender_name="bot" if is_bot else event.get_sender_name(),
                sender_id=sender_id, content=raw_content,
                normalized_content=normalized, timestamp=time.time(), is_bot=is_bot,
            ))
            self._get_chat_state(umo).last_recorded_message = normalized
            return True

    def _record_bot_reply_manual(self, umo: str, content: str) -> bool:
        if not content or not content.strip(): return False
        normalized = self._normalize_for_dedup(content)
        if not normalized: return False
        with self._lock:
            buf = self._raw_msg_buffer.setdefault(umo, deque(maxlen=self._raw_msg_buffer_size))
            if buf and buf[-1].is_bot and buf[-1].normalized_content == normalized and (time.time() - buf[-1].timestamp) < 5:
                return False
            buf.append(RawMessage(sender_name="bot", sender_id="bot", content=content, normalized_content=normalized, timestamp=time.time(), is_bot=True))
            self._get_chat_state(umo).last_recorded_message = normalized
            return True

    def _get_raw_buffer(self, umo: str) -> List[RawMessage]:
        with self._lock:
            return list(self._raw_msg_buffer.get(umo, []))

    def _cleanup_old_messages(self, umo: str) -> None:
        now = time.time()
        state = self._get_chat_state(umo)
        if now - state.last_cleanup_time < self._buffer_cleanup_interval or umo not in self._raw_msg_buffer: return
        state.last_cleanup_time = now
        self._raw_msg_buffer[umo] = deque((m for m in self._raw_msg_buffer[umo] if m.timestamp >= now - 3600), maxlen=self._raw_msg_buffer_size)

    # ============================================================
    # 活跃度与动态阈值 (支持群聊独立配置)
    # ============================================================
    def _calculate_activity_level(self, chat_id: str) -> float:
        state = self._get_chat_state(chat_id)
        now = time.time()
        if (now - state.activity_cache_time) < 5.0 and state.activity_cache_time > 0:
            return state.cached_activity_level
        
        msgs = self._get_raw_buffer(chat_id)
        recent_msgs = [m for m in msgs if m.timestamp >= now - self.dynamic_activity_window and not m.is_bot]
        if not recent_msgs:
            state.cached_activity_level = 0.0
            state.activity_cache_time = now
            return 0.0

        w_freq, w_interval, w_diversity = self.dynamic_activity_weights
        freq_score = min(1.0, (len(recent_msgs) / (self.dynamic_activity_window / 60.0)) / 10.0)
        
        interval_score = 0.0
        if len(recent_msgs) >= 2:
            intervals = [recent_msgs[i].timestamp - recent_msgs[i - 1].timestamp for i in range(1, len(recent_msgs))]
            avg_interval = sum(intervals) / len(intervals)
            interval_score = 1.0 if avg_interval <= 5 else (0.0 if avg_interval >= 120 else 1.0 - (avg_interval - 5) / 115.0)
            
        diversity_score = min(1.0, len(set(m.sender_id for m in recent_msgs)) / 5.0)
        activity = round(max(0.0, min(1.0, freq_score * w_freq + interval_score * w_interval + diversity_score * w_diversity)), 3)
        
        state.cached_activity_level = activity
        state.activity_cache_time = now
        return activity

    def _get_activity_description(self, activity: float) -> str:
        if activity >= 0.7: return "🔥非常活跃"
        if activity >= 0.4: return "💬一般活跃"
        if activity >= 0.15: return "😴较冷清"
        return "🥶几乎没人说话"

    def _get_group_config_info(self, chat_id: str) -> str:
        if chat_id in self.group_threshold_configs: return "🎯 独立配置"
        parts = chat_id.split(":")
        if len(parts) >= 3 and parts[-1] in self.group_threshold_configs: return f"🎯 独立配置 (群号 {parts[-1]})"
        return "🌐 全局配置"

    def _get_dynamic_threshold(self, chat_id: str) -> Tuple[float, float, float]:
        # 1. 获取当前群聊的特定配置，支持纯群号匹配
        group_cfg = self.group_threshold_configs.get(chat_id)
        if not group_cfg:
            parts = chat_id.split(":")
            if len(parts) >= 3:
                group_cfg = self.group_threshold_configs.get(parts[-1])
        
        # 2. 确定使用的阈值参数
        if group_cfg:
            base, t_max, t_min = group_cfg["reply_threshold"], group_cfg["dynamic_threshold_max"], group_cfg["dynamic_threshold_min"]
        else:
            base, t_max, t_min = self.reply_threshold, self.dynamic_threshold_max, self.dynamic_threshold_min

        # 3. 计算动态阈值
        if not self.enable_dynamic_threshold:
            return base, 0.0, self.score_min + (self.score_max - self.score_min) * base

        activity = self._calculate_activity_level(chat_id)
        offset_range = t_max - t_min
        normalized_activity = (activity - 0.5) * 2
        smoothed = max(-1.0, min(1.0, normalized_activity * (1 + 0.3 * abs(normalized_activity))))
        dynamic_threshold = round(max(t_min, min(t_max, base + smoothed * (offset_range / 2))), 3)
        absolute_threshold = self.score_min + (self.score_max - self.score_min) * dynamic_threshold
        
        return dynamic_threshold, activity, absolute_threshold
    # ============================================================
    # 沉浸模式管理
    # ============================================================
    def _is_immersive_mode_active(self, chat_id: str) -> bool:
        if self.immersive_mode_duration <= 0: return False
        state = self._get_chat_state(chat_id)
        if state.immersive_mode_end_time == 0: return False
        if time.time() > state.immersive_mode_end_time:
            state.immersive_mode_end_time = 0
            return False
        return True

    def _get_immersive_mode_remaining(self, chat_id: str) -> int:
        state = self._get_chat_state(chat_id)
        if state.immersive_mode_end_time == 0: return 0
        return max(0, int(state.immersive_mode_end_time - time.time()))

    def _activate_immersive_mode(self, chat_id: str) -> None:
        if self.immersive_mode_duration <= 0: return
        state = self._get_chat_state(chat_id)
        state.immersive_mode_end_time = time.time() + self.immersive_mode_duration
        logger.info(f"[心流] 🌊 沉浸模式已激活: {chat_id}, 持续 {self.immersive_mode_duration}秒")

    def _refresh_immersive_mode(self, chat_id: str) -> None:
        if self.immersive_mode_duration <= 0: return
        state = self._get_chat_state(chat_id)
        if state.immersive_mode_end_time > 0:
            state.immersive_mode_end_time = time.time() + self.immersive_mode_duration
            self._log_detail(f"[心流] 🔄 沉浸模式倒计时已刷新: {chat_id}")

    def _format_chat_history_for_immersive_mode(self, event: AstrMessageEvent) -> str:
        msgs = self._get_raw_buffer(event.unified_msg_origin)
        if not msgs: return "暂无聊天记录"
        history = [f"{'🤖 Bot' if msg.is_bot else f'👤 {msg.sender_name}'}: {self._clean_message_content(msg.content, 150)}" for msg in msgs[-20:]]
        return "\n".join(history) if history else "暂无聊天记录"

    # ============================================================
    # 上下文构建
    # ============================================================
    def _get_raw_history_for_context(self, event: AstrMessageEvent) -> List[RawMessage]:
        msgs = self._get_raw_buffer(event.unified_msg_origin)
        if msgs and not msgs[-1].is_bot and msgs[-1].sender_id == str(event.get_sender_id()) and msgs[-1].content == event.message_str:
            msgs = msgs[:-1]
        return msgs[-self.judge_context_count:]

    def _build_optimized_contexts(self, raw_msgs: List[RawMessage]) -> List[Dict[str, str]]:
        contexts = []
        for msg in raw_msgs:
            content = self._clean_message_content(msg.content, 200)
            if not content: continue
            role = "assistant" if msg.is_bot else "user"
            text = content if msg.is_bot else f"[{msg.sender_name}] {content}"
            if contexts and contexts[-1]["role"] == role:
                contexts[-1]["content"] += "\n" + text
            else:
                contexts.append({"role": role, "content": text})
        if contexts and contexts[0]["role"] == "assistant":
            contexts[0]["role"] = "user"
            contexts[0]["content"] = "[Bot自身历史发言] " + contexts[0]["content"]
        return contexts

    def _analyze_conversation_flow(self, event: AstrMessageEvent) -> str:
        msgs = self._get_raw_buffer(event.unified_msg_origin)[-self.context_messages_count:]
        if len(msgs) < 2: return "历史较短。"
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
        return (f"活跃度:{activity_desc}({activity:.0%}) | 回复率:{rate:.1f}% | "
                f"模式:{mode} | 阈值:{dynamic_threshold:.2f} | {datetime.datetime.now().strftime('%H:%M')}")

    def _get_last_bot_reply(self, event: AstrMessageEvent) -> Optional[str]:
        for m in reversed(self._get_raw_buffer(event.unified_msg_origin)):
            if m.is_bot and m.content.strip(): return m.content
        return None

    # ============================================================
    # 核心判断逻辑 (小模型)
    # ============================================================
    async def judge_with_tiny_model(self, event: AstrMessageEvent) -> JudgeResult:
        provider = self.context.get_provider_by_id(self.judge_provider_name)
        if not provider: return JudgeResult(should_reply=False, reasoning="提供商未配置或不存在")

        prompt_template = self.judge_prompt if self.judge_prompt.strip() else DEFAULT_JUDGE_PROMPT
        reasoning_part = ', "reasoning": "简短分析原因"' if self.judge_include_reasoning else ""
        mid_score = (self.score_min + self.score_max) / 2
        dynamic_threshold, activity, _ = self._get_dynamic_threshold(event.unified_msg_origin)
        activity_desc = self._get_activity_description(activity)

        fmt_kwargs = dict(
            chat_id=event.unified_msg_origin,
            minutes_since_reply=self._get_minutes_since_last_reply(event.unified_msg_origin),
            chat_context=self._build_chat_context(event),
            chat_flow=self._analyze_conversation_flow(event),
            last_bot_reply=self._get_last_bot_reply(event) or "无",
            sender_name=event.get_sender_name(),
            message=event.message_str,
            threshold=dynamic_threshold,
            reasoning_part=reasoning_part,
            score_min=int(self.score_min), score_max=int(self.score_max), mid=int(mid_score),
            activity_level=activity, activity_desc=activity_desc,
        )

        try:
            judge_prompt = prompt_template.format(**fmt_kwargs)
        except KeyError:
            judge_prompt = DEFAULT_JUDGE_PROMPT.format(**fmt_kwargs)

        system_inst = f"你是群聊机器人决策系统，严格返回JSON，不要输出其他内容。所有分数必须在{int(self.score_min)}到{int(self.score_max)}之间，不能为0。"
        contexts = self._build_optimized_contexts(self._get_raw_history_for_context(event))

        for attempt in range(self.judge_max_retries + 1):
            try:
                resp = await provider.text_chat(prompt=judge_prompt, contexts=contexts, system=system_inst, image_urls=[])
                data = _extract_json(resp.completion_text.strip())
                scores = {
                    "relevance": safe_extract_score(data, "relevance", self.score_min, self.score_max),
                    "willingness": safe_extract_score(data, "willingness", self.score_min, self.score_max),
                    "social": safe_extract_score(data, "social", self.score_min, self.score_max),
                    "timing": safe_extract_score(data, "timing", self.score_min, self.score_max),
                    "continuity": safe_extract_score(data, "continuity", self.score_min, self.score_max),
                }
                normalized_scores = {k: (v - self.score_min) / (self.score_max - self.score_min) for k, v in scores.items()}
                overall = sum(normalized_scores[k] * self.weights[k] for k in self.weights)
                should_reply = overall >= dynamic_threshold
                avg_absolute_score = sum(scores.values()) / len(scores)

                if should_reply:
                    logger.info(f"[心流] 📊 评分模式: {overall:.3f}/{dynamic_threshold:.2f} (均分:{avg_absolute_score:.1f}分) | ✅触发 | 活跃度:{activity_desc}({activity:.0%}) | {data.get('reasoning', '')}")
                else:
                    self._log_detail(f"[心流] 📊 评分模式: {overall:.3f}/{dynamic_threshold:.2f} (均分:{avg_absolute_score:.1f}分) | ❌跳过 | 活跃度:{activity_desc}({activity:.0%}) | {data.get('reasoning', '')}")

                return JudgeResult(**scores, reasoning=data.get("reasoning", ""), should_reply=should_reply, confidence=overall, overall_score=overall)
            except (json.JSONDecodeError, ValueError):
                if attempt == self.judge_max_retries: return JudgeResult(should_reply=False, reasoning="JSON解析失败")
            except Exception as e:
                logger.error(f"[心流] 评分模式判断异常: {e}")
                return JudgeResult(should_reply=False, reasoning=f"异常: {str(e)}")
        return JudgeResult(should_reply=False, reasoning="未知错误")

    async def judge_immersive_mode(self, event: AstrMessageEvent) -> JudgeResult:
        provider = self.context.get_provider_by_id(self.judge_provider_name)
        if not provider: return JudgeResult(should_reply=False, reasoning="提供商未配置或不存在")

        remaining = self._get_immersive_mode_remaining(event.unified_msg_origin)
        _, activity, _ = self._get_dynamic_threshold(event.unified_msg_origin)
        activity_desc = self._get_activity_description(activity)
        system_inst = "你是严格的群聊机器人判断系统，严格返回JSON，不要输出其他内容。"
        immersive_reasoning_part = ', "reasoning": "简短原因"' if self.judge_include_reasoning else ""
        contexts = self._build_optimized_contexts(self._get_raw_history_for_context(event))
        
        judge_prompt = f"""
## ⚠️ 最高优先级：追踪机器人自己的发言
对话历史（contexts）中，role 为 assistant 的消息是【机器人自己】之前说的话，role 为 user 的消息是群友说的话（带 [昵称] 前缀）。
你必须先找到机器人最近说了什么，然后判断当前消息与机器人发言的关系。

## 沉浸模式判断规则
你的默认倾向是 **不回复**。只有在以下情况之一时才回复：
1. 消息直接 @ 了机器人或提到机器人名字
2. 消息明确向机器人提问
3. 消息是机器人发起的对话的延续
4. 消息内容与机器人刚聊的话题直接相关
5. 群聊中出现冷场，机器人的回复能自然活跃气氛

## 当前状态
- 模式: 沉浸模式 | 剩余: {remaining}秒 | 活跃度: {activity_desc} ({activity:.0%})

## 待判断消息
[{event.get_sender_name()}] {event.message_str}

## 严格判断
根据上述规则严格判断。沉默也是好的选择。
严格返回 JSON：
{{"should_reply": true或false{immersive_reasoning_part}}}
"""
        for attempt in range(self.judge_max_retries + 1):
            try:
                resp = await provider.text_chat(prompt=judge_prompt, contexts=contexts, system=system_inst, image_urls=[])
                data = _extract_json(resp.completion_text.strip())
                should_reply = bool(data.get("should_reply", False))
                if should_reply:
                    logger.info(f"[心流] 🌊 沉浸模式: ✅触发 | 剩余{remaining}秒 | 活跃度:{activity_desc}({activity:.0%}) | {data.get('reasoning', '')}")
                else:
                    self._log_detail(f"[心流] 🌊 沉浸模式: ❌跳过 | 剩余{remaining}秒 | 活跃度:{activity_desc}({activity:.0%}) | {data.get('reasoning', '')}")
                return JudgeResult(should_reply=should_reply, reasoning=data.get("reasoning", ""), confidence=1.0 if should_reply else 0.0, overall_score=1.0 if should_reply else 0.0)
            except (json.JSONDecodeError, ValueError):
                if attempt == self.judge_max_retries: return JudgeResult(should_reply=False, reasoning="JSON解析失败")
            except Exception as e:
                logger.error(f"[心流] 沉浸模式判断异常: {e}")
                return JudgeResult(should_reply=False, reasoning=f"异常: {str(e)}")
        return JudgeResult(should_reply=False, reasoning="未知错误")

    # ============================================================
    # 核心事件监听器
    # ============================================================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self.config.get("enable_heartflow", False): return
        umo = event.unified_msg_origin
        state = self._get_chat_state(umo)

        if self._is_judgment_paused(umo):
            if self._should_process_message(event):
                self._record_raw_message(event, is_bot=False)
                self._cleanup_old_messages(umo)
                state.total_messages += 1
                debounce_state = self._debounce_states.get(umo)
                if debounce_state and debounce_state.get("active"):
                    debounce_state["last_reset"] = time.time()
                    debounce_state["pending_msgs"].append(event.message_str)
                    self._log_detail(f"[心流] ⏳ 防抖中收到新消息，已收集并重置计时器: {umo}")
            return

        if not self._should_process_message(event): return

        self._record_raw_message(event, is_bot=False)
        self._cleanup_old_messages(umo)

        try:
            is_immersive = self._is_immersive_mode_active(umo)
            res = await self.judge_immersive_mode(event) if is_immersive else await self.judge_with_tiny_model(event)

            if res.should_reply:
                mode_name = "immersive" if is_immersive else "score"
                state.pending_heartflow_reply = True
                state.pending_heartflow_mode = mode_name
                self._set_judgment_paused(umo, paused=True)

                if self.enable_debounce:
                    self._debounce_states[umo] = {
                        "active": True, "last_reset": time.time(),
                        "pending_msgs": [event.message_str], "cancelled": False
                    }
                    start_time = time.time()
                    logger.info(f"[心流] ⏳ 触发防抖等待 ({self.debounce_delay}s)")

                    while True:
                        debounce_state = self._debounce_states.get(umo)
                        if not debounce_state: break
                        elapsed_since_reset = time.time() - debounce_state["last_reset"]
                        total_elapsed = time.time() - start_time
                        remaining_delay = self.debounce_delay - elapsed_since_reset
                        remaining_max = self.max_debounce_wait - total_elapsed
                        sleep_time = min(remaining_delay, remaining_max)
                        if sleep_time <= 0 or debounce_state.get("cancelled"): break
                        await asyncio.sleep(min(sleep_time, 0.5))

                    debounce_state = self._debounce_states.get(umo)
                    if debounce_state and debounce_state.get("cancelled"):
                        logger.info(f"[心流] 🚫 防抖被取消: {umo}")
                        self._debounce_states.pop(umo, None)
                        self._set_judgment_paused(umo, paused=False)
                        return

                    final_msgs = debounce_state["pending_msgs"] if debounce_state else [event.message_str]
                    event.message_str = "\n".join(final_msgs)
                    self._debounce_states.pop(umo, None)
                    logger.info(f"[心流] ✅ 防抖结束，准备回复（合并{len(final_msgs)}条）")

                event.is_at_or_wake_command = True
                setattr(event, "heartflow_triggered", True)
                setattr(event, "heartflow_mode", mode_name)
                self._activate_immersive_mode(umo)
                logger.info(f"[心流] 🎯 心流触发: {event.message_str[:50]}... | 模式: {'🌊沉浸' if is_immersive else '📊评分'}")

            state.total_messages += 1
        except Exception as e:
            logger.error(f"[心流] 处理异常: {e}\n{traceback.format_exc()}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        if not getattr(event, "heartflow_triggered", False): return
        chat_history = self._format_chat_history_for_immersive_mode(event)
        if TextPart is not None:
            req.extra_user_content_parts.append(
                TextPart(text=f"<group_chat_context>\n以下是本群最近的聊天记录：\n{chat_history}\n</group_chat_context>").mark_as_temp()
            )
        else:
            req.system_prompt = (req.system_prompt or "") + f"\n<group_chat_context>\n{chat_history}\n</group_chat_context>"
        if req and hasattr(req, "system_prompt"):
            req.system_prompt = (req.system_prompt or "") + "\n（注意：本次是你主动参与群聊的，请参考群聊上下文自然切入。）"

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        if not getattr(event, "heartflow_triggered", False): return
        text = ""
        try:
            for attr in ("completion_text", "text", "content"):
                val = getattr(resp, attr, None)
                if isinstance(val, str) and val.strip():
                    text = val.strip(); break
            if not text:
                result = getattr(resp, "result", None)
                if result and getattr(result, "chain", None):
                    texts = [c.text.strip() for c in result.chain if isinstance(c, Plain) and c.text and c.text.strip()]
                    if texts: text = "\n".join(texts)
        except Exception:
            pass
        if text:
            self._record_bot_reply_manual(event.unified_msg_origin, text)

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        if not self.config.get("enable_heartflow"): return
        umo = event.unified_msg_origin
        state = self._get_chat_state(umo)
        bot_reply_content = None
        has_reply = False

        try:
            res = event.get_result()
            if res and getattr(res, "chain", None):
                texts = []
                non_plain_reply = False
                for component in res.chain:
                    if isinstance(component, Plain):
                        if component.text and component.text.strip(): texts.append(component.text.strip())
                    else: non_plain_reply = True
                if texts: bot_reply_content = "\n".join(texts); has_reply = True
                elif non_plain_reply: bot_reply_content = "[非文本回复]"; has_reply = True
        except Exception:
            pass

        msg_str = (event.message_str or "").strip()
        is_command = msg_str.startswith(("/", "#", "!", "！"))
        if has_reply and bot_reply_content and not is_command:
            self._record_bot_reply_manual(umo, bot_reply_content)

        if getattr(event, "heartflow_triggered", False) and state.pending_heartflow_reply:
            if has_reply:
                state.last_reply_time = time.time()
                state.total_replies += 1
            if self._is_immersive_mode_active(umo): self._refresh_immersive_mode(umo)
            else: self._activate_immersive_mode(umo)
            self._set_judgment_paused(umo, paused=False)
        elif event.is_at_or_wake_command and not is_command:
            if has_reply: state.last_reply_time = time.time()
            if not self._is_immersive_mode_active(umo): self._activate_immersive_mode(umo)
            else: self._refresh_immersive_mode(umo)

    # ============================================================
    # 指令处理
    # ============================================================
    @filter.command("heartflow", help="查看心流插件运行状态")
    async def heartflow_status(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        s = self._get_chat_state(umo)
        rate = (s.total_replies / max(1, s.total_messages) * 100) if s.total_messages > 0 else 0.0
        is_immersive = self._is_immersive_mode_active(umo)
        remaining = self._get_immersive_mode_remaining(umo)
        is_paused = self._is_judgment_paused(umo)
        buffer_size = len(self._raw_msg_buffer.get(umo, []))
        dynamic_threshold, activity, absolute_threshold = self._get_dynamic_threshold(umo)
        activity_desc = self._get_activity_description(activity)
        config_source = self._get_group_config_info(umo)

        if self.enable_dynamic_threshold:
            dynamic_info = "✅启用"
            current_threshold_info = f"当前: {dynamic_threshold:.2f} (绝对值: {absolute_threshold:.1f}分)"
        else:
            dynamic_info = "❌关闭"
            current_threshold_info = f"固定值: {dynamic_threshold:.2f} (绝对值: {absolute_threshold:.1f}分)"

        pending_info = "✅是" if s.pending_heartflow_reply else "❌否"
        log_info = "🔊开启" if self.enable_detailed_log else "🔇关闭"
        debounce_info = f"✅启用 ({self.debounce_delay}s/{self.max_debounce_wait}s)" if self.enable_debounce else "❌关闭"

        event.set_result(event.plain_result(f"""
🔮 **心流状态**
━━━━━━━━━━━━━━━━━━━━
📊 **统计信息**
• 上次回复: {self._get_minutes_since_last_reply(umo)}分钟前
• 消息总数: {s.total_messages} | 实际回复: {s.total_replies} ({rate:.1f}%)
• 缓冲区: {buffer_size}条消息

⚙️ **配置信息**
• 评分范围: {int(self.score_min)}-{int(self.score_max)}分
• 基础阈值: {self.reply_threshold} | 沉浸持续: {self.immersive_mode_duration}秒
• 暂停超时: {self.judgment_paused_timeout}秒 | 防抖延迟: {debounce_info}
• 详细日志: {log_info}

🎚️ **阈值配置**
• 来源: {config_source}
• 动态阈值: {dynamic_info}
• {current_threshold_info}

📊 **当前活跃度**
• 状态: {activity_desc} ({activity:.0%})

🌊 **当前模式**
• 沉浸模式: {"✅激活" if is_immersive else "❌未激活"} ({remaining}秒)
• 判断状态: {"⏸️ 已暂停" if is_paused else "▶️ 正常"}
• 等待Bot回复: {pending_info}
━━━━━━━━━━━━━━━━━━━━"""))

    @filter.command("heartflow_debug", help="查看最近的消息记录（调试用）")
    async def heartflow_debug(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        msgs = self._get_raw_buffer(umo)
        if not msgs:
            event.set_result(event.plain_result("📭 缓冲区为空，暂无消息记录")); return
        lines = ["📋 **最近消息记录**（最多20条）\n━━━━━━━━━━━━━━"]
        for msg in msgs[-20:]:
            prefix = "🤖" if msg.is_bot else "👤"
            time_str = datetime.datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S")
            content = msg.content[:60] + "..." if len(msg.content) > 60 else msg.content
            lines.append(f"{prefix} [{time_str}] {msg.sender_name}: {content or '[空消息]'}")
        event.set_result(event.plain_result("\n".join(lines) + f"\n━━━━━━━━━━\n📊 共 {len(msgs)} 条记录"))

    @filter.command("heartflow_clear", help="清除当前群的消息缓冲区")
    async def heartflow_clear_buffer(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        with self._lock:
            if umo in self._raw_msg_buffer:
                count = len(self._raw_msg_buffer[umo])
                self._raw_msg_buffer[umo].clear()
                event.set_result(event.plain_result(f"✅ 已清除 {count} 条缓冲消息"))
            else:
                event.set_result(event.plain_result("📭 缓冲区为空，无需清除"))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("heartflow_log", help="切换详细日志开关（管理员）")
    async def heartflow_toggle_log(self, event: AstrMessageEvent):
        self.enable_detailed_log = not self.enable_detailed_log
        status = "🔊 已开启" if self.enable_detailed_log else "🔇 已关闭"
        logger.info(f"[心流] 详细日志已切换: {status} (操作者: {event.get_sender_name()})")
        event.set_result(event.plain_result(f"{status} 详细日志"))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("heartflow_reset", help="重置当前群的所有心流状态（管理员）")
    async def heartflow_reset(self, event: AstrMessageEvent):
        cid = event.unified_msg_origin
        with self._lock:
            self.chat_states.pop(cid, None)
            self._raw_msg_buffer.pop(cid, None)
            self._cancel_pause_restore_task(cid)
            self._debounce_states.pop(cid, None)
        event.set_result(event.plain_result("✅ **心流状态已完全重置**"))

    @filter.command("heartflow_help", help="显示心流插件所有命令说明")
    async def heartflow_help(self, event: AstrMessageEvent):
        event.set_result(event.plain_result("🔮 **心流插件 - 命令帮助**\n• `/heartflow` - 查看运行状态\n• `/heartflow_debug` - 查看消息缓冲记录\n• `/heartflow_clear` - 清空消息缓冲区\n• `/heartflow_log` - 切换详细日志开关 (管理员)\n• `/heartflow_reset` - 重置所有状态 (管理员)\n• `/heartflow_help` - 显示此帮助"))

    async def terminate(self):
        for task in list(self._pause_restore_tasks.values()):
            if task and not task.done(): task.cancel()
        self._pause_restore_tasks.clear()
        self._debounce_states.clear()
        logger.info("[心流] 心流插件已卸载，后台任务已清理")

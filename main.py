"""
心流插件 - 智能群聊主动回复系统 (高性能优化版)
基于小模型判断是否应该主动参与群聊对话
"""

import json
import re
import time
import datetime
import traceback
import threading
from collections import deque
from typing import Dict, Optional, List, Any, Set
from dataclasses import dataclass, field

import astrbot.api.star as star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import logger
from astrbot.api.message_components import Plain, At

# ==================== 预编译正则 ====================
RE_CODE_BLOCK = re.compile(r"^(?:```(?:json)?|~~~(?:json)?|`{3,}(?:json)?)\s*\n?(.*?)\n?\s*(?:```|~~~|`{3,})$", re.DOTALL | re.IGNORECASE)
RE_BRACE = re.compile(r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}", re.DOTALL)
RE_ALL_BRACES = re.compile(r"\{.*\}", re.DOTALL)
RE_XML_TAG = re.compile(r'<[^>]+>')
RE_URL = re.compile(r'https?://\S+')

# ==================== 数据类定义 ====================

@dataclass
class JudgeResult:
    """判断结果数据类"""
    relevance: float = 0.0
    willingness: float = 0.0
    social: float = 0.0
    timing: float = 0.0
    continuity: float = 0.0
    reasoning: str = ""
    should_reply: bool = False
    confidence: float = 0.0
    overall_score: float = 0.0
    related_messages: List[str] = field(default_factory=list)

    def __post_init__(self):
        for attr in ('relevance', 'willingness', 'social', 'timing', 'continuity'):
            setattr(self, attr, _clamp_score(getattr(self, attr)))

@dataclass
class RawMessage:
    """原始群聊消息条目"""
    sender_name: str
    sender_id: str
    content: str
    timestamp: float
    is_bot: bool = False

@dataclass
class ChatState:
    """群聊状态数据类"""
    last_reply_time: float = 0.0
    last_reset_date: str = ""
    total_messages: int = 0
    total_replies: int = 0
    last_cleanup_time: float = 0.0
    immersive_mode_end_time: float = 0.0
    # 判断暂停标志 - bot已判定回复但尚未发送时暂停小模型判断
    judgment_paused: bool = False
    # 触发暂停时的消息时间戳（用于超时保护）
    judgment_paused_at: float = 0.0

@dataclass
class CacheEntry:
    """缓存条目"""
    original: str
    summarized: str
    persona_id: str
    created_at: float = 0.0
    last_used_at: float = 0.0
    use_count: int = 0

    def __post_init__(self):
        now = time.time()
        self.created_at = self.created_at or now
        self.last_used_at = self.last_used_at or now

# ==================== 工具函数 ====================

def _clamp_score(v: Any, min_val: float = 0.0, max_val: float = 10.0) -> float:
    try:
        return max(min_val, min(max_val, float(v)))
    except (TypeError, ValueError):
        return min_val

def _extract_json(text: str) -> Dict[str, Any]:
    """从模型返回的文本中稳健地提取 JSON 对象"""
    if not text or not (text := text.strip()):
        raise ValueError("输入文本为空")
    
    try: return json.loads(text)
    except json.JSONDecodeError: pass
    
    if m := RE_CODE_BLOCK.search(text):
        try: return json.loads(m.group(1).strip())
        except json.JSONDecodeError: pass
        
    if m := RE_BRACE.search(text):
        try: return json.loads(m.group())
        except json.JSONDecodeError: pass
        
    for candidate in reversed(RE_ALL_BRACES.findall(text)):
        try: return json.loads(candidate)
        except json.JSONDecodeError: continue
        
    raise ValueError(f"无法从文本中提取有效 JSON: {text[:100]}...")

def _validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    v = dict(config or {})
    v["reply_threshold"] = _clamp_score(v.get("reply_threshold", 0.6), 0.0, 1.0)
    
    limits = {
        "context_messages_count": (1, 50, 5), 
        "judge_context_count": (1, 50, 5),
        "judge_max_retries": (0, 10, 3),
        "immersive_mode_duration": (0, 300, 30),
        # 判断暂停超时时间（防止极端情况下bot未回复导致永久暂停）
        "judgment_paused_timeout": (5, 300, 60),
    }
    for k, (mn, mx, df) in limits.items():
        v[k] = max(mn, min(mx, int(v.get(k, df))))
        
    for k in ["judge_relevance", "judge_willingness", "judge_social", "judge_timing", "judge_continuity"]:
        v[k] = _clamp_score(v.get(k, 0.2), 0.0, 1.0)
        
    if "chat_whitelist" in v and not isinstance(v["chat_whitelist"], list):
        v["chat_whitelist"] = []
    return v

# ==================== 主插件类 ====================

class HeartflowPlugin(star.Star):
    """心流插件 - 智能群聊主动回复系统"""
    _lock = threading.RLock()

    def __init__(self, context: star.Context, config: Dict[str, Any]):
        super().__init__(context)
        self.config = _validate_config(config)

        # 配置项
        self.judge_provider_name: str = self.config.get("judge_provider_name", "")
        self.reply_threshold: float = self.config.get("reply_threshold", 0.6)
        self.context_messages_count: int = self.config.get("context_messages_count", 5)
        self.judge_context_count: int = self.config.get("judge_context_count", self.context_messages_count)
        self.whitelist_enabled: bool = self.config.get("whitelist_enabled", False)
        self._whitelist_set: Set[str] = set(self.config.get("chat_whitelist", []))
        self.judge_include_reasoning: bool = self.config.get("judge_include_reasoning", True)
        self.judge_max_retries: int = self.config.get("judge_max_retries", 3)
        self.immersive_mode_duration: int = self.config.get("immersive_mode_duration", 30)
        # 判断暂停超时配置
        self.judgment_paused_timeout: int = self.config.get("judgment_paused_timeout", 60)

        # 状态与缓存
        self.chat_states: Dict[str, ChatState] = {}
        self._raw_msg_buffer: Dict[str, deque[RawMessage]] = {}
        self._raw_msg_buffer_size: int = max(self.context_messages_count, self.judge_context_count) * 4
        self.system_prompt_cache: Dict[str, CacheEntry] = {}

        # 权重归一化
        self.weights = {
            "relevance": self.config.get("judge_relevance", 0.25),
            "willingness": self.config.get("judge_willingness", 0.2),
            "social": self.config.get("judge_social", 0.2),
            "timing": self.config.get("judge_timing", 0.15),
            "continuity": self.config.get("judge_continuity", 0.2),
        }
        if abs((w_sum := sum(self.weights.values())) - 1.0) > 1e-6:
            logger.warning(f"[心流] 判断权重和不为1，当前和为{w_sum:.4f}，自动归一化")
            self.weights = {k: v / w_sum for k, v in self.weights.items()}

        self._cache_ttl: float = 86400.0
        self._buffer_cleanup_interval: float = 3600.0
        logger.info(
            f"[心流] 心流插件已初始化完成 | 沉浸模式持续时间: {self.immersive_mode_duration}秒 | "
            f"判断暂停超时: {self.judgment_paused_timeout}秒"
        )

    # ==================== 辅助与状态管理 ====================

    def _get_chat_state(self, chat_id: str) -> ChatState:
        with self._lock:
            return self.chat_states.setdefault(chat_id, ChatState())

    def _is_judgment_paused(self, chat_id: str) -> bool:
        """
        检查判断是否处于暂停状态。
        当bot已判定要回复但尚未实际发送消息时，暂停小模型判断，避免bot连发多条回复。
        同时具备超时保护机制：超过judgment_paused_timeout秒后自动恢复。
        """
        state = self._get_chat_state(chat_id)
        if not state.judgment_paused:
            return False
        
        # 超时保护：防止极端情况下bot未回复导致永久暂停
        if state.judgment_paused_at > 0 and (time.time() - state.judgment_paused_at) > self.judgment_paused_timeout:
            logger.warning(
                f"[心流] ⚠️ 判断暂停超时（>{self.judgment_paused_timeout}秒），自动恢复判断: {chat_id}"
            )
            state.judgment_paused = False
            state.judgment_paused_at = 0.0
            return False
        
        return True

    def _set_judgment_paused(self, chat_id: str, paused: bool = True) -> None:
        """设置判断暂停状态"""
        state = self._get_chat_state(chat_id)
        if paused:
            state.judgment_paused = True
            state.judgment_paused_at = time.time()
            logger.debug(f"[心流] ⏸️  已暂停小模型判断: {chat_id}")
        else:
            state.judgment_paused = False
            state.judgment_paused_at = 0.0
            logger.debug(f"[心流] ▶️  已恢复小模型判断: {chat_id}")

    async def _get_persona_id(self, event: AstrMessageEvent) -> str:
        try:
            if curr_cid := await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin):
                conv = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
                if conv and conv.persona_id:
                    return conv.persona_id
        except Exception:
            pass
        return "default"

    def _get_minutes_since_last_reply(self, chat_id: str) -> int:
        t = self._get_chat_state(chat_id).last_reply_time
        return 999 if t == 0 else max(0, int((time.time() - t) / 60))

    def _should_process_message(self, event: AstrMessageEvent) -> bool:
        if not self.config.get("enable_heartflow", False): return False
        if self.whitelist_enabled and event.unified_msg_origin not in self._whitelist_set: return False
        if event.get_sender_id() == event.get_self_id(): return False
        if not (event.message_str and event.message_str.strip()): return False
        
        if event.is_at_or_wake_command:
            logger.debug(f"[心流] 消息已主动唤醒bot，跳过心流判断")
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
        if msg_str.startswith(('/', '#', '!', '！')):
            logger.debug(f"[心流] 消息为指令，跳过心流判断: {msg_str[:20]}...")
            return False
            
        return True

    # ==================== 沉浸模式 ====================

    def _is_immersive_mode_active(self, chat_id: str) -> bool:
        """检查沉浸模式是否激活"""
        state = self._get_chat_state(chat_id)
        if state.immersive_mode_end_time == 0:
            return False
        if time.time() > state.immersive_mode_end_time:
            state.immersive_mode_end_time = 0
            return False
        return True

    def _get_immersive_mode_remaining(self, chat_id: str) -> int:
        """获取沉浸模式剩余时间"""
        state = self._get_chat_state(chat_id)
        if state.immersive_mode_end_time == 0:
            return 0
        remaining = int(state.immersive_mode_end_time - time.time())
        return max(0, remaining)

    def _activate_immersive_mode(self, chat_id: str) -> None:
        """激活沉浸模式"""
        state = self._get_chat_state(chat_id)
        state.immersive_mode_end_time = time.time() + self.immersive_mode_duration
        logger.info(f"[心流] 🌊 沉浸模式已激活: {chat_id}, 持续 {self.immersive_mode_duration}秒")

    def _refresh_immersive_mode(self, chat_id: str) -> None:
        """刷新沉浸模式倒计时（重新计时）"""
        state = self._get_chat_state(chat_id)
        if state.immersive_mode_end_time > 0:  # 只有在沉浸模式已激活时才刷新
            state.immersive_mode_end_time = time.time() + self.immersive_mode_duration
            logger.debug(f"[心流] 🔄 沉浸模式倒计时已刷新: {chat_id}")

    def _format_chat_history_for_immersive_mode(self, event: AstrMessageEvent) -> str:
        """为沉浸模式格式化聊天历史，包含bot自己的回复"""
        msgs = self._get_raw_buffer(event.unified_msg_origin)
        if not msgs:
            return "暂无聊天记录"
        
        history = []
        for msg in msgs[-20:]:
            prefix = "🤖 Bot" if msg.is_bot else f"👤 {msg.sender_name}"
            content = self._clean_message_content(msg.content, 150)
            history.append(f"{prefix}: {content}")
        
        return "\n".join(history) if history else "暂无聊天记录"

    # ==================== 缓存与清理 ====================

    def _cleanup_expired_cache(self) -> None:
        now = time.time()
        expired = [k for k, v in self.system_prompt_cache.items() if now - v.created_at > self._cache_ttl]
        for k in expired:
            del self.system_prompt_cache[k]

    def _cleanup_old_messages(self, umo: str) -> None:
        now = time.time()
        state = self._get_chat_state(umo)
        if now - state.last_cleanup_time < self._buffer_cleanup_interval or umo not in self._raw_msg_buffer:
            return
        state.last_cleanup_time = now
        cutoff = now - 3600
        self._raw_msg_buffer[umo] = deque(
            (m for m in self._raw_msg_buffer[umo] if m.timestamp >= cutoff),
            maxlen=self._raw_msg_buffer_size
        )

    # ==================== 提示词处理 ====================

    async def _get_persona_system_prompt(self, event: AstrMessageEvent) -> str:
        try:
            persona_id = await self._get_persona_id(event)
            if persona_id not in ("[%None]", "", "default"):
                if (p := await self.context.persona_manager.get_persona(persona_id)) and p.system_prompt:
                    return p.system_prompt
            if dp := await self.context.persona_manager.get_default_persona_v3(event.unified_msg_origin):
                return dp.get("prompt", "") or dp.get("system_prompt", "")
        except Exception:
            pass
        return ""

    async def _get_or_create_summarized_system_prompt(self, event: AstrMessageEvent, original_prompt: str) -> str:
        try:
            self._cleanup_expired_cache()
            persona_id = await self._get_persona_id(event)
            cache_key = f"{persona_id}_{hash(original_prompt[:200]) & 0xFFFFFFFF:08x}"

            if cache_key in self.system_prompt_cache:
                cached = self.system_prompt_cache[cache_key]
                cached.last_used_at, cached.use_count = time.time(), cached.use_count + 1
                return cached.summarized

            if not original_prompt or len(original_prompt.strip()) < 20:
                return original_prompt

            summarized = await self._summarize_system_prompt(original_prompt)
            self.system_prompt_cache[cache_key] = CacheEntry(original_prompt, summarized, persona_id)
            logger.info(f"[心流] 创建精简提示词: [{persona_id}] 压缩率: {(1 - len(summarized) / max(1, len(original_prompt))) * 100:.1f}%")
            return summarized
        except Exception as e:
            logger.error(f"[心流] 获取精简提示词失败: {e}")
            return original_prompt

    async def _summarize_system_prompt(self, original_prompt: str) -> str:
        if not (provider := self.context.get_provider_by_id(self.judge_provider_name)):
            return original_prompt
        try:
            resp = await provider.text_chat(
                prompt=f"请将以下机器人角色设定总结为简洁的核心要点，保留关键性格和行为，不超过100字。\n\n{original_prompt}\n\n以JSON回复：{{\"summarized_persona\": \"精简后的设定\"}}",
                contexts=[]
            )
            if (s := _extract_json(resp.completion_text).get("summarized_persona", "")) and len(s.strip()) > 10:
                return s.strip()
        except Exception as e:
            logger.error(f"[心流] 总结提示词异常: {e}")
        return original_prompt

    def _clean_message_content(self, text: str, max_len: int = 200) -> str:
        if not text: return ""
        text = RE_XML_TAG.sub('', text)
        text = RE_URL.sub('[链接]', text)
        return (text[:max_len] + "...") if len(text) > max_len else text.strip()

    # ==================== 消息缓冲与上下文 ====================

    def _record_raw_message(self, event: AstrMessageEvent, is_bot: bool = False) -> None:
        umo = event.unified_msg_origin
        with self._lock:
            buf = self._raw_msg_buffer.setdefault(umo, deque(maxlen=self._raw_msg_buffer_size))
            content = self._clean_message_content(event.message_str, 200) if is_bot else event.message_str
            new_msg = RawMessage(
                sender_name="bot" if is_bot else event.get_sender_name(),
                sender_id="bot" if is_bot else str(event.get_sender_id()),
                content=content, timestamp=time.time(), is_bot=is_bot
            )
            if buf and (last := buf[-1]).sender_id == new_msg.sender_id and last.content == new_msg.content and new_msg.timestamp - last.timestamp < 5:
                return
            buf.append(new_msg)

    def _get_raw_buffer(self, umo: str) -> List[RawMessage]:
        with self._lock:
            return list(self._raw_msg_buffer.get(umo, []))

    def _get_raw_history_for_context(self, event: AstrMessageEvent) -> List[RawMessage]:
        msgs = self._get_raw_buffer(event.unified_msg_origin)
        if msgs and msgs[-1].content == event.message_str and not msgs[-1].is_bot:
            msgs = msgs[:-1]
        return msgs[-self.judge_context_count:]

    def _build_optimized_contexts(self, raw_msgs: List[RawMessage]) -> List[Dict[str, str]]:
        contexts = []
        for msg in raw_msgs:
            content = self._clean_message_content(msg.content, 200)
            if not content: continue
            contexts.append({"role": "assistant" if msg.is_bot else "user", "content": content})
        contexts.reverse()
        return contexts

    def _analyze_conversation_flow(self, event: AstrMessageEvent) -> str:
        msgs = self._get_raw_buffer(event.unified_msg_origin)[-self.context_messages_count:]
        if len(msgs) < 2: return "历史较短。"
        
        flow = []
        last, prev = msgs[-1], msgs[-2]
        if not last.is_bot and not prev.is_bot and any(q in prev.content for q in ("?", "？", "吗")):
            flow.append("用户似乎在回答机器人提问")
            
        if not flow:
            keywords = {"游戏", "工作", "吃"}
            topics = {kw for m in msgs for kw in keywords if kw in m.content}
            flow.append(f"话题: {','.join(topics)}" if topics else "闲聊")
        return " | ".join(flow)

    def _build_chat_context(self, event: AstrMessageEvent) -> str:
        s = self._get_chat_state(event.unified_msg_origin)
        level = "高" if s.total_messages > 100 else ("中" if s.total_messages > 20 else "低")
        rate = (s.total_replies / max(1, s.total_messages) * 100)
        mode = "沉浸" if self._is_immersive_mode_active(event.unified_msg_origin) else "正常"
        return f"活跃度:{level} | 回复率:{rate:.1f}% | 模式:{mode} | {datetime.datetime.now().strftime('%H:%M')}"

    def _get_last_bot_reply(self, event: AstrMessageEvent) -> Optional[str]:
        for m in reversed(self._get_raw_buffer(event.unified_msg_origin)):
            if m.is_bot and m.content.strip(): return m.content
        return None

    # ==================== 核心判断逻辑 ====================

    async def judge_with_tiny_model(self, event: AstrMessageEvent) -> JudgeResult:
        """正常模式：基于评分判断是否应该回复"""
        if not (provider := self.context.get_provider_by_id(self.judge_provider_name)):
            return JudgeResult(should_reply=False, reasoning="提供商未配置或不存在")

        persona_prompt = await self._get_or_create_summarized_system_prompt(event, await self._get_persona_system_prompt(event))
        reasoning_part = ',\n    "reasoning": "简短分析原因"' if self.judge_include_reasoning else ""
        
        system_inst = "你是一个群聊机器人决策系统，判断是否应回复。严格返回JSON。"
        judge_prompt = f"""
## 角色设定
{persona_prompt or "智能助手"}

## 环境
- 群聊ID: {event.unified_msg_origin}
- 上次回复: {self._get_minutes_since_last_reply(event.unified_msg_origin)}分钟前
- 状态: {self._build_chat_context(event)}
- 对话流: {self._analyze_conversation_flow(event)}
- 上次回复: {self._get_last_bot_reply(event) or "无"}

## 待判断消息
[{event.get_sender_name()}] {event.message_str}

## 评估 (0-10分)
1. relevance 2. willingness 3. social 4. timing 5. continuity (对上一句直接回应给高分)
阈值: {self.reply_threshold}

返回 JSON: {{"relevance":分, "willingness":分, "social":分, "timing":分, "continuity":分{reasoning_part}}}
"""
        contexts = self._build_optimized_contexts(self._get_raw_history_for_context(event))

        for attempt in range(self.judge_max_retries + 1):
            try:
                resp = await provider.text_chat(prompt=judge_prompt, contexts=contexts, system=system_inst, image_urls=[])
                data = _extract_json(resp.completion_text.strip())
                
                scores = {k: _clamp_score(data.get(k, 0)) for k in self.weights}
                overall = sum(scores[k] * self.weights[k] for k in self.weights) / 10.0
                should_reply = overall >= self.reply_threshold
                
                logger.info(f"[心流] 📊 正常模式: {overall:.3f}/{self.reply_threshold:.2f} | {'✅触发' if should_reply else '❌跳过'} | {scores}")
                return JudgeResult(**scores, reasoning=data.get("reasoning", ""), should_reply=should_reply, confidence=overall, overall_score=overall)
            except (json.JSONDecodeError, ValueError) as e:
                if attempt == self.judge_max_retries:
                    return JudgeResult(should_reply=False, reasoning="JSON解析失败")
            except Exception as e:
                logger.error(f"[心流] 正常模式判断异常: {e}")
                return JudgeResult(should_reply=False, reasoning=f"异常: {str(e)}")
        return JudgeResult(should_reply=False, reasoning="未知错误")

    async def judge_immersive_mode(self, event: AstrMessageEvent) -> JudgeResult:
        """沉浸模式：直接判断是否回复"""
        if not (provider := self.context.get_provider_by_id(self.judge_provider_name)):
            return JudgeResult(should_reply=False, reasoning="提供商未配置或不存在")

        persona_prompt = await self._get_or_create_summarized_system_prompt(event, await self._get_persona_system_prompt(event))
        reasoning_part = ',\n    "reasoning": "简短原因"' if self.judge_include_reasoning else ""
        
        chat_history = self._format_chat_history_for_immersive_mode(event)
        remaining = self._get_immersive_mode_remaining(event.unified_msg_origin)
        
        system_inst = "你是一个严格的群聊机器人判断系统。严格返回JSON。"
        judge_prompt = f"""
## 角色设定
{persona_prompt or "智能助手"}

## 沉浸模式判断规则
你的默认倾向是 **不回复**。只有在以下情况之一时才回复：
1. 消息直接 @ 了机器人或提到机器人名字
2. 消息明确向机器人提问
3. 消息是机器人发起的对话的延续（如机器人刚问了问题，现在有人回答）
4. 消息内容与机器人刚刚的话题直接相关，且机器人回复能明显推进对话
5. 群聊中出现了冷场，机器人的回复能自然活跃气氛

**不应该回复的情况**：
- 两个用户之间的正常闲聊
- 用户之间的问答
- 简单的表情包、语气词
- 即使bot最近活跃，也不应强行插入每条消息

## 当前状态
- 模式: 沉浸模式（bot正在积极参与群聊）
- 沉浸模式剩余: {remaining}秒

## 聊天记录（包含bot自己的回复）
{chat_history}

## 待判断消息
[{event.get_sender_name()}] {event.message_str}

## 严格判断
根据上述规则严格判断。不要因为"沉浸模式"就倾向于回复。沉默也是好的选择。

返回 JSON: {{"should_reply": true或false{reasoning_part}}}
"""
        contexts = self._build_optimized_contexts(self._get_raw_history_for_context(event))

        for attempt in range(self.judge_max_retries + 1):
            try:
                resp = await provider.text_chat(prompt=judge_prompt, contexts=contexts, system=system_inst, image_urls=[])
                data = _extract_json(resp.completion_text.strip())
                
                should_reply = bool(data.get("should_reply", False))
                
                logger.info(f"[心流] 🌊 沉浸模式: {'✅触发' if should_reply else '❌跳过'} | 剩余{remaining}秒 | {data.get('reasoning', '')}")
                return JudgeResult(
                    should_reply=should_reply, 
                    reasoning=data.get("reasoning", ""),
                    confidence=1.0 if should_reply else 0.0,
                    overall_score=1.0 if should_reply else 0.0
                )
            except (json.JSONDecodeError, ValueError) as e:
                if attempt == self.judge_max_retries:
                    return JudgeResult(should_reply=False, reasoning="JSON解析失败")
            except Exception as e:
                logger.error(f"[心流] 沉浸模式判断异常: {e}")
                return JudgeResult(should_reply=False, reasoning=f"异常: {str(e)}")
        return JudgeResult(should_reply=False, reasoning="未知错误")

    # ==================== 事件处理器 ====================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self._should_process_message(event): return
        umo = event.unified_msg_origin
        state = self._get_chat_state(umo)

        # 🆕 核心修复：bot回复后首条消息，自动恢复判断（兜底机制）
        if self._is_judgment_paused(umo):
            logger.debug(f"[心流] ⏸️  判断已暂停，仅记录消息: [{event.get_sender_name()}] {event.message_str[:30]}")
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
                state.last_reply_time = time.time()
                state.total_replies += 1
                self._activate_immersive_mode(umo)
                # 标记判断暂停 - 在bot回复发出前不再触发新的小模型判断
                self._set_judgment_paused(umo, paused=True)
                
            state.total_messages += 1
        except Exception as e:
            logger.error(f"[心流] 处理异常: {e}\n{traceback.format_exc()}")

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """bot回复后，记录消息并恢复小模型判断"""
        if not self.config.get("enable_heartflow"):
            return
        
        umo = event.unified_msg_origin
        
        # 🆕 核心修改：只要bot回复了，就刷新沉浸模式倒计时
        if (res := event.get_result()) and res.chain:
            if any(isinstance(c, Plain) and c.text.strip() for c in res.chain):
                # 无论是心流触发还是@回复，只要bot回复了，就刷新沉浸模式
                if self._is_immersive_mode_active(umo):
                    self._refresh_immersive_mode(umo)
                    logger.info(f"[心流] 🔄 bot回复，沉浸模式倒计时已刷新: {umo}")
        
        # 🆕 核心修复：只要心流触发过，就无条件恢复判断
        if event.get_extra("heartflow_triggered"):
            # 记录bot消息（如果有的话）
            if (res := event.get_result()) and res.chain:
                if any(isinstance(c, Plain) and c.text.strip() for c in res.chain):
                    self._record_raw_message(event, is_bot=True)
                    logger.debug(f"[心流] 📝 记录bot回复")
            
            # 无论如何都恢复判断，确保沉浸模式继续运作
            self._activate_immersive_mode(umo)
            self._set_judgment_paused(umo, paused=False)
            logger.info(f"[心流] ✅ bot回复完成，恢复小模型判断: {umo}")
            return
        
        # 🆕 新增：非心流触发的@回复也激活沉浸模式
        elif event.is_at_or_wake_command:
            # 记录bot消息
            if (res := event.get_result()) and res.chain:
                if any(isinstance(c, Plain) and c.text.strip() for c in res.chain):
                    self._record_raw_message(event, is_bot=True)
                    # 激活沉浸模式（如果未激活）
                    if not self._is_immersive_mode_active(umo):
                        self._activate_immersive_mode(umo)
                        logger.info(f"[心流] 🌊 @回复触发沉浸模式: {umo}")
                    else:
                        # 已激活则刷新倒计时
                        self._refresh_immersive_mode(umo)
                        logger.info(f"[心流] 🔄 @回复刷新沉浸模式: {umo}")
                    return
        
        # 非心流触发的bot回复也记录消息
        if (res := event.get_result()) and res.chain:
            if any(isinstance(c, Plain) and c.text.strip() for c in res.chain):
                self._record_raw_message(event, is_bot=True)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        if not event.get_extra("heartflow_triggered"): return
        
        if req and hasattr(req, "system_prompt"):
            req.system_prompt = (req.system_prompt or "") + "\n（注意：本次是你主动参与群聊的，请自然切入。）"

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        if not event.get_extra("heartflow_triggered"): return

    # ==================== 管理员命令 ====================

    @filter.command("heartflow")
    async def heartflow_status(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        s = self._get_chat_state(umo)
        rate = (s.total_replies / max(1, s.total_messages) * 100) if s.total_messages > 0 else 0.0
        is_immersive = self._is_immersive_mode_active(umo)
        remaining = self._get_immersive_mode_remaining(umo)
        is_paused = self._is_judgment_paused(umo)
        
        immersive_info = f"✅激活 ({remaining}秒)" if is_immersive else "❌未激活"
        paused_info = "⏸️ 已暂停（bot即将回复）" if is_paused else "▶️ 正常"
        
        event.set_result(event.plain_result(f"""
🔮 **心流状态**
- 上次回复: {self._get_minutes_since_last_reply(umo)}分钟前
- 统计: 消息{s.total_messages} | 回复{s.total_replies} ({rate:.1f}%)
- 阈值: {self.reply_threshold}
- 沉浸模式: {immersive_info}
- 沉浸持续时间: {self.immersive_mode_duration}秒
- 判断状态: {paused_info}
- 暂停超时: {self.judgment_paused_timeout}秒
"""))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("heartflow_reset")
    async def heartflow_reset(self, event: AstrMessageEvent):
        cid = event.unified_msg_origin
        with self._lock:
            self.chat_states.pop(cid, None)
            self._raw_msg_buffer.pop(cid, None)
        event.set_result(event.plain_result("✅ 心流状态已重置"))

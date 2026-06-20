import re
import time
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Plain

# 语言检测: 优先 langdetect，不可用时回退 Unicode 启发式
try:
    from langdetect import detect as lang_detect
    from langdetect.lang_detect_exception import LangDetectException
    _HAS_LANGDETECT = True
except ImportError:
    _HAS_LANGDETECT = False
    logger.warning("[bilingual_mw] langdetect 未安装，使用 Unicode 启发式检测。建议: pip install langdetect")

# Unicode 范围
_UNICODE_RANGES = {
    "ja": [(0x3040, 0x309F), (0x30A0, 0x30FF)],  # Hiragana, Katakana
    "ko": [(0xAC00, 0xD7AF), (0x1100, 0x11FF)],  # Hangul
    "ru": [(0x0400, 0x04FF)],                     # Cyrillic
    "zh": [(0x4E00, 0x9FFF)],                     # CJK
    "ar": [(0x0600, 0x06FF)],                     # Arabic
    "th": [(0x0E00, 0x0E7F)],                     # Thai
}
_TARGET = "zh"  # 默认翻译目标

# 内联协议标签清洗（stop_event 后跳过 sanitizer，需自清洗）
_RE_INLINE_SANITIZE = re.compile(
    r"<\s*(?:quote|msg|forward|reply|at|xml|record|video|file|image|json|app"
    r"|sakura|meta|source|action|rich)\b[^>]*/?\s*>",
    re.IGNORECASE,
)
_RE_CQ_INLINE = re.compile(r"\[CQ:\w+[,\]]", re.IGNORECASE)


class Main(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.enabled = self.config.get("enabled", True)
        self.input_enabled = self.config.get("input_translation", True)
        self.output_enabled = self.config.get("output_translation", True)
        self.template_input = self.config.get(
            "translation_template_input",
            "[消息翻译: {translation}]\n原文: {original}"
        )
        self.template_output = self.config.get(
            "translation_template_output",
            "\n\n（中文）\n{translation}"
        )
        self.persona_aware = self.config.get("persona_aware", True)
        self.target_lang = self.config.get("target_language", "zh")
        self.debug = self.config.get("debug_mode", False)
        self.block_segment = self.config.get("block_segment_on_output", True)
        self._persona_cache: str | None = None  # 缓存人格prompt
        logger.info(
            f"[bilingual_mw] v1.1 已加载 | enabled={self.enabled} "
            f"input={self.input_enabled} output={self.output_enabled} "
            f"target={self.target_lang} block_segment={self.block_segment} debug={self.debug}"
        )
        # 启动时强制打开 debug 日志确保可见
        logger.warning(
            f"[bilingual_mw] ===== 启动诊断 =====\n"
            f"  enabled: {self.enabled}\n"
            f"  input_translation: {self.input_enabled}\n"
            f"  output_translation: {self.output_enabled}\n"
            f"  langdetect: {_HAS_LANGDETECT}\n"
            f"  block_segment: {self.block_segment}\n"
            f"  如果群聊发外语消息后看不到 [bilingual_mw] 日志，请发送 /双语诊断"
        )

    # ==================== 诊断指令 ====================

    @filter.command("双语诊断")
    async def cmd_diagnose(self, event: AstrMessageEvent):
        """诊断指令：确认插件是否正常加载"""
        yield event.plain_result(
            f"🔍 双语中间件诊断\n"
            f"enabled={self.enabled} input={self.input_enabled} output={self.output_enabled}\n"
            f"langdetect={_HAS_LANGDETECT} target={self.target_lang} block_segment={self.block_segment}\n"
            f"persona_aware={self.persona_aware} debug={self.debug}\n\n"
            f"发送一条外语消息测试，观察日志中是否有 [bilingual_mw] 输出"
        )
        event.stop_event()

    # ==================== 语言检测 ====================

    @staticmethod
    def _detect_language(text: str) -> str | None:
        """检测文本主要语言，返回 ISO 639-1 代码"""
        if not text or not text.strip():
            return None
        text = text.strip()

        # 纯数字/符号/URL → 跳过
        if len(text) < 3:
            return None

        if _HAS_LANGDETECT:
            try:
                return lang_detect(text)
            except LangDetectException:
                pass
            except Exception:
                pass

        # 回退：Unicode 启发式
        return Main._unicode_detect(text)

    @staticmethod
    def _unicode_detect(text: str) -> str | None:
        """Unicode 范围启发式语言检测"""
        scores = {}
        total = 0
        for ch in text:
            total += 1
            code = ord(ch)
            for lang, ranges in _UNICODE_RANGES.items():
                for lo, hi in ranges:
                    if lo <= code <= hi:
                        scores[lang] = scores.get(lang, 0) + 1
                        break
        if not scores or total == 0:
            return None
        best = max(scores, key=scores.get)
        ratio = scores[best] / total
        return best if ratio > 0.15 else None

    # ==================== 源头防护（输入侧）====================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req=None):
        """LLM请求前：检测用户消息语言，非中文时翻译并注入"""
        logger.warning(f"[bilingual_mw] === on_llm_request 钩子触发 === (enabled={self.enabled}, input={self.input_enabled})")
        if not self.enabled or not self.input_enabled:
            logger.info("[bilingual_mw] 输入翻译已禁用，跳过")
            return
        if req is None:
            req = event.get_extra("provider_request")
            if req is None:
                return

        prompt = getattr(req, "prompt", None)
        if not prompt or not isinstance(prompt, str) or not prompt.strip():
            return

        lang = self._detect_language(prompt)
        if lang is None or lang == self.target_lang:
            if self.debug and lang:
                logger.debug(f"[bilingual_mw] 输入已是目标语言 {lang}，跳过翻译")
            return

        start = time.time()
        translation = await self._translate(prompt, source_lang=lang)
        elapsed = (time.time() - start) * 1000

        if translation:
            injected = self.template_input.format(original=prompt, translation=translation)
            req.prompt = injected
            logger.info(f"[bilingual_mw] 输入翻译: {lang}→{self.target_lang} ({elapsed:.0f}ms)")
            if self.debug:
                logger.debug(f"[bilingual_mw] 输入注入前: {prompt[:80]}")
                logger.debug(f"[bilingual_mw] 输入注入后: {injected[:120]}")
        else:
            logger.warning(f"[bilingual_mw] 输入翻译失败: {lang}→{self.target_lang}")

    # ==================== 发送前过滤（输出侧）====================

    @filter.on_decorating_result(priority=10)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """消息发送前（priority=10）：检测bot回复语言，非中文时追加翻译"""
        logger.warning(f"[bilingual_mw] === on_decorating_result 钩子触发 === (enabled={self.enabled}, output={self.output_enabled})")
        if not self.enabled or not self.output_enabled:
            return

        result = event.get_result()
        if result is None or not result.chain:
            return

        # 提取纯文本
        text_parts = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                text_parts.append(comp.text)
        full_text = "".join(text_parts).strip()
        if not full_text:
            return

        # 太短跳过
        if len(full_text) < 4:
            return

        lang = self._detect_language(full_text)
        if lang is None or lang == self.target_lang:
            return

        start = time.time()
        translation = await self._translate(full_text, source_lang=lang, output_side=True)
        elapsed = (time.time() - start) * 1000

        if translation and translation.strip():
            suffix = self.template_output.format(original=full_text, translation=translation)

            # 内联清理协议标签（因为 stop_event 会跳过 sanitizer）
            suffix = self._basic_sanitize(suffix)

            result.chain.append(Plain(text=suffix))
            logger.info(f"[bilingual_mw] 输出翻译: {lang}→{self.target_lang} ({elapsed:.0f}ms)")
            if self.debug:
                logger.debug(f"[bilingual_mw] 原文: {full_text[:80]}")
                logger.debug(f"[bilingual_mw] 译文: {translation[:80]}")

            # 屏蔽后续分段插件，防止双语原文+翻译被截断分离
            if self.block_segment:
                event.stop_event()
                logger.info("[bilingual_mw] 已屏蔽后续分段插件（block_segment_on_output=true）")
        else:
            logger.warning(f"[bilingual_mw] 输出翻译失败: {lang}→{self.target_lang}")

    # ==================== 翻译核心 ====================

    async def _translate(
        self, text: str, source_lang: str | None = None, output_side: bool = False
    ) -> str | None:
        """
        使用 LLM 生成翻译。output_side=True 时注入人格感知。
        """
        try:
            provider = self.context.get_using_provider(None)
            if not provider:
                logger.warning("[bilingual_mw] 无可用 LLM provider")
                return None

            lang_hint = f"（源语言: {source_lang}）" if source_lang else ""

            if output_side and self.persona_aware:
                persona_hint = await self._get_persona_hint()
                system_prompt = (
                    f"你是一个翻译助手。请将以下内容翻译成自然的中文。\n"
                    f"翻译风格要求：{persona_hint}\n"
                    f"要求：自然流畅、保持原意、不要逐字翻译。只输出翻译结果。"
                )
            else:
                system_prompt = (
                    "你是一个翻译助手。请将以下内容翻译成自然的中文。"
                    "要求：自然流畅、保持原意、不要逐字翻译。只输出翻译结果。"
                )

            result = await provider.text_chat(
                system_prompt=system_prompt,
                prompt=f"请翻译{lang_hint}：\n{text}",
            )
            translation = (
                result.completion_text
                if hasattr(result, "completion_text")
                else str(result)
            )
            return translation.strip() if translation else None
        except Exception as e:
            logger.error(f"[bilingual_mw] 翻译异常: {e}")
            return None

    async def _get_persona_hint(self) -> str:
        """获取人格描述用于翻译风格指导"""
        if self._persona_cache is not None:
            return self._persona_cache
        try:
            pm = self.context.persona_manager
            data = pm.get_v3_persona_data()
            raw = data.get("prompt", "") if data else ""
            # 截取人格描述的前200字作为风格提示
            hint = raw[:200] if raw else "保持原文语气和风格"
            self._persona_cache = hint
            return hint
        except Exception:
            self._persona_cache = "保持原文语气和风格"
            return self._persona_cache

    @staticmethod
    def _basic_sanitize(text: str) -> str:
        """内联清洗协议标签（stop_event 后替代 sanitizer 的基础功能）"""
        text = _RE_INLINE_SANITIZE.sub("", text)
        text = _RE_CQ_INLINE.sub("", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

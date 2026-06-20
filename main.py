import re
import time
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Plain

try:
    from astrbot.api.all import register
except ImportError:
    def register(*args, **kwargs):
        return lambda cls: cls

try:
    from astrbot.core.message.message_event_result import MessageChain
except ImportError:
    MessageChain = None

try:
    from langdetect import detect as lang_detect
    from langdetect.lang_detect_exception import LangDetectException
    _HAS_LANGDETECT = True
except ImportError:
    _HAS_LANGDETECT = False

_UNICODE_RANGES = {
    "ja": [(0x3040, 0x309F), (0x30A0, 0x30FF)],
    "ko": [(0xAC00, 0xD7AF), (0x1100, 0x11FF)],
    "ru": [(0x0400, 0x04FF)],
    "zh": [(0x4E00, 0x9FFF)],
    "ar": [(0x0600, 0x06FF)],
    "th": [(0x0E00, 0x0E7F)],
}

_RE_INLINE_SANITIZE = re.compile(
    r"<\s*(?:quote|msg|forward|reply|at|xml|record|video|file|image|json|app"
    r"|sakura|meta|source|action|rich)\b[^>]*/?\s*>",
    re.IGNORECASE,
)
_RE_CQ_INLINE = re.compile(r"\[CQ:\w+[,\]]", re.IGNORECASE)
_ALLOWED_LANGS = {"ja", "en"}  # 只翻译日语和英语
_RE_SYMBOLS = re.compile(r"[^\w\s\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]")


@register("astrbot_plugin_bilingual_mw", "Nya-Nya-Hoshino", "双语语言中间件", "1.2.0")
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
        self._persona_cache: str | None = None
        logger.info(
            f"[bilingual_mw] v1.2 已加载 | enabled={self.enabled} "
            f"input={self.input_enabled} output={self.output_enabled} "
            f"langdetect={_HAS_LANGDETECT} target={self.target_lang} debug={self.debug}"
        )


    # ==================== 语言检测 ====================

    @staticmethod
    def _normalize_lang(lang: str) -> str:
        """归一化语言代码: zh-cn/zh-tw → zh"""
        if lang.startswith("zh"):
            return "zh"
        return lang

    @staticmethod
    def _strip_symbols(text: str) -> str:
        return _RE_SYMBOLS.sub(" ", text)

    @staticmethod
    def _detect_language(text: str) -> str | None:
        if not text or not text.strip():
            return None
        text = text.strip()
        if len(text) < 4 or text.startswith("/"):
            return None
        clean = Main._strip_symbols(text)
        if len(clean.strip()) < 3:
            return None
        # 有假名(kana) → 日语，不含假名的纯CJK → 中文 → 跳过
        has_kana = any(0x3040 <= ord(ch) <= 0x30FF for ch in clean)
        has_cjk = any(0x4E00 <= ord(ch) <= 0x9FFF for ch in clean)
        if has_cjk and not has_kana:
            return None
        # 英语检测：至少4个连续英文字母才算
        alpha_only = re.sub(r"[^a-zA-Z]", " ", clean)
        words = [w for w in alpha_only.split() if len(w) >= 3]
        has_enough_en = len(words) >= 1 and len("".join(words)) >= 4
        # 日语检测：有假名即可
        has_ja = has_kana
        if not has_enough_en and not has_ja:
            return None
        if _HAS_LANGDETECT:
            try:
                lang = Main._normalize_lang(lang_detect(clean))
                if lang in _ALLOWED_LANGS:
                    # 二次确认：langdetect说英语但没足够英文 → 拒绝
                    if lang == "en" and not has_enough_en:
                        return None
                    return lang
            except (LangDetectException, Exception):
                pass
        if has_ja:
            return "ja"
        if has_enough_en:
            return "en"
        return None

    @staticmethod
    def _unicode_detect(text: str) -> str | None:
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
        return best if scores[best] / total > 0.15 else None

    # ==================== 环境翻译（无需@bot）====================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_ambient_message(self, event: AstrMessageEvent):
        """监听所有群消息：检测外语，静默翻译并发送"""
        if not self.enabled or not self.input_enabled:
            return
        text = event.message_str.strip()
        # 清洗 MSG_ID 标签和 URL（URL中拉丁字符会导致误判为英语）
        text = re.sub(r"\s*\[MSG_ID:\d+\]\s*", "", text).strip()
        text = re.sub(r"https?://\S+", "", text).strip()
        if not text or text.startswith("/"):
            return
        lang = self._detect_language(text)
        if lang is None or lang == self.target_lang:
            return
        translation = await self._translate(text, source_lang=lang)
        if translation:
            await event.send(
                self._build_translation_reply(text, translation, lang)
            )
            logger.info(f"[bilingual_mw] 环境翻译: {lang}→{self.target_lang} ({len(text)}字)")

    @staticmethod
    def _build_translation_reply(original: str, translation: str, lang: str) -> "MessageChain":
        lang_name = {"ja": "日语", "en": "英语", "ko": "韩语", "fr": "法语", "de": "德语", "ru": "俄语"}.get(lang, lang)
        # 清洗 MSG_ID 标签
        clean_original = re.sub(r"\s*\[MSG_ID:\d+\]\s*", "", original).strip()
        text = (
            f"{lang_name}翻译:{translation}\n"
            f"原文:{clean_original}\n"
            f"（温馨提示：为防止误识别，请勿引用此消息）"
        )
        return MessageChain().message(text) if MessageChain else None

    # ==================== 输入侧 ====================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req=None):
        if not self.enabled or not self.input_enabled:
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
            return
        translation = await self._translate(prompt, source_lang=lang)
        if translation:
            req.prompt = self.template_input.format(original=prompt, translation=translation)
            logger.info(f"[bilingual_mw] 输入翻译注入LLM: {lang}→{self.target_lang}")
            if self.debug:
                logger.debug(f"[bilingual_mw] 注入后: {req.prompt[:200]}")
        else:
            logger.warning(f"[bilingual_mw] 输入翻译失败: {lang}")

    # ==================== 输出侧 ====================

    @filter.on_decorating_result(priority=10)
    async def on_decorating_result(self, event: AstrMessageEvent):
        if not self.enabled or not self.output_enabled:
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        text_parts = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                text_parts.append(comp.text)
        full_text = "".join(text_parts).strip()
        if not full_text or len(full_text) < 4:
            return
        lang = self._detect_language(full_text)
        if lang is None or lang == self.target_lang:
            return
        translation = await self._translate(full_text, source_lang=lang, output_side=True)
        if translation and translation.strip():
            suffix = self.template_output.format(original=full_text, translation=translation)
            suffix = self._basic_sanitize(suffix)
            result.chain.append(Plain(text=suffix))
            logger.info(f"[bilingual_mw] 输出翻译: {lang}→{self.target_lang}")
            if self.block_segment:
                event.stop_event()
                logger.info("[bilingual_mw] 已屏蔽分段插件")
        else:
            logger.warning(f"[bilingual_mw] 输出翻译失败: {lang}")

    # ==================== 翻译核心 ====================

    async def _translate(self, text: str, source_lang: str | None = None, output_side: bool = False) -> str | None:
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
            translation = result.completion_text if hasattr(result, "completion_text") else str(result)
            return translation.strip() if translation else None
        except Exception as e:
            logger.error(f"[bilingual_mw] 翻译异常: {e}")
            return None

    async def _get_persona_hint(self) -> str:
        if self._persona_cache is not None:
            return self._persona_cache
        try:
            pm = self.context.persona_manager
            data = pm.get_v3_persona_data()
            raw = data.get("prompt", "") if data else ""
            self._persona_cache = raw[:200] if raw else "保持原文语气和风格"
            return self._persona_cache
        except Exception:
            self._persona_cache = "保持原文语气和风格"
            return self._persona_cache

    @staticmethod
    def _basic_sanitize(text: str) -> str:
        text = _RE_INLINE_SANITIZE.sub("", text)
        text = _RE_CQ_INLINE.sub("", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

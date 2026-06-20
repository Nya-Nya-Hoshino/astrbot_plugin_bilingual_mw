# Bilingual Language Middleware — 开发报告

## 1. 项目概况

| 项目 | 内容 |
|---|---|
| 插件名 | Bilingual Language Middleware |
| 版本 | v1.3.5 |
| 语言 | Python 3.10+ |
| 依赖 | langdetect (可选), AstrBot v4.24.4+ |
| 代码量 | 280行 (main.py) |

## 2. 开发历程

### 2.1 迭代记录

| 版本 | 关键变更 | 解决的问题 |
|---|---|---|
| v1.0.0 | 初始版本：on_llm_request + on_decorating_result | 基础双语框架 |
| v1.1.0 | 添加 block_segment_on_output + 内联 sanitize | 翻译后不被分段截断 |
| v1.1.1-1.1.3 | @register 修复 + 探针诊断 | 非标准环境 handler 注册失败 |
| v1.2.0-1.2.2 | 清理调试代码 + 生产发布 | 代码精简 |
| v1.3.0 | event_message_type 环境翻译 | 无需@bot即可翻译 |
| v1.3.1 | _ALLOWED_LANGS = {ja, en} | 限制翻译语种 |
| v1.3.2-1.3.4 | CJK/假名/英文字母下限检测 | 符号和中文误判 |
| v1.3.5 | 输出格式优化 | 可读性提升 |

### 2.2 核心难点与解决方案

#### 难点1: handler 注册失败 (v4.24.4 兼容性)

**现象**: `@filter.on_decorating_result()` 和 `@filter.on_llm_request()` 在用户环境的 AstrBot v4.24.4 上完全不触发，但 `@filter.command()` 正常。

**排查过程**:
1. 添加 WARNING/ERROR 级别日志确认钩子未执行
2. 添加 `/双语诊断` 指令确认插件已加载
3. 复制 emoji_filter 的钩子模式对比验证
4. 发现 `@register` 装饰器在 custome_segment_reply 中使用

**根因**: 非标准 Linux 环境下 Star 基类的自动 handler 发现机制失效。

**解决**: 添加 `@register("astrbot_plugin_bilingual_mw", ...)` 显式注册。

#### 难点2: langdetect 中文误判

**现象**: "喵喵喵请求搞一个插件出来" 被 langdetect 误判为越南语 (vi)。

**分析**: langdetect 对短文本/含特殊字符的中文可能产生错误结果。

**解决**: 
- 添加假名检测 (kana) 区分中日 — 日文含假名(あいう)，中文不含
- 纯 CJK 无假名直接判定为中文并跳过
- 添加 `_ALLOWED_LANGS` 白名单仅放行日语和英语

#### 难点3: 符号干扰语言检测

**现象**: 纯标点符号 "！！！" "Hello！" 等被 langdetect 误判。

**解决**:
- `_strip_symbols()` 正则剥离所有非文字符号
- 英语需要至少1个≥3字母单词 + 总计≥4英文字母
- langdetect 返回 "en" 但无足够英文 → 二次拒绝

#### 难点4: 环境翻译 vs LLM 上下文注入的协同

**问题**: 环境翻译 (event_message_type) 发送翻译到聊天后，当用户 @bot 时，LLM 也需要看到翻译才能正确回复。

**方案**: 
- 环境翻译负责"用户可见"的翻译
- on_llm_request 负责"LLM 理解"的翻译注入
- 两者独立运行，不冲突

## 3. 架构决策

### 3.1 为什么不用外部翻译 API？

| 方案 | 优点 | 缺点 |
|---|---|---|
| 外部 API (Google/DeepL) | 快速、准确 | 额外费用、网络依赖、不支持人格感知 |
| **AstrBot LLM** | 免费、自然、支持人格感知 | 增加一次 LLM 调用、速度较慢 |

选择 AstrBot LLM 的原因：翻译需要保持人格风格（猫娘→"喵~"），外部 API 无法实现。

### 3.2 为什么三钩子设计？

| 钩子 | 必要性 |
|---|---|
| event_message_type | 无@bot的群聊外语不能依赖 LLM 触发，需要独立消息监听 |
| on_llm_request | @bot 时需要翻译注入 LLM 上下文，否则 LLM 不理解外语 |
| on_decorating_result | Bot 可能用外语回复，需要追加翻译给用户 |

三个钩子覆盖了"用户外语输入不被理解"和"Bot外语输出用户看不懂"两种场景。

### 3.3 为什么 priority=10？

```
emoji_filter(100) → bilingual_mw(10) → segment_reply(0)
```

翻译追加在 emoji 过滤之后、分段之前。翻译可能显著增加消息长度，需在分段前完成以避免翻译内容被切散。

## 4. 已知限制

| 限制 | 说明 | 原因 |
|---|---|---|
| 仅支持日/英 | 其他语言被过滤 | 降低误判率，专注高频场景 |
| 需要 LLM Provider | 无 LLM 时插件完全静默 | 翻译依赖 LLM 调用 |
| 环境翻译会增加 LLM 消耗 | 每条外语触发一次翻译 | 可用 input_translation=false 关闭 |
| 输出翻译后跳过 sanitizer | 内联 _basic_sanitize 替代 | stop_event 会跳过所有后续插件 |

## 5. 与现有插件的协作

| 插件 | priority | 关系 |
|---|---|---|
| emoji_filter | 100 | 上游，先过滤emoji |
| bilingual_mw | 10 | 本插件 |
| segment_reply | 0 | 被 stop_event 屏蔽（翻译时） |
| message_sanitizer | -100 | 被 stop_event 跳过（内联替代） |

## 6. 未来改进方向

1. **缓存翻译结果**: 相同原文不重复调用 LLM
2. **支持更多语言**: 韩语、法语等实用场景
3. **语言学习模式**: 附加词汇注释 (こんにちは = 你好)
4. **配置化白名单**: 用户自定义哪些语言需要翻译
5. **翻译质量门控**: 译文质量低于阈值时放弃追加

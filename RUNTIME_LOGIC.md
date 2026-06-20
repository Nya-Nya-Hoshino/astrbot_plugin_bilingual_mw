# Bilingual Language Middleware — 运行逻辑书

## 1. 概述

本插件是 AstrBot 的语言感知中间层，自动识别群聊中的日语/英语消息，通过 LLM 生成自然中文翻译，以"原文+翻译"双语形式呈现。三钩子协同覆盖输入、输出、环境三种场景。

---

## 2. 架构总览

```
┌─ 钩子1: event_message_type(GROUP_MESSAGE) ──────────────────────────┐
│ 监听所有群消息（无需@bot）                                             │
│ 检测到日/英语 → LLM翻译 → event.send() 发送原文+翻译到聊天             │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌─ 钩子2: on_llm_request() ───────────────────────────────────────────┐
│ LLM请求前拦截（仅@bot或/命令触发）                                      │
│ 非中文消息 → LLM翻译 → 注入 req.prompt 供主模型理解                   │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
                       主模型 (DeepSeek等) 生成回复
                              ↓
┌─ 钩子3: on_decorating_result(priority=10) ──────────────────────────┐
│ 消息发送前装饰                                                       │
│ 检测bot回复非中文 → 人格感知翻译 → 追加译文 → stop_event()屏蔽分段      │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
                        消息发送至用户
```

## 3. 流水线位置

```
emoji_filter(100)  →  bilingual_mw(10)  →  segment_reply(0)  →  sanitizer(-100)
去emoji              原文+翻译追加         分段（被stop_event屏蔽） 清洗协议标签
```

输出侧触发翻译时调用 `event.stop_event()`，屏蔽后续 segment_reply 和 sanitizer，双语内容由插件内联的 `_basic_sanitize` 清洗协议标签。

---

## 4. 语言检测决策树

```
输入文本
  │
  ├─ len<4 或 以/开头? ──→ 跳过
  │
  ├─ _strip_symbols() 剥离所有标点符号
  │
  ├─ clean文本长度<3? ──→ 跳过
  │
  ├─ 含假名(あいう)? ──→ 日语候选 (has_ja=True)
  │   └─ 含CJK无假名? ──→ 纯中文 → 跳过
  │
  ├─ 英文单词≥1个(≥3字母) 且 总英文字母≥4? ──→ 英语候选 (has_enough_en=True)
  │
  ├─ has_ja和has_enough_en都为False? ──→ 跳过
  │
  ├─ langdetect可用?
  │   ├─ lang="en" 且 has_enough_en=False ──→ 二次拒绝 → 跳过
  │   ├─ lang in {"ja","en"} ──→ 返回该语言
  │   └─ 其他 ──→ 跳过
  │
  └─ langdetect不可用?
      ├─ has_ja ──→ 返回 "ja"
      └─ has_enough_en ──→ 返回 "en"
```

### 4.1 关键规则

| 规则 | 目的 |
|---|---|
| 假名优先 | 中日共用汉字 (CJK)，假名是区分日语的唯一可靠标志 |
| 英文字母下限 | 至少1个≥3字母单词 + 总计≥4字母，过滤"Hi" "OK"等短词 |
| langdetect 二次确认 | langdetect 返回"en"但实际无足够英文 → 拒绝 |
| 符号剥离 | 标点/Emoji/`/`等不参与语言判断 |

---

## 5. 翻译核心

### 5.1 技术路线

- **不使用外部翻译API** — 全部通过 AstrBot 当前 LLM Provider 完成
- **输入翻译**: 纯翻译模式（system_prompt: "你是一个翻译助手，只输出翻译结果"）
- **输出翻译**: 人格感知模式 — 读取 Persona 前200字作为风格提示，使译文匹配人格（猫娘→"喵~", 温柔→"呀~"）

### 5.2 人格感知机制

```
persona_manager.get_v3_persona_data()
  → 取 prompt 字段前200字
  → 注入翻译 system_prompt:
    "翻译风格要求：{persona_hint}"
  → LLM 生成的译文自动匹配人格口癖
```

### 5.3 降级策略

| 异常 | 行为 |
|---|---|
| LLM provider 不可用 | 静默跳过，不影响消息发送 |
| langdetect 未安装 | 回退 Unicode 启发式检测 |
| 翻译 API 报错 | 日志记录，不阻断消息 |
| 翻译返回空 | 跳过，不追加译文 |

---

## 6. 配置项

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | true | 总开关 |
| `input_translation` | bool | true | 环境翻译 + LLM注入 |
| `output_translation` | bool | true | Bot回复双语化 |
| `persona_aware` | bool | true | 人格感知翻译 |
| `target_language` | string | zh | 翻译目标语言 |
| `block_segment_on_output` | bool | true | 输出翻译时屏蔽分段 |
| `debug_mode` | bool | false | 详细日志 |

---

## 7. 输出格式

```
日语翻译:你好！
原文:こんにちは！
（温馨提示：为防止误识别，请勿引用此消息）
```

- 翻译标题标识源语言
- 原文保留不替换
- 防误识别提示防止引用消息被再次处理

---

## 8. 异常安全

- 所有钩子入口有 `enabled` 开关检查
- 所有 LLM 调用包裹 try/except
- `event.send()` 失败不影响主流程
- `event.stop_event()` 仅在翻译成功后调用
- 插件异常不导致 AstrBot 崩溃

# astrbot-plugin-bilingual_mw

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-plugin-6c5ce7.svg)](https://github.com/AstrBotDevs/AstrBot)

双语语言中间件 —— 自动识别非中文消息，LLM 人格感知翻译，原文+翻译双语呈现。

## 解决的问题

群聊中经常出现日语、英语、韩语等非中文消息，部分群成员无法理解。本插件自动检测并生成自然翻译，**输入输出双端覆盖**。

```
用户发 "こんにちは"
  ↓ 输入侧翻译
主模型看到 "[翻译: 你好]\n原文: こんにちは"
  ↓ 主模型回复
bot 回复 "おはようございます～"
  ↓ 输出侧翻译
发送: "おはようございます～\n\n（中文）早上好喵～"
```

## 流水线位置

```
emoji_filter(100) → bilingual_mw(10) → segment_reply(0) → sanitizer(-100) → 发送
```

- 在 emoji 过滤之后 (不影响表情),
- 在分段之前 (翻译加入后被一起分段),
- 在清洗之前 (不干扰协议标签清理)

## 安装

```bash
cd ~/.astrbot/data/plugins
git clone https://github.com/Nya-Nya-Hoshino/astrbot_plugin_bilingual_mw.git
pip install langdetect
```

## 配置

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | true | 总开关 |
| `input_translation` | bool | true | 输入翻译 |
| `output_translation` | bool | true | 输出翻译 |
| `target_language` | string | zh | 翻译目标语言 |
| `persona_aware` | bool | true | 人格感知翻译 |
| `debug_mode` | bool | false | 调试模式 |

## 许可证

MIT License

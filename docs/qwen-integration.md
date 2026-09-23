# Qwen Audio 3.0 Realtime 接入

本文只描述 `qwen-audio-3.0-realtime-flash` 和 `qwen-audio-3.0-realtime-plus`。CLI alias 为 `qwen-realtime`，工件必须保存实际模型 ID。

## 官方依据

| 来源 | 固定版本 | 用途 |
|---|---|---|
| [阿里云语音示例](https://github.com/aliyun/alibabacloud-bailian-speech-demo/tree/1082942345c555429ee61c04b8c585f12e06dab2/samples/conversation/fun-audiochat-realtime) | `1082942345c555429ee61c04b8c585f12e06dab2` | WebSocket 事件、smart-turn、Function Calling |
| [QwenAudio Smart Cockpit runner](https://github.com/QwenAudio/qwen-audio-agent/blob/149440d01d5f3ff4feaf2c6f904d05e6ac81d499/examples/smart-cockpit/bench/runner/run-realtime.mjs) | `149440d01d5f3ff4feaf2c6f904d05e6ac81d499` | 座舱工具时序与冻结语音 benchmark |

官方代码定义：

- endpoint 为 `wss://dashscope.aliyuncs.com/api-ws/v1/realtime`。
- session modalities 必须包含 text；本项目请求 text + audio。
- 输入为16 kHz单声道 PCM16，输出为24 kHz单声道 PCM16。
- 默认音色使用 `longanqian`，话轮检测使用 `smart_turn`。
- `session.updated` 不回显 input audio format；该字段保留为 unverified，不伪造确认。
- smart-turn 由服务端自动创建首轮 response，客户端不能为同一 user item 再补发一次。
- 工具调用完成后发送 `conversation.item.create(function_call_output)`，再显式创建工具后续 response。

## Adapter 时序

```text
session.update(tool_choice=auto, smart_turn)
  -> input_audio_buffer.append...
  -> server response.created
  -> function_call
  -> response.done
  -> local tool execution
  -> conversation.item.create(function_call_output)
  -> response.create(tool_choice=none)
  -> final audio/text response
```

Adapter 0.5.0 只接受 Audio 3.0 Flash/Plus。工具结果后的 response slot 如短暂 busy，会保存原始拒绝并按 1.2s、2.6s、5s 有界重试；不重发工具副作用。

## 配置

- `configs/qwen-audio-3.0-realtime-flash-agent.yaml`
- `configs/qwen-audio3-smart-turn.yaml`

连接探针默认同样使用 Audio 3.0 Flash：

```bash
uv run --locked --extra qwen python -m benchmark.qwen_probe \
  --audio recordings/question.wav \
  --output runs/my-qwen-probe
```

## 能力边界

- 当前模型名是服务别名，服务端没有返回不可变 revision。
- input format 未由 session echo 明确确认，因此工件继续记录 unverified basis。
- 真实运行需要 `DASHSCOPE_API_KEY`；默认测试只使用 fake WebSocket，不产生费用。
- 实际数据、运行工件和结果报告只保存在本地，不写入仓库文档。

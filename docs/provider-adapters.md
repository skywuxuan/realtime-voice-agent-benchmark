# Provider Adapter Boundaries

`adapters/registry.py` 当前注册：

| alias | 状态 | 行为 |
|---|---|---|
| `qwen-realtime` | implemented | 支持 Qwen Audio 3.0 Flash/Plus 与 3.1 Plus，包含 PCM、smart-turn、取消和工具结果回注 |
| `step-realtime` | implemented | StepAudio 3 Realtime，24 kHz PCM16、Bearer WebSocket、manual commit（server VAD 可显式试验）与自定义 function calling |
| `doubao-realtime` | implemented | Seed Duplex 3.0 JSON Realtime、音频流和工具结果回注 |

仍未核验的 provider 可以生成不联网的 capability artifact：

```bash
.venv/bin/python -m benchmark.provider_probe \
  --model step-realtime --output runs/probes/step
```

该 probe 不读取 key、不连接服务，也不把 unknown 写成 supported 或 unsupported。

StepAudio 3 Realtime 的当前核验资料为：

- endpoint：`wss://api.stepfun.com/v1/realtime?model=stepaudio-3-realtime-preview`
- 鉴权：`Authorization: Bearer $STEPFUN_API_KEY`
- 输入/输出：单声道 24 kHz PCM16
- 参考：[Realtime API](https://platform.stepfun.com/docs/zh/api-reference/realtime/chat)、[开发指南](https://platform.stepfun.com/docs/zh/guides/developer/realtime)

Step 接入仍遵守以下边界：

1. 固定官方文档和 example commit。
2. 用独立 probe 验证连接、session configure、音频格式、streaming、VAD、cancel 和工具结果。
3. 在 provider adapter 内映射 raw event，不在 runner 中加入厂商分支。
4. 用同一 Scenario、runner、evaluator 和 artifact layout 完成真实 case；云端模型行为和延迟仍需有效 key 实测。

Doubao 使用官方 Seed Duplex 3.0 端点
`wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue`，服务端模型版本固定为
`1.2.6.1`。当前凭据来自旧版控制台，环境变量分别为 `BYTEDANCE_LLM_APPID` 和
`BYTEDANCE_LLM_TOKEN`；adapter 按官方旧版兼容鉴权发送 App ID、Access Token、
`volc.speech.dialog` 与固定 App Key；当前 CLI 使用新版 `BYTEDANCE_LLM_API_KEY`。
协议与配置见 [Doubao 集成](doubao-integration.md)。实际探针和运行结果只保存在本地工件。

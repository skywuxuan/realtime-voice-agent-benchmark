# Seed Duplex 3.0 集成

本文只记录官方协议、配置和实现边界，不记录凭据来源、实际请求内容或运行结果。

## 官方协议

- [接入必读](https://www.volcengine.com/docs/6561/2549732)定义 JSON WebSocket Realtime
  事件、Function Calling 和 `call_id` 结果回注。
- [全双工协议](https://www.volcengine.com/docs/6561/2549778)定义端点
  `wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue`、会话模型版本、音频规格
  和会话事件。
- [旧版控制台鉴权](https://www.volcengine.com/docs/6561/2534847)定义兼容鉴权头。

厂商 wire protocol 只存在于 `adapters/doubao/`；runner 和 evaluator 不包含厂商分支。

## 凭据

CLI 默认从 `BYTEDANCE_LLM_API_KEY` 读取新版 API Key。Adapter 兼容旧版
`BYTEDANCE_LLM_APPID` 与 `BYTEDANCE_LLM_TOKEN` 组合。凭据只从进程环境进入请求，
不得写入源码、命令行、日志或工件。

## 会话生命周期

Adapter 依次建立 WebSocket、发送 `session.create`、流式追加 16 kHz PCM，并把服务端
24 kHz PCM、转写、响应状态和工具事件映射到统一事件模型。

输入按官方建议使用 20ms 分片。最后一帧发送后，Adapter 提交并 mute 输入；连续流短暂
空闲时也会自动 mute，下一帧到来前 unmute，以保持服务端话轮生命周期明确。

同一 response 的工具调用按批次收集。通用 Agent runtime 执行工具后，Adapter 使用原始
`call_id` 创建结果 item；同一批结果只提交一次，不重复执行副作用。

## 调度配置

`configs/latency.yaml` 是共享严格 profile。
`configs/latency-seed-duplex3-cockpit-tolerant.yaml` 只放宽本地输入和虚拟播放调度迟到，
不改变单次发送耗时限制、PCM 分片、尾静音或模型协议。不同 profile 的结果必须分组，
汇总器会把实际 timing profile 写入本地报告引用。

## 共享冻结音频

Seed campaign 可使用 `--cache-only --await-cache` 复用已经冻结的音频。该模式不读取 TTS
凭据，缓存缺失时只等待，不生成第二份音频。由此确保不同 target adapter 接收同一 PCM。

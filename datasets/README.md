# Phase 3 中文 latency 音频

## 文本源与自动冻结

`source/` 保存文本优先的数据集，`rendered/<profile_id>/` 保存内容寻址的 TTS WAV/JSON，`../scenarios/compiled/` 保存由文本展开的可运行 Scenario。半双工与全双工紧凑样例分别在 `source/half_duplex/corpus.yaml` 和 `source/full_duplex/corpus.yaml`。

```bash
.venv/bin/python -m dataset.render \
  --source datasets/source/half_duplex/corpus.yaml \
  --tts-profile configs/tts/qwen-cherry.yaml
```

TTS 必须在 benchmark session 前完成。正式复跑加 `--cache-only`，以保证缺失资产时直接失败且不访问供应商。详细 schema、缓存身份与全双工触发语义见 [文本测试集说明](../docs/text-dataset.md)。

`audio/latency/` 保存 10 条自编中文短问句的冻结 TTS 音频。场景入口为 [basic.yaml](../scenarios/realtime/basic.yaml)，使用时不重新合成语音。

输入为单声道 PCM16、16 kHz，由 `qwen3-tts-flash` / `Cherry` 合成后经 FFmpeg 转换。每个 WAV 都有对应 JSON，记录文本、模型、音色、SDK 版本、request ID、音频哈希、样本数及边界参数；[manifest.json](audio/latency/manifest.json) 汇总全部资产。它们是基础设施验证用的单音色 TTS 小样本，不代表中文真人语音、口音或噪声覆盖。

语音边界由 `energy_rms_v1` 自动估计：20 ms RMS 窗口，阈值为 `max(24, 峰值 RMS × 0.01)`，首尾各加一个保护窗口。`resolution_ms=20` 是窗口分辨率，**不是语义边界误差上限**。这些边界未经人工听辨确认，因此 evaluator 将其单独归入 `boundary_method=energy_rms_v1 / boundary_status=automatic`，不得与人工边界结果混合。

正常运行只使用已经冻结的文件。需要准备新的资产时，先设置 `DASHSCOPE_API_KEY`，再运行下面的可选数据准备步骤，它会调用真实 TTS API。

```bash
uv run --locked --extra assets python scripts/prepare_latency.py --limit 10
```

脚本复用已存在且哈希一致的音频，不覆盖已有 WAV。改变文本、音色或边界方法时应建立新场景版本；TTS API 再次生成不保证与旧音频逐字节相同。供应商返回的临时音频 URL 和 API key 不写入元数据。

与本地推理或其他 Realtime API 对比时保持相同输入 WAV 和标注。若目标输入格式不同，须预先生成并冻结转换后的资产，保存转换关系；当前 Phase 3 runner 不在计时路径临时重采样。

Phase 4 另有 `audio/interruption/` 中的两条冻结 TTS，对应 [interruption 场景 v2](../scenarios/realtime/interruption/zh_interruption_001.yaml)。v2 复用同一音频/边界，将 drain 预算设为 60 秒并显式记录旧城市规则；不覆盖历史 run 的冻结场景。该单音色、单内容 fixture 只用于基础链路验收，更多内容待扩展，见 [Phase 4 实现](../docs/interruption-implementation.md)。


## 2026-09-20 衍生与工具语料

`audio/duplex/` 包含 9 个离线衍生 WAV。来源为已冻结 TTS，5 个 pause 在同一源位置插入固定时长静音，3 个 overlap 保存增益/样本偏移混音配方。运行 `.venv/bin/python -m scripts.prepare_duplex` 可逐字节重现；已有资产不覆盖。

`audio/agent/zh_agent_weather_001.wav` 是真正的中文天气问题 TTS，已用于 Qwen Plus 工具闭环。`audio/backchannel/zh_backchannel_tts_001.wav`、`002.wav` 分别是“嗯嗯”“你继续”，通过 `--transport stdlib` 合成；原 330Hz 音调 fixture 不改写，独立标签与实验组保留。

新 stdlib TTS 路径根据固定 DashScope 官方 SDK 核对 HTTP 格式，key 仅用于 HTTPS 合成请求，下载供应商输出时不转发 key。元数据记录 request_id、协议来源、transport、hash；不保存 API key 或签名音频 URL。


高级 Agent 语料由 `.venv/bin/python -m scripts.prepare_agent_advanced --transport stdlib` 准备，现已冻结：查车→日历的12.8秒输入、改口两段4.32/4.16秒输入，故障重试复用已有天气问题。多步骤200ms配置使用同一 WAV，不重新生成音频；与原20ms场景通过 scenario_id/input_chunk_ms 区分。

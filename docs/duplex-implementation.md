# Turn Taking / Pause / Overlap 实现

`duplex-0.2` 使用显式音频区间和真实发送边界，接入统一 run/evaluate/HTML report。仓库包含可重复生成的中文 pause、turn-taking 和 overlap fixture。

## 运行

```bash
.venv/bin/python -m scripts.prepare_duplex

.venv/bin/python -m benchmark.run \
  --model qwen-realtime --suite realtime \
  --config configs/qwen-audio-3.0-realtime-flash-agent.yaml \
  --scenario scenarios/realtime/pause_basic.yaml \
  --output runs/my-pause --warmups 0

.venv/bin/python -m benchmark.evaluate --run runs/my-pause
```

每次 run 使用新目录。输入 WAV、场景、配置、PCM、raw/normalized events、转写、metrics 和 report 按统一布局保存。

## 场景边界

`AudioAsset.regions` 记录区间 ID、kind 和 `bounds_samples`。Pause 使用 `kind=pause`；干扰语音使用 `kind=interferer` 并标注 subtype。

Pause 必须位于整句 speech bounds 内。Sender 在所有区间边界拆帧并产生 `input_region_start/end`，整句话只产生一个 `user_audio_start/end`。暂停窗口内 PCM 标为 silence，不会被压缩或跳过。

冻结场景可以包含干净 turn-taking、人工静音和固定增益/偏移的混音干扰。它们是合成扰动，
不代表自然停顿或多真人说话人覆盖。具体输入与运行结果只保存在本地工件。

## 指标与有效性

`response_gap_ms` 是关联 response start 减最终 `user_audio_end`。TTFA receive/playback 分别使用首音频接收和播放边界，不把 `response.created` 当成音频输出。

Pause 同时报告整句完成前的提前响应和标注停顿窗口内的 response start。Overlap 计算用户发送音频、干扰区间与助手虚拟播放区间的交集，并先合并重叠区间避免重复计数。

输入/播放调度超限、损坏工件、缺失 region 或错误因果引用为 invalid。有效模型超时保留分母和 censored。Overlap 尚无独立内容 judge 时保持 unknown，不能仅凭收到音频判语义通过。

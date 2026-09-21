# Phase 5 Backchannel

更新日期：2026-09-20。Backchannel 使用与 interruption 相同的双轮刺激执行器，评价版本为 `backchannel-0.2`。已有“嗯嗯”“你继续”两条冻结中文 TTS；旧 330Hz 合成音调仍保留为协议实验，不充当中文语义语料。

## 准备与运行

```bash
# 已有 WAV 可直接复用；首次准备会调用 TTS，key 通过环境变量读取
.venv/bin/python -m scripts.prepare_backchannel --transport stdlib

.venv/bin/python -m benchmark.run --model qwen-realtime --suite realtime \
  --scenario scenarios/realtime/backchannel_tts.yaml \
  --output runs/my-backchannel --warmups 0
.venv/bin/python -m benchmark.evaluate --run runs/my-backchannel
```

stdlib TTS 请求格式来自固定官方 SDK，API 请求经 HTTPS，供应商签名输出 URL 独立下载且不转发 key。`--transport sdk` 仍可使用已声明的 assets extra。音频及请求 ID、来源、hash 和自动边界冻结后复用，正常 benchmark 不做 TTS。

## 执行与指标

`backchannel_start/end` 对齐第二段语音的标注边界，不生成 interrupt_start，不发送 client cancel。第一轮尾部静音可在触发后停止，t1/t2 输入不交错。where、occurrence、deadline、实际语音开始处的活动播放及未消费 PCM 前置条件，与 interruption 一致。

离线评价复核完整刺激、缓冲前置条件、调度门槛、原始配置/场景哈希和封口。客户端取消污染 native trial 时标 invalid，不能算模型 False Interruption。

- 观察期内目标 response 被 server cancelled：false_interruption=true，fail。
- 同一 response 正常完成、音频完整播放、第二段用户语音结束后仍有 continuation 且未切换新 response：false_interruption=false，pass。
- 缺少完成、播放被截断或生成了无法判定的新 response：unknown，不能当作“继续”或“误打断”。

FIR 同时给出 known/unknown 数、known 样本比例和基于全部 eligible 的上下界。无证据的 unknown 不会静默当 false；invalid/infra/unsupported 从有效分母排除。边界方法和模型配置分组，仍只测 native server-VAD 行为，不能据此声称模型具有语义级双工能力。

当前中文 TTS 均为 Cherry 单音色、自动边界；尚未覆盖真人 backchannel、不同语速和口音。旧 `runs/qwen-phase5-backchannel-20260919-001/` 使用音调，其历史 pass 只表示该协议实验结果。真实语音实验及回归状态见项目 README / AGENTS。

## 2026-09-20 真实中文实验

两次固定套件运行分别保存在 `runs/qwen-backchannel-tts-20260920-001/` 与 `002/`。全部四个 attempt 保留，未放宽原 20ms profile。

| Run / case | 结果 | 原因 |
|---|---|---|
| 001 / 嗯嗯 | invalid | 首轮缺少 playback trigger；没有形成有效刺激 |
| 001 / 你继续 | invalid | 输入 send 阻塞约 4.8 秒，超出门槛 |
| 002 / 嗯嗯 | invalid | 首轮仍未产生 playback trigger |
| 002 / 你继续 | fail，eligible | server cancel 的 reason=turn_detected，原回答停止 |

因此只有一个可判定的中文 Backchannel 样本，观测到一次 False Interruption。这个 n=1 结果不构成模型总体 FIR 结论，invalid 不应从报告中隐藏。原始“嗯嗯”TTS 长度约 3.44 秒，尚未经人工听辨/语义校准，后续应扩展真实录音和独立 ASR 核验。

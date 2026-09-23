# 数据与冻结音频

`source/` 保存可版本化的示例格式；`rendered/<profile_id>/` 保存内容寻址的 TTS WAV/JSON；
`scenarios/compiled/` 保存由文本展开的运行时 Scenario。外部测试集、实际转换数据和报告不
进入版本控制。

```bash
.venv/bin/python -m dataset.render \
  --source /path/to/local-corpus.yaml \
  --tts-profile configs/tts/qwen-cherry.yaml
```

TTS 必须在 benchmark session 前完成。正式比较使用 `--cache-only`，缺失资产时直接失败且
不访问供应商。详细 schema、缓存身份与全双工触发语义见
[文本数据说明](../docs/text-dataset.md)。

## 冻结规则

- 输入统一为单声道 PCM16，并在 metadata 中记录采样率、模型、音色、传输实现、请求 ID、
  音频哈希、样本数和边界参数。
- 语音边界方法及状态必须进入分组；自动能量边界不能与人工边界混合。
- 已存在且哈希一致的 WAV 不覆盖。文本、音色、格式或边界 recipe 变化时生成新的内容地址。
- 供应商临时 URL、签名参数和 API key 不写入 metadata。
- 派生 pause/overlap 资产保存源哈希和确定性变换配方；不同输入分帧配置使用独立场景身份。

需要模型凭据的准备脚本只从环境读取 key。正常 benchmark 只读取已经冻结且 SHA-256 一致
的本地 WAV，不在计时路径中调用 TTS 或临时重采样。

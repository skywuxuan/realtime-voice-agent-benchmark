"""Build a five-model cockpit comparison, allowing explicit partial coverage."""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from agent.artifacts import latency_summary
from benchmark.contracts import pretty_json
from events.replay import file_hash
from reports.cockpit_comparison import _attempt_detail, _provider_rows_from_reports, _read

PROVIDERS = ("flash", "plus", "plus31", "seed", "step")
DISPLAY_NAMES = {
    "flash": "Qwen Audio 3.0 Realtime Flash",
    "plus": "Qwen Audio 3.0 Realtime Plus",
    "plus31": "Qwen Audio 3.1 Realtime Plus",
    "seed": "Seed Duplex 3.0",
    "step": "StepAudio 3 Realtime",
}
PROVIDER_MAX_LINE = {"step": 200}


def _normalize_asr_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return "".join(character for character in normalized if character.isalnum())


def _edit_distance(reference: str, transcript: str) -> int:
    previous = list(range(len(transcript) + 1))
    for row, reference_character in enumerate(reference, 1):
        current = [row]
        for column, transcript_character in enumerate(transcript, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (reference_character != transcript_character),
                )
            )
        previous = current
    return previous[-1]


def _normalize_with_script(texts: list[str], normalizer: Path) -> dict[str, str]:
    unique = list(dict.fromkeys(texts))
    if not unique:
        return {}
    if not normalizer.is_file():
        raise ValueError(f"ASR text normalizer does not exist: {normalizer}")
    normalizer_python = shutil.which("python")
    if normalizer_python is None:
        raise ValueError("python executable for ASR text normalization is unavailable")
    if any("\n" in text or "\r" in text for text in unique):
        raise ValueError("ASR normalization input must contain one record per line")
    with tempfile.TemporaryDirectory(prefix="cockpit-asr-norm-") as directory:
        root = Path(directory)
        source, output = root / "input.txt", root / "output.txt"
        source.write_text("\n".join(unique) + "\n", encoding="utf-8")
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (str(normalizer.parent), environment.get("PYTHONPATH", "")))
        )
        result = subprocess.run(
            [normalizer_python, str(normalizer), str(source), str(output)],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"ASR text normalization failed with status {result.returncode}: "
                f"{result.stderr[-1000:]}"
            )
        normalized = output.read_text(encoding="utf-8").splitlines()
    if len(normalized) != len(unique):
        raise ValueError("ASR text normalizer changed the number of records")
    return dict(zip(unique, normalized))


def _asr_evaluation(
    reference_text: str,
    transcripts: list[str],
    final_status: str,
    normalized_texts: dict[str, str],
) -> dict:
    transcript = transcripts[-1] if transcripts else None
    normalized_reference = _normalize_asr_text(normalized_texts[reference_text])
    normalized_transcript = _normalize_asr_text(normalized_texts[transcript]) if transcript else ""
    available = bool(normalized_transcript)
    distance = (
        _edit_distance(normalized_reference, normalized_transcript) if available else None
    )
    exact = available and normalized_reference == normalized_transcript
    cer = distance / max(1, len(normalized_reference)) if distance is not None else None
    if final_status != "fail":
        attribution = "not_a_fail"
    elif not available:
        attribution = "missing_asr_suspect"
    elif exact:
        attribution = "asr_unlikely"
    else:
        attribution = "asr_possible"
    return {
        "reference_text": reference_text,
        "transcript": transcript,
        "normalized_reference": normalized_reference,
        "normalized_transcript": normalized_transcript if available else None,
        "available": available,
        "exact_match": exact,
        "edit_distance": distance,
        "cer": round(cer, 6) if cer is not None else None,
        "failure_attribution": attribution,
    }


def _provider_summary(cases: list[dict], provider: str) -> dict:
    rows = [
        case["providers"][provider]
        for case in cases
        if case["status"] == "valid_tool" and case["providers"][provider] is not None
    ]
    eligible = [row for row in rows if row["eligible"]]
    asr_available = [row for row in rows if row["asr_evaluation"]["available"]]
    asr_edits = sum(row["asr_evaluation"]["edit_distance"] for row in asr_available)
    asr_reference_characters = sum(
        len(row["asr_evaluation"]["normalized_reference"]) for row in asr_available
    )
    micro_cer = asr_edits / asr_reference_characters if asr_reference_characters else None
    return {
        "cases": len(rows),
        "eligible": len(eligible),
        "pass": sum(row["status"] == "pass" for row in rows),
        "fail": sum(row["status"] == "fail" for row in rows),
        "invalid": sum(row["status"] == "invalid" for row in rows),
        "eligible_pass_rate": sum(row["status"] == "pass" for row in eligible) / len(eligible)
        if eligible
        else None,
        "first_call_tool_correct": sum(row["first_call_tool_accuracy"] == 1 for row in eligible),
        "first_call_arguments_correct": sum(
            row["first_call_argument_accuracy"] == 1 for row in eligible
        ),
        "duplicate_calls": sum(row["duplicate_call_count"] for row in eligible),
        "actual_attempts": sum(row["attempt_count"] for row in rows),
        "asr": {
            "available": len(asr_available),
            "missing": len(rows) - len(asr_available),
            "exact": sum(row["asr_evaluation"]["exact_match"] for row in asr_available),
            "exact_rate": sum(
                row["asr_evaluation"]["exact_match"] for row in asr_available
            )
            / len(asr_available)
            if asr_available
            else None,
            "micro_cer": round(micro_cer, 6) if micro_cer is not None else None,
            "character_accuracy": round(1 - micro_cer, 6)
            if micro_cer is not None
            else None,
            "mismatch_but_pass": sum(
                row["status"] == "pass" and not row["asr_evaluation"]["exact_match"]
                for row in asr_available
            ),
            "fail_with_exact_asr": sum(
                row["asr_evaluation"]["failure_attribution"] == "asr_unlikely"
                for row in rows
            ),
            "fail_with_asr_mismatch": sum(
                row["asr_evaluation"]["failure_attribution"] == "asr_possible"
                for row in rows
            ),
            "fail_without_asr": sum(
                row["asr_evaluation"]["failure_attribution"] == "missing_asr_suspect"
                for row in rows
            ),
        },
        "failure_categories": dict(
            sorted(collections.Counter(row["failure_category"] for row in rows).items())
        ),
        "latency": latency_summary(rows),
    }


def build_comparison(
    *,
    conversion_manifest: Path,
    provider_reports: dict[str, Path],
    max_line: int,
    text_normalizer: Path,
) -> tuple[dict, list[dict], list[dict]]:
    conversion = _read(conversion_manifest)
    case_path = conversion_manifest.parent / "cases.json"
    if file_hash(case_path) != conversion["files"]["cases.json"]:
        raise ValueError("converted readable cases differ from manifest")
    source = [case for case in _read(case_path) if case["source_line_number"] <= max_line]
    coverage = {
        provider: min(max_line, PROVIDER_MAX_LINE.get(provider, max_line))
        for provider in provider_reports
    }
    provider_rows = {
        provider: _provider_rows_from_reports(
            report_paths=(report,),
            max_line=coverage[provider],
        )
        for provider, report in provider_reports.items()
    }
    valid_lines = {case["source_line_number"] for case in source if case["status"] == "valid_tool"}
    for provider, (rows, _) in provider_rows.items():
        expected_lines = {line for line in valid_lines if line <= coverage[provider]}
        if set(rows) != expected_lines:
            raise ValueError(f"{provider} report does not cover its declared valid-tool scope")

    cases = []
    for source_case in source:
        line = source_case["source_line_number"]
        row = {
            "source_line_number": line,
            "case_id": source_case["case_id"],
            "user_text": source_case["user_text"],
            "status": source_case["status"],
            "expected_call": source_case["expected_call"],
            "validation_error": source_case["validation_error"],
            "providers": None,
        }
        if source_case["status"] == "valid_tool":
            expected = source_case["expected_call"]
            details = {}
            for provider, (rows, _) in provider_rows.items():
                if line not in rows:
                    details[provider] = None
                    continue
                if rows[line]["function"] != expected["name"]:
                    raise ValueError(f"{provider} function differs from converted source")
                details[provider] = _attempt_detail(rows[line], expected)
            row["providers"] = details
        cases.append(row)

    normalization_inputs = []
    for case in cases:
        if case["status"] != "valid_tool":
            continue
        normalization_inputs.append(case["user_text"])
        for detail in case["providers"].values():
            if detail is not None:
                normalization_inputs.extend(detail["asr_text"])
    normalized_texts = _normalize_with_script(normalization_inputs, text_normalizer)
    for case in cases:
        if case["status"] != "valid_tool":
            continue
        for detail in case["providers"].values():
            if detail is not None:
                detail["asr_evaluation"] = _asr_evaluation(
                    case["user_text"],
                    detail["asr_text"],
                    detail["status"],
                    normalized_texts,
                )

    grouped = collections.defaultdict(list)
    for case in cases:
        if case["status"] == "valid_tool":
            grouped[case["expected_call"]["name"]].append(case)
    functions = []
    for name, rows in sorted(grouped.items()):
        item = {"function": name, "cases": len(rows), "providers": {}}
        for provider in PROVIDERS:
            provider_rows_for_function = [
                row["providers"][provider]
                for row in rows
                if row["providers"][provider] is not None
            ]
            item["providers"][provider] = {
                "pass": sum(row["status"] == "pass" for row in provider_rows_for_function),
                "fail": sum(row["status"] == "fail" for row in provider_rows_for_function),
                "invalid": sum(
                    row["status"] == "invalid" for row in provider_rows_for_function
                ),
            }
        functions.append(item)

    summaries = {provider: _provider_summary(cases, provider) for provider in PROVIDERS}
    matrix = collections.Counter()
    for case in cases:
        if case["status"] == "valid_tool":
            matrix[
                "__".join(
                    case["providers"][provider]["status"]
                    if case["providers"][provider] is not None
                    else "not_run"
                    for provider in PROVIDERS
                )
            ] += 1
    references = {
        provider: [{"path": str(report), "sha256": file_hash(report)}]
        for provider, report in provider_reports.items()
    }
    summary = {
        "schema_version": "0.1",
        "kind": "cockpit_five_model_comparison",
        "scope": {
            "source_lines": [1, max_line],
            "providers": list(PROVIDERS),
            "provider_source_lines": {
                provider: [1, coverage[provider]] for provider in PROVIDERS
            },
            "topology": "one expected tool schema exposed per case",
            "asr_text_normalizer": {
                "path": str(text_normalizer),
                "sha256": file_hash(text_normalizer),
            },
        },
        "source_counts": {
            "total": len(source),
            **{
                status: sum(case["status"] == status for case in source)
                for status in ("valid_tool", "invalid_tool", "no_tool")
            },
        },
        "providers": summaries,
        "status_matrix": dict(sorted(matrix.items())),
        "references": {
            "conversion_manifest": {
                "path": str(conversion_manifest),
                "sha256": file_hash(conversion_manifest),
            },
            "provider_reports": references,
        },
    }
    return summary, functions, cases


def _write_html(output: Path, summary: dict, functions: list[dict], cases: list[dict]) -> None:
    payload = json.dumps(
        {"summary": summary, "functions": functions, "cases": cases},
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    template = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>智能座舱五模型跑测对比</title>
<style>
:root{color-scheme:light;--ink:#17202a;--muted:#64717d;--line:#d8dee4;--soft:#f4f6f8;--green:#147a51;--red:#b42318;--amber:#9a6700;--blue:#1769aa}*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:#fff}header{border-bottom:1px solid var(--line);padding:24px max(20px,calc((100vw - 1680px)/2)) 18px}h1{font-size:26px;margin:0 0 6px}h2{font-size:18px;margin:26px 0 10px}p{margin:6px 0;color:var(--muted)}main{max-width:1680px;margin:auto;padding:0 20px 48px}.pass{color:var(--green)}.fail{color:var(--red)}.invalid{color:var(--amber)}.toolbar{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}input,select,button{font:inherit;border:1px solid var(--line);background:#fff;padding:7px 9px;border-radius:4px}input{min-width:280px;flex:1}button{cursor:pointer}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;vertical-align:top;border-bottom:1px solid var(--line);padding:8px}th{position:sticky;top:0;background:var(--soft);z-index:1}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-word}.pill{display:inline-block;border:1px solid currentColor;border-radius:999px;padding:1px 7px;font-size:12px}.note{border-left:3px solid var(--blue);padding:8px 12px;background:var(--soft);margin:14px 0}.scroll{max-height:650px;overflow:auto;border:1px solid var(--line)}.summary-scroll{max-height:none}.overview{min-width:1050px}.overview th,.overview td{white-space:nowrap}.overview td:first-child{font-weight:600}.overview tbody tr:last-child td{border-bottom:0}details{max-width:680px}summary{cursor:pointer;color:var(--blue)}.not-run{color:var(--muted)}@media(max-width:700px){table{min-width:1800px}.overview{min-width:1050px}}
</style></head><body><header><h1>智能座舱五模型跑测对比</h1><p>Qwen Audio 3.0 Flash / Plus · Qwen Audio 3.1 Plus · Seed Duplex 3.0 · StepAudio 3 Realtime</p></header><main>
<div class="note">Qwen 与 Seed 覆盖源行 1–3000，StepAudio 当前只覆盖源行 1–200。每条 case 只暴露标签工具。Pass 要求严格参数匹配和成功工具序列；Fail 包含参数不一致、未调用与响应超时；Invalid 是基础设施样本。参考文本和 ASR 文本先通过指定 norm.py 统一阿拉伯数字等格式；ASR字准为 1−微平均 CER。“逐字完全一致率”更严格。ASR 归因只把“ASR 不一致且最终 Fail”标记为疑似相关，不代表已经证明因果。Function Call 延时起点为用户音频结束，TTS 延时终点为收到首个有效 PCM 帧。</div>
<h2>总体结果</h2><div class="scroll summary-scroll"><table class="overview"><thead><tr><th>模型</th><th>总数</th><th>Pass</th><th>Fail</th><th>Invalid</th><th>通过率</th><th>ASR字准</th><th>Function Call P50</th><th>TTS 首帧 P50</th></tr></thead><tbody id="overview"></tbody></table></div>
<h2>函数级对比</h2><div class="scroll"><table><thead><tr id="function-head"><th>函数</th><th>Case</th></tr></thead><tbody id="functions"></tbody></table></div>
<h2>逐 Case</h2><div class="toolbar"><input id="search" placeholder="搜索源行、文本、函数、ASR 或参数"><select id="provider"><option value="all">五模型</option><option value="flash">3.0 Flash</option><option value="plus">3.0 Plus</option><option value="plus31">3.1 Plus</option><option value="seed">Seed</option><option value="step">StepAudio</option></select><select id="status"><option value="nonpass">默认：Fail + Invalid</option><option value="all">全部</option><option value="pass">Pass</option><option value="fail">Fail</option><option value="invalid">Invalid</option></select><select id="asr"><option value="all">ASR：全部</option><option value="suspected">疑似 ASR 相关 Fail</option><option value="mismatch">ASR 不一致</option><option value="exact">ASR 一致</option><option value="missing">缺少 ASR</option></select><button id="reset">重置</button></div><p id="shown"></p><div class="scroll"><table><thead><tr id="case-head"><th>源行</th><th>输入 / 标签</th></tr></thead><tbody id="cases"></tbody></table></div>
</main><script id="report-data" type="application/json">PAYLOAD</script><script>
const data=JSON.parse(document.getElementById('report-data').textContent),providers=['flash','plus','plus31','seed','step'],names={flash:'Qwen 3.0 Flash',plus:'Qwen 3.0 Plus',plus31:'Qwen 3.1 Plus',seed:'Seed Duplex 3.0',step:'StepAudio 3'};const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const fmt=n=>typeof n==='number'?n.toLocaleString():n;const pct=n=>n==null?'—':(n*100).toFixed(1)+'%';const fc=x=>x.latency.speech_end_to_final_tool_call,tts=x=>x.latency.speech_end_to_first_tts_frame;
document.getElementById('overview').innerHTML=providers.map(k=>{const x=data.summary.providers[k];return `<tr><td>${names[k]}</td><td>${fmt(x.cases)}</td><td class="pass">${fmt(x.pass)}</td><td class="fail">${fmt(x.fail)}</td><td class="invalid">${fmt(x.invalid)}</td><td><strong>${pct(x.eligible_pass_rate)}</strong></td><td><strong>${pct(x.asr.character_accuracy)}</strong></td><td>${fmt(Math.round(fc(x).p50_ms))} ms</td><td>${fmt(Math.round(tts(x).p50_ms))} ms</td></tr>`}).join('');
document.getElementById('function-head').innerHTML+providers.map(k=>`<th>${names[k]} P/F/I</th>`).join('');document.getElementById('functions').innerHTML=data.functions.map(x=>`<tr><td><code>${esc(x.function)}</code></td><td>${x.cases}</td>${providers.map(k=>`<td><span class="pass">${x.providers[k].pass}</span> / <span class="fail">${x.providers[k].fail}</span> / <span class="invalid">${x.providers[k].invalid}</span></td>`).join('')}</tr>`).join('');
const search=document.getElementById('search'),provider=document.getElementById('provider'),status=document.getElementById('status'),asr=document.getElementById('asr');document.getElementById('case-head').innerHTML+=''.concat(providers.map(k=>`<th>${names[k]}</th>`).join(''),'<th>工件</th>');function badge(x){return `<span class="pill ${x.status}">${esc(x.status)}</span> <small>${esc(x.failure_category)}</small>`}function asrLabel(a){if(!a.available)return '缺少 ASR';const accuracy=Math.max(0,1-a.cer);if(a.failure_attribution==='asr_possible')return `疑似相关 · ASR字准 ${pct(accuracy)}`;return `${a.exact_match?'一致':'不一致'} · ASR字准 ${pct(accuracy)}`}function detail(x){if(!x)return '<span class="not-run">未测试</span>';const fc=x.timing.speech_end_to_final_tool_call_ms,tts=x.timing.speech_end_to_first_tts_frame_ms;return `${badge(x)}<br><b>ASR</b> ${esc(x.asr_text.join(' / ')||'—')}<br><small>${esc(asrLabel(x.asr_evaluation))}</small><br><b>调用</b> <code>${esc(JSON.stringify(x.calls))}</code><br><b>延时</b> Function Call ${fc==null?'—':fmt(Math.round(fc))+' ms'} · TTS 首帧 ${tts==null?'—':fmt(Math.round(tts))+' ms'}<br><b>回复</b> ${esc(x.assistant_text.join(' / ')||'—')}`}function render(){const q=search.value.trim().toLowerCase(),pk=provider.value,sk=status.value,ak=asr.value;const rows=data.cases.filter(x=>x.status==='valid_tool').filter(x=>{const selected=pk==='all'?providers:[pk];const ps=selected.map(k=>x.providers[k]).filter(Boolean);const statusOk=ps.length>0&&(sk==='all'||(sk==='nonpass'?ps.some(p=>p.status!=='pass'):ps.some(p=>p.status===sk)));const asrOk=ak==='all'||(ak==='suspected'?ps.some(p=>['asr_possible','missing_asr_suspect'].includes(p.asr_evaluation.failure_attribution)):ak==='mismatch'?ps.some(p=>p.asr_evaluation.available&&!p.asr_evaluation.exact_match):ak==='exact'?ps.some(p=>p.asr_evaluation.exact_match):ps.some(p=>!p.asr_evaluation.available));return statusOk&&asrOk&&(!q||JSON.stringify(x).toLowerCase().includes(q))});document.getElementById('shown').textContent=`显示 ${rows.length} / ${data.summary.source_counts.valid_tool} 条有效工具 case`;document.getElementById('cases').innerHTML=rows.map(x=>`<tr><td>${x.source_line_number}<br><code>${esc(x.expected_call.name)}</code></td><td>${esc(x.user_text)}<br><code>${esc(JSON.stringify(x.expected_call.param))}</code></td>${providers.map(k=>`<td>${detail(x.providers[k])}</td>`).join('')}<td><details><summary>路径</summary><code>${providers.map(k=>names[k]+': '+(x.providers[k]?esc(x.providers[k].artifact_path):'未测试')).join('\n')}</code></details></td></tr>`).join('')}[search,provider,status,asr].forEach(x=>x.addEventListener('input',render));document.getElementById('reset').onclick=()=>{search.value='';provider.value='all';status.value='nonpass';asr.value='all';render()};render();
</script></body></html>'''
    (output / "index.html").write_text(template.replace("PAYLOAD", payload), encoding="utf-8")


def _write_readme(output: Path, summary: dict) -> None:
    lines = [
        f"# 智能座舱五模型跑测对比（源行 1–{summary['scope']['source_lines'][1]}）",
        "",
        "打开 `index.html` 查看五模型总览、函数级统计、逐 case 状态和延时。",
        "",
        "StepAudio 当前仅覆盖源行 1–200；其余模型覆盖源行 1–3000。",
        "ASR 对比前使用 `~/tools/ezmt-patslot-train_latest/norm.py` 统一数字等文本格式。",
        "",
        "| 模型 | 总数 | Eligible | Pass | Fail | Invalid | Eligible 通过率 | ASR字准 | ASR 逐字一致率 | 疑似 ASR Fail | Function Call P50 | TTS 首帧 P50 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for provider in PROVIDERS:
        item = summary["providers"][provider]
        lines.append(
            f"| {DISPLAY_NAMES[provider]} | {item['cases']} | {item['eligible']} | {item['pass']} | "
            f"{item['fail']} | {item['invalid']} | {item['eligible_pass_rate']:.1%} | "
            f"{item['asr']['character_accuracy']:.1%} | {item['asr']['exact_rate']:.1%} | "
            f"{item['asr']['fail_with_asr_mismatch']} | "
            f"{item['latency']['speech_end_to_final_tool_call']['p50_ms']} ms | "
            f"{item['latency']['speech_end_to_first_tts_frame']['p50_ms']} ms |"
        )
    lines.extend(
        [
            "",
            f"源数据共 {summary['source_counts']['total']} 行，其中 {summary['source_counts']['valid_tool']} 条进入工具 Task Completion。",
            "",
            "- `summary.json`：五模型总体指标、延时分位数和状态矩阵。",
            "- `functions.json`：逐函数的五模型 Pass/Fail/Invalid。",
            "- `cases.json`：逐源行的标签、ASR、实际调用、回复、延时和归档路径。",
            "- `asr_failures.json`：ASR 不一致或缺失且最终 Fail 的疑似相关样本。",
            "- `sources/`：五个模型各一份可核验的正式结果归档。",
        ]
    )
    (output / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversion-manifest", type=Path, required=True)
    parser.add_argument("--flash-report", type=Path, required=True)
    parser.add_argument("--plus-report", type=Path, required=True)
    parser.add_argument("--seed-report", type=Path, required=True)
    parser.add_argument("--plus31-report", type=Path, required=True)
    parser.add_argument("--step-report", type=Path, required=True)
    parser.add_argument(
        "--text-normalizer",
        type=Path,
        default=Path.home() / "tools/ezmt-patslot-train_latest/norm.py",
    )
    parser.add_argument("--max-line", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.max_line < 1:
        parser.error("max-line must be positive")
    summary, functions, cases = build_comparison(
        conversion_manifest=args.conversion_manifest,
        provider_reports={
            "flash": args.flash_report,
            "plus": args.plus_report,
            "seed": args.seed_report,
            "plus31": args.plus31_report,
            "step": args.step_report,
        },
        max_line=args.max_line,
        text_normalizer=args.text_normalizer.resolve(),
    )
    allowed = {
        "README.md",
        "index.html",
        "summary.json",
        "functions.json",
        "cases.json",
        "asr_failures.json",
        "sources",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    if any(path.name not in allowed for path in args.output.iterdir()):
        raise ValueError("output directory contains files not owned by this report")
    (args.output / "summary.json").write_text(pretty_json(summary), encoding="utf-8")
    (args.output / "functions.json").write_text(pretty_json(functions), encoding="utf-8")
    (args.output / "cases.json").write_text(pretty_json(cases), encoding="utf-8")
    asr_failures = [
        {
            "source_line_number": case["source_line_number"],
            "user_text": case["user_text"],
            "expected_call": case["expected_call"],
            "providers": {
                provider: {
                    "status": detail["status"],
                    "failure_category": detail["failure_category"],
                    "asr_evaluation": detail["asr_evaluation"],
                    "calls": detail["calls"],
                    "artifact_path": detail["artifact_path"],
                }
                for provider, detail in case["providers"].items()
                if detail is not None
                and detail["asr_evaluation"]["failure_attribution"]
                in {"asr_possible", "missing_asr_suspect"}
            },
        }
        for case in cases
        if case["status"] == "valid_tool"
        and any(
            detail is not None
            and detail["asr_evaluation"]["failure_attribution"]
            in {"asr_possible", "missing_asr_suspect"}
            for detail in case["providers"].values()
        )
    ]
    (args.output / "asr_failures.json").write_text(
        pretty_json(asr_failures), encoding="utf-8"
    )
    _write_readme(args.output, summary)
    _write_html(args.output, summary, functions, cases)
    print(json.dumps({"output": str(args.output), "providers": summary["providers"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

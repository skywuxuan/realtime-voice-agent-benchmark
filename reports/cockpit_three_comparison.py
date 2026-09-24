"""Build a three-provider cockpit comparison from sealed shard reports."""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from agent.artifacts import latency_summary
from benchmark.contracts import pretty_json
from events.replay import file_hash
from reports.cockpit_comparison import _attempt_detail, _provider_rows, _read

PROVIDERS = ("flash", "plus", "seed")
DISPLAY_NAMES = {
    "flash": "Qwen Audio 3.0 Realtime Flash",
    "plus": "Qwen Audio 3.0 Realtime Plus",
    "seed": "Seed Duplex 3.0",
}


def _provider_summary(cases: list[dict], provider: str) -> dict:
    rows = [case["providers"][provider] for case in cases if case["status"] == "valid_tool"]
    eligible = [row for row in rows if row["eligible"]]
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
        "failure_categories": dict(
            sorted(collections.Counter(row["failure_category"] for row in rows).items())
        ),
        "latency": latency_summary(rows),
    }


def build_comparison(
    *,
    conversion_manifest: Path,
    provider_progress: dict[str, Path],
    prefix_reports: dict[str, tuple[Path, ...]],
    max_line: int,
) -> tuple[dict, list[dict], list[dict]]:
    conversion = _read(conversion_manifest)
    case_path = conversion_manifest.parent / "cases.json"
    if file_hash(case_path) != conversion["files"]["cases.json"]:
        raise ValueError("converted readable cases differ from manifest")
    source = [case for case in _read(case_path) if case["source_line_number"] <= max_line]
    provider_rows = {
        provider: _provider_rows(
            progress_path=progress,
            prefix_reports=prefix_reports.get(provider, ()),
            max_line=max_line,
        )
        for provider, progress in provider_progress.items()
    }
    valid_lines = {case["source_line_number"] for case in source if case["status"] == "valid_tool"}
    for provider, (rows, _) in provider_rows.items():
        if set(rows) != valid_lines:
            raise ValueError(f"{provider} report does not cover the same valid-tool source lines")

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
                if rows[line]["function"] != expected["name"]:
                    raise ValueError(f"{provider} function differs from converted source")
                details[provider] = _attempt_detail(rows[line], expected)
            row["providers"] = details
        cases.append(row)

    grouped = collections.defaultdict(list)
    for case in cases:
        if case["status"] == "valid_tool":
            grouped[case["expected_call"]["name"]].append(case)
    functions = []
    for name, rows in sorted(grouped.items()):
        item = {"function": name, "cases": len(rows), "providers": {}}
        for provider in PROVIDERS:
            provider_rows_for_function = [row["providers"][provider] for row in rows]
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
            matrix["__".join(case["providers"][provider]["status"] for provider in PROVIDERS)] += 1
    references = {}
    for provider, progress in provider_progress.items():
        references[provider] = [
            {"path": str(path), "sha256": file_hash(path)}
            for path in prefix_reports.get(provider, ())
        ]
        progress_data = _read(progress)
        references[provider].extend(
            {"path": str(shard["report"]), "sha256": file_hash(Path(shard["report"]))}
            for shard in progress_data["shards"]
        )
    summary = {
        "schema_version": "0.1",
        "kind": "cockpit_three_provider_comparison",
        "scope": {
            "source_lines": [1, max_line],
            "providers": list(PROVIDERS),
            "topology": "one expected tool schema exposed per case",
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
<title>智能座舱三模型跑测对比</title>
<style>
:root{color-scheme:light;--ink:#17202a;--muted:#64717d;--line:#d8dee4;--soft:#f4f6f8;--green:#147a51;--red:#b42318;--amber:#9a6700;--blue:#1769aa}*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:#fff}header{border-bottom:1px solid var(--line);padding:24px max(20px,calc((100vw - 1500px)/2)) 18px}h1{font-size:26px;margin:0 0 6px}h2{font-size:18px;margin:26px 0 10px}p{margin:6px 0;color:var(--muted)}main{max-width:1500px;margin:auto;padding:0 20px 48px}.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border-bottom:1px solid var(--line)}.provider{padding:18px 20px 18px 0}.provider+.provider{border-left:1px solid var(--line);padding-left:20px}.numbers{display:grid;grid-template-columns:repeat(4,minmax(70px,1fr));gap:12px;margin-top:12px}.metric strong{display:block;font-size:22px}.metric span{color:var(--muted)}.pass{color:var(--green)}.fail{color:var(--red)}.invalid{color:var(--amber)}.toolbar{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}input,select,button{font:inherit;border:1px solid var(--line);background:#fff;padding:7px 9px;border-radius:4px}input{min-width:280px;flex:1}button{cursor:pointer}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;vertical-align:top;border-bottom:1px solid var(--line);padding:8px}th{position:sticky;top:0;background:var(--soft);z-index:1}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-word}.pill{display:inline-block;border:1px solid currentColor;border-radius:999px;padding:1px 7px;font-size:12px}.note{border-left:3px solid var(--blue);padding:8px 12px;background:var(--soft);margin:14px 0}.scroll{max-height:650px;overflow:auto;border:1px solid var(--line)}details{max-width:680px}summary{cursor:pointer;color:var(--blue)}@media(max-width:900px){.stats{grid-template-columns:1fr}.provider+.provider{border-left:0;border-top:1px solid var(--line);padding-left:0}.numbers{grid-template-columns:repeat(2,1fr)}table{min-width:1200px}}
</style></head><body><header><h1>智能座舱三模型跑测对比</h1><p>Qwen Audio 3.0 Realtime Flash · Qwen Audio 3.0 Realtime Plus · Seed Duplex 3.0 · 同一源文本与冻结音频</p></header><main>
<div class="note">每条 case 只暴露标签工具。Pass 要求严格参数匹配和成功工具序列；Fail 包含参数不一致、未调用与响应超时；Invalid 是基础设施样本。Function Call 延时起点为用户音频结束，TTS 延时终点为收到首个有效 PCM 帧。</div>
<section class="stats" id="stats"></section>
<h2>函数级对比</h2><div class="scroll"><table><thead><tr id="function-head"><th>函数</th><th>Case</th></tr></thead><tbody id="functions"></tbody></table></div>
<h2>逐 Case</h2><div class="toolbar"><input id="search" placeholder="搜索源行、文本、函数、ASR 或参数"><select id="provider"><option value="all">三模型</option><option value="flash">Flash</option><option value="plus">Plus</option><option value="seed">Seed</option></select><select id="status"><option value="nonpass">默认：Fail + Invalid</option><option value="all">全部</option><option value="pass">Pass</option><option value="fail">Fail</option><option value="invalid">Invalid</option></select><button id="reset">重置</button></div><p id="shown"></p><div class="scroll"><table><thead><tr id="case-head"><th>源行</th><th>输入 / 标签</th></tr></thead><tbody id="cases"></tbody></table></div>
</main><script id="report-data" type="application/json">PAYLOAD</script><script>
const data=JSON.parse(document.getElementById('report-data').textContent),providers=['flash','plus','seed'],names={flash:'Qwen Flash',plus:'Qwen Plus',seed:'Seed Duplex 3.0'};const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const fmt=n=>typeof n==='number'?n.toLocaleString():n;const pct=n=>n==null?'—':(n*100).toFixed(1)+'%';const fc=x=>x.latency.speech_end_to_final_tool_call,tts=x=>x.latency.speech_end_to_first_tts_frame;
document.getElementById('stats').innerHTML=providers.map(k=>{const x=data.summary.providers[k];return `<div class="provider"><h2>${names[k]}</h2><div class="numbers"><div class="metric"><strong class="pass">${fmt(x.pass)}</strong><span>Pass</span></div><div class="metric"><strong class="fail">${fmt(x.fail)}</strong><span>Fail</span></div><div class="metric"><strong class="invalid">${fmt(x.invalid)}</strong><span>Invalid</span></div><div class="metric"><strong>${pct(x.eligible_pass_rate)}</strong><span>Eligible 通过率</span></div></div><p>Function Call P50 ${fmt(fc(x).p50_ms)} ms · P95 ${fmt(fc(x).p95_ms)} ms</p><p>TTS 首帧 P50 ${fmt(tts(x).p50_ms)} ms · P95 ${fmt(tts(x).p95_ms)} ms</p><p>Eligible ${fmt(x.eligible)} · attempts ${fmt(x.actual_attempts)}</p></div>`}).join('');
document.getElementById('function-head').innerHTML+providers.map(k=>`<th>${names[k]} P/F/I</th>`).join('');document.getElementById('functions').innerHTML=data.functions.map(x=>`<tr><td><code>${esc(x.function)}</code></td><td>${x.cases}</td>${providers.map(k=>`<td><span class="pass">${x.providers[k].pass}</span> / <span class="fail">${x.providers[k].fail}</span> / <span class="invalid">${x.providers[k].invalid}</span></td>`).join('')}</tr>`).join('');
const search=document.getElementById('search'),provider=document.getElementById('provider'),status=document.getElementById('status');document.getElementById('case-head').innerHTML+=''.concat(providers.map(k=>`<th>${names[k]}</th>`).join(''),'<th>工件</th>');function badge(x){return `<span class="pill ${x.status}">${esc(x.status)}</span> <small>${esc(x.failure_category)}</small>`}function detail(x){const fc=x.timing.speech_end_to_final_tool_call_ms,tts=x.timing.speech_end_to_first_tts_frame_ms;return `${badge(x)}<br><b>ASR</b> ${esc(x.asr_text.join(' / ')||'—')}<br><b>调用</b> <code>${esc(JSON.stringify(x.calls))}</code><br><b>延时</b> Function Call ${fc==null?'—':fmt(Math.round(fc))+' ms'} · TTS 首帧 ${tts==null?'—':fmt(Math.round(tts))+' ms'}<br><b>回复</b> ${esc(x.assistant_text.join(' / ')||'—')}`}function render(){const q=search.value.trim().toLowerCase(),pk=provider.value,sk=status.value;const rows=data.cases.filter(x=>x.status==='valid_tool').filter(x=>{const selected=pk==='all'?providers:[pk];const ps=selected.map(k=>x.providers[k]);const ok=sk==='all'||(sk==='nonpass'?ps.some(p=>p.status!=='pass'):ps.some(p=>p.status===sk));return ok&&(!q||JSON.stringify(x).toLowerCase().includes(q))});document.getElementById('shown').textContent=`显示 ${rows.length} / ${data.summary.source_counts.valid_tool} 条有效工具 case`;document.getElementById('cases').innerHTML=rows.map(x=>`<tr><td>${x.source_line_number}<br><code>${esc(x.expected_call.name)}</code></td><td>${esc(x.user_text)}<br><code>${esc(JSON.stringify(x.expected_call.param))}</code></td>${providers.map(k=>`<td>${detail(x.providers[k])}</td>`).join('')}<td><details><summary>路径</summary><code>${providers.map(k=>names[k]+': '+esc(x.providers[k].artifact_path)).join('\n')}</code></details></td></tr>`).join('')}[search,provider,status].forEach(x=>x.addEventListener('input',render));document.getElementById('reset').onclick=()=>{search.value='';provider.value='all';status.value='nonpass';render()};render();
</script></body></html>'''
    (output / "index.html").write_text(template.replace("PAYLOAD", payload), encoding="utf-8")


def _write_readme(output: Path, summary: dict) -> None:
    lines = [
        f"# 智能座舱三模型跑测对比（源行 1–{summary['scope']['source_lines'][1]}）",
        "",
        "打开 `index.html` 查看三模型总览、函数级统计、逐 case 状态和延时。",
        "",
        "| 模型 | Eligible | Pass | Fail | Invalid | Eligible 通过率 | Function Call P50 | TTS 首帧 P50 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for provider in PROVIDERS:
        item = summary["providers"][provider]
        lines.append(
            f"| {DISPLAY_NAMES[provider]} | {item['eligible']} | {item['pass']} | "
            f"{item['fail']} | {item['invalid']} | {item['eligible_pass_rate']:.1%} | "
            f"{item['latency']['speech_end_to_final_tool_call']['p50_ms']} ms | "
            f"{item['latency']['speech_end_to_first_tts_frame']['p50_ms']} ms |"
        )
    lines.extend(
        [
            "",
            f"源数据共 {summary['source_counts']['total']} 行，其中 {summary['source_counts']['valid_tool']} 条进入工具 Task Completion。",
            "",
            "- `summary.json`：三模型总体指标、延时分位数和状态矩阵。",
            "- `functions.json`：逐函数的三模型 Pass/Fail/Invalid。",
            "- `cases.json`：逐源行的标签、ASR、实际调用、回复、延时和归档路径。",
        ]
    )
    (output / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversion-manifest", type=Path, required=True)
    parser.add_argument("--flash-progress", type=Path, required=True)
    parser.add_argument("--plus-progress", type=Path, required=True)
    parser.add_argument("--seed-progress", type=Path, required=True)
    parser.add_argument("--flash-prefix-report", type=Path, action="append", default=[])
    parser.add_argument("--max-line", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.max_line < 1:
        parser.error("max-line must be positive")
    summary, functions, cases = build_comparison(
        conversion_manifest=args.conversion_manifest,
        provider_progress={
            "flash": args.flash_progress,
            "plus": args.plus_progress,
            "seed": args.seed_progress,
        },
        prefix_reports={"flash": tuple(args.flash_prefix_report)},
        max_line=args.max_line,
    )
    allowed = {"README.md", "index.html", "summary.json", "functions.json", "cases.json"}
    args.output.mkdir(parents=True, exist_ok=True)
    if any(path.name not in allowed for path in args.output.iterdir()):
        raise ValueError("output directory contains files not owned by this report")
    (args.output / "summary.json").write_text(pretty_json(summary), encoding="utf-8")
    (args.output / "functions.json").write_text(pretty_json(functions), encoding="utf-8")
    (args.output / "cases.json").write_text(pretty_json(cases), encoding="utf-8")
    _write_readme(args.output, summary)
    _write_html(args.output, summary, functions, cases)
    print(json.dumps({"output": str(args.output), "providers": summary["providers"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

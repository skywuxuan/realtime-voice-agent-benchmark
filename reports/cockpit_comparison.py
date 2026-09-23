"""Build one readable Qwen/Seed comparison entry point from sealed cockpit shards."""

import argparse
import collections
import json
from pathlib import Path

from benchmark.contracts import pretty_json
from events.replay import file_hash


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _report_paths(progress_path: Path, *, prefix_reports: tuple[Path, ...]) -> tuple[Path, ...]:
    progress = _read(progress_path)
    return prefix_reports + tuple(Path(shard["report"]) for shard in progress["shards"])


def _provider_rows(
    *, progress_path: Path, prefix_reports: tuple[Path, ...], max_line: int
) -> tuple[dict[int, dict], list[dict]]:
    rows = {}
    references = []
    for report_path in _report_paths(progress_path, prefix_reports=prefix_reports):
        report = _read(report_path)
        references.append(
            {
                "path": str(report_path),
                "sha256": file_hash(report_path),
                "case_count_in_scope": sum(
                    case["source_line_number"] <= max_line for case in report["cases"]
                ),
            }
        )
        for case in report["cases"]:
            line = case["source_line_number"]
            if line > max_line:
                continue
            if line in rows:
                raise ValueError(f"provider reports contain duplicate source line {line}")
            rows[line] = case
    return rows, references


def _argument_diff(expected: dict, actual: dict) -> dict:
    return {
        "missing_keys": sorted(expected.keys() - actual.keys()),
        "extra_keys": sorted(actual.keys() - expected.keys()),
        "wrong_values": {
            key: {"expected": expected[key], "actual": actual[key]}
            for key in sorted(expected.keys() & actual.keys())
            if expected[key] != actual[key]
        },
    }


def _attempt_detail(case: dict, expected: dict) -> dict:
    attempt = case["selected_attempt"]
    root = Path(attempt["run"]) / attempt["artifact_path"]
    scenario = _read(root / "scenario.json")
    if scenario["world"]["source_line_number"] != case["source_line_number"]:
        raise ValueError("scenario source line differs from shard report")
    if scenario["expected_calls"] != [
        {"tool": expected["name"], "arguments": expected["param"]}
    ]:
        raise ValueError("sealed scenario label differs from converted source")
    transcript = _read(root / "transcript.json")
    records = _read(root / "tool_calls.json")
    calls = [
        {
            "tool": record["tool"],
            "arguments": record["arguments"],
            "status": record["status"],
            "error": record["error"],
        }
        for record in records
    ]
    first = calls[0] if calls else None
    if attempt["status"] == "pass":
        category = "pass"
    elif not attempt["eligible"]:
        category = "infrastructure_invalid"
    elif attempt["reason"] == "agent_response_timeout":
        category = "response_timeout"
    elif first is None:
        category = "no_tool_call"
    elif first["tool"] != expected["name"]:
        category = "wrong_tool"
    elif first["arguments"] != expected["param"]:
        category = "argument_mismatch"
    elif first["status"] == "error":
        category = "tool_execution_error"
    else:
        category = "other_failure"
    return {
        "status": attempt["status"],
        "eligible": attempt["eligible"],
        "task_completion": attempt["task_completion"],
        "failure_category": category,
        "execution_reason": attempt["reason"],
        "attempt_count": len(case["attempts"]),
        "first_call_tool_accuracy": attempt["first_call_tool_accuracy"],
        "first_call_argument_accuracy": attempt["first_call_argument_accuracy"],
        "duplicate_call_count": attempt["duplicate_call_count"],
        "asr_text": [
            event["payload"]["text"]
            for event in transcript
            if event["event"] == "user_text_done"
        ],
        "assistant_text": [
            event["payload"]["text"]
            for event in transcript
            if event["event"] == "assistant_text_done"
        ],
        "calls": calls,
        "first_call_argument_diff": _argument_diff(
            expected["param"], first["arguments"]
        )
        if first and first["tool"] == expected["name"]
        else None,
        "artifact_path": str(root),
        "evaluation_id": attempt["evaluation_id"],
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
    }


def build_comparison(
    *,
    conversion_manifest: Path,
    qwen_progress: Path,
    seed_progress: Path,
    qwen_prefix_reports: tuple[Path, ...],
    max_line: int,
) -> tuple[dict, list[dict], list[dict]]:
    conversion = _read(conversion_manifest)
    case_path = conversion_manifest.parent / "cases.json"
    if file_hash(case_path) != conversion["files"]["cases.json"]:
        raise ValueError("converted readable cases differ from manifest")
    source = [case for case in _read(case_path) if case["source_line_number"] <= max_line]
    qwen, qwen_refs = _provider_rows(
        progress_path=qwen_progress,
        prefix_reports=qwen_prefix_reports,
        max_line=max_line,
    )
    seed, seed_refs = _provider_rows(
        progress_path=seed_progress,
        prefix_reports=(),
        max_line=max_line,
    )
    valid_lines = {case["source_line_number"] for case in source if case["status"] == "valid_tool"}
    if set(qwen) != valid_lines or set(seed) != valid_lines:
        raise ValueError("provider reports do not cover the same valid-tool source lines")
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
            if qwen[line]["function"] != source_case["expected_call"]["name"] or seed[line][
                "function"
            ] != source_case["expected_call"]["name"]:
                raise ValueError("provider function differs from converted source")
            row["providers"] = {
                "qwen": _attempt_detail(qwen[line], source_case["expected_call"]),
                "seed": _attempt_detail(seed[line], source_case["expected_call"]),
            }
        cases.append(row)
    functions = []
    grouped = collections.defaultdict(list)
    for case in cases:
        if case["status"] == "valid_tool":
            grouped[case["expected_call"]["name"]].append(case)
    for name, rows in sorted(grouped.items()):
        item = {"function": name, "cases": len(rows)}
        for provider in ("qwen", "seed"):
            provider_rows = [row["providers"][provider] for row in rows]
            item[provider] = {
                "pass": sum(row["status"] == "pass" for row in provider_rows),
                "fail": sum(row["status"] == "fail" for row in provider_rows),
                "invalid": sum(row["status"] == "invalid" for row in provider_rows),
            }
        item["pass_delta_qwen_minus_seed"] = item["qwen"]["pass"] - item["seed"]["pass"]
        functions.append(item)
    qwen_summary = _provider_summary(cases, "qwen")
    seed_summary = _provider_summary(cases, "seed")
    matrix = collections.Counter()
    for case in cases:
        if case["status"] == "valid_tool":
            matrix[
                f'{case["providers"]["qwen"]["status"]}__{case["providers"]["seed"]["status"]}'
            ] += 1
    summary = {
        "schema_version": "0.1",
        "kind": "cockpit_provider_comparison",
        "scope": {
            "source_lines": [1, max_line],
            "topology": "one expected tool schema exposed per case",
            "interpretation": [
                "Pass requires exact expected arguments and one successful tool sequence.",
                "Tool-name accuracy does not measure selection among 100 functions.",
                "Schema-invalid labels and no-tool rows are excluded from task completion.",
                "Original sealed runs and shard reports are referenced, not rewritten.",
            ],
        },
        "source_counts": {
            "total": len(source),
            **{
                status: sum(case["status"] == status for case in source)
                for status in ("valid_tool", "invalid_tool", "no_tool")
            },
        },
        "providers": {"qwen": qwen_summary, "seed": seed_summary},
        "status_matrix": dict(sorted(matrix.items())),
        "references": {
            "conversion_manifest": {
                "path": str(conversion_manifest),
                "sha256": file_hash(conversion_manifest),
            },
            "qwen_reports": qwen_refs,
            "seed_reports": seed_refs,
        },
    }
    return summary, functions, cases


def _write_readme(output: Path, summary: dict) -> None:
    qwen, seed = summary["providers"]["qwen"], summary["providers"]["seed"]
    text = f"""# 智能座舱 1–{summary['scope']['source_lines'][1]} 跑测总览

优先打开 `index.html`。它提供总览、函数对比和逐 case 筛选。

## 快速结论

| 模型 | Eligible | Pass | Fail | Invalid | Eligible 通过率 | 重复调用 |
|---|---:|---:|---:|---:|---:|---:|
| Qwen Audio 3.0 Realtime Flash | {qwen['eligible']} | {qwen['pass']} | {qwen['fail']} | {qwen['invalid']} | {qwen['eligible_pass_rate']:.1%} | {qwen['duplicate_calls']} |
| Seed Duplex 3.0 | {seed['eligible']} | {seed['pass']} | {seed['fail']} | {seed['invalid']} | {seed['eligible_pass_rate']:.1%} | {seed['duplicate_calls']} |

源数据共 {summary['source_counts']['total']} 行，其中 {summary['source_counts']['valid_tool']} 条进入工具 Task Completion，{summary['source_counts']['invalid_tool']} 条标签与 schema 冲突，{summary['source_counts']['no_tool']} 条为 no-tool。

## 文件

- `summary.json`：总体指标、交叉矩阵和原始报告引用。
- `functions.json`：逐函数的 pass/fail/invalid。
- `cases.json`：逐源行的标签、ASR、实际调用、回复和工件路径。

本报告每个 case 只暴露标签对应的一个工具 schema，不代表模型能在100个工具中完成选择。
"""
    (output / "README.md").write_text(text, encoding="utf-8")


def _write_html(output: Path, summary: dict, functions: list[dict], cases: list[dict]) -> None:
    payload = json.dumps(
        {"summary": summary, "functions": functions, "cases": cases},
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    template = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>智能座舱 1–MAX_LINE 跑测总览</title>
<style>
:root{color-scheme:light;--ink:#17202a;--muted:#64717d;--line:#d8dee4;--soft:#f4f6f8;--green:#147a51;--red:#b42318;--amber:#9a6700;--blue:#1769aa}*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:#fff;letter-spacing:0}header{border-bottom:1px solid var(--line);padding:24px max(20px,calc((100vw - 1280px)/2)) 18px}h1{font-size:26px;margin:0 0 6px}h2{font-size:18px;margin:26px 0 10px}p{margin:6px 0;color:var(--muted)}main{max-width:1280px;margin:auto;padding:0 20px 48px}.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));border-bottom:1px solid var(--line)}.provider{padding:18px 0}.provider:first-child{border-right:1px solid var(--line);padding-right:24px}.provider:last-child{padding-left:24px}.numbers{display:grid;grid-template-columns:repeat(4,minmax(80px,1fr));gap:12px;margin-top:12px}.metric strong{display:block;font-size:22px}.metric span{color:var(--muted)}.pass{color:var(--green)}.fail{color:var(--red)}.invalid{color:var(--amber)}.toolbar{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}input,select,button{font:inherit;border:1px solid var(--line);background:#fff;padding:7px 9px;border-radius:4px}input{min-width:280px;flex:1}button{cursor:pointer}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;vertical-align:top;border-bottom:1px solid var(--line);padding:8px}th{position:sticky;top:0;background:var(--soft);z-index:1}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-word}.pill{display:inline-block;border:1px solid currentColor;border-radius:999px;padding:1px 7px;font-size:12px}.note{border-left:3px solid var(--blue);padding:8px 12px;background:var(--soft);margin:14px 0}.scroll{max-height:650px;overflow:auto;border:1px solid var(--line)}details{max-width:680px}summary{cursor:pointer;color:var(--blue)}@media(max-width:760px){.stats{grid-template-columns:1fr}.provider:first-child{border-right:0;border-bottom:1px solid var(--line);padding-right:0}.provider:last-child{padding-left:0}.numbers{grid-template-columns:repeat(2,1fr)}table{min-width:980px}}
</style></head><body><header><h1>智能座舱 1–MAX_LINE 跑测总览</h1><p>Qwen Audio 3.0 Realtime Flash 对比 Seed Duplex 3.0 · 同一源文本与冻结音频</p></header><main>
<div class="note">每条 case 只暴露标签工具，函数名指标不是100选1准确率。Fail 包含严格参数不一致、未调用与响应超时；Invalid 是基础设施样本。</div>
<section class="stats" id="stats"></section>
<h2>函数级对比</h2><div class="scroll"><table><thead><tr><th>函数</th><th>Case</th><th>Qwen P/F/I</th><th>Seed P/F/I</th><th>Pass 差值</th></tr></thead><tbody id="functions"></tbody></table></div>
<h2>逐 Case</h2><div class="toolbar"><input id="search" placeholder="搜索源行、文本、函数、ASR 或参数"><select id="provider"><option value="both">两模型</option><option value="qwen">Qwen</option><option value="seed">Seed</option></select><select id="status"><option value="nonpass">默认：Fail + Invalid</option><option value="all">全部</option><option value="pass">Pass</option><option value="fail">Fail</option><option value="invalid">Invalid</option></select><button id="reset">重置</button></div><p id="shown"></p><div class="scroll"><table><thead><tr><th>源行</th><th>输入 / 标签</th><th>Qwen</th><th>Seed</th><th>工件</th></tr></thead><tbody id="cases"></tbody></table></div>
</main><script id="report-data" type="application/json">PAYLOAD</script><script>
const data=JSON.parse(document.getElementById('report-data').textContent);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const fmt=n=>typeof n==='number'?n.toLocaleString():n;const pct=n=>n==null?'—':(n*100).toFixed(1)+'%';
document.getElementById('stats').innerHTML=['qwen','seed'].map(k=>{const x=data.summary.providers[k],name=k==='qwen'?'Qwen Audio 3.0 Realtime Flash':'Seed Duplex 3.0';return `<div class="provider"><h2>${name}</h2><div class="numbers"><div class="metric"><strong class="pass">${fmt(x.pass)}</strong><span>Pass</span></div><div class="metric"><strong class="fail">${fmt(x.fail)}</strong><span>Fail</span></div><div class="metric"><strong class="invalid">${fmt(x.invalid)}</strong><span>Invalid</span></div><div class="metric"><strong>${pct(x.eligible_pass_rate)}</strong><span>Eligible 通过率</span></div></div><p>Eligible ${fmt(x.eligible)} · 实际 attempts ${fmt(x.actual_attempts)} · 重复调用 ${fmt(x.duplicate_calls)}</p></div>`}).join('');
document.getElementById('functions').innerHTML=data.functions.map(x=>`<tr><td><code>${esc(x.function)}</code></td><td>${x.cases}</td><td><span class="pass">${x.qwen.pass}</span> / <span class="fail">${x.qwen.fail}</span> / <span class="invalid">${x.qwen.invalid}</span></td><td><span class="pass">${x.seed.pass}</span> / <span class="fail">${x.seed.fail}</span> / <span class="invalid">${x.seed.invalid}</span></td><td>${x.pass_delta_qwen_minus_seed>0?'+':''}${x.pass_delta_qwen_minus_seed}</td></tr>`).join('');
const search=document.getElementById('search'),provider=document.getElementById('provider'),status=document.getElementById('status');function badge(x){return `<span class="pill ${x.status}">${esc(x.status)}</span> <small>${esc(x.failure_category)}</small>`}function detail(x){return `${badge(x)}<br><b>ASR</b> ${esc(x.asr_text.join(' / ')||'—')}<br><b>调用</b> <code>${esc(JSON.stringify(x.calls))}</code><br><b>回复</b> ${esc(x.assistant_text.join(' / ')||'—')}`}function render(){const q=search.value.trim().toLowerCase(),pk=provider.value,sk=status.value;const rows=data.cases.filter(x=>x.status==='valid_tool').filter(x=>{const ps=pk==='both'?[x.providers.qwen,x.providers.seed]:[x.providers[pk]];const ok=sk==='all'||(sk==='nonpass'?ps.some(p=>p.status!=='pass'):ps.some(p=>p.status===sk));return ok&&(!q||JSON.stringify(x).toLowerCase().includes(q))});document.getElementById('shown').textContent=`显示 ${rows.length} / ${data.summary.source_counts.valid_tool} 条有效工具 case`;document.getElementById('cases').innerHTML=rows.map(x=>`<tr><td>${x.source_line_number}<br><code>${esc(x.expected_call.name)}</code></td><td>${esc(x.user_text)}<br><code>${esc(JSON.stringify(x.expected_call.param))}</code></td><td>${detail(x.providers.qwen)}</td><td>${detail(x.providers.seed)}</td><td><details><summary>路径</summary><code>Qwen: ${esc(x.providers.qwen.artifact_path)}\nSeed: ${esc(x.providers.seed.artifact_path)}</code></details></td></tr>`).join('')}[search,provider,status].forEach(x=>x.addEventListener('input',render));document.getElementById('reset').onclick=()=>{search.value='';provider.value='both';status.value='nonpass';render()};render();
</script></body></html>"""
    (output / "index.html").write_text(
        template.replace("MAX_LINE", str(summary["scope"]["source_lines"][1])).replace(
            "PAYLOAD", payload
        ),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversion-manifest", type=Path, required=True)
    parser.add_argument("--qwen-progress", type=Path, required=True)
    parser.add_argument("--seed-progress", type=Path, required=True)
    parser.add_argument("--qwen-prefix-report", type=Path, action="append", default=[])
    parser.add_argument("--max-line", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.max_line < 1:
        parser.error("max-line must be positive")
    summary, functions, cases = build_comparison(
        conversion_manifest=args.conversion_manifest,
        qwen_progress=args.qwen_progress,
        seed_progress=args.seed_progress,
        qwen_prefix_reports=tuple(args.qwen_prefix_report),
        max_line=args.max_line,
    )
    allowed = {"README.md", "index.html", "summary.json", "functions.json", "cases.json"}
    if args.output.exists() and any(path.name not in allowed for path in args.output.iterdir()):
        raise ValueError("output directory contains files not owned by this report")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(pretty_json(summary), encoding="utf-8")
    (args.output / "functions.json").write_text(pretty_json(functions), encoding="utf-8")
    (args.output / "cases.json").write_text(pretty_json(cases), encoding="utf-8")
    _write_readme(args.output, summary)
    _write_html(args.output, summary, functions, cases)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "source_counts": summary["source_counts"],
                "providers": summary["providers"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

"""Portable HTML summaries with separate dimensions and local artifact links."""

import argparse
import html
import json
from pathlib import Path, PurePosixPath
from urllib.parse import quote

LABELS = {
    "attempted": "尝试数",
    "eligible": "有效样本",
    "pass": "通过",
    "fail": "失败",
    "invalid": "无效",
    "unknown": "未知",
    "warmup_count": "预热",
    "task_completion": "任务完成",
    "task_completion_rate": "任务完成率",
    "tool_selection_accuracy": "工具选择准确率",
    "argument_accuracy": "参数准确率",
    "tool_call_sequence_accuracy": "调用序列准确率",
    "dependency_accuracy": "结果依赖准确率",
    "correction_handling_accuracy": "改口处理准确率",
    "failure_recovery_rate": "故障恢复率",
    "hallucinated_action": "虚构执行率",
    "false_interruption_rate": "误打断率",
    "response_continuation_rate": "继续响应率",
    "interruption_detection_rate": "中断检测率",
    "case_success_rate": "用例成功率",
    "premature_response_rate": "提前响应率",
    "turn_completion_accuracy": "回合完成准确率",
}


def esc(value):
    return html.escape(str(value), quote=True)


def value(value):
    if value is None:
        return "未判定"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, (dict, list)):
        return esc(json.dumps(value, ensure_ascii=False))
    return esc(value)


def table(mapping):
    return (
        '<table class="metrics">'
        + "".join(
            f"<tr><th>{esc(LABELS.get(k, k))}</th><td>{value(v)}</td></tr>"
            for k, v in mapping.items()
        )
        + "</table>"
    )


def links(row):
    directory = row.get("artifact_path")
    if not directory:
        return ""
    path = PurePosixPath(directory)
    if path.is_absolute() or ".." in path.parts or "\\" in directory or ":" in directory:
        return ""
    names = {
        "input.wav": "输入音频",
        "output.wav": "播放音频",
        "transcript.json": "转写",
        "events.jsonl": "事件",
    }
    if row.get("records") is not None:
        names.update({"tool_calls.json": "工具调用", "state.json": "最终状态"})
    return (
        "<nav>"
        + "".join(
            f'<a href="{esc(quote((path / name).as_posix()))}">{title}</a> '
            for name, title in names.items()
        )
        + "</nav>"
    )


def render(metrics: dict, *, title: str = "Realtime Voice Agent Benchmark") -> str:
    sections = []
    dimensions = [
        ("realtime", "Realtime Metrics"),
        ("agent", "Agent Metrics"),
        ("response_quality", "Response Quality"),
    ]
    if not any(key in metrics for key, _ in dimensions):
        dimensions = [("root", "Agent Metrics" if "task_completion" in metrics else "Metrics")]
    for key, label in dimensions:
        data = metrics if key == "root" else metrics.get(key, {"status": "not_run"})
        if data.get("status") == "not_run":
            sections.append(f'<section><h2>{label}</h2><p class="muted">未运行</p></section>')
            continue
        blocks = [f"<h2>{label}</h2>", "<h3>Counts</h3>", table(data.get("counts", {}))]
        summary = {k: v for k, v in data.items() if k in LABELS and k != "counts"}
        summary.update(
            {
                k: v
                for k, v in data.get("metrics", {}).items()
                if k in LABELS or k.endswith("known_count")
            }
        )
        if summary:
            blocks.append(table(summary))
        for index, group in enumerate(data.get("groups", []), 1):
            blocks.append(f"<details><summary>实验组 {index}</summary>")
            blocks.append(
                table({k: v for k, v in group.items() if k not in {"cases", "dimensions"}})
            )
            blocks.append(
                "<pre>"
                + esc(json.dumps(group.get("dimensions", {}), ensure_ascii=False, indent=2))
                + "</pre></details>"
            )
        cases = data.get("cases", [data] if "scenario_id" in data else [])
        if cases:
            blocks.append(
                '<table class="cases"><thead><tr><th>Scenario</th><th>结果</th><th>有效</th><th>任务完成</th><th>原因 / 工件</th></tr></thead><tbody>'
            )
            for case in cases:
                status = case.get("status", "unknown")
                css = status if status in {"pass", "fail", "invalid", "unknown"} else "unknown"
                reasons = case.get("reasons") or [
                    case.get("execution_reason") or case.get("reason", "")
                ]
                blocks.append(
                    f"<tr><td>{esc(case.get('scenario_id', ''))}<br><small>{esc(case.get('attempt_id', ''))}</small></td>"
                    f'<td><span class="{css}">{esc(status)}</span></td><td>{value(case.get("eligible"))}</td>'
                    f"<td>{value(case.get('task_completion'))}</td><td>{esc('; '.join(map(str, reasons)))}{links(case)}</td></tr>"
                )
            blocks.append("</tbody></table>")
        sections.append("<section>" + "".join(blocks) + "</section>")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;color:#172637;background:#f3f6fa;margin:0}}main{{max-width:1200px;margin:auto;padding:28px}}h1{{font-size:28px}}h2{{font-size:22px}}section{{background:white;padding:24px;margin:20px 0;border:1px solid #dce3eb;border-radius:10px;overflow:auto}}table{{border-collapse:collapse;width:100%;margin:12px 0}}td,th{{border-bottom:1px solid #dce3eb;padding:10px;text-align:left;vertical-align:top}}th{{font-weight:600}}small,.muted{{color:#66788a}}.metrics th{{width:45%}}a{{color:#12639e;margin-right:12px}}.pass{{color:#137449}}.fail{{color:#ba3341}}.invalid,.unknown{{color:#88641e}}summary{{cursor:pointer;padding:10px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf2f6;padding:16px}}nav{{margin-top:8px;font-size:14px}}</style></head><body><main><h1>{esc(title)}</h1>
<p class="muted">Run {esc(metrics.get("run_id", ""))}<br>Evaluation {esc(metrics.get("evaluation_id", ""))}</p>
{"".join(sections)}<details><summary>Metrics JSON</summary><pre>{esc(json.dumps(metrics, ensure_ascii=False, indent=2))}</pre></details></main></body></html>"""


def write(metrics_path: Path, output: Path) -> Path:
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(metrics), encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(write(args.metrics, args.output))


if __name__ == "__main__":
    main()

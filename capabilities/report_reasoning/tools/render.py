"""Optional v8-style view of an analyst-written report, outside the reasoning procedure."""

import argparse
import json
import re
from pathlib import Path

from capabilities.report_reasoning.internal.storage import digest, read, write

# This replaces only the signal presentation in the trusted, finalized repository template.
VARIABLE_VIEW = """def signals(rows, groups):
 if not rows:return
 heading(5,'变量综合判断')
 labels={'UP':'上升','DOWN':'下降','STABLE':'平稳','MIXED':'分化','UNKNOWN':'未明确'}
 def variable_label(g):
  split=sum(x['variable_id']==g['variable_id'] for x in groups)>1
  return g['variable_name']+('｜'+g['scope']+'｜'+g['timeframe'] if split else '')
 table(['变量','综合方向','综合判断'],[
  (variable_label(g),labels[g['direction']],g['synthesis']) for g in groups])
 lookup={x['signal_id']:x for x in rows}
 for g in groups:
  H.append('<details class="sources"><summary>'+esc(g['variable_name'])+'：查看综合依据与原始信号</summary>')
  p('适用范围：'+g['scope']+'；时间：'+g['timeframe'])
  p('冲突处理：'+g['conflict_resolution'])
  categories=[('support_signal_ids','支持证据'),('counter_signal_ids','反向或冲突证据'),
   ('excluded_signal_ids','不适用证据及原因')]
  for key,label in categories:
   if g[key]:
    heading(5,label)
    table(['变量','原始方向','来源记载'],[(lookup[i]['variable_name'],
     labels[lookup[i]['source_direction']],lookup[i]['signal']) for i in g[key]])
    for i in g[key]:
     x=lookup[i];p(x['qualification']+'｜Signal '+i+'｜Event '+ '、'.join(x['event_ids']))
  evidence(g['evidence_ids'])
  H.append('</details>')
"""

UPSTREAM_NAMES = """def all_objects(value):
 if isinstance(value,dict):
  yield value
  for v in value.values():yield from all_objects(v)
 elif isinstance(value,list):
  for v in value:yield from all_objects(v)
judgment_names={x['local_key']:x.get('name',x.get('title',x['source_id']))
 for x in all_objects(r) if 'judgment_origin' in x}
"""


def display_text(value: object) -> str:
    labels = {"up": "上升", "down": "下降", "stable": "平稳", "mixed": "分化", "unknown": "未明确", "unknow": "未明确"}
    return re.sub(
        r"(?<![A-Za-z0-9_-])(up|down|stable|mixed|unknown|unknow)(?![A-Za-z0-9_-])",
        lambda match: labels[match.group().lower()],
        str(value),
        flags=re.IGNORECASE,
    )


def render(report: Path, evidence_catalog: Path, output: Path) -> dict[str, str]:
    data = read(report)
    if data.get("schema_version") != "report-publication/v6-draft":
        raise ValueError("variable synthesis renderer requires v6-draft")
    output.mkdir(parents=True, exist_ok=False)
    write(output / "report.json", data)
    write(output / "evidence-catalog.json", read(evidence_catalog))
    template = Path(__file__).resolve().parents[3] / "report/v8/scripts/render_v8.py.txt"
    source = template.read_text()
    source = source.replace("html.escape(str(x))", "html.escape(display_text(x))")
    source = source.replace(
        "rows.append((t['name'],OR[t['judgment_origin']]",
        "rows.append((t['name'],{'macroeconomic_story':'宏观经济','industry_chain':'产业链',"
        "'industry_chain_node':'产业链节点'}[a['target_type']],OR[t['judgment_origin']]",
    )
    source = source.replace(
        "['受影响锚点','判断来源','方向','结论']",
        "['受影响锚点','锚点类型','判断来源','方向','结论']",
    )
    for name, level, label in (("m", 4, "宏观经济"), ("c", 3, "产业链"), ("n", 4, "产业链节点"), ("co", 4, "公司")):
        heading = f"heading({level},{name}['name'],{name}['local_key']);"
        source = source.replace(heading, heading + f"p('锚点类型：{label}','muted');")
    start, end = source.index("def signals(rows):"), source.index("def evidence(ids):")
    source = source[:start] + VARIABLE_VIEW + source[end:]
    source = source.replace("def heading(level,text,key=None):", UPSTREAM_NAMES + "def heading(level,text,key=None):")
    for name in ("co", "c", "n", "m"):
        source = source.replace(
            f"signals({name}['variable_signals'])",
            f"signals({name}['variable_signals'],{name}['variable_assessments'])",
        )
    source = source.replace(
        "signals(u['detail']['variable_signals'])",
        "signals(u['detail']['variable_signals'],u['detail']['variable_assessments'])",
    )
    start = source.index("  if refs:heading(5,'公司传导依据')")
    end = source.index("\n", start)
    source = (
        source[:start]
        + (
            "  if refs:heading(5,'传导依据');table(['上游锚点','传导逻辑'],"
            "[(judgment_names[x['local_key']],x['mechanism']+'；'+x.get('condition','')) for x in refs])"
        )
        + source[end:]
    )
    source = source.replace("三层投研报告 v8", "三层投研报告 · 变量综合版")
    source = source.replace(
        "直接：本实体有本层输入范围内可采用的变量信号。",
        "直接：对本实体自身信号作综合判断，包括错挂、失效等情况下的未明确判断。",
    )
    source = source.replace("固定输入重审 · 详情变量信号 · 直接与推理判断", "变量综合判断 → 基本面影响 → 关联锚点传导")
    source = source.replace(
        "沿用 v7 的 168 个 Event、77 条 Signal 和冻结实体图；未刷新来源、未发布。来源记载不等于已独立核实。",
        "2026 年 9 月 7 日全量冻结数据回放；三路独立推理。原始信号作为支持、冲突或不适用证据折叠展示。未发布。",
    )
    source = source.replace(
        "以下公司有可采用的信号，但冻结图中的业务挂接不足以支持可靠的产业链传导。"
        "保留公司自身判断，不虚构产业链或 Concept。它们属于产业链层的公司输入，不另设第四个报告层级。",
        "公司变量先综合，再判断基本面影响。错挂、失效和口径异常保留原文并明确限定；公司属于产业分析范围。",
    )
    # Execute only repository-owned template code. Report text is read as JSON and HTML-escaped by the template.
    exec(
        compile(source, str(template), "exec"),
        {"__file__": str(output / "scripts/render_v8.py"), "display_text": display_text},
    )
    markdown = output / "report.md"
    markdown.write_text(display_text(markdown.read_text()))
    result = {"html": str((output / "report.html").resolve()), "source_report_hash": digest(data)}
    write(output / "render-metadata.json", {**result, "template": str(template), "data_changed": False})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--evidence-catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(render(args.report, args.evidence_catalog, args.output), ensure_ascii=False))

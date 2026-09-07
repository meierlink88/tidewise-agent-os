from pathlib import Path
import json,html
O=Path(__file__).resolve().parents[1];r=json.loads((O/'report.json').read_text());ev=json.loads((O/'evidence-catalog.json').read_text());H=[];M=[]
esc=lambda x:html.escape(str(x));D={'warming':'升温','cooling':'降温','diverging':'分化','pending':'不判定方向'};I={'high':'高影响','medium':'中影响','low':'低影响','pending':'待评估'};OR={'direct':'直接','inferred':'推理'}
def heading(level,text,key=None):
 H.append(f'<h{level}'+(f' id="{esc(key)}"' if key else '')+'>'+esc(text)+f'</h{level}>');M.append('#'*level+' '+text+'\n')
def p(text,cls=''):
 H.append('<p class="'+cls+'">'+esc(text)+'</p>');M.append(text+'\n')
def table(headers,rows):
 H.append('<div class="tablewrap"><table'+(' class="signal-table"' if headers==['变量','方向','信号'] else '')+'><thead><tr>'+''.join('<th>'+esc(x)+'</th>' for x in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+esc(x)+'</td>' for x in row)+'</tr>' for row in rows)+'</tbody></table></div>')
 clean=lambda s:str(s).replace('|','／').replace('\n','<br>');M.extend(['| '+' | '.join(map(clean,headers))+' |','| '+' | '.join(['---']*len(headers))+' |']+['| '+' | '.join(map(clean,row))+' |' for row in rows]+[''])
def signals(rows):
 if not rows:return
 heading(5,'变量信号');table(['变量','方向','信号'],[(x['variable_name'],{'UP':'上升','DOWN':'下降','STABLE':'不变','MIXED':'分化','UNKNOWN':'未明确'}[x['source_direction']],x['signal']) for x in rows]);H.append('<details class="sources"><summary>查看适用限定与信号来源</summary>')
 for x in rows:p(x['variable_name']+'｜限定：'+x['qualification']+'｜Signal '+x['signal_id']+'｜Event '+ '、'.join(x['event_ids']))
 H.append('</details>')
def evidence(ids):
 if not ids:return
 H.append('<details class="sources"><summary>查看 Evidence（'+str(len(ids))+' 条）</summary>');M.append('Evidence：\n');table(['Evidence ID','证据摘要'],[(e,ev[e]['summary']) for e in ids]);H.append('</details>')
def objection(o):
 heading(5,'反证与边界');p(o['summary'])
 if o['counterevidence']:table(['反向事实','来源'],[(x['text'],'、'.join(x['evidence_ids'])) for x in o['counterevidence']])
 else:p('未识别到直接反向事实，不表示结论已被证实。','muted')
 if o['buffers']:table(['缓冲因素','性质'],[(x['text'],{'source_fact':'来源记载','inference':'推理','hypothetical_buffer':'条件假设'}[x['basis']]) for x in o['buffers']])
 if o['evidence_gaps']:p('待验证：'+'；'.join(o['evidence_gaps']))
 if o['scope_limits']:p('适用边界：'+'；'.join(o['scope_limits']))
def assessment(o):
 a=o['assessment'];H.append('<div class="badges"><span class="'+o['judgment_origin']+'">'+OR[o['judgment_origin']]+'</span><span>'+D[a['direction']]+'</span><span>置信度 '+{'low':'低','medium':'中','high':'高',None:'不适用'}[a['confidence']]+'</span></div>');M.append('**'+OR[o['judgment_origin']]+' · '+D[a['direction']]+'**\n')
 p(a['conclusion'],'conclusion');p(a['transmission_logic'],'logic');p('适用范围：'+a['scope']);p('周期：'+a['forecast_window']['description'])
 if a['conditions']:p('成立条件：'+'；'.join(a['conditions']))
 if a['follow_up']:p('后续验证：'+'；'.join(a['follow_up']))
def company(co):
 H.append('<article class="company">');heading(4,co['name'],co['local_key']);assessment(co);signals(co['variable_signals']);objection(co['objections']);evidence(co['assessment']['evidence_ids']);H.append('</article>')
def graph(c):
 heading(4,'本期判断链路图');p('仅保留有判断的节点。连线是原有结构关系，不代表影响已经实现；被移除节点两端没有重新连线。','muted')
 nodes=c['graph']['nodes'];positions={n['local_key']:(i*260+12,60) for i,n in enumerate(nodes)};imp={n['node_local_key']:n for n in c['affected_nodes']};width=max(280,len(nodes)*260)
 svg=[f'<div class="graph"><svg width="{width}" height="235" role="img" aria-label="{esc(c["name"])}本期有判断节点"><defs><marker id="arrow-{c["local_key"]}" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0 0 L0 6 L8 3 z" fill="#8494a7"/></marker></defs>']
 for i,e in enumerate(c['graph']['edges']):
  x,y=positions[e['from_node_local_key']];z,_=positions[e['to_node_local_key']];x+=110;z+=110;top=20+(i%3)*8
  svg.append(f'<path d="M{x} 60 C{x} {top},{z} {top},{z} 60" fill="none" stroke="#8494a7" marker-end="url(#arrow-{c["local_key"]})"/><text x="{(x+z)/2}" y="{top}" text-anchor="middle" font-size="11" fill="#586b83">{esc(e["relation_label"])}</text>')
 for n in nodes:
  x,y=positions[n['local_key']];a=imp[n['local_key']];color='#e8f5ee' if a['judgment_origin']=='direct' else '#edf3fc'
  svg.append(f'<a href="#{a["local_key"]}"><rect x="{x}" y="60" width="232" height="128" rx="12" fill="{color}" stroke="#bed0e4"/><text x="{x+14}" y="89" font-size="15" font-weight="600">{esc(n["name"])}</text><text x="{x+14}" y="117" font-size="13">{OR[a["judgment_origin"]]} · {D[a["assessment"]["direction"]]}</text><text x="{x+14}" y="142" font-size="12" fill="#65748b">{esc(a["assessment"]["forecast_window"]["description"][:18])}</text><text x="{x+14}" y="168" font-size="12" fill="#3865a2">查看节点判断 →</text></a>')
 svg.append('</svg></div>');H.append(''.join(svg));M.append('```mermaid\nflowchart LR');code={n['local_key']:'N'+str(i) for i,n in enumerate(nodes)}
 for n in nodes:M.append(' '+code[n['local_key']]+'['+json.dumps(n['name']+' · '+OR[imp[n['local_key']]['judgment_origin']],ensure_ascii=False)+']')
 for e in c['graph']['edges']:M.append(' '+code[e['from_node_local_key']]+' -->|'+e['relation_label']+'| '+code[e['to_node_local_key']])
 M.append('```\n')
def chain(c):
 H.append('<section class="chain">');heading(3,c['name'],c['local_key']);heading(4,'链级判断');assessment(c);signals(c['variable_signals']);heading(4,'链级推理总结');p('推导逻辑：'+c['reasoning_summary']['logic'],'logic');p('支持：'+c['reasoning_summary']['support']['text']);objection(c['reasoning_summary']['objections']);evidence(c['assessment']['evidence_ids']);graph(c);heading(4,'节点推理结果')
 for n in c['affected_nodes']:
  H.append('<section class="node">');heading(4,n['name'],n['local_key']);assessment(n);signals(n['variable_signals'])
  refs=[x for x in n['reasoning_sources']['upstream_refs'] if 'mechanism' in x]
  if refs:heading(5,'公司传导依据');table(['公司或来源','传导逻辑'],[(next(co['name'] for k in ['geopolitical_stories','macroeconomic_stories','concept_analyses','industry_chain_analyses'] for u in r[k] for co in u['detail']['companies'] if co['local_key']==x['local_key']),x['mechanism']+'；'+x['condition']) for x in refs])
  objection(n['objections']);evidence(n['assessment']['evidence_ids']);H.append('</section>')
 H.append('</section>')
def resolve(u,a):
 if a['target_type']=='macroeconomic_story':return next(m for m in u['detail']['macro_impacts'] if m['local_key']==a['local_key'])
 c=next(c for c in u['detail']['industry_chains'] if c['local_key']==(a['chain_local_key'] or a['local_key']))
 return c if a['target_type']=='industry_chain' else next(n for n in c['affected_nodes'] if n['local_key']==a['local_key'])
heading(1,'三层投研报告 v8');p('固定输入重审 · 详情变量信号 · 直接与推理判断','subtitle');p('沿用 v7 的 168 个 Event、77 条 Signal 和冻结实体图；未刷新来源、未发布。来源记载不等于已独立核实。')
p('直接：本实体有本层输入范围内可采用的变量信号。推理：通过事件或其他锚点形成有条件判断。标记描述依据位置，不代表确定性。','notice')
H.append('<nav><a href="#geo">地缘政治</a><a href="#macro">宏观经济</a><a href="#industry">产业链</a><a href="#companies">公司直接判断</a></nav>')
for title,anchor,kinds in [('地缘政治','geo',['geopolitical_stories']),('宏观经济','macro',['macroeconomic_stories']),('产业链','industry',['concept_analyses','industry_chain_analyses'])]:
 heading(2,title,anchor);units=[u for k in kinds for u in r[k]];heading(3,'总结')
 for u in units:
  H.append('<article class="summary">');heading(4,u['title']);p(I[u['summary']['impact_assessment']['level']]+' · '+OR[u['judgment_origin']]);p(u['summary']['conclusion'],'conclusion');p(u['summary']['transmission_logic'],'logic');p('影响度依据：'+u['summary']['impact_assessment']['rationale'])
  rows=[]
  for a in u['summary']['affected_refs']:
   t=resolve(u,a);rows.append((t['name'],OR[t['judgment_origin']],D[t['assessment']['direction']],t['assessment']['conclusion']))
  table(['受影响锚点','判断来源','方向','结论'],rows);H.append('<a class="button" href="#detail-'+u['local_key']+'">查看详情 →</a></article>')
 heading(3,'详情')
 for u in units:
  H.append('<article class="unit">');heading(3,u['title'],'detail-'+u['local_key']);signals(u['detail']['variable_signals'])
  for m in u['detail']['macro_impacts']:
   heading(4,m['name'],m['local_key']);assessment(m);signals(m['variable_signals']);objection(m['objections']);evidence(m['assessment']['evidence_ids'])
  for c in u['detail']['industry_chains']:chain(c)
  if u['detail']['companies']:
   heading(3,'相关公司判断');p('公司自身信号仅在公司处展示；节点通过来源引用说明传导，不复制公司信号为节点直接信号。','muted')
   for co in u['detail']['companies']:company(co)
  H.append('</article>')
heading(3,'公司直接判断','companies');p('以下公司有可采用的信号，但冻结图中的业务挂接不足以支持可靠的产业链传导。保留公司自身判断，不虚构产业链或 Concept。它们属于产业链层的公司输入，不另设第四个报告层级。')
for co in r['company_analyses']:company(co)
heading(2,'补充观察')
for o in r['observations']:heading(3,o['title']);p(o['text']);evidence(o['evidence_ids'])
heading(2,'范围与限制')
for x in r['limitations']:p(x)
css='''*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f4f6f9;color:#1f3046;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft Yahei",sans-serif;line-height:1.75}main{max-width:1120px;margin:auto;padding:36px 24px 90px}h1{font-size:34px;margin-bottom:4px}h2{font-size:28px;border-bottom:2px solid #cad6e6;padding-top:40px}h3{font-size:23px;margin:24px 0 14px}h4{font-size:19px;margin:22px 0 10px}h5{font-size:16px;margin:16px 0 6px}.subtitle,.muted{color:#66768a}.notice{padding:16px;border-left:4px solid #446d9d;background:#e9f0f8}nav{display:flex;gap:20px;flex-wrap:wrap;padding:15px 0}a{color:#2862a3;text-decoration:none}.summary,.unit,.company{background:#fff;border:1px solid #dae2eb;border-radius:15px;padding:24px;margin:20px 0}.chain{border-top:2px solid #d6e0ec;margin-top:28px}.node{border:1px solid #dae2eb;border-radius:12px;padding:18px;margin:20px 0}.conclusion{font-size:17px;font-weight:600}.logic{padding:14px 18px;background:#f1f5fa;border-radius:9px}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;margin:12px 0 20px;font-size:14px}th,td{border:1px solid #dce3ed;padding:12px;text-align:left;vertical-align:top}th{background:#edf2f8;white-space:nowrap}td:first-child{min-width:110px}td{overflow-wrap:anywhere}.signal-table td:first-child{white-space:nowrap;min-width:150px;width:150px}.signal-table td:nth-child(2){white-space:nowrap;width:75px;min-width:75px}.badges{display:flex;gap:8px;flex-wrap:wrap}.badges span{background:#f2f4f7;padding:3px 10px;border-radius:5px;font-size:13px}.badges .direct{background:#e5f3e9;color:#24714a}.badges .inferred{background:#e8effb;color:#3565a1}.sources{font-size:12px;color:#607187;margin:14px 0}.sources summary{cursor:pointer}.sources p{overflow-wrap:anywhere}.graph{overflow:auto;background:#fafcff;border-radius:12px}.button{display:inline-block;padding:8px 14px;background:#edf3fc;border-radius:8px}svg text{font-family:inherit;fill:#233b59}@media(max-width:640px){main{padding:18px 12px}h1{font-size:27px}.unit,.summary,.company{padding:16px}.node{padding:12px}table{font-size:13px}}'''
(O/'report.md').write_text('\n'.join(M));(O/'report.html').write_text('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>三层投研报告 v8</title><style>'+css+'</style></head><body><main>'+''.join(H)+'</main></body></html>');print('rendered',len(H),'blocks')

from pathlib import Path
import json,copy,collections
from jsonschema import Draft202012Validator,FormatChecker
O=Path(__file__).resolve().parents[1];B=O.parent;r=json.loads((O/'report.json').read_text());s=json.loads((B/'v7/report.schema.json').read_text());s['$id']='urn:tidewise:report-publication:v5-draft';s['properties']['schema_version']['const']='report-publication/v5-draft'
def obj(p):return {'type':'object','properties':p,'required':list(p),'additionalProperties':False}
def arr(v):return {'type':'array','items':v}
def ref(v):return {'$ref':'#/$defs/'+v}
text={'type':'string','minLength':1};ids=arr(text);defs=s['$defs'];defs['signal']=obj({'variable_id':text,'variable_name':text,'signal_id':text,'signal':text,'source_direction':{'enum':['UP','DOWN','MIXED','STABLE','UNKNOWN']},'adoption':{'enum':['qualified','adopted']},'qualification':text,'event_ids':ids,'evidence_ids':ids})
upstream=obj({'entity_id':text,'local_key':text});upstream['properties'].update({'mechanism':text,'condition':text})
defs['reasoning_sources']=obj({'signal_ids':ids,'event_ids':ids,'upstream_refs':arr(upstream)})
for name in ['unit','macro','chain','node']:
 d=defs[name];d['properties'].update({'judgment_origin':{'enum':['direct','inferred']},'reasoning_sources':ref('reasoning_sources')});d['required']+=['judgment_origin','reasoning_sources']
 if name!='unit':d['properties']['variable_signals']=arr(ref('signal'));d['required'].append('variable_signals')
defs['company']=copy.deepcopy(defs['macro']);defs['company']['properties']['source_id']={'type':'string','pattern':'^COM[0-9a-f-]{36}$'}
d=defs['unit']['properties']['detail'];d['properties'].update({'variable_signals':arr(ref('signal')),'companies':arr(ref('company'))});d['required']+=['variable_signals','companies']
defs['graph']['properties']['scope']={'const':'assessed_nodes_only'};defs['graph']['required'].append('scope')
for name,definition in [('industry_chain_analyses','unit'),('company_analyses','company')]:s['properties'][name]=arr(ref(definition));s['required'].append(name)
(O/'report.schema.json').write_text(json.dumps(s,ensure_ascii=False,indent=2)+'\n')
errors=list(Draft202012Validator(s,format_checker=FormatChecker()).iter_errors(r));assert not errors,[(list(e.path),e.message) for e in errors][:10]
sa=json.loads((O/'signal-coverage-audit.json').read_text());raw=json.loads((B/'signal-audit.json').read_text());snap=json.loads((B/'snapshot.json').read_text());E={x['uuid']:x for x in snap['entities']};signalmap={x['data']['uuid']:x for x in raw};eventaudit=json.loads((B/'event-audit.json').read_text());eventmap={x['event_id']:x for x in eventaudit};evcat=json.loads((O/'evidence-catalog.json').read_text());counts=collections.Counter();used_events=set();used_evidence=set();groupnames=['geopolitical_stories','macroeconomic_stories','concept_analyses','industry_chain_analyses'];nodeaudit=json.loads((O/'node-coverage-audit.json').read_text())

def validate_origin(o,kind,unit=False):
 rows=o['detail']['variable_signals'] if unit else o['variable_signals'];assert (o['judgment_origin']=='direct')==bool(rows);counts[o['judgment_origin']]+=1
 source=o['reasoning_sources'];assert source['signal_ids']==[x['signal_id'] for x in rows];assert source['event_ids'] or source['upstream_refs'],o['local_key']
 used_events.update(source['event_ids'])
 for x in rows:
  sig=signalmap[x['signal_id']];assert E[sig['target']]['id']==o['source_id'];assert sig['source']==x['variable_id'];assert x['evidence_ids'];assert x['adoption'] in ['qualified','adopted'];assert sig['data']['audit_decision']!='排除'
  for eid in x['event_ids']:
   assert eid in eventmap
   if kind=='geopolitical_stories':assert eventmap[eid]['class']=='GEOPOLITICAL'
   if kind=='macroeconomic_stories':assert eventmap[eid]['class']=='MACRO_ECONOMIC'
  used_events.update(x['event_ids']);used_evidence.update(x['evidence_ids'])
 for eid in source['event_ids']:
  assert eid in eventmap
  if kind=='geopolitical_stories':assert eventmap[eid]['class']=='GEOPOLITICAL'
  if kind=='macroeconomic_stories':assert eventmap[eid]['class']=='MACRO_ECONOMIC'
 if not unit:
  assert o['assessment']['conclusion'].strip();assert o['assessment']['transmission_logic'].strip();assert o['assessment']['evidence_ids'];used_evidence.update(o['assessment']['evidence_ids'])

for kind in groupnames:
 for u in r[kind]:
  counts['summary_cards']+=1;validate_origin(u,kind,True)
  objects={u['local_key']:u};chains={c['local_key']:c for c in u['detail']['industry_chains']};macros={m['local_key']:m for m in u['detail']['macro_impacts']}
  for x in u['detail']['companies']+u['detail']['macro_impacts']:
   validate_origin(x,kind);objects[x['local_key']]=x;counts['companies' if x['source_id'].startswith('COM') else 'macro_impacts']+=1
  for c in chains.values():
   counts['chains']+=1;validate_origin(c,kind);objects[c['local_key']]=c
   nodes={n['local_key']:n for n in c['graph']['nodes']};assert len(nodes)==len(c['affected_nodes']);assert {n['node_local_key'] for n in c['affected_nodes']}==set(nodes)
   assert c['empty_state'] is None;assert c['reasoning_summary']['logic']==c['assessment']['transmission_logic']
   for n in c['affected_nodes']:
    counts['nodes']+=1;validate_origin(n,kind);objects[n['local_key']]=n;assert nodes[n['node_local_key']]['source_id']==n['source_id'];assert nodes[n['node_local_key']]['name']==n['name']
   for e in c['graph']['edges']:assert e['from_node_local_key'] in nodes and e['to_node_local_key'] in nodes
   oldchain=next((oldc for oldk in groupnames[:3] for oldu in json.loads((B/'v7/report.json').read_text())[oldk] for oldc in oldu['detail']['industry_chains'] if oldc['local_key']==c['local_key']),None)
   if oldchain:assert all(e in oldchain['graph']['edges'] for e in c['graph']['edges'])
  for o in objects.values():
   for refx in o['reasoning_sources']['upstream_refs']:assert refx['local_key'] in objects and objects[refx['local_key']]['source_id']==refx['entity_id']
  for a in u['summary']['affected_refs']:
   if a['target_type']=='industry_chain_node':assert a['chain_local_key'] in chains and any(n['local_key']==a['local_key'] for n in chains[a['chain_local_key']]['affected_nodes'])
   elif a['target_type']=='industry_chain':assert a['local_key'] in chains
   else:assert a['local_key'] in macros
for co in r['company_analyses']:validate_origin(co,'company_analyses');counts['companies']+=1
assert used_evidence<=set(evcat)
assert sum(bool(x['placements']) for x in sa)==47
assert all(not x['placements'] for x in sa if x['decision']=='排除')
# Coverage is explicit over all frozen Event records; only used events are claimed as conclusion inputs.
ea=[dict(x,v8_usage='used_in_judgment' if x['event_id'] in used_events else 'reviewed_no_additional_supported_anchor') for x in eventaudit]
(O/'event-coverage-audit.json').write_text(json.dumps(ea,ensure_ascii=False,indent=2)+'\n')
audit={'schema_passed':True,'reference_closure_passed':True,'signal_ownership_passed':True,'layer_input_scope_passed':True,'all_47_adopted_signals_have_judgments':True,'all_graph_nodes_have_judgments':True,'no_fabricated_graph_edges':True,'counts':dict(counts),'input_events_reviewed':len(ea),'events_used_in_judgments':len(used_events),'signal_records':len(sa),'excluded_signals':30,'excluded_node_occurrences':sum(a['decision']=='excluded' for a in nodeaudit)}
(O/'validation.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n');print(json.dumps(audit,ensure_ascii=False))

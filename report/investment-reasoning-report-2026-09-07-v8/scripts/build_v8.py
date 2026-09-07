# -*- coding: utf-8 -*-
from pathlib import Path
import json,copy,collections,hashlib
B=Path(__file__).resolve().parents[2];O=B/'v8'
load=lambda p:json.loads((B/p).read_text())
snapshot=load('snapshot.json');signals=load('signal-audit.json');events=load('event-audit.json');event_index=load('event-index.json');old=load('v7/report.json');r=copy.deepcopy(old)
E={x['uuid']:x for x in snapshot['entities']};ID={x['id']:x for x in E.values() if x['id']};NAME={x['name']:x for x in E.values()};EA={x['event_id']:x for x in events};evcat=load('evidence-index.json');rels=snapshot['relations'];audit=[]
allowed={'geopolitical_stories':{'GEOPOLITICAL'},'macroeconomic_stories':{'MACRO_ECONOMIC'},'concept_analyses':None,'industry_chain_analyses':None,'company_analyses':None}
print('classes',collections.Counter(x['class'] for x in events))
def evid(evts):return sorted({e for x in evts for e in EA[x]['evidence_ids']})
def signalrows(entity_id,kind):
 rows=[]
 for i,x in enumerate(signals):
  d=x['data']
  if E[x['target']]['id']!=entity_id or d['audit_decision']=='排除':continue
  # Layer scope is tied to the classified source Event, not entity type.
  ids=[e for e in d['source_event_ids'] if e in EA and (allowed[kind] is None or EA[e]['class'] in allowed[kind])]
  if not ids:continue
  symbol={'UP':'↑','DOWN':'↓','MIXED':'↕','STABLE':'→','UNKNOWN':'?'}[d['direction']]
  text=symbol+' 来源记载：'+d['fact']+('；限定：'+d['audit_reason'] if d['audit_decision']=='限定采用' else '')
  if i==2:text='规模观察：2026年上半年中国人形机器人出货超4万台、全球占比97%；缺少上年同期绝对出货量，因此无法据此判断需求增长。'
  if i in [63,68]:text='潜在改善：清障活动可能改善安全条件；尚不能认定实际通道流量或整体安全恢复。'
  rows.append({'variable_id':x['source'],'variable_name':E[x['source']]['name'],'signal_id':d['uuid'],'signal':d['fact'],'source_direction':d['direction'],'adoption':'qualified' if d['audit_decision']=='限定采用' else 'adopted','qualification':d['audit_reason'],'event_ids':ids,'evidence_ids':evid(ids)})
 return rows
all_source_evidence={e for a in events for e in a['evidence_ids']}
def eventrefs(ids,kind):return [e['event_id'] for e in events if set(e['evidence_ids'])&set(ids) and (allowed[kind] is None or e['class'] in allowed[kind])]
def judgement(obj,rows,kind,parent=None):
 a=obj.get('assessment',obj.get('summary',{}));ids=a.get('evidence_ids',[]);sigids=[v['signal_id'] for v in rows]
 obj['judgment_origin']='direct' if rows else 'inferred'
 evs=eventrefs(ids,kind);evs=sorted(set(evs+[e for v in rows for e in v['event_ids']]))
 obj['reasoning_sources']={'signal_ids':sigids,'event_ids':evs,'upstream_refs':([parent] if parent and not rows else [])}
 if 'assessment' in obj:obj['assessment']['evidence_ids']=sorted(set(ids+[e for v in rows for e in v['evidence_ids']]))

def objections(text='缺少实际订单、成本或交付兑现数据。'):
 return {'summary':text,'counterevidence':[],'buffers':[],'counterevidence_status':'none_identified','evidence_gaps':[text],'scope_limits':['结论仅覆盖正文所述主体、产品、地区和时间，不外推全行业利润。']}
def assessment(conclusion,logic,ids,direction='diverging',window='后续项目落实期',conditions=None):
 return {'conclusion':conclusion,'direction':direction,'conclusion_basis':'reasoning_hypothesis','validation_status':'pending_validation','confidence':'low','forecast_window':{'kind':'stage','description':window,'start_at':None,'end_at':None},'scope':conclusion,'conditions':conditions or ['仅在所述业务、地区和项目适用，且后续实际执行得到验证时成立。'],'follow_up':['核对对应项目、产品或地区的订单、交付与实现结果。'],'transmission_logic':logic,'evidence_ids':sorted(set(ids))}
def graph_for(chain_id,key):
 uid=ID[chain_id]['uuid'];members={x['source'] for x in rels if x['data']['name']=='ChainNodeBelongsToIndustryChain' and x['target']==uid};nodes=[{'local_key':key+'-g'+str(i),'source_id':E[n]['id'],'name':E[n]['name']} for i,n in enumerate(sorted(members))];lookup={ID[n['source_id']]['uuid']:n['local_key'] for n in nodes};edges=[]
 for x in rels:
  if x['source'] in members and x['target'] in members and x['data']['name'] in ['ChainNodeInputTo','ChainNodeDependsOn','ChainNodeIsComponentOf']:
   q={'from_node_local_key':lookup[x['source']],'to_node_local_key':lookup[x['target']],'relation_label':{'ChainNodeInputTo':'投入','ChainNodeDependsOn':'依赖','ChainNodeIsComponentOf':'组成'}[x['data']['name']]}
   if q not in edges:edges.append(q)
 return {'nodes':nodes,'edges':edges}
# Explicit per-node decisions; these are reviewable in the generated audit, not topology-generated conclusions.
exclude_reasons={
 '港口运营':'未明确受扰的具体港口及其吞吐量、靠泊或收费变化；不能将航线风险外推所有港口。',
 '商用运输船舶':'未有新增造船、购船或船队更新依据，运营航线受扰不等于船舶产品需求变化。',
 '混合稀土溶液':'限制与交付证据集中在钇产品；未确认该中间品中钇的占比、对应出口合同与传导敞口。',
 '稀土分解浸出服务':'未提供钇相关原料处理合同和工作量，不能由出口政策直接确定该加工服务方向。',
 '稀土精矿':'未确认精矿组成、销售合同及地域暴露，不把特定钇产品的交付约束扩成全部原矿供需变化。',
 '银行核心业务系统':'资金或制裁事件未对应新的系统采购、改造或服务收入，缺少该技术节点传导依据。',
 '征信与信用风险管理服务':'没有对应征信服务采购或工作量变化，银行业务风险不等于该服务收入变化。',
 '客户存款':'本故事线的制裁范围尚未证明涉及存款迁移或存款定价。',
 '商业银行信贷服务':'制裁对象及结算限制尚未形成特定信贷敞口与授信变化证据。',
 '商业银行支付结算服务':'利率分歧本身不足以确定支付结算业务量或收费变化。',
 '城市燃气调压计量服务':'气源成本变化未证明调压计量工作量或服务价格变化。',
 '城市燃气接收储运服务':'未确认本期吞吐变化、仓储利用率或服务合同，不将采购价格压力直接外推储运收入。',
 '城市燃气输配管网基础设施':'缺少新增管网建设或改造安排，不能由进口气价变化推出资产投资需求。',
 '住宅开发用地使用权':'购房资格调整未包含土地出让、供地或地价变化依据。',
 '住宅项目规划设计服务':'缺少新增项目规划设计委托，销售政策不等于设计订单。',
 '工程勘察服务':'尚无相关工程新增勘察合同或工作量，更新融资与施工安排不足以确定勘察需求。',
 '工程设计服务':'未明确设计阶段与合同，不能从资金或施工线索推定新增设计订单。',
 '楼宇设备':'未明确改造清单、设备品类与采购主体，设备需求方向缺乏产品落点。',
 '视频生成基础模型':'标准制作报价下跌未提供该特定基础模型的调用量、授权费或成本变化。',
 '模型训练服务':'视频模型能力及制作报价不能证明新增训练服务订单或训练工作量。',
 '模型训练数据集':'没有具体训练数据采购、许可或需求变化依据。',
 'AI语料':'未给出语料新增采购与授权变化，不将视频业务竞争直接外推语料收入。',
 '算力供给':'本单元的模型能力或制作报价变化没有提供实际算力采购、利用率或收费变化。',
 '储能热管理系统':'项目容量及产品发布未明确热管理配置变化或供应合同，不外推该子系统需求增量。',
 '储能电池管理系统（BMS）':'未确认BMS规格、供应关系和采购增量，系统级线索不足以确定该节点变化。',
 '储能隔离变压器':'构网型控制要求未直接证明隔离变压器规格、配置数量或订单变化。',
 'IGBT':'构网型技术要求可能由控制和系统设计实现，缺少IGBT器件用量或采购变化。',
 '光伏级单晶硅片':'未确认新增电池片产线的硅片规格与采购合同，跨地区组件线索不足以确定硅片需求。',
 '人形机器人电机':'现有整机出货水平不能确认需求增长，亦无电机配置及订单变化。',
 '人形机器人减速器':'现有整机规模信号不足以推导减速器增量，缺少构型和采购依据。',
 '机器人控制计算平台':'没有平台配置、采用率或采购变化，不能由整机份额直接推导。',
 '人形机器人传感器':'未提供传感器配置、用量或采购合同，整机规模不能单独证明节点方向。',
 '石脑油加氢处理服务':'未确认具体进料品质、处理路线与业务收费，原油扰动不能单独决定加氢处理量或收益。',
 '乙烯蒸汽裂解服务':'本故事线缺少裂解原料路线、成本传递和产品调价依据，暂不将通道扰动推到该加工服务。',
 '乙烯及裂解联产品':'未拆分联产品结构、终端需求及调价能力，不能以原料压力确定产品净影响。',
 '大飞机':'短时机场停航未形成飞机新增采购或制造订单变化。',
 '煤炭采掘设备':'原煤产量下降未证明设备投资削减或更新需求，不能将产出直接等同资本开支。',
 '电站锅炉':'燃机技术进展不能外推锅炉技术、订单或需求变化。',
 '汽轮机':'没有该燃机项目对汽轮机产品的具体配置和采购依据。',
 '发电机':'缺少同项目发电机采购与技术变化依据，不能仅凭组成关系赋值。',
 '烟气脱硝设备':'没有新增环保配置、改造或采购依据。'
}

extra={
 'g1-3-chain':{'跨境物流':('受扰海运航线相关跨境物流的时效与履约成本可能承压。','航道约束 → 海运绕行或等待 → 使用相关海运段的跨境物流交付周期拉长')},
 'g2-1-chain':{'跨境物流':('交火若导致相关商业航次调整，依赖这些航次的跨境物流履约稳定性可能下降。','商业船舶受击风险 → 承运安排变化 → 相关跨境物流履约波动')},
 'g1-2-chain':{'直馏石脑油':('若受扰原油来源影响实际炼厂进料，相关直馏石脑油的成本与供给稳定性可能分化。','原油进料受扰 → 炼厂原料成本及加工安排变化 → 直馏石脑油成本与可供量分化')},
 'm2-1-chain':{'直馏石脑油':('进口原油成本若传入国内加工，直馏石脑油成本可能上升；报价与利润取决于转嫁。','进口能源价格压力 → 炼厂进料成本 → 直馏石脑油成本传递'),'乙烯蒸汽裂解服务':('以受涨价石脑油为原料的裂解业务可能承受加工价差压力，其他原料路线不作同向判断。','原油及石脑油成本传递 → 石脑油路线裂解原料成本提高 → 裂解价差取决于产品调价')},
 'm3-1-chain':{'住宅工程施工服务':('海口销售政策若转为开发商回款并带动施工支付，相关住宅工程施工需求可能改善。','购买资格放宽 → 成交与回款可能改善 → 实际施工支付和工程推进'),'住宅项目融资服务':('销售回款改善可能降低部分项目融资压力，但政策本身并未确认授信增加或融资利率下降。','购房政策调整 → 成交回款改善的可能性 → 项目资金缺口及融资安排变化')},
 'm3-2-chain':{'建筑结构材料':('湖南旧房更新项目实际开工且包含结构改造时，相关建筑结构材料需求可能增加。','旧房更新安排 → 结构改造施工落实 → 对应结构材料采购'),'建筑机电安装服务':('旧房更新若包含给排水、电气等机电改造，相关安装需求可能获得支持。','旧房更新 → 具体机电改造清单与资金落实 → 机电安装工作量')},
 'm4-1-chain':{'建筑结构材料':('河南城市更新融资若转成开工与工程支付，相关材料采购可能改善。','融资到位 → 工程支付与开工 → 结构材料采购兑现'),'建筑机电安装服务':('河南城市更新项目进入配套施工并落实支付时，相关机电安装工作量可能增加。','城市更新放款 → 项目执行及配套施工 → 机电安装服务需求')},
 'c2-n1':{'商品原油':('雪佛龙委内瑞拉扩产若按计划投产，相关商品原油可供量可能在远期增加；不能用山西天然气目标推导原油增长，也不能提前抵消短期缺口。','雪佛龙委内瑞拉投资及原油增产目标 → 实际投产与商业出货 → 对应商品原油远期供给'), '油气勘探评价服务':('山西非常规气开发与雪佛龙委内瑞拉投资计划若进入具体勘探评价合同，对应服务需求可能增加；两地项目不能互相替代。','已披露开发计划 → 对应项目评价与预算落实 → 勘探评价服务需求'),'具备产能的油气生产井':('开发计划若完成钻完井、验收和投产，可用生产井供给可能增加；规划不计为现有产能。','开发投入 → 钻完井与验收 → 可投产生产井增加'),'油气集输与初步处理服务':('对应新增井投产且接入设施后，集输与初步处理工作量可能增加，短期不能以远期规划抵消进口缺口。','生产井落地 → 实际产出与设施接入 → 集输处理工作量')},
 'c3-s1':{'储能变流器（PCS）':('印度构网型草案若按相关锂电项目范围生效，将提高 PCS 技术配置要求，合规能力决定项目适配。','印度技术草案 → 储能系统配置要求 → PCS 构网型能力与认证需求')},
 'c3-s3':{'发电电动机':('寻乌抽水蓄能项目施工推进可能形成后续发电电动机需求，订单仍以招标和设备合同为准。','抽蓄主体工程推进 → 机组设备采购阶段 → 发电电动机需求'),'抽水蓄能上水库':('寻乌项目实际建设推进支持对应上水库工程需求，限项目自身，不外推其他抽蓄项目。','主体建设推进 → 项目水库配套施工 → 上水库工程需求'),'抽水蓄能下水库':('寻乌项目建设若按整体工程安排推进，对应下水库建设需求可能落实。','主体建设推进 → 上下库配套工程安排 → 下水库建设需求')},
 'c4-p2':{'光伏支架系统':('非洲新增装机预测若落实为适用项目建设，支架采购可能随之增加；已并网的印度历史装机不重复计作未来订单。','新增项目预测 → 工程采购落实 → 对应支架需求'),'晶硅光伏组件':('采用晶硅路线的新增电站若落实采购，组件需求可能增加，设备准入和融资条件仍约束项目兑现。','新增光伏项目 → 路线与采购确认 → 晶硅组件需求'),'光伏逆变器':('新增电站建设可能带动逆变器需求，但美国设备准入与各地区配置要求会分化实际交付。','新增电站 → 并网设备采购 → 逆变器需求受准入及配置约束')},
 'c5-ai2':{'算力调度平台':('新增数据中心容量只有在客户工作负载和平台部署实际落实后，才可能带动调度平台服务需求。','数据中心建设及部署承诺 → 可用资源与客户负载兑现 → 算力调度需求')},
}
# Signals with no valid Concept mapping receive a real chain grouping, never an invented Concept.
newchains=[('炼油及石油化工产业链','俄油来源采购增加可能改善部分炼厂的进料可得性，但不等于炼厂全部原油供给增加。',['炼厂原油进料','炼油化工','直馏石脑油']),('煤炭开采产业链','原煤产量下降使供给承压，后续煤炭加工与运输取决于库存和采购替代。',['煤炭开采','原煤','煤炭洗选','商品煤','铁路运输服务']),('航空运输服务产业链','苏加诺—哈达机场停航压低对应航空运输能力，恢复时间决定后续运力修复。',['机场运营','航空运输服务']),('火力发电设备产业链','燃气轮机技术与工程体系进展支持特定设备交付能力，商业订单仍待验证。',['火电设备','火电设备安装调试服务'])]
r['industry_chain_analyses']=[];r['company_analyses']=[]
for j,(name,conclusion,names) in enumerate(newchains):
 ent=NAME[name];key='new-chain-'+str(j+1);g=graph_for(ent['id'],key);ids=[]
 for n in g['nodes']:
  ids += [e for v in signalrows(n['source_id'],'industry_chain_analyses') for e in v['evidence_ids']]
 logic={'炼油及石油化工产业链':'俄罗斯来源原油进口增加 → 匹配炼厂的俄油进料可得性改善 → 炼化加工与副产品供给取决于原料替代和装置安排','煤炭开采产业链':'原煤实际产量下降 → 新增原煤供给减少 → 洗选、商品煤与运量取决于库存和进口替代','航空运输服务产业链':'火山喷发 → 苏加诺—哈达机场暂停起降 → 对应航线航空运输能力下降','火力发电设备产业链':'燃机示范项目验收 → 技术与工程体系进展 → 同路线设备及安装调试机会仍待订单兑现'}[name]
 a=assessment(conclusion,logic,ids,'cooling' if name in ['煤炭开采产业链','航空运输服务产业链'] else 'diverging',conditions=['限材料所述地区、产品与时间；不得由产量推断利润，或由技术验收推断已获订单。'])
 c={'local_key':key,'source_id':ent['id'],'name':name,'assessment':a,'reasoning_summary':{'logic':logic,'support':{'text':conclusion,'basis':'inference','evidence_ids':sorted(set(ids))},'objections':objections()},'graph':g,'affected_nodes':[],'empty_state':None}
 for n in g['nodes']:
  if n['name'] not in names:continue
  name2=n['name'];text=conclusion
  if name2=='炼厂原油进料':text='中印增加俄罗斯原油进口支持俄油来源可得性改善；总进料还取决于其他来源变化，不确认整体供给增长。'
  if name2=='炼油化工':text='俄油进料改善可能缓冲相关炼厂采购与加工安排，但原料结构、加工适配及成本决定净影响。'
  if name2=='直馏石脑油':text='若俄油采购改善支持实际炼厂加工，相关直馏石脑油可供量可能获得缓冲；不把来源替代当总产量增长。'
  if name2=='原煤':text='7月原煤产量同比下降10.1%、前7个月下降2.9%意味着相应统计口径新增原煤供给减少；库存及进口可改变市场可得性。'
  if name2=='煤炭洗选':text='国内新增原煤若减少且库存、外购无法弥补，相关洗选业务原料及处理量可能承压。'
  if name2=='商品煤':text='国内原煤减产可能压低商品煤新增供应，但洗选率、库存及进口决定可售量，不据此确认价格上涨。'
  if name2=='铁路运输服务':text='相关煤炭装运量若随国内产出下降而减少且未获其他货源替代，对应线路货运需求可能承压，不外推全国铁路。'
  if name2=='机场运营':text='火山喷发已使苏加诺—哈达机场起降能力暂时受限；恢复取决于火山灰条件与航行许可。'
  if name2=='航空运输服务':text='苏加诺—哈达机场停航使对应航线航空运输能力下降；其他机场及全国航空需求不作同向推断。'
  if name2=='火电设备':text='燃机示范项目验收支持燃气轮机技术与工程体系进步，不能推成所有火电设备技术或订单同时改善。'
  if name2=='火电设备安装调试服务':text='同技术路线燃气轮机项目若转入商业部署，安装调试需求可能增加；仅验收研发项目还不能确认订单。'
  c['affected_nodes'].append({'local_key':key+'-impact-'+str(len(c['affected_nodes'])),'source_id':n['source_id'],'node_local_key':n['local_key'],'name':name2,'assessment':assessment(text,logic,ids,a['direction']), 'objections':objections()})
 u={'local_key':'i'+str(j+1),'source_id':ent['id'],'title':name,'summary':{'conclusion':conclusion,'transmission_logic':logic,'impact_assessment':{'level':'medium','rationale':'涉及对应统计范围或具体业务能力，尚无全链利润兑现依据。','evidence_ids':sorted(set(ids))},'affected_refs':[],'evidence_ids':sorted(set(ids))},'detail':{'macro_impacts':[],'industry_chains':[c]}}
 r['industry_chain_analyses'].append(u)

# Company judgments are explicit and business-scoped; graph company memberships are NOT automatic transmission.
company_decisions={
 'CHEVRON CORP':('70亿美元委内瑞拉投资及2031年前增产目标提高项目开发意愿；实际资本支出、产出及服务合同仍取决于批准与执行。','投资及扩产计划 → 项目预算、评价和钻完井合同 → 远期产能兑现','warming','c2','投资计划不等于已支出或已形成新增产量。'),
 'Tesla, Inc.':('Megapack 3产品发布及生产启动支持储能业务商业化推进，50GWh是名义产能目标，不能当作实际产出或确认销量。','产品发布及生产启动 → 良率与交付爬坡 → 储能系统供给兑现','warming','c3','名义容量、生产启动与实际客户交付须分开核验。'),
 '宁德时代':('行业转述的314Ah电芯降价意味着该规格报价承压，可能影响对应储能电芯收入单价；不能推断公司整体均价或利润下降。','特定规格电芯降价线索 → 匹配产品收入单价承压 → 利润取决于成本及销量','diverging','c3','行业人士转述，缺少实际成交价和规格销量。'),
 'EQUINOR ASA':('美国PJM的4个储能项目已进入建设阶段，80MW/160MWh属于规划容量；设备交付、并网及收益尚待兑现。','项目开建 → 储能设备交付与并网 → 商业运营兑现','warming','c3','不能沿公司油气图谱关系传播储能项目影响，也未确认电池技术路线或供应商。'),
 'IONIS PHARMACEUTICALS INC':('Zanvastro获批与拟上市推动对应产品商业化，pelacarsen临床结局不利构成另一管线压力；公司研发商业化呈分化，不推断整体收入方向。','一个产品获批和拟上市 ＋ 另一管线临床终点受挫 → 不同产品商业化路径分化','diverging',None,'不能把相关药物泛归为已批准siRNA治疗药物；不得把不同管线试验结果混为同一产品。'),
 'Climb Bio, Inc.':('CLYM116拟进入II期意味着研发推进计划，但尚未进入获批销售阶段，不能确认商业收入。','I期积极数据 → 拟推进II期试验 → 后续疗效与审批决定商业化','warming',None,'尚缺II期实际入组与结果；商业化仍有临床风险。'),
 'Ultragenyx Pharmaceutical Inc.':('Angelman综合征候选药物试验效果不理想使该产品推进承压，不能外推其他管线或重组蛋白节点。','目标药物试验结果不利 → 后续试验与审批路径承压 → 对应商业化不确定性增加','cooling',None,'图谱中的重组蛋白挂接不足以证明此候选药物属于该业务节点。'),
 'ALUMIS INC.':('envudeucitinib在系统性红斑狼疮中期研究未达终点，使该适应症开发承压；不外推其他适应症。','指定适应症试验未达终点 → 开发路径调整风险 → 该适应症商业化承压','cooling',None,'缺少其他适应症和后续方案结果。'),
 'Broadcom Inc.':('VMware指定版本的修复发布降低已识别漏洞的未修复暴露，效果取决于用户升级；不能传导为AI芯片业务改善。','安全更新发布 → 用户部署修复 → 特定版本漏洞暴露下降','warming',None,'仅Workstation/Fusion指定版本；不沿公司芯片图谱关系外推。'),
 'Li Auto Inc.':('理想8月交付同比增长32.07%，前8个月累计下降0.6%，月度与累计表现分化，尚不能判断全年持续增长。','单月交付改善 ＋ 累计交付略降 → 不同统计窗口销量分化','diverging',None,'未区分增程与纯电车型，不能将总交付信号挂为纯电整车节点的直接增长。'),
 '欣旺达':('欣旺达EVB 7月动力电池装机份额3.59%仅证明当月水平，不能确认份额上升，也不能外推上市公司全部业务。','子公司当月装机与份额水平 → 确认该口径业务规模 → 无前期对比不判定增长','pending',None,'信号只涉及EVB子公司动力电池口径，不能传至消费电子图谱节点。'),
}
company_windows={'CHEVRON CORP':'至2031年的投资及投产规划期','Tesla, Inc.':'生产爬坡及2026年底拟交付阶段','宁德时代':'本周报价线索及后续实际成交','EQUINOR ASA':'项目建设、交付与并网阶段','IONIS PHARMACEUTICALS INC':'按各产品获批、拟上市及临床试验节点分别判断','Climb Bio, Inc.':'II期计划推进与后续临床阶段','Ultragenyx Pharmaceutical Inc.':'相关试验结果及后续开发方案','ALUMIS INC.':'指定适应症中期研究及后续方案','Broadcom Inc.':'修复发布后至用户部署更新','Li Auto Inc.':'2026年8月与前8个月两个统计窗口','欣旺达':'2026年7月单月统计口径'}
company_parent={}
for name,(text,logic,direction,unitkey,gap) in company_decisions.items():
 ent=NAME[name];rows=signalrows(ent['id'],'company_analyses');assert rows,name;ids=[e for v in rows for e in v['evidence_ids']]
 obj={'local_key':'company-'+ent['id'][3:11],'source_id':ent['id'],'name':name,'assessment':assessment(text,logic,ids,direction,company_windows[name]),'variable_signals':rows,'objections':objections(gap)};judgement(obj,rows,'company_analyses');company_parent[name]=(unitkey,obj)
 if unitkey:
  u=next(u for u in r['concept_analyses'] if u['local_key']==unitkey);u['detail'].setdefault('companies',[]).append(obj)
 else:r['company_analyses'].append(obj)

for kind in ['geopolitical_stories','macroeconomic_stories','concept_analyses','industry_chain_analyses']:
 for u in r[kind]:
  detail=u['detail'];detail['variable_signals']=signalrows(u['source_id'],kind);detail.setdefault('companies',[]);judgement(u,detail['variable_signals'],kind)
  for m in detail['macro_impacts']:
   m['variable_signals']=signalrows(m['source_id'],kind);judgement(m,m['variable_signals'],kind,{'entity_id':u['source_id'],'local_key':u['local_key']})
  for c in detail['industry_chains']:
   c['variable_signals']=signalrows(c['source_id'],kind)
   # Reassess robot: a directly signalled level can yield a bounded judgment without inventing growth.
   if c['local_key']=='c5-ai1':
    n=next(n for n in c['graph']['nodes'] if n['name']=='人形机器人');rows=signalrows(n['source_id'],kind);ids=[e for v in rows for e in v['evidence_ids']]
    text='上半年出货超4万台及全球份额97%支持规模判断，但缺少同比绝对量，不能确认需求增速或利润方向。';logic='出货数量与全球份额 → 确认当期规模 → 缺少同期绝对量及库存数据，不外推需求增长'
    c['assessment']=assessment(text,logic,ids,'pending','2026年上半年统计口径');c['reasoning_summary']['logic']=logic;c['reasoning_summary']['support']={'text':text,'basis':'inference','evidence_ids':ids};c['empty_state']=None
    c['affected_nodes'].append({'local_key':'c5-ai1-robot-impact','source_id':n['source_id'],'node_local_key':n['local_key'],'name':n['name'],'assessment':copy.deepcopy(c['assessment']),'objections':objections('缺少同比绝对出货、渠道库存和终端使用数据。')})
   for name,(text,logic) in extra.get(c['local_key'],{}).items():
    n=next(n for n in c['graph']['nodes'] if n['name']==name)
    if any(q['source_id']==n['source_id'] for q in c['affected_nodes']):continue
    a=assessment(text,logic,c['assessment']['evidence_ids'],window=c['assessment']['forecast_window']['description'],conditions=copy.deepcopy(c['assessment']['conditions']))
    c['affected_nodes'].append({'local_key':c['local_key']+'-new-'+str(len(c['affected_nodes'])),'source_id':n['source_id'],'node_local_key':n['local_key'],'name':name,'assessment':a,'objections':objections('需核对该节点对应产品、项目采购及实际业务敞口；结构相连不代表结果已实现。')})
   judgement(c,c['variable_signals'],kind,{'entity_id':u['source_id'],'local_key':u['local_key']})
   for n in c['affected_nodes']:
    n['variable_signals']=signalrows(n['source_id'],kind);judgement(n,n['variable_signals'],kind,{'entity_id':c['source_id'],'local_key':c['local_key']})
    audit.append({'kind':kind,'unit':u['local_key'],'chain':c['local_key'],'entity_id':n['source_id'],'name':n['name'],'decision':'included','origin':n['judgment_origin'],'reason':n['assessment']['transmission_logic']})
   kept={n['node_local_key'] for n in c['affected_nodes']}
   for n in c['graph']['nodes']:
    if n['local_key'] not in kept:audit.append({'kind':kind,'unit':u['local_key'],'chain':c['local_key'],'entity_id':n['source_id'],'name':n['name'],'decision':'excluded','reason':exclude_reasons.get(n['name'],'未确认该节点对应产品、项目或业务敞口；不将同链关系当作独立影响依据。')})
   c['graph']['nodes']=[n for n in c['graph']['nodes'] if n['local_key'] in kept];c['graph']['edges']=[e for e in c['graph']['edges'] if e['from_node_local_key'] in kept and e['to_node_local_key'] in kept]
   c['graph']['scope']='assessed_nodes_only'
  if kind in ['concept_analyses','industry_chain_analyses']:
   u['summary']['affected_refs']=[{'target_type':'industry_chain_node','local_key':n['local_key'],'chain_local_key':c['local_key']} for c in detail['industry_chains'] for n in c['affected_nodes']]

# Explicit company-to-node business reasoning references. No company Signal is copied into node.variable_signals.
for company,uk,chainkey,nodenames in [('CHEVRON CORP','c2','c2-n1',['油气开采','油气钻完井服务','油气勘探评价服务','具备产能的油气生产井','商品原油']),('Tesla, Inc.','c3','c3-s1',['锂电储能系统']),('宁德时代','c3','c3-s1',['储能锂离子电芯']),('EQUINOR ASA','c3','c3-s1',['锂电储能系统'])]:
 co=company_parent[company][1];u=next(u for u in r['concept_analyses'] if u['local_key']==uk);c=next(c for c in u['detail']['industry_chains'] if c['local_key']==chainkey)
 for n in c['affected_nodes']:
  if n['name'] not in nodenames:continue
  condition='Equinor项目仅在采购明确采用锂电路线时才适用于本节点。' if company=='EQUINOR ASA' else '公司材料所述项目或产品与本节点业务范围匹配，且订单和实际执行落实。'
  n['reasoning_sources']['upstream_refs'].append({'entity_id':co['source_id'],'local_key':co['local_key'],'mechanism':co['assessment']['transmission_logic'],'condition':condition})
  n['reasoning_sources']['event_ids']=sorted(set(n['reasoning_sources']['event_ids']+co['reasoning_sources']['event_ids']))
  n['assessment']['evidence_ids']=sorted(set(n['assessment']['evidence_ids']+co['assessment']['evidence_ids']))
  n['assessment']['conditions']=list(dict.fromkeys(n['assessment']['conditions']+[condition]))

r['concept_analyses'][4]['summary']['conclusion']='算力建设有具体项目支持，但工程化和供电就绪决定兑现；人形机器人已有规模判断，现有出货与份额信号不足以确认需求增长。'
r['schema_version']='report-publication/v5-draft'
r['limitations']=[x for x in r['limitations'] if '无方向节点' not in x]+['直接表示本实体存在本层输入范围内可采用的变量信号，不代表未来结果已被证实。','推理标记须结合明确业务机制和成立条件；图中只保留有判断的节点，未重连被删节点两端。','未映射Concept的真实产业链单独成组；无法可靠传至产业链的公司仍保留直接判断，不伪造公司所属产业链。','本次是固定输入上的报告基线重审与补充推理，不是原生Workflow运行，不包含后续新采集数据。']
# Signal coverage and provenance are internal audit, not a public report section.
placements=collections.defaultdict(list)
def walk(x,p=''):
 if isinstance(x,dict):
  for sig in x.get('variable_signals',[]):placements[sig['signal_id']].append(p)
  for k,v in x.items():walk(v,p+'/'+k)
 elif isinstance(x,list):
  for i,v in enumerate(x):walk(v,p+'/'+str(i))
walk(r)
sa=[]
for i,x in enumerate(signals):
 d=x['data'];sa.append({'input_index':i,'signal_id':d['uuid'],'entity_id':E[x['target']]['id'],'entity_name':E[x['target']]['name'],'variable_name':E[x['source']]['name'],'decision':d['audit_decision'],'reason':d['audit_reason'],'placements':placements[d['uuid']]})
assert all(q['placements'] for q in sa if q['decision']!='排除'),[q for q in sa if q['decision']!='排除' and not q['placements']]
used=set()
def refs(x):
 if isinstance(x,dict):
  used.update(x.get('evidence_ids',[]))
  for v in x.values():refs(v)
 elif isinstance(x,list):
  for v in x:refs(v)
refs(r);assert used<=all_source_evidence;assert used<=evcat.keys()
for name,obj in [('report.json',r),('node-coverage-audit.json',audit),('signal-coverage-audit.json',sa),('evidence-catalog.json',{e:evcat[e] for e in sorted(used)})]:
 (O/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
manifest={'baseline':'v8','source_baseline':'v7','input_events':168,'input_signals':77,'adopted_or_qualified_signals':47,'excluded_signals':30,'unique_evidence':len(used),'company_judgments':len(company_decisions),'source_sha256':{f:hashlib.sha256((B/f).read_bytes()).hexdigest() for f in ['snapshot.json','signal-audit.json','event-audit.json','v7/report.json']},'published':False,'native_workflow_run':False}
(O/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');print(manifest)

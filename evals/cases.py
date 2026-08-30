"""Project eval registry; product-agent cases are added with each agent."""

import json

from agno.eval import Case

from agents.evidence_extractor import build_evidence_extractor_agent
from agents.title_curator import build_title_curator_agent
from app.settings import default_model
from db import get_postgres_db

eval_db = get_postgres_db()

title_curator = build_title_curator_agent()
evidence_extractor = build_evidence_extractor_agent()
eval_judge = default_model()


def _evidence_input(title: str, raw_text: str) -> str:
    return json.dumps(
        {
            "document": {
                "schema_version": "prepared_raw_document.v2",
                "collection_id": "eval-collection",
                "manifest_path": "/tmp/eval-manifest.json",
                "manifest_offset": 0,
                "next_manifest_offset": 1,
                "document_index": 0,
                "document_count": 1,
                "document_path": "/tmp/eval-document.md",
                "document_url_path": "/raw-evidence/documents/eval.md",
                "document_sha256": "0" * 64,
                "content_sha256": "1" * 64,
                "publication_key": "eval-publication",
                "source_id": "SRC_eval_000000000000000000000",
                "source_name": "路透社",
                "source_level": "L2_WIRE",
                "source_url": "https://example.test/eval",
                "title": title,
                "raw_text": raw_text,
                "published_at": "2026-08-30T00:00:00Z",
                "collected_at": "2026-08-30T00:01:00Z",
            },
            "categories": [{"code": "EVENT_BRIEF", "name": "事件简报", "description": "新增现实事件或业务变化"}],
        },
        ensure_ascii=False,
    )


CASES: tuple[Case, ...] = (
    Case(
        name="title-curator-representative-market-relevance",
        agent=title_curator,
        tags=("release", "title-curator"),
        timeout_seconds=60,
        judge_model=eval_judge,
        input="""{"candidates":[
          {"candidate_id":"policy","title":"国务院发布促进民营经济发展的新政策"},
          {"candidate_id":"industry","title":"存储芯片现货价格上涨，主要厂商扩产"},
          {"candidate_id":"company","title":"某上市公司签署20亿元服务器订单"},
          {"candidate_id":"sports","title":"世界杯决赛首发阵容公布"},
          {"candidate_id":"entertainment","title":"知名演员新电影今日上映"},
          {"candidate_id":"lifestyle","title":"周末露营装备选购指南"},
          {"candidate_id":"advertisement","title":"限时优惠，扫码领取健身体验课"}
        ]}""",
        criteria=(
            "输出必须是 TitleCurationDraft，并且恰好覆盖输入的七个 candidate_id、每个 ID 只出现一次、不增加未知 ID。"
            "policy、industry、company 的 is_relevant 必须为 true；"
            "sports、entertainment、lifestyle、advertisement 的 is_relevant 必须为 false。"
            "不得出现工具调用或正文推断。"
        ),
    ),
    Case(
        name="title-curator-retains-ambiguous-title",
        agent=title_curator,
        tags=("release", "title-curator"),
        timeout_seconds=60,
        judge_model=eval_judge,
        input='{"candidates":[{"candidate_id":"ambiguous","title":"项目取得新进展"}]}',
        criteria=(
            "输出必须是 TitleCurationDraft，恰好包含 candidate_id=ambiguous 一次，"
            "并将 is_relevant 设为 true；不得因为标题信息不足而过滤。"
        ),
    ),
    Case(
        name="evidence-extractor-keeps-business-actor-separate-from-reporter",
        agent=evidence_extractor,
        tags=("release", "evidence-extractor"),
        timeout_seconds=90,
        judge_model=eval_judge,
        input=_evidence_input("路透：美国或于周末打击伊朗", "路透社援引知情人士称，美国计划于周末对伊朗发动打击。"),
        criteria=(
            "输出必须是 EvidenceExtractionDraft 且只包含一条 Evidence。actors 必须包含美国且不得把路透社作为业务主体；"
            "attribution.reported_by 必须是路透社；modality 必须为 PLAN；time.raw 保留周末，"
            "start_at 和 end_at 为 null，precision 为 UNKNOWN。"
        ),
    ),
    Case(
        name="evidence-extractor-groups-related-metrics-and-splits-guidance",
        agent=evidence_extractor,
        tags=("release", "evidence-extractor"),
        timeout_seconds=90,
        judge_model=eval_judge,
        input=_evidence_input(
            "英伟达公布季度业绩与下一季度指引",
            "英伟达公布本季度营收300亿美元、数据中心收入260亿美元、毛利率75%；公司预计下一季度营收约320亿美元。",
        ),
        criteria=(
            "输出必须是 EvidenceExtractionDraft 且恰好包含两条 Evidence：已公布的本季度实际业绩为一条，"
            "其 metrics 同时保留营收、数据中心收入和毛利率，不得按指标拆分；"
            "下一季度营收指引单独一条且 modality 为 PLAN。"
        ),
    ),
    Case(
        name="evidence-extractor-collapses-title-lead-and-body-repetition",
        agent=evidence_extractor,
        tags=("release", "evidence-extractor"),
        timeout_seconds=90,
        judge_model=eval_judge,
        input=_evidence_input(
            "某服务器厂商获得20亿元AI服务器订单",
            "导语：某服务器厂商获得20亿元AI服务器订单。\n正文：该厂商公告称，公司已获得总额20亿元的AI服务器订单。",
        ),
        criteria=(
            "标题、导语和正文重复表达同一个业务命题。输出必须是包含 raw_evidence 和 evidences "
            "的 EvidenceExtractionDraft，且 evidences 数组只包含一条；该 Evidence 应保留20亿元订单信息，"
            "不得因为同一信息出现在不同文本位置而拆成多条。"
        ),
    ),
)

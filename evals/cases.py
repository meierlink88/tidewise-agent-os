"""Project eval registry; product-agent cases are added with each agent."""

from agno.eval import Case

from agents.title_curator import build_title_curator_agent
from app.settings import default_model
from db import get_postgres_db

eval_db = get_postgres_db()

title_curator = build_title_curator_agent()
eval_judge = default_model()

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
)

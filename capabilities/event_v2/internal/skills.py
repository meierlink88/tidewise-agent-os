"""Code-owned Skill loading and per-step Agent configuration."""

import hashlib
from pathlib import Path

from agno.agent import Agent
from agno.skills import LocalSkills, Skills

ANALYST_ID = "event-analyst"
ANALYST_REVISION = 2
EXTRACTION_SKILL = "document-event-extraction"


def extraction_skills() -> Skills:
    skills = Skills(loaders=[LocalSkills(str(Path(__file__).resolve().parents[3] / "skills" / EXTRACTION_SKILL))])
    if EXTRACTION_SKILL not in skills.get_skill_names():
        raise RuntimeError("Event extraction Skill is unavailable")
    return skills


def extraction_skill_snapshot() -> dict:
    skill = extraction_skills().get_skill(EXTRACTION_SKILL)
    if skill is None:
        raise RuntimeError("Event extraction Skill is unavailable")
    return {"name": EXTRACTION_SKILL, "sha256": hashlib.sha256(skill.instructions.encode()).hexdigest()}


def bind_extraction_skill(agent: Agent, *, tools: list, schema=None) -> Agent:
    agent.skills = extraction_skills()
    skill = agent.skills.get_skill(EXTRACTION_SKILL)
    if skill is None:
        raise RuntimeError("Event extraction Skill is unavailable")
    # Fixed Workflow steps must receive full rules even if the model does not call a Skill tool.
    base = agent.instructions or ""
    if not isinstance(base, str):
        raise TypeError("Event analyst instructions must be text")
    agent.instructions = base + f"\n当前步骤：事件提取；当前Skill：{EXTRACTION_SKILL}。\n" + skill.instructions
    agent.tools = tools
    agent.output_schema = schema
    agent.use_json_mode = schema is not None
    agent.add_history_to_context = False
    agent.add_datetime_to_context = False
    return agent

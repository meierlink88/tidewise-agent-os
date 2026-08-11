"""Tidewise Assistant Agent."""

from agno.agent import Agent

from app.settings import default_model
from db import get_postgres_db

INSTRUCTIONS = """\
你是观潮家（Tidewise）的本地 AgentOS 助手。用中文直接、准确地回答问题；
不知道的信息要明确说明，不虚构工具调用、系统状态或外部事实。回答保持简洁，
需要步骤时给出可执行顺序。你当前的首要职责是验证本地 AgentOS 的模型、会话和接口链路。
"""

tidewise_assistant = Agent(
    id="tidewise-assistant",
    name="Tidewise Assistant",
    model=default_model(),
    db=get_postgres_db(),
    instructions=INSTRUCTIONS,
    add_datetime_to_context=True,
    add_history_to_context=True,
    num_history_runs=5,
    markdown=True,
)

"""Lifecycle helpers for the Agno Studio-managed Evidence Extractor Agent."""

from pathlib import Path

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry

from app.settings import default_model
from capabilities.evidence import EvidenceExtractionDraft
from db import get_postgres_db

EVIDENCE_EXTRACTOR_AGENT_ID = "evidence-extractor"
EVIDENCE_EXTRACTOR_CONTRACT_VERSION = 3
EVIDENCE_EXTRACTOR_DESCRIPTION = "Classifies one Raw Evidence and extracts its atomic Evidences in one reading."
_SEED_PROMPT = Path(__file__).with_name("evidence_extractor.seed.md")
_RUNTIME_CONTRACT = """Evidence Extractor runtime contract version 3:
- Read the supplied EvidenceAnalysisRequest exactly once.
- It contains one document and the complete allowed Category vocabulary.
- Choose exactly one category and return its code as raw_evidence.category_code.
- Category IDs are deliberately absent. Never invent an ID or return more than one category code.
- In the same structured response, return Raw Evidence enrichment and the complete set of directly supported
  atomic Evidences.
- Each Evidence contains only a concise summary and semantic with exactly who, what, when, where, why and how.
- semantic.what is required; use null for any other dimension not directly supported by the document.
- Do not call Tools or publish data.
- The deterministic Workflow validates the code, resolves the formal ID and owns all side effects.
"""


def _seed_instructions() -> str:
    instructions = _SEED_PROMPT.read_text(encoding="utf-8").strip()
    if not instructions:
        raise ValueError("Evidence Extractor seed prompt is empty")
    return instructions


def build_evidence_extractor_agent() -> Agent:
    """Return the code-reviewed initial Agent saved to Studio once."""
    return Agent(
        id=EVIDENCE_EXTRACTOR_AGENT_ID,
        name="Evidence Extractor",
        description=EVIDENCE_EXTRACTOR_DESCRIPTION,
        model=default_model(),
        db=get_postgres_db(),
        tools=[],
        instructions=_seed_instructions(),
        additional_context=_RUNTIME_CONTRACT,
        output_schema=EvidenceExtractionDraft,
        parse_response=True,
        use_json_mode=True,
        metadata={"evidence_extractor_contract_version": EVIDENCE_EXTRACTOR_CONTRACT_VERSION},
        retries=0,
        add_datetime_to_context=False,
        add_history_to_context=False,
        store_tool_messages=False,
        markdown=False,
    )


def ensure_evidence_extractor_agent(registry: Registry) -> int:
    """Create published version 1 once; preserve operator-managed instructions."""
    db = get_postgres_db()
    component = db.get_component(EVIDENCE_EXTRACTOR_AGENT_ID, component_type=ComponentType.AGENT)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Evidence Extractor has no published Studio version")
        current = Agent.load(EVIDENCE_EXTRACTOR_AGENT_ID, db=db, registry=registry, version=version)
        if current is None:
            raise ValueError("Evidence Extractor published version could not be rehydrated")
        metadata = dict(current.metadata or {})
        if metadata.get("evidence_extractor_contract_version") == EVIDENCE_EXTRACTOR_CONTRACT_VERSION:
            return version
        current.db = db
        current.description = EVIDENCE_EXTRACTOR_DESCRIPTION
        current.tools = []
        current.tool_call_limit = None
        current.additional_context = _RUNTIME_CONTRACT
        current.output_schema = EvidenceExtractionDraft
        current.parse_response = True
        current.use_json_mode = True
        current.retries = 0
        current.add_datetime_to_context = False
        current.add_history_to_context = False
        current.store_tool_messages = False
        current.markdown = False
        current.metadata = {
            **metadata,
            "evidence_extractor_contract_version": EVIDENCE_EXTRACTOR_CONTRACT_VERSION,
        }
        migrated = current.save(
            db=db,
            stage="published",
            notes=f"Evidence Extractor runtime contract migration {EVIDENCE_EXTRACTOR_CONTRACT_VERSION}",
        )
        if not isinstance(migrated, int):
            raise ValueError("Evidence Extractor runtime contract migration failed")
        return migrated

    version = build_evidence_extractor_agent().save(
        db=db,
        stage="published",
        notes="Initial code-reviewed Evidence Extractor seed",
    )
    if not isinstance(version, int):
        raise ValueError("Evidence Extractor seed did not produce a published version")
    return version


def load_evidence_extractor_agent(registry: Registry) -> Agent:
    """Load the current published Studio Agent for Workflow composition."""
    db = get_postgres_db()
    component = db.get_component(EVIDENCE_EXTRACTOR_AGENT_ID, component_type=ComponentType.AGENT)
    if component is None:
        raise ValueError("Evidence Extractor Studio component is missing")
    version = component.get("current_version")
    if not isinstance(version, int):
        raise ValueError("Evidence Extractor has no published Studio version")
    agent = Agent.load(EVIDENCE_EXTRACTOR_AGENT_ID, db=db, registry=registry, version=version)
    if agent is None:
        raise ValueError("Evidence Extractor published version could not be rehydrated")
    return agent

"""Independent code-owned pure collection Workflow; no default Schedule or Agent."""

from agno.workflow import Step, Workflow
from agno.workflow.types import HumanReview, OnError

from capabilities.collection_v2.functions import collect_raw_v2, publish_raw_v2
from db import get_postgres_db

RAW_COLLECTION_V2_WORKFLOW_ID = "raw-collection-v2"

raw_collection_v2 = Workflow(
    id=RAW_COLLECTION_V2_WORKFLOW_ID,
    name="Raw Collection V2",
    description="Collect sources, deduplicate article versions and archive raw documents to MinIO without an LLM.",
    db=get_postgres_db(),
    steps=[
        Step(
            name="Collect Raw V2",
            executor=collect_raw_v2,  # type: ignore[arg-type]  # Agno injects RunContext by name.
            max_retries=0,
            human_review=HumanReview(on_error=OnError.fail),
        ),
        Step(
            name="Publish Raw V2",
            executor=publish_raw_v2,  # type: ignore[arg-type]  # Agno injects RunContext by name.
            max_retries=0,
            human_review=HumanReview(on_error=OnError.fail),
        ),
    ],
)

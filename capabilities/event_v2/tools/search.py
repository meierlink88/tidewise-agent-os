"""Read-only vector retrieval with an invocation-local execution receipt."""

from typing import Any

from capabilities.event_v2.internal.models import DocumentEventAnalysis, EventRecallCandidate


class EventSearch:
    def __init__(self, vectors: Any, article_key: str):
        self.vectors = vectors
        self.article_key = article_key
        self.receipt: dict | None = None

    async def search_similar_events(self, title: str, summary: str) -> dict:
        """Search similar Events using the extracted event's exact title and summary.

        Args:
            title: Extracted event title, unchanged in the final output.
            summary: Extracted event summary, unchanged in the final output.

        Returns:
            status and candidates containing candidate_id, title, summary, semantic, score.
            An error is not an empty successful search; retry or let the step fail.
        """
        self.receipt = None
        vector = await self.vectors.embed(title, summary)
        candidates = [
            EventRecallCandidate.model_validate(c).model_dump(mode="json")
            for c in await self.vectors.recall(vector, self.article_key)
        ]
        self.receipt = {"title": title, "summary": summary, "vector": vector, "candidates": candidates}
        return {"status": "success", "candidates": candidates}

    def verify(self, analysis: DocumentEventAnalysis) -> dict:
        receipt = self.receipt
        if receipt is None:
            raise ValueError("Event search did not complete successfully")
        if (analysis.event.title, analysis.event.summary) != (receipt["title"], receipt["summary"]):
            raise ValueError("Final event differs from the searched event; search again")
        decision = analysis.deduplication
        allowed = {c["candidate_id"] for c in receipt["candidates"]}
        if (decision.duplicate and decision.matched_id not in allowed) or (
            not decision.duplicate and decision.matched_id is not None
        ):
            raise ValueError("Identity selected a candidate outside recall or contradictory identity")
        return receipt

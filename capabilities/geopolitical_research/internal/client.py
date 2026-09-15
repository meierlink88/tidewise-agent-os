"""Consumer of the existing Research swarm API; creation is never auto-retried."""

import os
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from capabilities.geopolitical_research.internal.models import PRESET, ResearchRunDetail


class CreatedRun(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    status: str
    preset_name: str


class ResearchClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = os.getenv("TIDEWISE_RESEARCH_BASE_URL", "").rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("TIDEWISE_RESEARCH_BASE_URL must be an HTTP service URL without credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("Research URL cannot have a query or fragment")
        headers = {}
        token = os.getenv("TIDEWISE_RESEARCH_API_KEY", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.AsyncClient(
            base_url=self.base_url + "/",
            headers=headers,
            timeout=30,
            transport=transport,
            follow_redirects=False,
        )

    async def create(self, user_vars: dict[str, str]) -> str:
        response = await self.http.post("swarm/runs", json={"preset_name": PRESET, "user_vars": user_vars})
        response.raise_for_status()
        created = CreatedRun.model_validate(response.json())
        if created.preset_name != PRESET:
            raise ValueError("Research create returned the wrong team")
        return created.id

    async def get(self, run_id: str, user_vars: dict[str, str]) -> ResearchRunDetail:
        response = await self.http.get(f"swarm/runs/{run_id}")
        response.raise_for_status()
        detail = ResearchRunDetail.model_validate(response.json())
        if detail.id != run_id or any(detail.user_vars.get(key) != value for key, value in user_vars.items()):
            raise ValueError("Research report identity does not match the frozen request")
        return detail

    async def close(self) -> None:
        await self.http.aclose()

"""ASGI entry point for the standalone Tidewise ingestion service."""

from sematica.ingestion.runtime import create_runtime_app, load_ingestion_config

app = create_runtime_app(load_ingestion_config())

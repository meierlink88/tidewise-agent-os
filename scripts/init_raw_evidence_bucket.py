"""Idempotently initialize the AgentOS-owned Raw Evidence MinIO bucket."""

from capabilities.collection.internal.object_storage import ensure_raw_evidence_bucket


def main() -> None:
    ensure_raw_evidence_bucket()
    print("PASS raw-evidence-bucket-ready")


if __name__ == "__main__":
    main()

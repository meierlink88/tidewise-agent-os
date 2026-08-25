"""Explicitly reconcile historical local Evidence identities with Data Service."""

import sys

from capabilities.evidence import reconcile_evidence_bindings


def main() -> None:
    result = reconcile_evidence_bindings()
    print(result.model_dump_json(indent=2))
    if result.ineligible:
        sys.exit(1)


if __name__ == "__main__":
    main()

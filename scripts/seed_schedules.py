"""Explicitly seed missing AgentOS Schedule defaults for a new environment."""

from app.schedules import seed_schedules


def main() -> int:
    return 0 if seed_schedules() else 1


if __name__ == "__main__":
    raise SystemExit(main())

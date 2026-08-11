---
name: deploy-platform
description: Prepare and deploy Tidewise AgentOS to a user-controlled production host after confirming topology, public URL, PostgreSQL, secrets, JWT, observability and rollback.
---

# Deploy Tidewise AgentOS

Production topology is not inferred from the local shared-Compose setup. First confirm the target host/provider, public `AGENTOS_URL`, PostgreSQL ownership/backups, TLS ingress, image registry, secret manager, desired replicas and rollback mechanism.

Require `RUNTIME_ENV=prd`, JWT verification (`JWT_VERIFICATION_KEY` or `JWT_JWKS_FILE`), a production DeepSeek key, strong DB credentials, and optional MCP OAuth secret. Never copy the local `.env` wholesale or expose PostgreSQL publicly.

Build an immutable image, run migrations/readiness against a non-production target, deploy, and verify HTTPS health, Agent, Workflow, scheduler callback, MCP auth, traces and restart persistence. Record the exact image digest and rollback command. Do not use the local `compose.prod.yaml` until the production network/database topology has been explicitly reviewed.

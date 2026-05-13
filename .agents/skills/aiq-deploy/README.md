# AIQ Deploy Skill

Portable Agent Skill for deploying, verifying, troubleshooting, and stopping the NVIDIA AI-Q Blueprint.

## What This Skill Provides

- CLI, local web, Docker Compose, and Kubernetes deployment routing.
- Environment validation without exposing secrets.
- Backend, UI, and PostgreSQL health checks.
- Logs, rebuild, stop, and safe teardown workflows.
- FRAG/RAG prerequisite checks.

## Canonical Location

```text
.agents/skills/aiq-deploy/
```

Claude Code compatibility is provided by:

```text
.claude/skills/aiq-deploy -> ../../.agents/skills/aiq-deploy
```

## Relationship To aiq-research

Use `aiq-deploy` to get AI-Q running. Use `aiq-research` after a local AI-Q server is reachable.

## Quick Verification

From this skill directory:

```bash
test -f SKILL.md && echo "aiq-deploy skill present"
```

---
name: aiq-deploy
description: Deploy, verify, troubleshoot, or stop the NVIDIA AI-Q Blueprint in CLI, local web, Docker Compose, or Kubernetes mode. Use when asked to start AI-Q, deploy AIQ, run the UI/backend, check health, inspect logs, rebuild, stop services, or prepare a running AI-Q server for the aiq-research skill.
license: Apache-2.0
compatibility: Claude Code, OpenCode, Codex, and Agent Skills-compatible tools. Requires access to the AI-Q repository and the runtime selected by the user.
metadata:
  version: "1.0.0"
  github-url: "https://github.com/NVIDIA-AI-Blueprints/aiq"
  tags: "nvidia aiq blueprint deploy operations agent-skills"
allowed-tools: Read Bash
---

# AIQ Deploy Skill

Use this skill to get the NVIDIA AI-Q Blueprint running and verified. This skill owns deployment and operations. Use `aiq-research` only after an AI-Q server is already reachable.

## Supported Modes

| User Intent | Mode | Primary Entry Point |
|---|---|---|
| "run AIQ in my terminal", "start CLI" | CLI | `scripts/start_cli.sh` |
| "run AIQ locally", "start web UI", "start backend" | Local web | `scripts/start_e2e.sh` |
| "deploy with compose", "run containers" | Docker Compose | `deploy/compose/docker-compose.yaml` |
| "deploy to Kubernetes", "Helm" | Kubernetes | `deploy/helm/README.md` |
| "use Foundational RAG", "FRAG mode" | Docker/Helm plus external RAG | `configs/config_web_frag.yml` |

Infer the mode from the user request. If the user says only "deploy AIQ", prefer Docker Compose for a durable backend, UI, and PostgreSQL stack. If they ask for a quick local dev run, prefer local web mode.

## Safety Rules

- Never print secret values. Check only whether required variables are set.
- Do not overwrite `deploy/.env` if it already exists. If missing, copy from `deploy/.env.example` and tell the user which values must be filled.
- Ask before destructive cleanup such as deleting Docker volumes with `down -v`.
- Do not claim FRAG is ready unless both `RAG_SERVER_URL` and `RAG_INGEST_URL` are configured and reachable.
- Run verification commands yourself. Do not give the user a command to run when you can run it in the current environment.

## Step 1 - Locate The Repository

Work from the AI-Q repository root. Verify the expected files exist:

```bash
test -f pyproject.toml
test -f deploy/.env.example
test -f deploy/compose/docker-compose.yaml
test -f scripts/start_cli.sh
test -f scripts/start_e2e.sh
```

If any are missing, stop and report that the current directory is not an AI-Q checkout.

## Step 2 - Check Environment File

Use `deploy/.env` as the source of truth for local and Docker deployments.

```bash
if [ ! -f deploy/.env ]; then
  cp deploy/.env.example deploy/.env
  echo "created deploy/.env from deploy/.env.example"
fi

python3 - <<'PY'
from pathlib import Path

env = Path("deploy/.env")
values = {}
for line in env.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip()

def present(key: str) -> str:
    return "SET" if values.get(key) else "MISSING"

print(f"NVIDIA_API_KEY={present('NVIDIA_API_KEY')}")
print(f"TAVILY_API_KEY={present('TAVILY_API_KEY')}")
print(f"SERPER_API_KEY={present('SERPER_API_KEY')}")
print(f"EXA_API_KEY={present('EXA_API_KEY')}")
print(f"NAT_JOB_STORE_DB_URL={present('NAT_JOB_STORE_DB_URL')}")
print(f"AIQ_CHECKPOINT_DB={present('AIQ_CHECKPOINT_DB')}")
print(f"BACKEND_CONFIG={values.get('BACKEND_CONFIG') or 'default'}")
PY
```

Core hosted-model usage requires `NVIDIA_API_KEY`. Web research requires at least one configured web-search provider key such as `TAVILY_API_KEY`, `SERPER_API_KEY`, or `EXA_API_KEY`, depending on the selected config.

If required keys are missing, stop and ask the user to fill `deploy/.env`. Do not request or echo the secret values in chat.

## Step 3 - Check Runtime Prerequisites

For all modes:

```bash
python3 --version
test -d .venv && echo "venv=present" || echo "venv=missing"
```

For local web mode:

```bash
node --version 2>/dev/null || echo "node=missing"
npm --version 2>/dev/null || echo "npm=missing"
```

For Docker Compose:

```bash
docker --version
docker compose version
docker info >/dev/null
for port in 3000 8000 5432; do
  if lsof -nP -iTCP:$port -sTCP:LISTEN >/dev/null 2>&1; then
    echo "port $port is already in use"
  else
    echo "port $port is free"
  fi
done
```

If port 8000 is already used by another blueprint such as RAG page-elements, set `PORT=8100` in `deploy/.env` before starting Compose.

## Mode A - CLI

Use this when the user wants an interactive terminal research assistant.

```bash
./scripts/start_cli.sh
```

For a non-default config:

```bash
./scripts/start_cli.sh --config_file configs/config_cli_default.yml
```

If `.venv` is missing, tell the user to run `./scripts/setup.sh` or run the repository's documented setup flow if the user has authorized dependency installation.

## Mode B - Local Web

Use this for local development without Docker Compose.

```bash
./scripts/start_e2e.sh --config_file configs/config_web_default_llamaindex.yml
```

Verify:

```bash
curl -sf http://localhost:8000/health >/dev/null && echo "backend=healthy"
curl -sf http://localhost:3000 >/dev/null && echo "frontend=reachable"
```

The script starts a backend at `http://localhost:8000` and frontend at `http://localhost:3000`.

## Mode C - Docker Compose

Use this as the default durable local deployment.

```bash
cd deploy/compose
docker compose --env-file ../.env -f docker-compose.yaml config --quiet
docker compose --env-file ../.env -f docker-compose.yaml config >/tmp/aiq-compose.resolved.yml
docker compose --env-file ../.env -f docker-compose.yaml up -d --build
```

Use pre-built images when the user requests registry images or faster startup:

```bash
cd deploy/compose
docker compose --env-file ../.env -f docker-compose.yaml up -d
```

Verify:

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E 'aiq-agent|aiq-blueprint-ui|aiq-postgres'
curl -sf "http://localhost:${PORT:-8000}/health" >/dev/null && echo "aiq-agent=healthy"
curl -sf "http://localhost:${FRONTEND_PORT:-3000}" >/dev/null && echo "ui=reachable"
docker exec aiq-postgres pg_isready -U aiq -d aiq_jobs
docker exec aiq-postgres pg_isready -U aiq -d aiq_checkpoints
```

Logs:

```bash
docker logs aiq-agent --tail 100
docker logs aiq-blueprint-ui --tail 100
docker logs aiq-postgres --tail 100
```

Stop:

```bash
cd deploy/compose
docker compose --env-file ../.env -f docker-compose.yaml down
```

Only remove volumes after explicit confirmation:

```bash
cd deploy/compose
docker compose --env-file ../.env -f docker-compose.yaml down -v
```

Rebuild:

```bash
cd deploy/compose
docker compose --env-file ../.env -f docker-compose.yaml build --no-cache
docker compose --env-file ../.env -f docker-compose.yaml up -d
```

## Mode D - Kubernetes / Helm

Use this when the user explicitly asks for Kubernetes, Helm, or production deployment. Read `deploy/helm/README.md` and the relevant chart values before acting.

Initial checks:

```bash
kubectl version --client
helm version
find deploy/helm -name Chart.yaml -maxdepth 4 -print
```

Do not guess cluster namespace, image registry, secrets, or ingress values. Inspect the target cluster and values files, then ask only for missing deployment choices.

## FRAG / Foundational RAG Mode

FRAG uses `configs/config_web_frag.yml` and requires a running RAG server and ingestor.

Check configuration:

```bash
grep -E '^(RAG_SERVER_URL|RAG_INGEST_URL)=' deploy/.env || true
```

Probe when values are set:

```bash
set -a
. deploy/.env
set +a
test -n "${RAG_SERVER_URL:-}" && curl -sf "$RAG_SERVER_URL/health" >/dev/null || true
test -n "${RAG_INGEST_URL:-}" && curl -sf "$RAG_INGEST_URL/health" >/dev/null || true
```

When AI-Q and RAG run as separate Docker Compose stacks, connect the AI-Q backend container to the RAG network after both stacks are up:

```bash
docker network connect nvidia-rag aiq-agent
```

If `aiq-agent` is recreated, repeat the network connection.

## Troubleshooting Checklist

1. Confirm the selected config file exists.
2. Confirm `deploy/.env` has required keys without printing values.
3. Check backend health: `curl -sf http://localhost:${PORT:-8000}/health`.
4. Check UI reachability: `curl -sf http://localhost:${FRONTEND_PORT:-3000}`.
5. Check logs for `aiq-agent`, `aiq-blueprint-ui`, and `aiq-postgres`.
6. For async-job failures, inspect `NAT_JOB_STORE_DB_URL`, `AIQ_CHECKPOINT_DB`, and PostgreSQL readiness.
7. For tool/search failures, verify the relevant provider key and selected config.
8. For FRAG failures, verify RAG URLs, RAG service health, and Docker network membership.

## Handoff To aiq-research

After verification, tell the user the AI-Q server URL. If the backend is on the default port, `aiq-research` can use its default `AIQ_SERVER_URL=http://localhost:8000`. Otherwise tell the user to set:

```bash
export AIQ_SERVER_URL="http://localhost:<PORT>"
```

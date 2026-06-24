# SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""DeepAgents skills and sandbox runtime support for deep research.

Provider-specific sandbox logic lives in the :mod:`sandbox` package (config,
registry, providers, artifacts). This module is the thin wiring layer: it composes
the routed backend, preloads skills, and owns the sandbox provider lifecycle for a
job. ``SandboxConfig`` is re-exported here for backward compatibility.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from deepagents.backends import CompositeBackend
from deepagents.backends import StateBackend
from deepagents.backends.protocol import EditResult
from deepagents.backends.protocol import ReadResult
from deepagents.backends.protocol import WriteResult
from pydantic import BaseModel
from pydantic import Field

from .sandbox import SandboxConfig
from .sandbox import SandboxProvider
from .sandbox import create_sandbox_backend
from .sandbox.artifacts import ArtifactManager
from .sandbox.artifacts import SqlArtifactStore
from .sandbox.config import DEFAULT_WORKDIR

__all__ = ["SkillsConfig", "SandboxConfig", "DeepAgentsRuntime"]

logger = logging.getLogger(__name__)

AGENT_DIR = Path(__file__).parent
BUILTIN_SKILLS_DIR = AGENT_DIR / "skills"
BUILTIN_SKILL_SOURCE = "/skills/"
SHARED_ROUTE = "/shared/"


class _PrefixedStateBackend(StateBackend):
    """StateBackend that re-prepends a route prefix on error messages.

    Why: deepagents' CompositeBackend strips the route prefix before delegating
    to the routed backend, then rewrites WriteResult.path back to the full path
    on success - but does NOT rewrite the path embedded in WriteResult.error /
    EditResult.error. The agent then sees an error referencing a path it never
    wrote to (e.g. ``/0_weather_data.txt`` instead of ``/shared/0_weather_data.txt``)
    and chases the phantom path via shell, which routes to a different backend.
    Restore the user-visible path here so error messages are consistent with
    what the agent actually invoked.
    """

    def __init__(self, route_prefix: str) -> None:
        super().__init__()
        self._prefix = route_prefix.rstrip("/")

    def _restore(self, key: str) -> str:
        if key.startswith("/"):
            return f"{self._prefix}{key}"
        return f"{self._prefix}/{key}"

    def _rewrite_error(self, error: str, file_path: str) -> str:
        return error.replace(file_path, self._restore(file_path))

    def write(self, file_path: str, content: str) -> WriteResult:
        result = super().write(file_path, content)
        if result.error and file_path in result.error:
            return WriteResult(error=self._rewrite_error(result.error, file_path))
        return result

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        result = super().edit(file_path, old_string, new_string, replace_all=replace_all)
        if result.error and file_path in result.error:
            return EditResult(error=self._rewrite_error(result.error, file_path))
        return result

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        result = super().read(file_path, offset=offset, limit=limit)
        if result.error and file_path in result.error:
            return ReadResult(error=self._rewrite_error(result.error, file_path))
        return result


class SkillsConfig(BaseModel):
    """Configuration for built-in DeepAgents skills."""

    enabled: bool = Field(default=False, description="Enable DeepAgents skills for the orchestrator")
    sources: tuple[str, ...] = Field(
        default=(BUILTIN_SKILL_SOURCE,),
        description="DeepAgents skill source paths. Defaults to the built-in deep researcher skills directory.",
    )

    @classmethod
    def enabled_builtin(cls) -> SkillsConfig:
        return cls(enabled=True)


class DeepAgentsRuntime:
    """Builds DeepAgents backend kwargs, preloads skills, and owns sandbox lifecycle."""

    def __init__(
        self,
        *,
        skills: SkillsConfig | None = None,
        sandbox: SandboxConfig | None = None,
        job_id: str | None = None,
        artifact_db_url: str | None = None,
        artifact_emit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.skills = skills or SkillsConfig()
        # A disabled sandbox is treated as no sandbox (no provider, no sandbox prompts).
        self.sandbox = sandbox if (sandbox is not None and sandbox.enabled) else None
        self.job_id = str(job_id) if job_id is not None else str(uuid4())
        self._backend: Any | None = None
        self._sandbox_provider: SandboxProvider | None = None
        self.artifact_manager: ArtifactManager | None = None

        if self.sandbox is not None:
            # Fail-fast: construct the provider and verify its capabilities now
            # (import guard + capability gate). No SDK sandbox is created yet; the
            # actual session is built lazily on first execute.
            self._sandbox_provider = create_sandbox_backend(self.sandbox, self.job_id)
            if self.sandbox.artifact_capture.enabled and artifact_db_url:
                self.artifact_manager = ArtifactManager(
                    job_id=self.job_id,
                    backend=self._sandbox_provider,
                    store=SqlArtifactStore(artifact_db_url),
                    config=self.sandbox.artifact_capture,
                    artifact_dir=self.artifact_dir,
                    emit=artifact_emit,
                )

    @property
    def skill_sources(self) -> list[str] | None:
        if not self.skills.enabled:
            return None
        return list(self.skills.sources)

    @property
    def builtin_skills_dir(self) -> Path:
        return BUILTIN_SKILLS_DIR

    @property
    def sandbox_provider(self) -> SandboxProvider | None:
        """The job-scoped provider backend, or None when no sandbox is configured."""
        return self._sandbox_provider

    @property
    def workdir(self) -> str:
        """Effective sandbox working directory, used to keep prompts/skills aligned."""
        return self.sandbox.workdir if self.sandbox is not None else DEFAULT_WORKDIR

    @property
    def artifact_dir(self) -> str:
        """Effective sandbox artifact directory where generated outputs are harvested from.

        Job-scoped (``<configured artifact_dir>/<job_id>``) so a persistent or shared
        sandbox (e.g. the OpenShell demo container) cannot leak one job's files into
        another job's harvest, and concurrent jobs never collide on the same directory.
        """
        base = self.sandbox.artifact_dir if self.sandbox is not None else f"{DEFAULT_WORKDIR}/aiq-artifacts"
        # job_id becomes a path segment, so keep only filename-safe characters: a crafted id
        # containing "/" or ".." must not be able to move the harvest root outside the base
        # (which the ArtifactManager then trusts as its confinement boundary).
        safe_job = "".join(c if (c.isalnum() or c in "-_") else "_" for c in self.job_id) or "job"
        return f"{base.rstrip('/')}/{safe_job}"

    @property
    def create_agent_kwargs(self) -> dict[str, Any]:
        backend = self.backend
        kwargs: dict[str, Any] = {"backend": backend}
        if self.skill_sources is not None:
            kwargs["skills"] = self.skill_sources
        return kwargs

    @property
    def backend(self) -> Any:
        """Return the concrete backend instance passed to DeepAgents."""
        if self._backend is not None:
            return self._backend

        default_backend: Any = self._sandbox_provider if self._sandbox_provider is not None else StateBackend()
        self._backend = CompositeBackend(
            default=default_backend,
            routes={
                BUILTIN_SKILL_SOURCE: _PrefixedStateBackend(BUILTIN_SKILL_SOURCE),
                SHARED_ROUTE: _PrefixedStateBackend(SHARED_ROUTE),
            },
        )
        return self._backend

    def prepare_state(self, state: Any) -> Any:
        """Preload built-in skills into state when using the StateBackend."""
        if not self.skills.enabled:
            return state

        files = dict(getattr(state, "files", None) or {})
        skill_files = _builtin_skill_state_files(self.workdir)
        for file_path, file_data in skill_files.items():
            files.setdefault(file_path, file_data)
        return state.model_copy(update={"files": files})

    def final_harvest(self) -> None:
        """Best-effort final artifact harvest before cleanup (terminal job path)."""
        manager = self.artifact_manager
        if manager is None:
            return
        try:
            manager.final_harvest()
        except Exception:
            logger.warning("Final artifact harvest failed for job %s", self.job_id, exc_info=True)

    def close(self) -> None:
        """Release the sandbox provider on a normal terminal job path (idempotent)."""
        provider = self._sandbox_provider
        if provider is not None:
            provider.close()

    def terminate(self) -> None:
        """Forcibly stop the sandbox on an interrupted job (cancel/timeout), idempotent.

        Unlike :meth:`close`, this interrupts a still-running ``execute`` instead of
        waiting for it, so a cancelled job does not keep burning sandbox resources.
        """
        provider = self._sandbox_provider
        if provider is not None:
            provider.terminate()


def _collect_builtin_skill_files() -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    if not BUILTIN_SKILLS_DIR.exists():
        return files

    for skill_dir in sorted(BUILTIN_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        if skill_dir.name.startswith(".") or skill_dir.name == "__pycache__":
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        files.append((f"{BUILTIN_SKILL_SOURCE}{skill_dir.name}/SKILL.md", skill_md.read_bytes()))
    return files


def _builtin_skill_state_files(workdir: str = DEFAULT_WORKDIR) -> dict[str, dict[str, str]]:
    timestamp = datetime.now().isoformat()
    files: dict[str, dict[str, str]] = {}
    for file_path, content in _collect_builtin_skill_files():
        text = content.decode("utf-8")
        if workdir != DEFAULT_WORKDIR:
            # Keep the skill's writable-dir references aligned with the active workdir.
            text = text.replace(DEFAULT_WORKDIR, workdir)
        files[_strip_builtin_skill_source(file_path)] = {
            "content": text,
            "encoding": "utf-8",
            "created_at": timestamp,
            "modified_at": timestamp,
        }
    return files


def _strip_builtin_skill_source(file_path: str) -> str:
    if file_path.startswith(BUILTIN_SKILL_SOURCE):
        return "/" + file_path[len(BUILTIN_SKILL_SOURCE) :].lstrip("/")
    return file_path

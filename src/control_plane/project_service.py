from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.errors import ConflictError, NotFoundError, RepositoryResolutionError
from control_plane.models import Issue, Project, ProjectWorkflowSnapshot, utc_now
from control_plane.schemas import ProjectCreate, ProjectPatch, ProjectView, WorkflowSnapshotView
from symphony_windows.skill import validate_skill_package
from symphony_windows.workflow import Workflow, WorkflowError, load_workflow


COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")


class ProjectService:
    def __init__(self, session: AsyncSession, token: str | None = None) -> None:
        self.session = session
        self.token = token or "local-project-validation-token"

    async def create(self, command: ProjectCreate) -> ProjectView:
        repository = self._repository_path(command.repository_path)
        async with self.session.begin():
            if await self.session.scalar(select(Project.id).where(Project.key == command.key)):
                raise ConflictError(f"project key already exists: {command.key}")
            if await self.session.scalar(select(Project.id).where(Project.repository_path == str(repository))):
                raise ConflictError(f"project repository already exists: {repository}")
            if command.bootstrap_workflow:
                self._bootstrap_workflow(repository, command.workflow_path)
            project = Project(
                **command.model_dump(exclude={"repository_path", "bootstrap_workflow"}),
                repository_path=str(repository),
            )
            self.session.add(project)
            await self.session.flush()
        await self.validate(project.id)
        return await self.get(project.id)

    async def list(self) -> list[ProjectView]:
        rows = (await self.session.scalars(select(Project).order_by(Project.name, Project.key))).all()
        return [await self._view(row) for row in rows]

    async def get(self, project_id: str) -> ProjectView:
        project = await self.session.get(Project, project_id)
        if project is None:
            raise NotFoundError(f"project not found: {project_id}")
        return await self._view(project)

    async def patch(self, project_id: str, command: ProjectPatch) -> ProjectView:
        values = command.model_dump(exclude_none=True)
        if "repository_path" in values:
            values["repository_path"] = str(self._repository_path(values["repository_path"]))
        async with self.session.begin():
            project = await self._require(project_id)
            for key, value in values.items():
                setattr(project, key, value)
            project.updated_at = utc_now()
            if not project.enabled:
                project.status = "disabled"
                project.validation_error = None
        if values.get("enabled", project.enabled):
            await self.validate(project_id)
        return await self.get(project_id)

    async def delete(self, project_id: str) -> None:
        async with self.session.begin():
            project = await self._require(project_id)
            await self._assert_no_issues(project_id)
            await self.session.delete(project)

    async def assert_deletable(self, project_id: str) -> None:
        await self._require(project_id)
        await self._assert_no_issues(project_id)

    async def _assert_no_issues(self, project_id: str) -> None:
        issue_id = await self.session.scalar(
            select(Issue.id).where(Issue.project_id == project_id).limit(1)
        )
        if issue_id is not None:
            raise ConflictError(
                "project has Issues and cannot be deleted; keep it disabled if historical records are required"
            )

    async def validate(self, project_id: str) -> ProjectView:
        project = await self._require(project_id)
        source_commit = "0" * 40
        source = ""
        revision = hashlib.sha256(source.encode("utf-8")).hexdigest()
        try:
            repository = self._repository_path(project.repository_path)
            workflow_path = self._contained_file(repository, project.workflow_path)
            source = workflow_path.read_text(encoding="utf-8")
            revision = hashlib.sha256(source.encode("utf-8")).hexdigest()
            source_commit = await self._git(repository, "rev-parse", "--verify", "HEAD^{commit}")
            if not COMMIT_PATTERN.fullmatch(source_commit):
                raise RepositoryResolutionError("repository HEAD is not a full commit SHA")
            await self._git(repository, "rev-parse", "--verify", f"{project.default_branch}^{{commit}}")
            tracked = await self._git(
                repository,
                "status",
                "--porcelain",
                "--",
                project.workflow_path,
                "AGENTS.md",
                ".codex/skills",
            )
            if tracked:
                if f"?? {project.workflow_path.lower()}" in tracked.replace("\\", "/"):
                    raise RepositoryResolutionError(
                        f"default {project.workflow_path} was generated; review and commit it before validation"
                    )
                raise RepositoryResolutionError("WORKFLOW.md, AGENTS.md and .codex/skills must be committed before validation")
            workflow = load_workflow(
                workflow_path,
                token_override=self.token,
                worker_id_override=f"project-validator:{project.key}",
                project_id_override=project.id,
            )
            self._validate_skills(repository)
            parsed = self._workflow_snapshot(workflow)
            parsed["project_assets"] = self._asset_manifest(repository)
        except (OSError, TimeoutError, WorkflowError, RepositoryResolutionError) as error:
            await self.session.rollback()
            async with self.session.begin():
                current = await self._require(project_id)
                failed = await self.session.scalar(
                    select(ProjectWorkflowSnapshot).where(
                        ProjectWorkflowSnapshot.project_id == project_id,
                        ProjectWorkflowSnapshot.source_commit == source_commit,
                        ProjectWorkflowSnapshot.workflow_revision == revision,
                        ProjectWorkflowSnapshot.status == "invalid",
                    )
                )
                if failed is None:
                    failed = ProjectWorkflowSnapshot(
                        project_id=project_id,
                        source_commit=source_commit,
                        workflow_revision=revision,
                        workflow_content=source,
                        parsed_config={},
                        status="invalid",
                        validation_error=str(error),
                    )
                    self.session.add(failed)
                else:
                    failed.status = "invalid"
                    failed.validation_error = str(error)
                current.status = "invalid" if current.enabled else "disabled"
                current.validation_error = str(error)
                current.updated_at = utc_now()
            return await self.get(project_id)

        await self.session.rollback()
        async with self.session.begin():
            current = await self._require(project_id)
            snapshot = await self.session.scalar(
                select(ProjectWorkflowSnapshot).where(
                    ProjectWorkflowSnapshot.project_id == project_id,
                    ProjectWorkflowSnapshot.source_commit == source_commit,
                    ProjectWorkflowSnapshot.workflow_revision == revision,
                    ProjectWorkflowSnapshot.status == "valid",
                )
            )
            if snapshot is None:
                snapshot = ProjectWorkflowSnapshot(
                    project_id=project_id,
                    source_commit=source_commit,
                    workflow_revision=revision,
                    workflow_content=source,
                    parsed_config=parsed,
                    status="valid",
                )
                self.session.add(snapshot)
                await self.session.flush()
            current.current_snapshot_id = snapshot.id
            current.status = "available" if current.enabled else "disabled"
            current.validation_error = None
            current.updated_at = utc_now()
        return await self.get(project_id)

    async def snapshots(self, project_id: str) -> list[WorkflowSnapshotView]:
        await self._require(project_id)
        rows = (
            await self.session.scalars(
                select(ProjectWorkflowSnapshot)
                .where(ProjectWorkflowSnapshot.project_id == project_id)
                .order_by(ProjectWorkflowSnapshot.created_at.desc())
            )
        ).all()
        return [WorkflowSnapshotView.model_validate(row) for row in rows]

    async def _require(self, project_id: str) -> Project:
        project = await self.session.get(Project, project_id)
        if project is None:
            raise NotFoundError(f"project not found: {project_id}")
        return project

    async def _view(self, project: Project) -> ProjectView:
        snapshot = await self.session.get(ProjectWorkflowSnapshot, project.current_snapshot_id) if project.current_snapshot_id else None
        return ProjectView(
            **{column.name: getattr(project, column.name) for column in Project.__table__.columns},
            current_snapshot=WorkflowSnapshotView.model_validate(snapshot) if snapshot else None,
        )

    @staticmethod
    def _repository_path(value: str) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise RepositoryResolutionError("project repository path must be absolute")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise RepositoryResolutionError("project repository path does not exist") from error
        if not resolved.is_dir():
            raise RepositoryResolutionError("project repository path must be a directory")
        return resolved

    @staticmethod
    def _bootstrap_workflow(repository: Path, relative_value: str) -> bool:
        relative = PurePosixPath(relative_value.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise RepositoryResolutionError("workflow_path must stay inside the project repository")
        target = repository.joinpath(*relative.parts)
        resolved_parent = target.parent.resolve(strict=False)
        if not resolved_parent.is_relative_to(repository):
            raise RepositoryResolutionError("workflow_path must stay inside the project repository")
        if target.exists():
            return False
        template = Path(__file__).with_name("templates") / "default-workflow.md"
        resolved_parent.mkdir(parents=True, exist_ok=True)
        target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        return True

    @staticmethod
    def _contained_file(repository: Path, relative_value: str) -> Path:
        relative = PurePosixPath(relative_value.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise RepositoryResolutionError("workflow_path must stay inside the project repository")
        path = (repository / Path(*relative.parts)).resolve(strict=True)
        if not path.is_relative_to(repository) or not path.is_file():
            raise RepositoryResolutionError("WORKFLOW.md must be a file inside the project repository")
        return path

    @staticmethod
    def _validate_skills(repository: Path) -> None:
        root = repository / ".codex" / "skills"
        if not root.exists():
            return
        if root.is_symlink() or not root.is_dir():
            raise RepositoryResolutionError(".codex/skills must be a real directory")
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_relative_to(repository):
            raise RepositoryResolutionError(".codex/skills must stay inside the project repository")
        packages = {}
        for skill_file in root.glob("*/SKILL.md"):
            resolved = skill_file.resolve(strict=True)
            if not resolved.is_relative_to(resolved_root):
                raise RepositoryResolutionError(f"skill escapes project repository: {skill_file}")
            packages[skill_file.parent.name] = validate_skill_package(skill_file.parent.name, skill_file.parent)
        for name, package in packages.items():
            missing = set(package.required_skills) - set(packages)
            if missing:
                raise RepositoryResolutionError(f"skill {name} requires missing repository Skills: {', '.join(sorted(missing))}")

    @staticmethod
    def _asset_manifest(repository: Path) -> dict[str, Any]:
        agents = repository / "AGENTS.md"
        agents_entry = None
        if agents.is_file():
            agents_entry = {
                "path": "AGENTS.md",
                "sha256": hashlib.sha256(agents.read_bytes()).hexdigest(),
            }
        skills: list[dict[str, str]] = []
        root = repository / ".codex" / "skills"
        if root.is_dir():
            for instruction in sorted(root.glob("*/SKILL.md")):
                skill_root = instruction.parent
                digest = hashlib.sha256()
                for path in sorted(item for item in skill_root.rglob("*") if item.is_file()):
                    resolved = path.resolve(strict=True)
                    if not resolved.is_relative_to(root.resolve()):
                        raise RepositoryResolutionError(f"skill file escapes project repository: {path}")
                    digest.update(path.relative_to(skill_root).as_posix().encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(path.read_bytes())
                    digest.update(b"\0")
                skills.append({"name": skill_root.name, "path": instruction.relative_to(repository).as_posix(), "sha256": digest.hexdigest()})
        return {"agents_md": agents_entry, "skills": skills}

    @staticmethod
    async def _git(repository: Path, *arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(repository), *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        if process.returncode != 0:
            raise RepositoryResolutionError(stderr.decode(errors="replace").strip() or "invalid Git repository")
        return stdout.decode(errors="replace").strip().lower()

    @staticmethod
    def _workflow_snapshot(workflow: Workflow) -> dict[str, Any]:
        return {
            "tracker": {
                "kind": workflow.tracker.kind,
                "endpoint": workflow.tracker.endpoint,
                "required_labels": list(workflow.tracker.required_labels),
                "active_states": list(workflow.tracker.active_states),
                "terminal_states": list(workflow.tracker.terminal_states),
            },
            "polling_interval_ms": workflow.polling_interval_ms,
            "workspace": {"root": str(workflow.workspace.root)},
            "agent": workflow.agent.snapshot(),
            "codex": {
                "command": workflow.codex.command,
                "approval_policy": workflow.codex.approval_policy,
                "turn_sandbox_policy": workflow.codex.turn_sandbox_policy,
                "turn_timeout_ms": workflow.codex.turn_timeout_ms,
                "stall_timeout_ms": workflow.codex.stall_timeout_ms,
                "isolate_user_home": workflow.codex.isolate_user_home,
            },
        }

"""UI-independent state and request identity for one folder workspace."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class WorkspaceRequest:
    """Identifies a result that may update one workspace's UI."""

    workspace_id: str
    directory_generation: int


@dataclass
class WorkspaceState:
    """State owned by a tab, deliberately independent of Qt widgets."""

    directory: Path
    workspace_id: str = field(default_factory=lambda: uuid4().hex)
    directory_generation: int = 0
    thumbnail_size: int = 1
    current_photo: Path | None = None
    closed: bool = False

    def begin_directory(self, directory: Path) -> WorkspaceRequest:
        self.directory = directory
        self.directory_generation += 1
        return self.request()

    def request(self) -> WorkspaceRequest:
        return WorkspaceRequest(self.workspace_id, self.directory_generation)

    def accepts(self, request: WorkspaceRequest) -> bool:
        return not self.closed and request == self.request()

    def close(self) -> None:
        self.closed = True

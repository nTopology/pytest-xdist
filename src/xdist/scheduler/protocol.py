from __future__ import annotations

from typing import Any
from typing import Protocol
from typing import Sequence

from xdist.workermanage import WorkerController


class Scheduling(Protocol):
    node2pending: Any
    do_resched: bool
    pending: list[int]
    dist_groups: dict[str, Any]
    pending_groups: list[str]

    @property
    def nodes(self) -> list[WorkerController]: ...

    @property
    def collection_is_completed(self) -> bool: ...

    @property
    def tests_finished(self) -> bool: ...

    @property
    def has_pending(self) -> bool: ...

    def add_node(self, node: WorkerController) -> None: ...

    def add_node_collection(
        self,
        node: WorkerController,
        collection: Sequence[str],
    ) -> None: ...

    def check_schedule(self, node: WorkerController, duration: float = 0, from_dsession: bool = False) -> None: ...

    def mark_test_complete(
        self,
        node: WorkerController,
        item_index: int,
        duration: float = 0,
    ) -> None: ...

    def mark_test_pending(self, item: str) -> None: ...

    def remove_pending_tests_from_node(
        self,
        node: WorkerController,
        indices: Sequence[int],
    ) -> None: ...

    def remove_node(self, node: WorkerController) -> str | None: ...

    def schedule(self) -> None: ...

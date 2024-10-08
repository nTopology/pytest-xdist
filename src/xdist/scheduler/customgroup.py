from __future__ import annotations

from itertools import cycle
from typing import Any
from typing import Sequence

import pytest

from xdist.remote import Producer
from xdist.report import report_collection_diff
from xdist.workermanage import parse_spec_config
from xdist.workermanage import WorkerController


class CustomGroup:
    """Implement grouped load scheduling across a variable number of nodes.

    This distributes tests into groups based on the presence of xdist_custom pytest marks.
    Groups are ran sequentially with tests within each group running in parallel.
    The number of workers assigned to each group is based on the xdist_custom pytest mark.
    Tests without the xdist_custom pytest mark are assigned to a "default" group and run
    using all available workers.

    Example:
        Consider 12 pytest test cases.
            - 4 test cases are marked with @pytest.mark.xdist_custom(name="low_4")
            - 2 test cases are marked with @pytest.mark.xdist_custom(name="med_2")
            - 2 test cases are marked with @pytest.mark.xdist_custom(name="high_1")
            - 4 test cases are not marked with a xdist_custom mark.
        Consider the pytest run was initiated with 4 workers (-n 4)
            - The 4 test cases marked with "low_4" would run in a group using 4 workers
            - The 2 test cases marked with "med_2" would run in a group using 2 workers
            - The 2 test cases marked with "high_1" would run in a group with 1 worker
            - The 4 unmarked test cases would run in a group using 4 workers.
        Only one group would run at any given time. For example, while the "high_1" tests are executing,
        the other pending test groups would not be scheduled or excuting. The order in which groups
        are executed is variable. For example, "high_1" may execute first, or it may execute second, etc.
        If a group pytest mark specifies more workers than the pytest run is initialized with the
        number of workers the run was initialized with will be used instead (-n argument is a maximum).

    Attributes::

    :terminal: Terminal reporter for writing terminal output

    :numnodes: The expected number of nodes taking part.  The actual
       number of nodes will vary during the scheduler's lifetime as
       nodes are added by the DSession as they are brought up and
       removed either because of a dead node or normal shutdown.  This
       number is primarily used to know when the initial collection is
       completed.

    :node2collection: Map of nodes and their test collection.  All
       collections should always be identical.

    :node2pending: Map of nodes and the indices of their pending
       tests.  The indices are an index into ``.pending`` (which is
       identical to their own collection stored in
       ``.node2collection``).

    :pending: List of indices of globally pending tests.  These are
       tests which have not yet been allocated to a chunk for a node
       to process.

    :collection: The one collection once it is validated to be
       identical between all the nodes.  It is initialised to None
       until ``.schedule()`` is called.

    :log: A py.log.Producer instance.

    :config: Config object, used for handling hooks.

    :dist_groups: Execution groups. Updated based on xdist_custom pytest marks.
        Maps group names to tests, test indices, pending indices, and stores the number of workers to use
        for that test execution group.

    :pending_groups: List of dist_group keys that are pending

    :is_first_time: Boolean to track whether we have called schedule() before or not

    :do_resched: Boolean to track whether we should schedule another distribution group.
        Accessed in dsession.py
    """

    def __init__(self, config: pytest.Config, log: Producer | None = None) -> None:
        self.terminal = config.pluginmanager.getplugin("terminalreporter")
        self.numnodes = len(parse_spec_config(config))
        self.node2collection: dict[WorkerController, list[str]] = {}
        self.node2pending: dict[WorkerController, list[int]] = {}
        self.pending: list[int] = []
        self.collection: list[str] | None = None
        if log is None:
            self.log = Producer("loadsched")
        else:
            self.log = log.loadsched
        self.config = config
        self.dist_groups: dict[str, Any] = {}
        self.pending_groups: list[str] = []
        self.is_first_time: bool = True
        self.do_resched: bool = False

    @property
    def nodes(self) -> list[WorkerController]:
        """A list of all nodes in the scheduler."""
        return list(self.node2pending.keys())

    @property
    def collection_is_completed(self) -> bool:
        """Boolean indication initial test collection is complete.

        This is a boolean indicating all initial participating nodes
        have finished collection.  The required number of initial
        nodes is defined by ``.numnodes``.
        """
        return len(self.node2collection) >= self.numnodes

    @property
    def tests_finished(self) -> bool:
        """Return True if all tests have been executed by the nodes."""
        if not self.collection_is_completed:
            return False
        if self.pending:
            return False
        for pending in self.node2pending.values():
            if len(pending) >= 2:
                return False
        return True

    @property
    def has_pending(self) -> bool:
        """Return True if there are pending test items.

        This indicates that collection has finished and nodes are
        still processing test items, so this can be thought of as
        "the scheduler is active".
        """
        if self.pending:
            return True
        for pending in self.node2pending.values():
            if pending:
                return True
        return False

    def add_node(self, node: WorkerController) -> None:
        """Add a new node to the scheduler.

        From now on the node will be allocated chunks of tests to
        execute.

        Called by the ``DSession.worker_workerready`` hook when it
        successfully bootstraps a new node.
        """
        assert node not in self.node2pending
        self.node2pending[node] = []

    def add_node_collection(
        self, node: WorkerController, collection: Sequence[str]
    ) -> None:
        """Add the collected test items from a node.

        The collection is stored in the ``.node2collection`` map.
        Called by the ``DSession.worker_collectionfinish`` hook.
        """
        assert node in self.node2pending
        if self.collection_is_completed:
            # A new node has been added later, perhaps an original one died.
            # .schedule() should have
            # been called by now
            assert self.collection
            if collection != self.collection:
                other_node = next(iter(self.node2collection.keys()))
                msg = report_collection_diff(
                    self.collection, collection, other_node.gateway.id, node.gateway.id
                )
                self.log(msg)
                return
        self.node2collection[node] = list(collection)

    def mark_test_complete(
        self, node: WorkerController, item_index: int, duration: float = 0
    ) -> None:
        """Mark test item as completed by node.

        The duration it took to execute the item is used as a hint to
        the scheduler.

        This is called by the ``DSession.worker_testreport`` hook.
        """
        self.node2pending[node].remove(item_index)
        self.check_schedule(node, duration=duration)

    def mark_test_pending(self, item: str) -> None:
        assert self.collection is not None
        self.pending.insert(
            0,
            self.collection.index(item),
        )
        for node in self.node2pending:
            self.check_schedule(node)

    def remove_pending_tests_from_node(
        self,
        node: WorkerController,
        indices: Sequence[int],
    ) -> None:
        raise NotImplementedError()

    def check_schedule(
        self, node: WorkerController, duration: float = 0, from_dsession: bool = False
    ) -> None:
        """Maybe schedule new items on the node.

        If there are any globally pending nodes left then this will
        check if the given node should be given any more tests.  The
        ``duration`` of the last test is optionally used as a
        heuristic to influence how many tests the node is assigned.
        """
        if node.shutting_down:
            self.report_line(
                f"[-] [csg] {node.workerinput['workerid']} is already shutting down"
            )
            return

        if self.pending:
            any_working = False
            for node in self.nodes:
                if len(self.node2pending[node]) not in [0, 1]:
                    any_working = True

            if not any_working and from_dsession:
                if self.pending_groups:
                    dist_group_key = self.pending_groups.pop(0)
                    dist_group = self.dist_groups[dist_group_key]
                    nodes = cycle(self.nodes[0 : dist_group["group_workers"]])
                    schedule_log: dict[str, Any] = {
                        n.gateway.id: []
                        for n in self.nodes[0 : dist_group["group_workers"]]
                    }
                    for _ in range(len(dist_group["test_indices"])):
                        n = next(nodes)
                        # needs cleaner way to be identified
                        tests_per_node = self.dist_groups[dist_group_key][
                            "pending_indices"
                        ][:1]
                        schedule_log[n.gateway.id].extend(tests_per_node)

                        self._send_tests_group(n, 1, dist_group_key)
                    del self.dist_groups[dist_group_key]
                    message = (
                        f"\n[-] [csg] check_schedule: processed scheduling for {dist_group_key}:"
                        f" {' '.join([f'{nid} ({len(nt)})' for nid,nt in schedule_log.items()])}"
                    )
                    self.report_line(message)

        else:
            pending = self.node2pending.get(node, [])
            if len(pending) < 2:
                self.report_line(
                    f"[-] [csg] Shutting down {node.workerinput['workerid']} because only one case is pending"
                )
                node.shutdown()

        self.log("num items waiting for node:", len(self.pending))

    def remove_node(self, node: WorkerController) -> str | None:
        """Remove a node from the scheduler.

        This should be called either when the node crashed or at
        shutdown time.  In the former case any pending items assigned
        to the node will be re-scheduled.  Called by the
        ``DSession.worker_workerfinished`` and
        ``DSession.worker_errordown`` hooks.

        Return the item which was being executing while the node
        crashed or None if the node has no more pending items.

        """
        pending = self.node2pending.pop(node)
        if not pending:
            return None

        # The node crashed, reassing pending items
        assert self.collection is not None
        crashitem = self.collection[pending.pop(0)]
        self.pending.extend(pending)
        for node in self.node2pending:
            self.check_schedule(node)
        return crashitem

    def schedule(self) -> None:
        """Initiate distribution of the test collection.

        Initiate scheduling of the items across the nodes.  If this
        gets called again later it behaves the same as calling
        ``.check_schedule()`` on all nodes so that newly added nodes
        will start to be used.

        This is called by the ``DSession.worker_collectionfinish`` hook
        if ``.collection_is_completed`` is True.
        """
        assert self.collection_is_completed

        # Initial distribution already happened, reschedule on all nodes
        if self.collection is not None:
            for node in self.nodes:
                self.check_schedule(node)
            return

        # XXX allow nodes to have different collections
        if not self._check_nodes_have_same_collection():
            self.log("**Different tests collected, aborting run**")
            return

        # Collections are identical, create the index of pending items.
        self.collection = next(iter(self.node2collection.values()))
        self.pending[:] = range(len(self.collection))
        if not self.collection:
            return

        dist_groups: dict[str, dict[Any, Any]] = {}

        if self.is_first_time:
            for i, test in enumerate(self.collection):
                if "@" in test:
                    group_mark = test.split("@")[-1]
                    group_workers = int(group_mark.split("_")[-1])
                    if group_workers > len(self.nodes):
                        # We can only distribute across as many nodes as we have available
                        # If a group requests more, we fallback to our actual max
                        group_workers = len(self.nodes)
                else:
                    group_mark = "default"
                    group_workers = len(self.nodes)
                existing_tests = dist_groups.get(group_mark, {}).get("tests", [])
                existing_tests.append(test)
                existing_indices = dist_groups.get(group_mark, {}).get(
                    "test_indices", []
                )
                existing_indices.append(i)

                dist_groups[group_mark] = {
                    "tests": existing_tests,
                    "group_workers": group_workers,
                    "test_indices": existing_indices,
                    "pending_indices": existing_indices,
                }
            self.dist_groups = dist_groups
            self.pending_groups = list(dist_groups.keys())
            self.is_first_time = False
        else:
            for node in self.nodes:
                self.check_schedule(node)

        if not self.pending_groups:
            return
        dist_group_key = self.pending_groups.pop(0)
        dist_group = self.dist_groups[dist_group_key]
        nodes = cycle(self.nodes[0 : dist_group["group_workers"]])
        schedule_log: dict[str, Any] = {
            n.gateway.id: [] for n in self.nodes[0 : dist_group["group_workers"]]
        }
        for _ in range(len(dist_group["test_indices"])):
            n = next(nodes)
            # needs cleaner way to be identified
            tests_per_node = self.dist_groups[dist_group_key]["pending_indices"][:1]
            schedule_log[n.gateway.id].extend(tests_per_node)
            self._send_tests_group(n, 1, dist_group_key)
        del self.dist_groups[dist_group_key]
        message = (
            "\n[-] [csg] schedule: processed scheduling for "
            f"{dist_group_key}: {' '.join([f'{nid} ({len(nt)})' for nid, nt in schedule_log.items()])}"
        )
        self.report_line(message)

    def _send_tests(self, node: WorkerController, num: int) -> None:
        tests_per_node = self.pending[:num]
        if tests_per_node:
            del self.pending[:num]
            self.node2pending[node].extend(tests_per_node)
            node.send_runtest_some(tests_per_node)

    def _send_tests_group(
        self, node: WorkerController, num: int, dist_group_key: str
    ) -> None:
        tests_per_node = self.dist_groups[dist_group_key]["pending_indices"][:num]
        if tests_per_node:
            del self.dist_groups[dist_group_key]["pending_indices"][:num]
            for test_index in tests_per_node:
                self.pending.remove(test_index)
            self.node2pending[node].extend(tests_per_node)
            node.send_runtest_some(tests_per_node)

    def _check_nodes_have_same_collection(self) -> bool:
        """Return True if all nodes have collected the same items.

        If collections differ, this method returns False while logging
        the collection differences and posting collection errors to
        pytest_collectreport hook.
        """
        node_collection_items = list(self.node2collection.items())
        first_node, col = node_collection_items[0]
        same_collection = True
        for node, collection in node_collection_items[1:]:
            msg = report_collection_diff(
                col, collection, first_node.gateway.id, node.gateway.id
            )
            if msg:
                same_collection = False
                self.log(msg)
                if self.config is not None:
                    rep = pytest.CollectReport(
                        nodeid=node.gateway.id,
                        outcome="failed",
                        longrepr=msg,
                        result=[],
                    )
                    self.config.hook.pytest_collectreport(report=rep)

        return same_collection

    def report_line(self, line: str) -> None:
        if self.terminal and self.config.option.verbose >= 0:
            self.terminal.write_line(line)

from __future__ import annotations

from enum import auto
from enum import Enum
from queue import Empty
from queue import Queue
import sys
from typing import Any
from typing import Sequence
import warnings

import execnet
import pytest

from xdist.remote import Producer
from xdist.remote import WorkerInfo
from xdist.scheduler import EachScheduling
from xdist.scheduler import LoadFileScheduling
from xdist.scheduler import LoadGroupScheduling
from xdist.scheduler import LoadScheduling
from xdist.scheduler import LoadScopeScheduling
from xdist.scheduler import Scheduling
from xdist.scheduler import WorkStealingScheduling
from xdist.scheduler import CustomGroup
from xdist.workermanage import NodeManager
from xdist.workermanage import WorkerController


class Interrupted(KeyboardInterrupt):
    """signals an immediate interruption."""


class DSession:
    """A pytest plugin which runs a distributed test session.

    At the beginning of the test session this creates a NodeManager
    instance which creates and starts all nodes.  Nodes then emit
    events processed in the pytest_runtestloop hook using the worker_*
    methods.

    Once a node is started it will automatically start running the
    pytest mainloop with some custom hooks.  This means a node
    automatically starts collecting tests.  Once tests are collected
    it will wait for instructions.
    """

    shouldstop: bool | str

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.log = Producer("dsession", enabled=config.option.debug)
        self.nodemanager: NodeManager | None = None
        self.sched: Scheduling | None = None
        self.shuttingdown = False
        self.countfailures = 0
        self.maxfail: int = config.getvalue("maxfail")
        self.queue: Queue[tuple[str, dict[str, Any]]] = Queue()
        self._session: pytest.Session | None = None
        self._failed_collection_errors: dict[object, bool] = {}
        self._active_nodes: set[WorkerController] = set()
        self._failed_nodes_count = 0
        self._max_worker_restart = get_default_max_worker_restart(self.config)
        # summary message to print at the end of the session
        self._summary_report: str | None = None
        self.terminal = config.pluginmanager.getplugin("terminalreporter")
        if self.terminal:
            self.trdist = TerminalDistReporter(config)
            config.pluginmanager.register(self.trdist, "terminaldistreporter")

    @property
    def session_finished(self) -> bool:
        """Return True if the distributed session has finished.

        This means all nodes have executed all test items.  This is
        used by pytest_runtestloop to break out of its loop.
        """
        return bool(self.shuttingdown and not self._active_nodes)

    def report_line(self, line: str) -> None:
        if self.terminal and self.config.option.verbose >= 0:
            self.terminal.write_line(line)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionstart(self, session: pytest.Session) -> None:
        """Creates and starts the nodes.

        The nodes are setup to put their events onto self.queue.  As
        soon as nodes start they will emit the worker_workerready event.
        """
        self.nodemanager = NodeManager(self.config)
        nodes = self.nodemanager.setup_nodes(putevent=self.queue.put)
        self._active_nodes.update(nodes)
        self._session = session

    @pytest.hookimpl
    def pytest_sessionfinish(self) -> None:
        """Shutdown all nodes."""
        nm = getattr(self, "nodemanager", None)  # if not fully initialized
        if nm is not None:
            nm.teardown_nodes()
        self._session = None

    @pytest.hookimpl
    def pytest_collection(self) -> bool:
        # prohibit collection of test items in controller process
        return True

    @pytest.hookimpl(trylast=True)
    def pytest_xdist_make_scheduler(
        self,
        config: pytest.Config,
        log: Producer,
    ) -> Scheduling | None:
        dist = config.getvalue("dist")
        if dist == "each":
            return EachScheduling(config, log)
        if dist == "load":
            return LoadScheduling(config, log)
        if dist == "loadscope":
            return LoadScopeScheduling(config, log)
        if dist == "loadfile":
            return LoadFileScheduling(config, log)
        if dist == "loadgroup":
            return LoadGroupScheduling(config, log)
        if dist == "worksteal":
            return WorkStealingScheduling(config, log)
        if dist == "customgroup":
            return CustomGroup(config, log)
        return None

    @pytest.hookimpl
    def pytest_runtestloop(self) -> bool:
        self.sched = self.config.hook.pytest_xdist_make_scheduler(
            config=self.config, log=self.log
        )
        assert self.sched is not None

        self.shouldstop = False
        pending_exception = None
        while not self.session_finished:
            self.loop_once()
            if self.shouldstop:
                self.triggershutdown()
                pending_exception = Interrupted(str(self.shouldstop))
        if pending_exception:
            raise pending_exception
        return True

    def loop_once(self) -> None:
        """Process one callback from one of the workers."""
        x = 0
        while 1:
            if not self._active_nodes:
                # If everything has died stop looping
                self.triggershutdown()
                raise RuntimeError("Unexpectedly no active workers available")
            try:
                eventcall = self.queue.get(timeout=2.0)
                break
            except Empty:
                x += 1
                self.terminal.write_line(f"Here! iteration: {x}\n{self.sched.node2pending}\n\n")
                all_one = True
                for node in self.sched.nodes:
                    if len(self.sched.node2pending[node]) not in [0, 1]:
                        all_one = False
                if all_one:
                    self.terminal.write_line(f"Calling check_schedule")
                    self.sched.check_schedule(self.sched.nodes[0], 1.0, True)
                #breakpoint()
                continue
        callname, kwargs = eventcall
        # self.terminal.write_line(f"Got callname: {callname}")
        assert callname, kwargs
        method = "worker_" + callname
        call = getattr(self, method)
        self.log("calling method", method, kwargs)
        call(**kwargs)
        assert self.sched is not None
        if self.sched.tests_finished:
            self.triggershutdown()

    #
    # callbacks for processing events from workers
    #

    def worker_workerready(
        self,
        node: WorkerController,
        workerinfo: WorkerInfo,
    ) -> None:
        """Emitted when a node first starts up.

        This adds the node to the scheduler, nodes continue with
        collection without any further input.
        """
        node.workerinfo = workerinfo
        node.workerinfo["id"] = node.gateway.id
        node.workerinfo["spec"] = node.gateway.spec

        self.config.hook.pytest_testnodeready(node=node)
        if self.shuttingdown:
            node.shutdown()
        else:
            assert self.sched is not None
            self.sched.add_node(node)

    def worker_workerfinished(self, node: WorkerController) -> None:
        """Emitted when node executes its pytest_sessionfinish hook.

        Removes the node from the scheduler.

        The node might not be in the scheduler if it had not emitted
        workerready before shutdown was triggered.
        """
        self.config.hook.pytest_testnodedown(node=node, error=None)
        if node.workeroutput["exitstatus"] == 2:  # keyboard-interrupt
            self.shouldstop = f"{node} received keyboard-interrupt"
            self.worker_errordown(node, "keyboard-interrupt")
            return
        shouldfail = node.workeroutput["shouldfail"]
        shouldstop = node.workeroutput["shouldstop"]
        for shouldx in [shouldfail, shouldstop]:
            if shouldx:
                if not self.shouldstop:
                    self.shouldstop = shouldx
                break
        else:
            assert self.sched is not None
            if node in self.sched.nodes:
                crashitem = self.sched.remove_node(node)
                assert not crashitem, (crashitem, node)
        self._active_nodes.remove(node)

    def worker_internal_error(
        self, node: WorkerController, formatted_error: str
    ) -> None:
        """
        pytest_internalerror() was called on the worker.

        pytest_internalerror() arguments are an excinfo and an excrepr, which can't
        be serialized, so we go with a poor man's solution of raising an exception
        here ourselves using the formatted message.
        """
        self._active_nodes.remove(node)
        try:
            assert False, formatted_error
        except AssertionError:
            excinfo = pytest.ExceptionInfo.from_current()
            excrepr = excinfo.getrepr()
            self.config.hook.pytest_internalerror(excrepr=excrepr, excinfo=excinfo)

    def worker_errordown(self, node: WorkerController, error: object | None) -> None:
        """Emitted by the WorkerController when a node dies."""
        self.config.hook.pytest_testnodedown(node=node, error=error)
        assert self.sched is not None
        try:
            crashitem = self.sched.remove_node(node)
        except KeyError:
            pass
        else:
            if crashitem:
                self.handle_crashitem(crashitem, node)

        self._failed_nodes_count += 1
        maximum_reached = (
            self._max_worker_restart is not None
            and self._failed_nodes_count > self._max_worker_restart
        )
        if maximum_reached:
            if self._max_worker_restart == 0:
                msg = f"worker {node.gateway.id} crashed and worker restarting disabled"
            else:
                msg = f"maximum crashed workers reached: {self._max_worker_restart}"
            self._summary_report = msg
            self.report_line("\n" + msg)
            self.triggershutdown()
        else:
            self.report_line("\nreplacing crashed worker %s" % node.gateway.id)
            self.shuttingdown = False
            self._clone_node(node)
        self._active_nodes.remove(node)

    @pytest.hookimpl
    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if self.config.option.verbose >= 0 and self._summary_report:
            terminalreporter.write_sep("=", f"xdist: {self._summary_report}")

    def worker_collectionfinish(
        self, node: WorkerController, ids: Sequence[str]
    ) -> None:
        """Worker has finished test collection.

        This adds the collection for this node to the scheduler.  If
        the scheduler indicates collection is finished (i.e. all
        initial nodes have submitted their collections), then tells the
        scheduler to schedule the collected items.  When initiating
        scheduling the first time it logs which scheduler is in use.
        """
        if self.shuttingdown:
            return
        self.config.hook.pytest_xdist_node_collection_finished(node=node, ids=ids)
        # tell session which items were effectively collected otherwise
        # the controller node will finish the session with EXIT_NOTESTSCOLLECTED
        assert self._session is not None
        self._session.testscollected = len(ids)
        assert self.sched is not None
        self.sched.add_node_collection(node, ids)
        if self.terminal:
            self.trdist.setstatus(
                node.gateway.spec, WorkerStatus.CollectionDone, tests_collected=len(ids)
            )
        if self.sched.collection_is_completed:
            if self.terminal and not self.sched.has_pending:
                self.trdist.ensure_show_status()
                self.terminal.write_line("")
                if self.config.option.verbose > 0:
                    self.terminal.write_line(
                        f"scheduling tests via {self.sched.__class__.__name__}"
                    )
            self.sched.schedule()

    def worker_logstart(
        self,
        node: WorkerController,
        nodeid: str,
        location: tuple[str, int | None, str],
    ) -> None:
        """Emitted when a node calls the pytest_runtest_logstart hook."""
        self.config.hook.pytest_runtest_logstart(nodeid=nodeid, location=location)

    def worker_logfinish(
        self,
        node: WorkerController,
        nodeid: str,
        location: tuple[str, int | None, str],
    ) -> None:
        """Emitted when a node calls the pytest_runtest_logfinish hook."""
        self.config.hook.pytest_runtest_logfinish(nodeid=nodeid, location=location)

    def worker_testreport(self, node: WorkerController, rep: pytest.TestReport) -> None:
        """Emitted when a node calls the pytest_runtest_logreport hook."""
        rep.node = node  # type: ignore[attr-defined]
        self.config.hook.pytest_runtest_logreport(report=rep)
        self._handlefailures(rep)

    def worker_runtest_protocol_complete(
        self, node: WorkerController, item_index: int, duration: float
    ) -> None:
        """
        Emitted when a node fires the 'runtest_protocol_complete' event,
        signalling that a test has completed the runtestprotocol and should be
        removed from the pending list in the scheduler.
        """
        assert self.sched is not None
        self.sched.mark_test_complete(node, item_index, duration)

    def worker_unscheduled(
        self, node: WorkerController, indices: Sequence[int]
    ) -> None:
        """
        Emitted when a node fires the 'unscheduled' event, signalling that
        some tests have been removed from the worker's queue and should be
        sent to some worker again.

        This should happen only in response to 'steal' command, so schedulers
        not using 'steal' command don't have to implement it.
        """
        assert self.sched is not None
        self.sched.remove_pending_tests_from_node(node, indices)

    def worker_collectreport(
        self,
        node: WorkerController,
        rep: pytest.CollectReport | pytest.TestReport,
    ) -> None:
        """Emitted when a node calls the pytest_collectreport hook.

        Because we only need the report when there's a failure/skip, as optimization
        we only expect to receive failed/skipped reports from workers (#330).
        """
        assert not rep.passed
        self._failed_worker_collectreport(node, rep)

    def worker_warning_recorded(
        self,
        warning_message: warnings.WarningMessage,
        when: str,
        nodeid: str,
        location: tuple[str, int, str] | None,
    ) -> None:
        """Emitted when a node calls the pytest_warning_recorded hook."""
        kwargs = dict(
            warning_message=warning_message, when=when, nodeid=nodeid, location=location
        )
        self.config.hook.pytest_warning_recorded.call_historic(kwargs=kwargs)

    def _clone_node(self, node: WorkerController) -> WorkerController:
        """Return new node based on an existing one.

        This is normally for when a node dies, this will copy the spec
        of the existing node and create a new one with a new id.  The
        new node will have been setup so it will start calling the
        "worker_*" hooks and do work soon.
        """
        spec = node.gateway.spec
        spec.id = None
        assert self.nodemanager is not None
        self.nodemanager.group.allocate_id(spec)
        clone = self.nodemanager.setup_node(spec, self.queue.put)
        self._active_nodes.add(clone)
        return clone

    def _failed_worker_collectreport(
        self,
        node: WorkerController,
        rep: pytest.CollectReport | pytest.TestReport,
    ) -> None:
        # Check we haven't already seen this report (from
        # another worker).
        if rep.longrepr not in self._failed_collection_errors:
            self._failed_collection_errors[rep.longrepr] = True
            self.config.hook.pytest_collectreport(report=rep)
            self._handlefailures(rep)

    def _handlefailures(
        self,
        rep: pytest.CollectReport | pytest.TestReport,
    ) -> None:
        if rep.failed:
            self.countfailures += 1
            if (
                self.maxfail
                and self.countfailures >= self.maxfail
                and not self.shouldstop
            ):
                self.shouldstop = f"stopping after {self.countfailures} failures"

    def triggershutdown(self) -> None:
        if not self.shuttingdown:
            self.log("triggering shutdown")
            self.shuttingdown = True
            assert self.sched is not None
            for node in self.sched.nodes:
                node.shutdown()

    def handle_crashitem(self, nodeid: str, worker: WorkerController) -> None:
        # XXX get more reporting info by recording pytest_runtest_logstart?
        # XXX count no of failures and retry N times
        fspath = nodeid.split("::")[0]
        msg = f"worker {worker.gateway.id!r} crashed while running {nodeid!r}"
        rep = pytest.TestReport(
            nodeid=nodeid,
            location=(fspath, None, fspath),
            keywords={},
            outcome="failed",
            longrepr=msg,
            when="???",  # type: ignore[arg-type]
        )
        rep.node = worker  # type: ignore[attr-defined]

        self.config.hook.pytest_handlecrashitem(
            crashitem=nodeid,
            report=rep,
            sched=self.sched,
        )
        self.config.hook.pytest_runtest_logreport(report=rep)


class WorkerStatus(Enum):
    """Status of each worker during creation/collection."""

    # Worker spec has just been created.
    Created = auto()

    # Worker has been initialized.
    Initialized = auto()

    # Worker is now ready for collection.
    ReadyForCollection = auto()

    # Worker has finished collection.
    CollectionDone = auto()


class TerminalDistReporter:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.tr = config.pluginmanager.getplugin("terminalreporter")
        self._status: dict[object, tuple[WorkerStatus, int]] = {}
        self._lastlen = 0
        self._isatty = getattr(self.tr, "isatty", self.tr.hasmarkup)

    def write_line(self, msg: str) -> None:
        self.tr.write_line(msg)

    def ensure_show_status(self) -> None:
        if not self._isatty:
            self.write_line(self.getstatus())

    def setstatus(
        self,
        spec: execnet.XSpec,
        status: WorkerStatus,
        *,
        tests_collected: int,
        show: bool = True,
    ) -> None:
        self._status[spec.id] = (status, tests_collected)
        if show and self._isatty:
            self.rewrite(self.getstatus())

    def getstatus(self) -> str:
        if self.config.option.verbose >= 0:
            line = get_workers_status_line(list(self._status.values()))
            if line:
                return line

        return "bringing up nodes..."

    def rewrite(self, line: str, newline: bool = False) -> None:
        pline = line + " " * max(self._lastlen - len(line), 0)
        if newline:
            self._lastlen = 0
            pline += "\n"
        else:
            self._lastlen = len(line)
        self.tr.rewrite(pline, bold=True)

    @pytest.hookimpl
    def pytest_xdist_setupnodes(self, specs: Sequence[execnet.XSpec]) -> None:
        self._specs = specs
        for spec in specs:
            self.setstatus(spec, WorkerStatus.Created, tests_collected=0, show=False)
        self.setstatus(spec, WorkerStatus.Created, tests_collected=0, show=True)
        self.ensure_show_status()

    @pytest.hookimpl
    def pytest_xdist_newgateway(self, gateway: execnet.Gateway) -> None:
        if self.config.option.verbose > 0:
            rinfo = gateway._rinfo()
            different_interpreter = rinfo.executable != sys.executable
            if different_interpreter:
                version = "{}.{}.{}".format(*rinfo.version_info[:3])
                self.rewrite(
                    f"[{gateway.id}] {rinfo.platform} Python {version} cwd: {rinfo.cwd}",
                    newline=True,
                )
        self.setstatus(gateway.spec, WorkerStatus.Initialized, tests_collected=0)

    @pytest.hookimpl
    def pytest_testnodeready(self, node: WorkerController) -> None:
        if self.config.option.verbose > 0:
            d = node.workerinfo
            different_interpreter = d.get("executable") != sys.executable
            if different_interpreter:
                version = d["version"].replace("\n", " -- ")
                self.rewrite(f"[{d['id']}] Python {version}", newline=True)
        self.setstatus(
            node.gateway.spec, WorkerStatus.ReadyForCollection, tests_collected=0
        )

    @pytest.hookimpl
    def pytest_testnodedown(self, node: WorkerController, error: object) -> None:
        if not error:
            return
        self.write_line(f"[{node.gateway.id}] node down: {error}")


def get_default_max_worker_restart(config: pytest.Config) -> int | None:
    """Gets the default value of --max-worker-restart option if it is not provided.

    Use a reasonable default to avoid workers from restarting endlessly due to crashing collections (#226).
    """
    result_str: str | None = config.option.maxworkerrestart
    if result_str is not None:
        result = int(result_str)
    elif config.option.numprocesses:
        # if --max-worker-restart was not provided, use a reasonable default (#226)
        result = config.option.numprocesses * 4
    else:
        result = None
    return result


def get_workers_status_line(
    status_and_items: Sequence[tuple[WorkerStatus, int]],
) -> str:
    """
    Return the line to display during worker setup/collection based on the
    status of the workers and number of tests collected for each.
    """
    statuses = [s for s, c in status_and_items]
    total_workers = len(statuses)
    workers_noun = "worker" if total_workers == 1 else "workers"
    if status_and_items and all(s == WorkerStatus.CollectionDone for s in statuses):
        # All workers collect the same number of items, so we grab
        # the total number of items from the first worker.
        first = status_and_items[0]
        status, tests_collected = first
        tests_noun = "item" if tests_collected == 1 else "items"
        return f"{total_workers} {workers_noun} [{tests_collected} {tests_noun}]"
    if WorkerStatus.CollectionDone in statuses:
        done = sum(1 for s, c in status_and_items if c > 0)
        return f"collecting: {done}/{total_workers} {workers_noun}"
    if WorkerStatus.ReadyForCollection in statuses:
        ready = statuses.count(WorkerStatus.ReadyForCollection)
        return f"ready: {ready}/{total_workers} {workers_noun}"
    if WorkerStatus.Initialized in statuses:
        initialized = statuses.count(WorkerStatus.Initialized)
        return f"initialized: {initialized}/{total_workers} {workers_noun}"
    if WorkerStatus.Created in statuses:
        created = statuses.count(WorkerStatus.Created)
        return f"created: {created}/{total_workers} {workers_noun}"

    return ""

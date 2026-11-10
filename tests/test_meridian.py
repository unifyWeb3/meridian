"""Meridian tests - stdlib unittest only (the plugin itself has zero deps).

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Import the plugin as package "meridian" regardless of the checkout's
# directory name (CI may clone into anything).
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "meridian", REPO / "__init__.py", submodule_search_locations=[str(REPO)]
)
_module = importlib.util.module_from_spec(_spec)
sys.modules["meridian"] = _module
_spec.loader.exec_module(_module)

from meridian.analyzer import graph, ledger, spec as spec_mod  # noqa: E402
from meridian.analyzer.scan import scan_root  # noqa: E402
from meridian.analyzer.spec import SpecError, draft_spec, load_spec, _mini_yaml  # noqa: E402

DEMO = REPO / "examples" / "demo-order-pipeline"


class TempRepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="meridian-test-"))
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.tmp / "hermes-home")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home

    def write(self, rel: str, content: str) -> Path:
        target = self.tmp / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content), encoding="utf-8")
        return target


class ScanTests(TempRepoTest):
    def test_enum_and_class_machine_discovered(self):
        self.write("orders.py", """
        from enum import Enum
        class OrderStatus(str, Enum):
            DRAFT = "draft"
            SUBMITTED = "submitted"
            PAID = "paid"
        class Order:
            def __init__(self):
                self.status = OrderStatus.DRAFT
            def pay(self):
                if self.status == OrderStatus.SUBMITTED:
                    self.status = OrderStatus.PAID
        """)
        result = scan_root(self.tmp)
        by_id = {m.id: m for m in result.machines}
        self.assertIn("Order.status", by_id)
        machine = by_id["Order.status"]
        self.assertIn("draft", machine.states)
        self.assertIn("paid", machine.states)
        self.assertEqual(machine.initial, ["draft"])
        edges = {(t.src, t.dst) for t in machine.transitions}
        self.assertIn(("submitted", "paid"), edges)

    def test_guard_resolution_and_unguarded_write(self):
        self.write("m.py", """
        class T:
            def __init__(self):
                self.state = "idle"
            def work(self):
                if self.state == "idle":
                    self.state = "busy"
            def admin(self):
                self.state = "done"
        """)
        result = scan_root(self.tmp)
        machine = next(m for m in result.machines if m.id == "T.state")
        guarded = [t for t in machine.transitions if t.guarded]
        unguarded = [t for t in machine.transitions if not t.guarded and t.kind == "write"]
        self.assertIn(("idle", "busy"), {(t.src, t.dst) for t in guarded})
        self.assertIn(("done",), {(t.dst,) for t in unguarded})

    def test_node_modules_and_syntax_errors_skipped(self):
        self.write("node_modules/pkg/index.py", "class X:\n    def __init__(self):\n        self.status = 'running'\n")
        self.write("broken.py", "def (:\n")
        result = scan_root(self.tmp)
        self.assertEqual(len(result.machines), 0)
        self.assertTrue(any("broken.py" in w for w in result.warnings))

    def test_demo_repo_finds_expected_machines(self):
        result = scan_root(DEMO)
        ids = {m.id for m in result.machines}
        self.assertIn("Order.status", ids)
        self.assertIn("Order.payment_status", ids)
        order = next(m for m in result.machines if m.id == "Order.status")
        self.assertTrue(set(order.states) >= {"draft", "submitted", "paid", "shipped", "refunded"})


class GraphTests(TempRepoTest):
    def test_unreachable_and_dead_and_terminal(self):
        self.write("g.py", """
        class O:
            def __init__(self):
                self.status = "draft"
            def ship_dead_branch(self):
                if self.status == "ghost":
                    self.status = "shipped"
        """)
        result = scan_root(self.tmp)
        machine = next(m for m in result.machines if m.id == "O.status")
        machine.states = sorted(set(machine.states) | {"ghost", "warehouse"})
        verdict = graph.analyze_machine(machine)
        self.assertIn("ghost", verdict.unreachable)
        self.assertIn("warehouse", verdict.dead)
        self.assertIn("draft", verdict.terminal)

    def test_spec_deny_and_allow(self):
        self.write("s.py", """
        class O:
            def __init__(self):
                self.status = "draft"
            def refund(self):
                if self.status in ("paid", "returned", "draft"):
                    self.status = "refunded"
        """)
        result = scan_root(self.tmp)
        machine = next(m for m in result.machines if m.id == "O.status")
        spec = spec_mod.SpecMachine(id="O.status", states=machine.states,
                                    initial=["draft"],
                                    allow={("paid", "refunded"), ("returned", "refunded")},
                                    deny={("draft", "refunded")})
        verdict = graph.analyze_machine(machine, spec)
        rules = {(v.src, v.dst, v.rule) for v in verdict.violations}
        self.assertIn(("draft", "refunded", "deny"), rules)
        self.assertFalse(verdict.ok)


class SpecTests(TempRepoTest):
    def test_draft_accept_roundtrip(self):
        self.write("o.py", """
        class O:
            def __init__(self):
                self.status = "draft"
            def go(self):
                if self.status == "draft":
                    self.status = "done"
        """)
        result = scan_root(self.tmp)
        text = draft_spec(result.machines)
        path = self.tmp / ".meridian.yaml"
        path.write_text(text, encoding="utf-8")
        spec = load_spec(path)
        self.assertIn("O.status", spec.ids())
        machine = next(m for m in spec.machines if m.id == "O.status")
        self.assertIn(("draft", "done"), machine.allow)

    def test_mini_yaml_parser_matches_spec(self):
        text = """version: 1
machines:
  - id: O.status
    states: [draft, done]
    initial: [draft]
    allow:
      - [draft, done]
    deny:
      - [done, draft]
"""
        spec = spec_mod._coerce_spec(_mini_yaml(text), Path("test.yaml"))
        self.assertEqual(spec.version, 1)
        machine = spec.machines[0]
        self.assertEqual(machine.allow, {("draft", "done")})
        self.assertEqual(machine.deny, {("done", "draft")})

    def test_mini_yaml_two_machines_keep_separate(self):
        """Regression: the old parser merged machine 2 into machine 1 and
        dropped both allow/deny sets (Hermes's venv has no PyYAML, so this
        subset parser is the production path for .meridian.yaml)."""
        text = Path(DEMO / ".meridian.yaml").read_text(encoding="utf-8")
        spec = spec_mod._coerce_spec(_mini_yaml(text), Path("demo.yaml"))
        ids = [m.id for m in spec.machines]
        self.assertEqual(ids, ["Order.status", "Order.payment_status"])
        order = next(m for m in spec.machines if m.id == "Order.status")
        payment = next(m for m in spec.machines if m.id == "Order.payment_status")
        self.assertIn(("paid", "refunded"), order.allow)
        self.assertIn(("delivered", "returned"), order.allow)
        self.assertEqual(order.deny, {("draft", "refunded")})
        self.assertEqual(payment.deny, set())
        self.assertIn(("pending", "succeeded"), payment.allow)

    def test_cli_exit_codes(self):
        from meridian import _setup_cli, _cli_main
        import argparse

        def run(*argv):
            parser = argparse.ArgumentParser()
            _setup_cli(parser)
            args = parser.parse_args(list(argv))
            return _cli_main(args)

        self.assertEqual(run("check", "--root", str(DEMO)), 1)
        self.assertEqual(run("check", "--root", str(self.tmp)), 2)  # no spec → error, never a false green
        self.assertEqual(run("scan", "--root", str(DEMO)), None)  # returns None → rc 0
        self.assertEqual(run("ledger", "verify"), 0)

    def test_json_in_yaml_loader(self):
        payload = {"version": 1,
                   "machines": [{"id": "A.status", "states": ["x", "y"],
                                 "initial": ["x"],
                                 "allow": [["x", "y"]],
                                 "deny": []}]}
        path = self.tmp / "s.yaml"
        path.write_text(json.dumps(payload), encoding="utf-8")
        spec = load_spec(path)
        self.assertEqual(spec.machines[0].allow, {("x", "y")})

    def test_demo_spec_parses(self):
        spec = load_spec(DEMO / ".meridian.yaml")
        order = next(m for m in spec.machines if m.id == "Order.status")
        self.assertIn(("draft", "refunded"), order.deny)

    def test_demo_check_reports_seeded_violations(self):
        result = scan_root(DEMO)
        spec = load_spec(DEMO / ".meridian.yaml")
        all_violations = []
        for machine in result.machines:
            verdict = graph.analyze_machine(machine, spec.machine(machine.id))
            all_violations.extend(verdict.violations)
        anchors = {(v.src, v.dst, v.rule) for v in all_violations}
        self.assertIn(("draft", "refunded", "deny"), anchors)

    def test_missing_spec_raises_clean_error(self):
        with self.assertRaises(SpecError):
            load_spec(self.tmp / "missing.yaml")


class LedgerTests(TempRepoTest):
    def test_append_and_verify(self):
        ledger.append("scan", {"root": "/tmp/x", "files": 3, "machines": 1})
        ledger.append("verify", {"violation_count": 0, "ok": True})
        status = ledger.verify()
        self.assertTrue(status["ok"])
        self.assertEqual(status["entries"], 2)

    def test_tamper_breaks_chain(self):
        ledger.append("scan", {"root": "/tmp/x"})
        ledger.append("verify", {"ok": True})
        path = ledger.ledger_path()
        lines = path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[0])
        entry["data"]["root"] = "/tmp/evil"
        lines[0] = json.dumps(entry)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        status = ledger.verify()
        self.assertFalse(status["ok"])
        self.assertTrue(status["problems"])
        with self.assertRaises(ledger.LedgerError):
            ledger.append("scan", {"root": "/tmp/y"})


class ReviewFixTests(TempRepoTest):
    """Regressions for review findings - each of these was a real defect."""

    def test_not_branch_is_not_a_constraint(self):
        self.write("n.py", """
        class T:
            def __init__(self):
                self.status = "draft"
            def ship_even_if_not_draft(self):
                if not (self.status == "draft"):
                    self.status = "shipped"
        """)
        result = scan_root(self.tmp)
        machine = next(m for m in result.machines if m.id == "T.status")
        edges = {(t.src, t.dst) for t in machine.transitions}
        self.assertNotIn(("draft", "shipped"), edges)
        self.assertIn((None, "shipped"), {(t.src, t.dst) for t in machine.transitions})
        note = [n for n in machine.notes if "unguarded" in n]
        self.assertTrue(note)

    def test_all_caps_enum_values_are_states(self):
        self.write("c.py", """
        from enum import Enum
        class Status(str, Enum):
            PENDING = "PENDING"
            DONE = "DONE"
        class T:
            def __init__(self):
                self.status = Status.PENDING
            def finish(self):
                if self.status == Status.PENDING:
                    self.status = Status.DONE
        """)
        result = scan_root(self.tmp)
        machine = next(m for m in result.machines if m.id == "T.status")
        self.assertEqual(machine.states, ["DONE", "PENDING"])

    def test_non_enum_attribute_is_not_fabricated(self):
        self.write("a.py", """
        class T:
            def __init__(self):
                self.status = "draft"
            def reset(self):
                self.status = cfg.DEFAULT_MODE
        """)
        result = scan_root(self.tmp)
        machine = next(m for m in result.machines if m.id == "T.status")
        self.assertNotIn("default_mode", machine.states)
        self.assertEqual({t.dst for t in machine.transitions}, {"draft"})
        self.assertTrue(any("unresolved value" in w for w in result.warnings))

    def test_nested_scope_writers_are_reported_not_analyzed(self):
        self.write("s.py", """
        class T:
            def __init__(self):
                self.status = "idle"
            def outer(self):
                def inner():
                    self.status = "busy"
                inner()
        """)
        result = scan_root(self.tmp)
        self.assertTrue(any("nested scope" in w and "status" in w for w in result.warnings))
        machine = next(m for m in result.machines if m.id == "T.status")
        self.assertNotIn("busy", machine.states)

    def test_spec_covers_nothing_refuses_green(self):
        from meridian import _setup_cli, _cli_dispatch
        import argparse
        self.write("o.py", """
        class O:
            def __init__(self):
                self.status = "draft"
        """)
        (self.tmp / ".meridian.yaml").write_text(
            'version: 1\nmachines:\n  - id: "Other.machine"\n    states: ["a"]\n', encoding="utf-8")
        parser = argparse.ArgumentParser()
        _setup_cli(parser)
        args = parser.parse_args(["check", "--root", str(self.tmp)])
        self.assertEqual(_cli_dispatch(args), 2)

    def test_uncovered_machine_fails_check(self):
        from meridian import _setup_cli, _cli_dispatch
        import argparse
        self.write("o.py", """
        class O:
            def __init__(self):
                self.status = "draft"
                self.payment_status = "pending"
            def go(self):
                if self.status == "draft":
                    self.status = "done"
        """)
        (self.tmp / ".meridian.yaml").write_text(
            'version: 1\nmachines:\n  - id: "O.status"\n    states: ["draft", "done"]\n    initial: ["draft"]\n',
            encoding="utf-8")
        parser = argparse.ArgumentParser()
        _setup_cli(parser)
        args = parser.parse_args(["check", "--root", str(self.tmp)])
        self.assertEqual(_cli_dispatch(args), 1)  # O.payment_status uncovered
        args = parser.parse_args(["check", "--root", str(self.tmp), "--allow-uncovered"])
        self.assertEqual(_cli_dispatch(args), 0)

    def test_constructor_initial_rule_in_strict_mode(self):
        self.write("i.py", """
        class O:
            def __init__(self):
                self.status = "shipped"
        """)
        result = scan_root(self.tmp)
        machine = next(m for m in result.machines if m.id == "O.status")
        spec = spec_mod.SpecMachine(id="O.status", states=["shipped", "draft"],
                                    initial=["draft"], allow=set(), deny=set())
        verdict = graph.analyze_machine(machine, spec, strict=True)
        rules = {v.rule for v in verdict.violations}
        self.assertIn("initial", rules)
        spec2 = spec_mod.SpecMachine(id="O.status", states=["shipped", "draft"],
                                     initial=["shipped"], allow=set(), deny=set())
        verdict2 = graph.analyze_machine(machine, spec2, strict=True)
        self.assertNotIn("initial", {v.rule for v in verdict2.violations})

    def test_ledger_repair_after_torn_write(self):
        ledger.append("scan", {"root": "/tmp/x"})
        ledger.append("verify", {"ok": True})
        path = ledger.ledger_path()
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw + '{"seq": 3, "ts": "now", "event": "scan", "prev"', encoding="utf-8")
        status = ledger.verify()
        self.assertFalse(status["ok"])
        with self.assertRaises(ledger.LedgerError):
            ledger.append("check", {"ok": True})
        result = ledger.repair()
        self.assertTrue(result["ok"])
        self.assertGreater(result["dropped_lines"], 0)
        status = ledger.verify()
        self.assertTrue(status["ok"])
        again = ledger.append("check", {"ok": True})
        self.assertTrue(again["hash"])

    def test_draft_spec_quotes_yaml_scalars(self):
        self.write("y.py", """
        class T:
            def __init__(self):
                self.status = "on"
            def go(self):
                if self.status == "on":
                    self.status = "off"
        """)
        result = scan_root(self.tmp)
        machine = next(m for m in result.machines if m.id == "T.status")
        machine.states = ["on", "off"]
        text = draft_spec([machine])
        spec = spec_mod._coerce_spec(_mini_yaml(text), Path("x.yaml"))
        self.assertEqual(spec.machines[0].states, ["on", "off"])

    def test_unreachable_not_reported_without_initial(self):
        self.write("u.py", """
        class T:
            def mystery(self):
                self.status = "lonely"
        """)
        # force no discovery of initial: craft machine manually
        from meridian.analyzer.model import Machine, Transition
        m = Machine(id="T.status", field="status", owner="T", language="python",
                    confidence="low", states=["lonely", "elsewhere"], initial=[],
                    transitions=[Transition(None, "lonely", "mystery", "u.py", 3, True, "write")],
                    sources=["u.py"], notes=[])
        verdict = graph.analyze_machine(m)
        self.assertFalse(verdict.unreachable_reported)
        self.assertEqual(verdict.unreachable, [])
        self.assertTrue(any("no initial state" in n for n in verdict.notes))

    def test_on_off_draft_roundtrip_matches_pyyaml_semantics(self):
        # "on"/"off" MUST stay strings through draft -> mini-yaml/json paths
        self.write("r.py", """
        class T:
            def __init__(self):
                self.status = "on"
            def go(self):
                if self.status == "on":
                    self.status = "off"
        """)
        result = scan_root(self.tmp)
        machine = next(m for m in result.machines if m.id == "T.status")
        text = draft_spec([machine])
        path = self.tmp / ".meridian.yaml"
        path.write_text(text, encoding="utf-8")
        spec = load_spec(path)
        self.assertIn("on", spec.machines[0].states)


class DiagramTests(TempRepoTest):
    def test_mermaid_contains_edges(self):
        from meridian.tools import _mermaid
        self.write("d.py", """
        class O:
            def __init__(self):
                self.status = "draft"
            def go(self):
                if self.status == "draft":
                    self.status = "done"
        """)
        result = scan_root(self.tmp)
        machine = next(m for m in result.machines if m.id == "O.status")
        diagram = _mermaid(machine)
        self.assertIn("stateDiagram-v2", diagram)
        self.assertIn("[*] --> draft", diagram)
        self.assertIn("draft --> done", diagram)


class RootConfinementTests(TempRepoTest):
    """`root` is a model-controlled tool argument: it has to be pinned to the
    active working directory, or "inside root" is not a sandbox at all."""

    def setUp(self):
        super().setUp()
        self.project = self.tmp / "project"
        self.project.mkdir(parents=True, exist_ok=True)
        (self.project / "o.py").write_text(
            'class O:\n    def __init__(self):\n        self.status = "draft"\n',
            encoding="utf-8")
        import meridian.tools as tools_mod

        self._orig_agent_cwd = tools_mod._agent_cwd
        tools_mod._agent_cwd = lambda: self.project
        self.addCleanup(setattr, tools_mod, "_agent_cwd", self._orig_agent_cwd)

    def call(self, tool, **args):
        import meridian.tools as tools_mod

        return json.loads(getattr(tools_mod, tool)(args))

    def test_home_root_is_refused(self):
        out = self.call("meridian_scan", root="~")
        self.assertFalse(out["ok"])
        self.assertIn("working directory", out["error"])

    def test_filesystem_root_is_refused(self):
        out = self.call("meridian_scan", root="/")
        self.assertFalse(out["ok"])
        self.assertIn("working directory", out["error"])

    def test_parent_traversal_root_is_refused(self):
        out = self.call("meridian_scan", root=os.path.join(str(self.project), "..", ".."))
        self.assertFalse(out["ok"])
        self.assertIn("working directory", out["error"])

    def test_symlink_that_escapes_the_working_dir_is_refused(self):
        escape = self.tmp / "outside"
        escape.mkdir(parents=True, exist_ok=True)
        link = self.project / "link-out"
        link.symlink_to(escape, target_is_directory=True)
        out = self.call("meridian_scan", root=str(link))
        self.assertFalse(out["ok"])
        self.assertIn("working directory", out["error"])

    def test_hermes_home_root_is_refused(self):
        # inside the working directory on purpose: only the Hermes-home rule
        # can catch this one.
        home = self.project / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(home)
        out = self.call("meridian_scan", root=str(home))
        self.assertFalse(out["ok"])
        self.assertIn("Hermes home", out["error"])

    def test_in_project_root_still_works(self):
        out = self.call("meridian_scan", root=str(self.project))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["scan"]["root"], str(self.project.resolve()))
        sub = self.project / "sub"
        sub.mkdir()
        out = self.call("meridian_scan", root=str(sub))
        self.assertTrue(out["ok"], out)

    def test_out_file_traversal_is_refused(self):
        out = self.call("meridian_report", root=str(self.project),
                        out_file="../escaped.md")
        self.assertFalse(out["ok"])
        self.assertIn("out_file", out["error"])
        self.assertFalse((self.tmp / "escaped.md").exists())

    def test_out_file_inside_root_is_written(self):
        out = self.call("meridian_report", root=str(self.project),
                        out_file=".meridian/report.md")
        self.assertTrue(out["ok"], out)
        self.assertTrue((self.project / ".meridian" / "report.md").is_file())

    def test_spec_accept_cannot_write_outside_the_working_dir(self):
        out = self.call("meridian_spec", root=os.path.expanduser("~"), action="accept")
        self.assertFalse(out["ok"])
        self.assertFalse((Path(os.path.expanduser("~")) / ".meridian.yaml").exists())


class HookCorrelationTests(TempRepoTest):
    """A hook finding must reach the session that caused it - and only that one.

    The host passes a task id AND a session id, and they are not the same
    value: storing under one and reading under the other loses the finding,
    and a shared bucket leaks it into somebody else's chat.
    """

    def setUp(self):
        super().setUp()
        import meridian

        self.mod = meridian
        meridian._PENDING_NOTES.clear()
        self.project = self.tmp / "project"
        self.project.mkdir(parents=True, exist_ok=True)
        (self.project / "o.py").write_text(
            'class O:\n'
            '    def __init__(self):\n'
            '        self.status = "draft"\n'
            '    def refund(self):\n'
            '        if self.status == "draft":\n'
            '            self.status = "refunded"\n',
            encoding="utf-8")
        (self.project / ".meridian.yaml").write_text(
            'version: 1\n'
            'machines:\n'
            '  - id: "O.status"\n'
            '    states: ["draft", "refunded"]\n'
            '    initial: ["draft"]\n'
            '    allow:\n'
            '      - ["draft", "refunded"]\n'
            '    deny:\n'
            '      - ["draft", "refunded"]\n',
            encoding="utf-8")

    def test_finding_surfaces_on_the_next_turn_of_the_same_session(self):
        self.mod._on_post_tool_call(
            "write_file", {"path": str(self.project / "o.py")}, "ok",
            task_id="task-1", session_id="session-1")
        context = self.mod._on_pre_llm_call(session_id="session-1")
        self.assertIsNotNone(context)
        self.assertIn("violates O.status", context["context"])

    def test_a_second_session_never_sees_them(self):
        self.mod._on_post_tool_call(
            "write_file", {"path": str(self.project / "o.py")}, "ok",
            task_id="task-1", session_id="session-1")
        self.assertIsNone(self.mod._on_pre_llm_call(session_id="session-2"))

    def test_the_task_id_bucket_is_not_read(self):
        self.mod._on_post_tool_call(
            "write_file", {"path": str(self.project / "o.py")}, "ok",
            task_id="task-1", session_id="session-1")
        # the finding was stored under the session, not under the task
        self.assertIsNone(self.mod._on_pre_llm_call(session_id="task-1"))

    def test_no_shared_local_bucket(self):
        # A finding with no session identity is dropped, not parked in a
        # bucket that every chat would read.
        self.mod._on_post_tool_call(
            "write_file", {"path": str(self.project / "o.py")}, "ok")
        self.assertEqual(self.mod._PENDING_NOTES, {})
        self.assertIsNone(self.mod._on_pre_llm_call(session_id="session-1"))
        self.assertIsNone(self.mod._on_pre_llm_call(session_id=""))

    def test_notes_are_consumed_once(self):
        self.mod._on_post_tool_call(
            "write_file", {"path": str(self.project / "o.py")}, "ok",
            task_id="task-1", session_id="session-1")
        self.assertIsNotNone(self.mod._on_pre_llm_call(session_id="session-1"))
        self.assertIsNone(self.mod._on_pre_llm_call(session_id="session-1"))


class SpecAcceptMergeTests(TempRepoTest):
    """Accepting a spec must never throw away reviewed rules."""

    def setUp(self):
        super().setUp()
        import meridian.tools as tools_mod

        self._orig_agent_cwd = tools_mod._agent_cwd
        tools_mod._agent_cwd = lambda: self.tmp
        self.addCleanup(setattr, tools_mod, "_agent_cwd", self._orig_agent_cwd)
        (self.tmp / "o.py").write_text(
            'class O:\n'
            '    def __init__(self):\n'
            '        self.status = "draft"\n'
            '    def refund(self):\n'
            '        if self.status == "draft":\n'
            '            self.status = "refunded"\n',
            encoding="utf-8")
        self.spec_path = self.tmp / ".meridian.yaml"
        self.spec_path.write_text(
            'version: 1\n'
            'machines:\n'
            '  - id: "O.status"\n'
            '    states: ["draft", "refunded", "shipped"]\n'
            '    initial: ["draft"]\n'
            '    allow:\n'
            '      - ["draft", "shipped"]\n'
            '    deny:\n'
            '      - ["draft", "refunded"]\n',
            encoding="utf-8")

    def accept(self, **args):
        from meridian import tools

        args.setdefault("root", str(self.tmp))
        args.setdefault("action", "accept")
        return json.loads(tools.meridian_spec(args))

    def test_accept_keeps_the_deny_rule(self):
        out = self.accept()
        self.assertTrue(out["ok"], out)
        spec = load_spec(self.spec_path)
        machine = spec.machine("O.status")
        self.assertIn(("draft", "refunded"), machine.deny)

    def test_accept_keeps_the_deny_rule_with_force(self):
        out = self.accept(force=True)
        self.assertTrue(out["ok"], out)
        spec = load_spec(self.spec_path)
        self.assertIn(("draft", "refunded"), spec.machine("O.status").deny)

    def test_accept_keeps_the_deny_rule_on_disk(self):
        self.accept()
        text = self.spec_path.read_text(encoding="utf-8")
        self.assertIn('"draft", "refunded"', text)
        # the denied edge is never quietly re-allowed
        spec = load_spec(self.spec_path)
        machine = spec.machine("O.status")
        self.assertNotIn(("draft", "refunded"), machine.allow)
        # the authored allow rule survives too
        self.assertIn(("draft", "shipped"), machine.allow)

    def test_accept_adds_newly_discovered_machines(self):
        (self.tmp / "p.py").write_text(
            'class P:\n'
            '    def __init__(self):\n'
            '        self.state = "new"\n',
            encoding="utf-8")
        out = self.accept()
        self.assertTrue(out["ok"], out)
        spec = load_spec(self.spec_path)
        self.assertIn("O.status", spec.ids())
        self.assertIn("P.state", spec.ids())
        self.assertIn(("draft", "refunded"), spec.machine("O.status").deny)

    def test_accept_keeps_a_machine_that_is_no_longer_discovered(self):
        self.spec_path.write_text(
            'version: 1\n'
            'machines:\n'
            '  - id: "Gone.status"\n'
            '    states: ["a"]\n'
            '    deny:\n'
            '      - ["a", "b"]\n',
            encoding="utf-8")
        out = self.accept()
        self.assertTrue(out["ok"], out)
        spec = load_spec(self.spec_path)
        self.assertIn("Gone.status", spec.ids())
        self.assertIn(("a", "b"), spec.machine("Gone.status").deny)

    def test_merged_spec_still_parses_and_enforces(self):
        self.accept()
        spec = load_spec(self.spec_path)
        machine = spec.machine("O.status")
        self.assertEqual(machine.allow, {("draft", "shipped")})
        self.assertEqual(machine.deny, {("draft", "refunded")})

    def test_cli_accept_keeps_the_deny_rule(self):
        from meridian import _setup_cli, _cli_dispatch
        import argparse

        parser = argparse.ArgumentParser()
        _setup_cli(parser)
        args = parser.parse_args(["spec", "accept", "--root", str(self.tmp)])
        self.assertEqual(_cli_dispatch(args), None)
        spec = load_spec(self.spec_path)
        self.assertIn(("draft", "refunded"), spec.machine("O.status").deny)


class CwdGuardTests(TempRepoTest):
    """Confinement is only a sandbox when the base is a project directory."""

    def scan_with_cwd(self, cwd, args=None):
        import meridian.tools as tools_mod

        orig = tools_mod._agent_cwd
        tools_mod._agent_cwd = lambda: Path(cwd)
        old = os.getcwd()
        os.chdir(cwd)
        try:
            return json.loads(tools_mod.meridian_scan(args or {}))
        finally:
            os.chdir(old)
            tools_mod._agent_cwd = orig

    def test_home_cwd_is_refused(self):
        out = self.scan_with_cwd(Path.home())
        self.assertFalse(out["ok"])
        self.assertIn("project directory", out["error"])

    def test_home_cwd_with_dot_root_is_refused(self):
        out = self.scan_with_cwd(Path.home(), {"root": "."})
        self.assertFalse(out["ok"])
        self.assertIn("project directory", out["error"])

    def test_filesystem_root_cwd_is_refused(self):
        out = self.scan_with_cwd("/")
        self.assertFalse(out["ok"])
        self.assertIn("project directory", out["error"])

    def test_hermes_home_cwd_is_refused(self):
        home = Path(os.environ["HERMES_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        out = self.scan_with_cwd(home)
        self.assertFalse(out["ok"])
        self.assertIn("Hermes home", out["error"])

    def test_project_cwd_still_works(self):
        project = self.tmp / "project"
        project.mkdir()
        (project / "o.py").write_text(
            'class O:\n    def __init__(self):\n        self.status = "draft"\n',
            encoding="utf-8")
        out = self.scan_with_cwd(project)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["scan"]["root"], str(project.resolve()))


class AllowListSemanticsTests(TempRepoTest):
    """An absent allow key states no policy; an explicit empty list forbids
    every transition. Treating both as "unrestricted" is a false green."""

    def setUp(self):
        super().setUp()
        import meridian.tools as tools_mod

        self._orig_agent_cwd = tools_mod._agent_cwd
        tools_mod._agent_cwd = lambda: self.tmp
        self.addCleanup(setattr, tools_mod, "_agent_cwd", self._orig_agent_cwd)
        (self.tmp / "o.py").write_text(
            'class O:\n'
            '    def __init__(self):\n'
            '        self.status = "draft"\n'
            '    def go(self):\n'
            '        if self.status == "draft":\n'
            '            self.status = "done"\n',
            encoding="utf-8")

    def check(self, spec_text):
        from meridian import tools

        (self.tmp / ".meridian.yaml").write_text(spec_text, encoding="utf-8")
        return json.loads(tools.meridian_spec({"root": str(self.tmp), "action": "check"}))

    ABSENT = ('version: 1\nmachines:\n  - id: "O.status"\n'
              '    states: ["draft", "done"]\n    initial: ["draft"]\n')
    EMPTY = ('version: 1\nmachines:\n  - id: "O.status"\n'
             '    states: ["draft", "done"]\n    initial: ["draft"]\n    allow: []\n')

    def test_explicit_empty_allow_forbids_every_transition(self):
        out = self.check(self.EMPTY)
        check = out["check"]
        self.assertEqual(check["violation_count"], 1)
        self.assertEqual(check["violations"][0]["rule"], "not-allowed")

    def test_absent_allow_key_states_no_policy(self):
        out = self.check(self.ABSENT)
        check = out["check"]
        self.assertEqual(check["violation_count"], 0)
        notes = [n for v in check["verdicts"] for n in v["notes"]]
        self.assertTrue(any("not enforced" in n for n in notes), notes)

    def test_the_two_cases_differ(self):
        empty = self.check(self.EMPTY)["check"]
        absent = self.check(self.ABSENT)["check"]
        self.assertEqual(empty["violation_count"], 1)
        self.assertEqual(absent["violation_count"], 0)

    def test_a_spec_without_the_key_still_loads_and_keeps_working(self):
        from meridian.analyzer.spec import load_spec

        self.check(self.ABSENT)
        machine = load_spec(self.tmp / ".meridian.yaml").machine("O.status")
        self.assertIsNone(machine.allow)  # absent key, not an empty list
        self.assertFalse(machine.enforced)

    def test_an_explicit_empty_list_is_an_enforced_rule(self):
        from meridian.analyzer.spec import load_spec

        self.check(self.EMPTY)
        machine = load_spec(self.tmp / ".meridian.yaml").machine("O.status")
        self.assertEqual(machine.allow, set())
        self.assertTrue(machine.enforced)


if __name__ == "__main__":
    unittest.main()

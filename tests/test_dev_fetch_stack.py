"""The offline development stack (tools/dev_fetch_stack.py) must never show stale code."""
from __future__ import annotations

import ast
import importlib.util
import unittest

import helpers
from strange_ringtone import config

STACK = helpers.ROOT / "tools" / "dev_fetch_stack.py"


def load_stack():
    spec = importlib.util.spec_from_file_location("dev_fetch_stack", STACK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DevVersionTest(unittest.TestCase):
    def test_each_start_gets_a_new_valid_version(self) -> None:
        # The core serves /static/<version>/ as immutable. The stack's version
        # changes on every start, or a browser that saw an earlier start keeps
        # running its modules after the code changed.
        stack = load_stack()
        first = stack.dev_version()
        second = stack.dev_version()
        self.assertNotEqual(first, second)
        for version in (first, second):
            self.assertTrue(version.startswith("dev-"), version)
            self.assertEqual(config._parse_version(version), version)

    def test_the_core_is_started_with_that_version(self) -> None:
        source = STACK.read_text(encoding="utf-8")
        self.assertIn("version=dev_version()", source)


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


class DevStackWiringTest(unittest.TestCase):
    """The stack runs the chain the deployment runs: fetcher -> its real runner client -> a runner (S1)."""

    def test_the_stack_runs_the_runner_in_process_behind_the_real_client(self) -> None:
        # S1: the offline stack exercises the fetcher's RunnerClient against a
        # RunnerServer running the stub yt-dlp, never a yt-dlp in the fetcher.
        from strange_ringtone_runner.ytdlp import YtDlp as RunnerYtDlp

        source = STACK.read_text(encoding="utf-8")
        for needle in ("RunnerServer(", "RunnerClient(", "YTDLP_STUB"):
            self.assertIn(needle, source)
        self.assertNotIn("strange_ringtone_fetch.ytdlp", source)
        tree = ast.parse(source, filename=str(STACK))
        runner_servers = _calls(tree, "RunnerServer")
        self.assertEqual(len(runner_servers), 1)
        self.assertIn("ytdlp", [keyword.arg for keyword in runner_servers[0].keywords])
        # The fetcher's runner is the client: passed as the call itself, or as
        # a name bound to one.
        clients = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name) and node.value.func.id == "RunnerClient"
            for target in node.targets if isinstance(target, ast.Name)
        }
        fetch_servers = _calls(tree, "FetchServer")
        self.assertEqual(len(fetch_servers), 1)
        arguments = list(fetch_servers[0].args) + [k.value for k in fetch_servers[0].keywords if k.arg == "runner"]
        self.assertEqual(len(arguments), 3, "FetchServer gets an explicit runner")
        runner = arguments[2]
        is_client = (isinstance(runner, ast.Call) and isinstance(runner.func, ast.Name)
                     and runner.func.id == "RunnerClient") or (isinstance(runner, ast.Name) and runner.id in clients)
        self.assertTrue(is_client, ast.dump(runner))
        stack = load_stack()
        self.assertTrue(issubclass(stack.StubYtDlp, RunnerYtDlp))

    def test_the_stack_calls_no_entry_point(self) -> None:
        # No entry point's main() runs, so no route check runs and the stack
        # needs no setting (docs/PLAN.md 6.1): the override is never set here.
        source = STACK.read_text(encoding="utf-8")
        self.assertNotIn("__main__ import main", source)
        self.assertNotIn("FETCH_ALLOW_DEFAULT_ROUTE", source)
        tree = ast.parse(source, filename=str(STACK))
        modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertFalse([module for module in modules if module and module.endswith("__main__")], modules)

    def test_the_stack_needs_no_sweep_override_either(self) -> None:
        # The runner's entry refuses to start where it can't sweep unless
        # FETCH_ALLOW_NO_SWEEP is set (Q4); the stack builds RunnerServer
        # itself, whose sweep is off by default, so it needs no setting.
        source = STACK.read_text(encoding="utf-8")
        self.assertNotIn("FETCH_ALLOW_NO_SWEEP", source)
        self.assertNotIn("sweep_strays=True", source.replace(" ", ""))
        tree = ast.parse(source, filename=str(STACK))
        runner_servers = _calls(tree, "RunnerServer")
        self.assertEqual(len(runner_servers), 1)
        self.assertNotIn("sweep_strays", [keyword.arg for keyword in runner_servers[0].keywords])


if __name__ == "__main__":
    unittest.main()

"""Nothing a yt-dlp run leaves on the runner's tmpfs wedges the next job (docs/PLAN.md 6.1).

A run has the runner's UID, so it can write anywhere in the runner's /tmp: put
a file where results/ or the scratch directory should be, take the
permissions off a directory so that a plain rmtree leaves it in place, plant
a FIFO, a symlink out of the tree or a tree deeper than Python's recursion
limit, or fill the tmpfs. After every run and its sweep, before the runner
answers, the runner removes all of it (remove_leftovers), keeping only the
result it holds for that job, and makes the scratch directory and results/
plain owner-only directories again (restore_scratch). The rest of /tmp is
cleared too, but only under the container's contract, which one small
function decides (may_clean_all_of_tmp): as PID 1, with the sweep on, and
with exactly the contract's scratch directory. Anywhere else, a developer's
machine and these tests above all, nothing outside the scratch directory is
ever touched. The wide clean-up runs for real only in the fetcher image's
test container (RealTmpCleanupTest), where the test process is PID 1, as
the real kill(-1) does.
"""
from __future__ import annotations

import contextlib
import inspect
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import helpers
from strange_ringtone_runner import server as runner_server
from strange_ringtone_runner.config import SCRATCH_DIR, Settings
from strange_ringtone_runner.ytdlp import kill_and_reap
from test_fetcher_runner import LOG_LINE_RE, OTHER_ID, RunnerServerTestCase, _TestRunnerServer, job_body, token
from test_fetcher_runner_sweep import IN_IMAGE, Syscalls
from test_fetcher_server import LogCapture, Response, build_request, exchange, wait_for
from test_fetcher_ytdlp import MODE_VAR, VIDEO_ID, StubYtDlp

MIB = 1024 * 1024
JSON_TYPE = "application/json"
POSIX = os.name == "posix"
# Mode bits mean nothing to root, so the permission cases need another user.
ROOT_USER = POSIX and os.geteuid() == 0
# What tests/stubs/ytdlp_stub.py names everything it leaves outside the job
# directory, and what its litter files hold.
LITTER_NAME = "strange_ringtone_leftover"
LITTER_BYTES = b"left behind"
# How long a clean-up may take on the trees below before it counts as stuck.
STUCK_SECONDS = 30.0


def seam(module: Any, name: str) -> Any:
    """module.name, or a test failure (not an error) that names what is missing."""
    value = getattr(module, name, None)
    if value is None:
        raise AssertionError(f"{module.__name__}.{name} is missing")
    return value


def same_path(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def inside(path: Path | str, top: Path | str) -> bool:
    """path is top or lies under it, by name."""
    path_text = os.path.normcase(os.path.abspath(path))
    top_text = os.path.normcase(os.path.abspath(top))
    return path_text == top_text or path_text.startswith(top_text.rstrip(os.sep) + os.sep)


def can_symlink(directory: Path) -> bool:
    probe = directory / "symlink-probe"
    try:
        os.symlink(directory / "nowhere", probe)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


def force_remove(path: Path) -> None:
    """Remove path whatever its modes, for the tests' own clean-up; never follows a symlink."""
    if not os.path.lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        with contextlib.suppress(OSError):
            path.unlink()
        return
    stack = [path]
    while stack:
        directory = stack.pop()
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
        with contextlib.suppress(OSError), os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
    shutil.rmtree(path, ignore_errors=True)


def release_fifos(top: Path) -> None:
    """Open every FIFO under top for writing, so a reader blocked on one gets its EOF."""
    if not hasattr(os, "mkfifo"):
        return
    for directory, _dirs, files in os.walk(top):
        for name in files:
            path = os.path.join(directory, name)
            with contextlib.suppress(OSError):
                if stat.S_ISFIFO(os.lstat(path).st_mode):
                    os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))


def locked_tree(top: Path, depth: int, mode: int = 0) -> None:
    """top/d1/.../d<depth-1> with a file in each, then each directory set to mode, the innermost first."""
    directories = [top]
    for level in range(1, depth):
        directories.append(directories[-1] / f"d{level}")
    for directory in directories:
        directory.mkdir()
        (directory / "file").write_bytes(LITTER_BYTES)
    for directory in reversed(directories):
        os.chmod(directory, mode)


def make_chain(top: Path, name: str, depth: int) -> None:
    """top/name/name/... depth levels deep with a file at the bottom, made relative to directory descriptors.

    Relative creation is how a run builds a tree whose full path is longer
    than PATH_MAX; Linux only (mkdir and open take dir_fd there).
    """
    flags = os.O_RDONLY | os.O_DIRECTORY
    fd = os.open(top, flags)
    try:
        for _ in range(depth):
            os.mkdir(name, 0o700, dir_fd=fd)
            below = os.open(name, flags | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = below
        os.close(os.open("leaf", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))
    finally:
        os.close(fd)


def remove_chain(top: Path, name: str) -> None:
    """Undo make_chain whatever is left of it, without recursion or long paths (the tests' own clean-up)."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with contextlib.suppress(OSError):
        os.chmod(top, 0o700)
    try:
        fds = [os.open(top, flags)]
    except OSError:
        return
    try:
        while True:
            with contextlib.suppress(OSError):
                os.chmod(name, 0o700, dir_fd=fds[-1], follow_symlinks=False)
            try:
                fds.append(os.open(name, flags, dir_fd=fds[-1]))
            except OSError:
                break
        while fds:
            for entry in os.listdir(fds[-1]):
                with contextlib.suppress(OSError):
                    os.unlink(entry, dir_fd=fds[-1])
            os.close(fds.pop())
            if fds:
                with contextlib.suppress(OSError):
                    os.rmdir(name, dir_fd=fds[-1])
    finally:
        for fd in fds:
            os.close(fd)


def run_bounded(test: unittest.TestCase, function: Callable[[], Any], top: Path) -> Any:
    """function() on a thread, failing the test if it doesn't return within STUCK_SECONDS; its result."""
    outcome: list[Any] = []

    def run() -> None:
        try:
            outcome.append(("value", function()))
        except BaseException as error:  # reported below, on the test's thread
            outcome.append(("error", error))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(STUCK_SECONDS)
    if thread.is_alive():
        release_fifos(top)
        thread.join(5)
        test.fail(f"the clean-up did not finish within {STUCK_SECONDS} s")
    kind, value = outcome[0]
    if kind == "error":
        raise AssertionError(f"the clean-up raised {type(value).__name__}: {value}") from value
    return value


def plain_directory(path: Path) -> bool:
    """path is a directory itself, not a link to one; False when it is missing or can't be reached."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def tree(top: Path) -> list[str]:
    """Every name under top, relative, sorted; never follows a symlink."""
    names: list[str] = []
    for directory, dirs, files in os.walk(top):
        for name in dirs + files:
            names.append((Path(directory) / name).relative_to(top).as_posix())
    return sorted(names)


class TempTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.temp = Path(temp.name)
        self.addCleanup(temp.cleanup)
        # Before the temporary directory's own clean-up (cleanups run last
        # first): modes the tests took away are given back.
        self.addCleanup(force_remove, self.temp)
        self.addCleanup(release_fifos, self.temp)
        self.root = self.temp / "root"
        self.root.mkdir()


# --- remove_leftovers ----------------------------------------------------------


class RemoveLeftoversTest(TempTestCase):
    """remove_leftovers(root, keep): everything inside root goes but keep, whatever a run did to it."""

    def remove(self, root: Path | None = None, keep: Path | None = None) -> bool:
        remove_leftovers = seam(runner_server, "remove_leftovers")
        top = root if root is not None else self.root
        return run_bounded(self, lambda: remove_leftovers(top, keep), self.temp)

    def test_everything_inside_goes_and_the_root_stays(self) -> None:
        (self.root / "file").write_bytes(LITTER_BYTES)
        (self.root / "job-1" / "deno").mkdir(parents=True)
        (self.root / "job-1" / "deno" / "cache").write_bytes(LITTER_BYTES)
        (self.root / "results").mkdir()
        (self.root / "results" / f"{token()}.m4a").write_bytes(LITTER_BYTES)
        self.assertIs(self.remove(), True)
        self.assertTrue(self.root.is_dir())
        self.assertEqual(tree(self.root), [])

    def test_the_held_result_is_kept_and_nothing_else(self) -> None:
        # The scratch directory's case: keep is results/<job>.m4a, just moved
        # there by Results.hold; the directories that lead to it stay, and
        # everything else in them goes.
        results = self.root / "results"
        results.mkdir()
        keep = results / f"{token()}.m4a"
        keep.write_bytes(b"the held audio")
        (results / f"{token()}.m4a").write_bytes(LITTER_BYTES)
        (results / "sub").mkdir()
        (results / "sub" / "file").write_bytes(LITTER_BYTES)
        (self.root / "job-1").mkdir()
        (self.root / "job-1" / "x.m4a").write_bytes(LITTER_BYTES)
        (self.root / "stray").write_bytes(LITTER_BYTES)
        self.assertIs(self.remove(keep=keep), True)
        self.assertEqual(tree(self.root), ["results", f"results/{keep.name}"])
        self.assertEqual(keep.read_bytes(), b"the held audio")

    def test_a_kept_directory_is_kept_whole(self) -> None:
        # The case of the rest of /tmp: keep is the scratch directory, kept
        # with everything in it, while its siblings go.
        scratch = self.root / "strange_ringtone_runner"
        (scratch / "results").mkdir(parents=True)
        (scratch / "results" / "held.m4a").write_bytes(b"the held audio")
        (self.root / LITTER_NAME).write_bytes(LITTER_BYTES)
        (self.root / f"{LITTER_NAME}_dir" / "sub").mkdir(parents=True)
        (self.root / f"{LITTER_NAME}_dir" / "sub" / "file").write_bytes(LITTER_BYTES)
        self.assertIs(self.remove(keep=scratch), True)
        self.assertEqual(tree(self.root), ["strange_ringtone_runner", "strange_ringtone_runner/results",
                                           "strange_ringtone_runner/results/held.m4a"])

    def test_a_file_in_place_of_a_directory_goes(self) -> None:
        # A run put a file where results/ should be: the kept path can't
        # exist, and the file is not kept for its name.
        (self.root / "results").write_bytes(b"not a directory")
        keep = self.root / "results" / f"{token()}.m4a"
        self.assertIs(self.remove(keep=keep), True)
        self.assertEqual(tree(self.root), [])

    def test_read_only_files_at_several_depths_go(self) -> None:
        # On Windows a read-only file can't be deleted until its mode is
        # given back; on POSIX this is the plain case.
        directory = self.root
        for level in range(4):
            directory = directory / f"d{level}"
            directory.mkdir()
            file = directory / "file"
            file.write_bytes(LITTER_BYTES)
            os.chmod(file, stat.S_IREAD)
        self.assertIs(self.remove(), True)
        self.assertEqual(tree(self.root), [])

    @unittest.skipUnless(POSIX and not ROOT_USER, "POSIX permissions, as a user they bind")
    def test_entries_a_run_made_unreadable_or_unwritable_go_at_every_depth(self) -> None:
        # shutil.rmtree(ignore_errors=True) leaves each of these in place.
        locked_tree(self.root / "none", 4, 0)
        locked_tree(self.root / "read-only", 3, 0o500)
        locked_tree(self.root / "write-only", 3, 0o300)
        locked_tree(self.root / "search-only", 2, 0o100)
        (self.root / "plain" / "deeper").mkdir(parents=True)
        locked_tree(self.root / "plain" / "deeper" / "none", 3, 0)
        os.chmod(self.root / "plain", 0o500)
        nothing = self.root / "no-mode"
        nothing.write_bytes(LITTER_BYTES)
        os.chmod(nothing, 0)
        self.assertIs(self.remove(), True)
        self.assertEqual(tree(self.root), [])

    @unittest.skipUnless(POSIX and not ROOT_USER, "POSIX permissions, as a user they bind")
    def test_a_root_a_run_locked_is_opened_again(self) -> None:
        (self.root / "job-1").mkdir()
        (self.root / "job-1" / "file").write_bytes(LITTER_BYTES)
        os.chmod(self.root, 0)
        self.assertIs(self.remove(), True)
        os.chmod(self.root, 0o700)
        self.assertEqual(tree(self.root), [])

    @unittest.skipUnless(POSIX and not ROOT_USER, "POSIX permissions, as a user they bind")
    def test_the_held_result_is_kept_in_a_locked_results_directory(self) -> None:
        results = self.root / "results"
        results.mkdir()
        keep = results / f"{token()}.m4a"
        keep.write_bytes(b"the held audio")
        (results / "other").write_bytes(LITTER_BYTES)
        os.chmod(results, 0)
        self.assertIs(self.remove(keep=keep), True)
        os.chmod(results, 0o700)
        self.assertEqual(sorted(p.name for p in results.iterdir()), [keep.name])
        self.assertEqual(keep.read_bytes(), b"the held audio")

    def test_a_symlink_out_of_the_tree_goes_and_its_target_survives(self) -> None:
        if not can_symlink(self.temp):
            self.skipTest("this account can't create symlinks")
        outside = self.temp / "outside"
        (outside / "sub").mkdir(parents=True)
        (outside / "file").write_bytes(b"outside")
        (outside / "sub" / "file").write_bytes(b"outside")
        links = {
            "to-dir": outside,
            "to-file": outside / "file",
            "to-sub": outside / "sub",
            "dangling": self.temp / "nowhere",
        }
        for name, target in links.items():
            os.symlink(target, self.root / name, target_is_directory=target.is_dir())
        nested = self.root / "nested"
        nested.mkdir()
        os.symlink(outside, nested / "up", target_is_directory=True)
        if POSIX:
            # Inside a directory a run locked, so that giving its mode back
            # is part of removing the link; a chmod that follows the link
            # would change the target's mode instead.
            os.chmod(nested, 0)
            for path in (outside / "file", outside / "sub" / "file"):
                os.chmod(path, 0o444)
            os.chmod(outside / "sub", 0o555)
        modes = {path: os.lstat(path).st_mode for path in (outside, outside / "file", outside / "sub")}
        self.assertIs(self.remove(), True)
        self.assertEqual(tree(self.root), [])
        self.assertEqual(tree(outside), ["file", "sub", "sub/file"])
        self.assertEqual((outside / "file").read_bytes(), b"outside")
        self.assertEqual((outside / "sub" / "file").read_bytes(), b"outside")
        self.assertEqual({path: os.lstat(path).st_mode for path in modes}, modes)

    def test_a_root_that_is_a_symlink_is_never_followed(self) -> None:
        # A run that replaced the scratch directory with a link to a
        # directory of the user's: nothing there may go.
        if not can_symlink(self.temp):
            self.skipTest("this account can't create symlinks")
        outside = self.temp / "outside"
        (outside / "sub").mkdir(parents=True)
        (outside / "file").write_bytes(b"outside")
        link = self.temp / "link"
        os.symlink(outside, link, target_is_directory=True)
        self.assertIs(self.remove(root=link), False)
        self.assertEqual(tree(outside), ["file", "sub"])
        self.assertEqual((outside / "file").read_bytes(), b"outside")

    def test_a_kept_path_under_a_symlink_is_not_followed_either(self) -> None:
        if not can_symlink(self.temp):
            self.skipTest("this account can't create symlinks")
        outside = self.temp / "outside"
        outside.mkdir()
        (outside / "held.m4a").write_bytes(b"outside")
        (outside / "other").write_bytes(b"outside")
        os.symlink(outside, self.root / "results", target_is_directory=True)
        self.remove(keep=self.root / "results" / "held.m4a")
        self.assertFalse(os.path.lexists(self.root / "results"))
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["held.m4a", "other"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are POSIX")
    def test_fifos_go_without_being_opened(self) -> None:
        # Opening a FIFO waits for a writer: a clean-up that opened one would
        # hold the runner's lock for good.
        os.mkfifo(self.root / "fifo")
        (self.root / "sub" / "deeper").mkdir(parents=True)
        os.mkfifo(self.root / "sub" / "fifo")
        os.mkfifo(self.root / "sub" / "deeper" / "fifo")
        if not ROOT_USER:
            os.chmod(self.root / "sub" / "deeper", 0)
        self.assertIs(self.remove(), True)
        self.assertEqual(tree(self.root), [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "a tree built through directory descriptors (Linux)")
    def test_a_tree_deeper_than_the_recursion_limit_goes(self) -> None:
        # One-letter names keep the path under PATH_MAX, so only recursion is
        # at stake; the long names take the full path far past PATH_MAX, so
        # a clean-up by full path fails there too.
        depth = sys.getrecursionlimit() + 100
        for name in ("d", "deep-directory"):
            with self.subTest(name=name):
                self.addCleanup(remove_chain, self.root, name)
                make_chain(self.root, name, depth)
                self.assertIs(self.remove(), True)
                self.assertEqual(os.listdir(self.root), [])

    def test_a_directory_with_very_many_entries_goes(self) -> None:
        crowded = self.root / "crowded"
        crowded.mkdir()
        for index in range(10000):
            (crowded / f"f{index}").write_bytes(b"")
        for index in range(500):
            (crowded / f"d{index}").mkdir()
        self.assertIs(self.remove(), True)
        self.assertEqual(os.listdir(self.root), [])


# --- restore_scratch -----------------------------------------------------------


class RestoreScratchTest(TempTestCase):
    """restore_scratch(directory): the scratch directory and results/ are plain owner-only directories again."""

    def setUp(self) -> None:
        super().setUp()
        self.scratch = self.temp / "scratch"
        self.results = self.scratch / "results"

    def restore(self) -> None:
        seam(runner_server, "restore_scratch")(self.scratch)

    def assertUsable(self) -> None:
        for directory in (self.scratch, self.results):
            info = os.lstat(directory)
            self.assertTrue(stat.S_ISDIR(info.st_mode), f"{directory.name} is not a plain directory")
            if POSIX:
                self.assertEqual(info.st_mode & 0o777, 0o700, directory.name)
                self.assertEqual(info.st_uid, os.getuid(), directory.name)

    def test_a_missing_scratch_directory_is_made(self) -> None:
        self.restore()
        self.assertUsable()

    def test_a_file_in_place_of_either_is_replaced(self) -> None:
        self.scratch.write_bytes(b"not a directory")
        self.restore()
        self.assertUsable()
        self.results.rmdir()
        self.results.write_bytes(b"not a directory")
        self.restore()
        self.assertUsable()

    def test_a_symlink_in_place_of_either_is_replaced_and_never_followed(self) -> None:
        if not can_symlink(self.temp):
            self.skipTest("this account can't create symlinks")
        outside = self.temp / "outside"
        outside.mkdir()
        (outside / "file").write_bytes(b"outside")
        os.symlink(outside, self.scratch, target_is_directory=True)
        self.restore()
        self.assertUsable()
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["file"])
        self.results.rmdir()
        os.symlink(outside, self.results, target_is_directory=True)
        self.restore()
        self.assertUsable()
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["file"])
        self.assertEqual((outside / "file").read_bytes(), b"outside")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are POSIX")
    def test_a_fifo_in_place_of_results_is_replaced(self) -> None:
        self.scratch.mkdir(mode=0o700)
        os.mkfifo(self.results)
        run_bounded(self, self.restore, self.temp)
        self.assertUsable()

    @unittest.skipUnless(POSIX and not ROOT_USER, "POSIX permissions, as a user they bind")
    def test_modes_a_run_changed_are_owner_only_again(self) -> None:
        for scratch_mode, results_mode in ((0o777, 0o755), (0, 0o777), (0o500, 0)):
            with self.subTest(scratch=oct(scratch_mode), results=oct(results_mode)):
                # Each case from usable directories, whatever the last left.
                for directory in (self.scratch, self.results):
                    with contextlib.suppress(FileNotFoundError):
                        os.chmod(directory, 0o700)
                self.results.mkdir(parents=True, exist_ok=True)
                os.chmod(self.results, results_mode)
                os.chmod(self.scratch, scratch_mode)
                self.restore()
                self.assertUsable()

    def test_the_held_result_stays(self) -> None:
        self.results.mkdir(parents=True)
        held = self.results / f"{token()}.m4a"
        held.write_bytes(b"the held audio")
        self.restore()
        self.assertUsable()
        self.assertEqual(held.read_bytes(), b"the held audio")

    def test_a_directory_of_another_user_is_refused(self) -> None:
        self.results.mkdir(parents=True)
        uid = os.lstat(self.scratch).st_uid
        # Patched on the os module itself (create=True: Windows has no
        # getuid), as the start-up check's test does.
        with mock.patch.object(os, "getuid", create=True, return_value=uid + 1):
            with self.assertRaises(RuntimeError):
                self.restore()

    @unittest.skipUnless(POSIX and not ROOT_USER, "POSIX permissions, as a user they bind")
    def test_a_scratch_directory_that_cannot_be_made_again_raises(self) -> None:
        self.scratch.write_bytes(b"not a directory")
        os.chmod(self.temp, 0o500)
        self.addCleanup(os.chmod, self.temp, 0o700)
        with self.assertRaises(RuntimeError):
            self.restore()


# --- the decision about the rest of /tmp ---------------------------------------


class WideCleanupDecisionTest(unittest.TestCase):
    """may_clean_all_of_tmp(scratch_dir, sweep_strays): True only under the container's contract."""

    def decide(self, scratch_dir: Path, sweep_strays: bool, pid: int = 1, platform: str = "linux") -> Any:
        decide = seam(runner_server, "may_clean_all_of_tmp")
        with mock.patch.object(os, "getpid", lambda: pid), mock.patch.object(sys, "platform", platform):
            return decide(scratch_dir, sweep_strays)

    def test_the_contract_and_nothing_else(self) -> None:
        # Control: PID 1 on Linux, sweeping, with the contract's directory.
        self.assertIs(self.decide(SCRATCH_DIR, True), True)
        self.assertEqual(str(SCRATCH_DIR).replace(os.sep, "/"), "/tmp/strange_ringtone_runner")

    def test_never_unless_pid_1(self) -> None:
        for pid in (0, 2, 7, 4242):
            with self.subTest(pid=pid):
                self.assertIs(self.decide(SCRATCH_DIR, True, pid=pid), False)

    def test_never_without_the_sweep(self) -> None:
        # FETCH_ALLOW_NO_SWEEP's case: the runner serves, but nothing killed
        # what a run left running, which could still be writing in /tmp.
        self.assertIs(self.decide(SCRATCH_DIR, False), False)

    def test_never_off_linux(self) -> None:
        for platform in ("win32", "darwin", "cygwin", "freebsd14"):
            with self.subTest(platform=platform):
                self.assertIs(self.decide(SCRATCH_DIR, True, platform=platform), False)

    def test_never_with_any_other_scratch_directory(self) -> None:
        others = [
            Path("/tmp"),
            Path("/"),
            SCRATCH_DIR / "results",
            SCRATCH_DIR.parent / "strange_ringtone_runner2",
            SCRATCH_DIR.parent / "strange_ringtone_fetch",
            Path("tmp/strange_ringtone_runner"),
            Path("/var/tmp/strange_ringtone_runner"),
            Path("/tmp/../tmp/strange_ringtone_runner"),
            Path("/home/user/strange_ringtone_runner"),
            Path(tempfile.gettempdir()) / "scratch",
        ]
        for scratch_dir in others:
            with self.subTest(scratch_dir=str(scratch_dir)):
                self.assertIs(self.decide(scratch_dir, True), False)

    def test_the_tests_own_servers_never_qualify(self) -> None:
        # Every server these tests start has a temporary scratch directory,
        # even in the image where the test process really is PID 1.
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.assertIs(self.decide(Path(temp.name) / "scratch", True), False)


# --- the server ----------------------------------------------------------------


def post_mode(port: int, ytdlp: StubYtDlp, mode: str, video_id: str = VIDEO_ID) -> tuple[Response, str]:
    """POST a job the stub runs in mode; the answer, read at its last byte, and the token."""
    ytdlp.extra[MODE_VAR] = mode
    job = token()
    body = job_body(video_id, job)
    headers = [("Content-Type", JSON_TYPE), ("Content-Length", str(len(body)))]
    return exchange(port, build_request("POST", "/jobs", headers, body)), job


class LeftoverServerTestCase(RunnerServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(force_remove, self.temp)
        self.addCleanup(release_fifos, self.temp)
        # A developer's file next to the scratch directory, as in a shared /tmp.
        self.bystander = self.temp / "bystander"
        self.bystander.write_bytes(b"not the runner's")

    def post_mode(self, mode: str, video_id: str = VIDEO_ID) -> tuple[Response, str]:
        response, job = post_mode(self.port, self.ytdlp, mode, video_id)
        # Read at the answer's last byte, before the runner lets go of its
        # lock: what the clean-up did by now, it did before answering.
        self.at_answer = self.scratch_contents()
        wait_for(lambda: not self.server.busy.locked(), "the runner to let go of its lock")
        return response, job

    def assertOnlyHeld(self, job: str | None) -> None:
        for directory in (self.scratch, self.scratch / "results"):
            self.assertTrue(plain_directory(directory), f"{directory.name} is not a plain directory")
        self.assertEqual(self.scratch_contents(), [f"results/{job}.m4a"] if job else [])
        if POSIX:
            for directory in (self.scratch, self.scratch / "results"):
                self.assertEqual(os.lstat(directory).st_mode & 0o777, 0o700, directory.name)

    def assertNextJobWorks(self) -> None:
        response, job = self.post_mode("ok", OTHER_ID)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.json()["status"], "done", response.body)
        self.assertOnlyHeld(job)
        audio = self.audio(job)
        self.assertEqual(audio.status, 200, audio.body)
        self.assertEqual(audio.body, b"STUB-M4A\n" + OTHER_ID.encode("ascii"))

    def assertOutsideUntouched(self) -> None:
        # Not the container's contract here, so the clean-up never reaches
        # past the scratch directory.
        self.assertEqual(self.bystander.read_bytes(), b"not the runner's")
        self.assertTrue(self.runs_log.is_file())

    def other_lines(self) -> list[str]:
        """stderr lines that are not request log lines."""
        return [line for line in self.log.lines() if not LOG_LINE_RE.match(line)]


class LeftoverServerTest(LeftoverServerTestCase):
    """After each run, before the answer, the scratch directory holds only the job's result."""

    def test_a_runs_litter_is_gone_before_the_answer_and_its_result_stays(self) -> None:
        response, job = self.post_mode("litter")
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.json()["status"], "done", response.body)
        # The clean-up came before the answer.
        self.assertEqual(self.at_answer, [f"results/{job}.m4a"])
        self.assertOnlyHeld(job)
        audio = self.audio(job)
        self.assertEqual(audio.status, 200, audio.body)
        self.assertEqual(audio.body, b"STUB-M4A\n" + VIDEO_ID.encode("ascii"))
        # What the stub left above the scratch directory is outside it: here,
        # not under the contract, it stays.
        self.assertEqual((self.temp / LITTER_NAME).read_bytes(), LITTER_BYTES)
        self.assertTrue(os.path.lexists(self.temp / f"{LITTER_NAME}_dir"))
        self.assertOutsideUntouched()
        self.assertNextJobWorks()

    def test_a_file_in_place_of_results_does_not_wedge_the_runner(self) -> None:
        response, job = self.post_mode("wedge")
        self.assertEqual(response.json(), {"status": "failed", "error": "failed"})
        self.assertOnlyHeld(None)
        self.assertJsonError(self.audio(job), 404, "not_found")
        self.assertNextJobWorks()

    @unittest.skipUnless(POSIX, "a working directory can't be moved on Windows")
    def test_a_scratch_directory_moved_aside_is_made_again(self) -> None:
        response, _job = self.post_mode("displace")
        self.assertEqual(response.json(), {"status": "failed", "error": "failed"})
        self.assertOnlyHeld(None)
        # Moved outside the scratch directory: here it is not the runner's.
        self.assertTrue((self.temp / f"{LITTER_NAME}_scratch").is_dir())
        self.assertOutsideUntouched()
        self.assertNextJobWorks()

    @unittest.skipUnless(POSIX and not ROOT_USER, "POSIX permissions, as a user they bind")
    def test_a_scratch_directory_a_run_locked_is_opened_again(self) -> None:
        response, _job = self.post_mode("lock_scratch")
        self.assertEqual(response.json(), {"status": "failed", "error": "failed"})
        self.assertOnlyHeld(None)
        self.assertNextJobWorks()

    def test_a_clean_up_that_cannot_leave_a_usable_scratch_directory_fails_the_job_closed(self) -> None:
        seam(runner_server, "restore_scratch")

        def unusable(directory: Path) -> None:
            raise RuntimeError("scratch directory unusable")

        with mock.patch.object(runner_server, "restore_scratch", unusable):
            response, job = self.post_mode("ok")
        # The run itself succeeded; the answer is failed all the same, and
        # nothing of it is served.
        self.assertEqual(response.json(), {"status": "failed", "error": "failed"})
        self.assertJsonError(self.audio(job), 404, "not_found")
        self.assertStderrSaysScratch(job)
        # The runner neither exits nor changes its health answer.
        health = self.request("GET", "/healthz")
        self.assertEqual((health.status, health.body), (200, b"ok"))

    @unittest.skipUnless(POSIX and not ROOT_USER, "POSIX permissions, as a user they bind")
    def test_a_scratch_directory_that_cannot_be_made_again_fails_closed_and_says_so(self) -> None:
        # The stub takes the write permission off the directory above, as a
        # stand-in for a tmpfs where the directory can't be made again.
        self.addCleanup(os.chmod, self.temp, 0o700)
        response, job = self.post_mode("strand")
        self.assertEqual(response.json(), {"status": "failed", "error": "failed"})
        self.assertStderrSaysScratch(job)
        health = self.request("GET", "/healthz")
        self.assertEqual((health.status, health.body), (200, b"ok"))

    def assertStderrSaysScratch(self, job: str) -> None:
        lines = self.other_lines()
        self.assertEqual(len(lines), 1, self.log.getvalue())
        self.assertIn("scratch", lines[0].lower())
        for secret in (job, VIDEO_ID, "Stub"):
            self.assertNotIn(secret, lines[0])

    def test_as_pid_1_with_the_sweep_on_still_nothing_outside_the_scratch_directory(self) -> None:
        # The tests' own case in the image, where the process is PID 1: a
        # scratch directory that is not the contract's keeps the clean-up in
        # it. The sweep's calls are faked (Syscalls): no real kill(-1).
        scratch = self.temp / "sweeping"
        with Syscalls(pid=1).installed():
            settings = Settings(scratch_dir=scratch)
            ytdlp = StubYtDlp(600, MIB, 30, sweep_strays=True)
            server = _TestRunnerServer(("127.0.0.1", 0), settings, ytdlp=ytdlp, sweep_strays=True)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                response, job = post_mode(server.server_address[1], ytdlp, "litter")
                wait_for(lambda: not server.busy.locked(), "the runner to let go of its lock")
                left = tree(scratch)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(10)
        self.assertEqual(response.json()["status"], "done", response.body)
        self.assertEqual(left, ["results", f"results/{job}.m4a"])
        self.assertEqual((self.temp / LITTER_NAME).read_bytes(), LITTER_BYTES)
        self.assertOutsideUntouched()


class TmpCleanupWiringTest(LeftoverServerTestCase):
    """Which roots the server hands remove_leftovers, with the wide clean-up itself never run here."""

    def record_removals(self) -> list[tuple[Path, Path | None]]:
        real = seam(runner_server, "remove_leftovers")
        signature = inspect.signature(real)
        calls: list[tuple[Path, Path | None]] = []

        def recording(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            root, keep = Path(bound.arguments["root"]), bound.arguments.get("keep")
            calls.append((root, None if keep is None else Path(keep)))
            # Only what lies in the test's scratch directory is really
            # cleaned; a wider root is recorded and left alone.
            if inside(root, self.scratch):
                return real(*args, **kwargs)
            return True

        patcher = mock.patch.object(runner_server, "remove_leftovers", recording)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_the_scratch_directory_is_cleaned_keeping_the_held_result(self) -> None:
        calls = self.record_removals()
        response, job = self.post_mode("litter")
        self.assertEqual(response.json()["status"], "done", response.body)
        held = self.scratch / "results" / f"{job}.m4a"
        self.assertTrue([call for call in calls if same_path(call[0], self.scratch)
                         and call[1] is not None and same_path(call[1], held)], calls)

    def test_otherwise_no_root_outside_the_scratch_directory(self) -> None:
        calls = self.record_removals()
        for mode in ("litter", "wedge", "fail"):
            with self.subTest(mode=mode):
                self.post_mode(mode)
        self.assertTrue(calls, "remove_leftovers was never called")
        for root, _keep in calls:
            self.assertTrue(inside(root, self.scratch), f"a clean-up outside the scratch directory: {root}")
        self.assertOutsideUntouched()

    def test_under_the_contract_the_rest_of_tmp_goes_around_the_scratch_directory(self) -> None:
        seam(runner_server, "may_clean_all_of_tmp")
        decisions: list[tuple[Any, ...]] = []

        def contract(*args: Any, **kwargs: Any) -> bool:
            decisions.append((args, kwargs))
            return True

        calls = self.record_removals()
        with mock.patch.object(runner_server, "may_clean_all_of_tmp", contract):
            # A server started under the patch, so a decision taken once at
            # start-up is seen as well as one taken for each job.
            self.close_server()
            self.start()
            response, _job = self.post_mode("litter")
        self.assertEqual(response.json()["status"], "done", response.body)
        self.assertTrue(decisions, "the decision was never asked")
        wide = [(root, keep) for root, keep in calls if not inside(root, self.scratch)]
        self.assertTrue(wide, f"no clean-up around the scratch directory: {calls}")
        for root, keep in wide:
            self.assertTrue(same_path(root, self.scratch.parent) or root.as_posix() == "/tmp", root)
            self.assertIsNotNone(keep)
            assert keep is not None
            self.assertTrue(same_path(keep, self.scratch), keep)

    def test_the_decision_is_asked_with_the_servers_scratch_directory_and_sweep(self) -> None:
        real = seam(runner_server, "may_clean_all_of_tmp")
        signature = inspect.signature(real)
        asked: list[dict[str, Any]] = []

        def recording(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind(*args, **kwargs)
            asked.append(dict(bound.arguments))
            return real(*args, **kwargs)

        with mock.patch.object(runner_server, "may_clean_all_of_tmp", recording):
            self.close_server()
            self.start()
            self.post_mode("ok")
        self.assertTrue(asked, "the decision was never asked")
        for arguments in asked:
            self.assertTrue(same_path(arguments["scratch_dir"], self.scratch), arguments)
            self.assertIs(arguments["sweep_strays"], False)


# --- the docs ------------------------------------------------------------------


PLAN = helpers.ROOT / "docs" / "PLAN.md"


@unittest.skipUnless(PLAN.is_file(), "docs/PLAN.md is not here (the image's test run mounts tests/ only)")
class RisksWrittenDownTest(unittest.TestCase):
    """docs/PLAN.md 6.1 says what a run can still do: stop the runner with a signal it handles."""

    def section(self) -> str:
        text = PLAN.read_text(encoding="utf-8")
        start = text.index("### 6.1 ")
        return " ".join(text[start:text.index("\n## 7. ", start)].split())

    def test_the_signals_a_run_can_send_are_accepted_risks(self) -> None:
        section = self.section()
        for signal_name in ("SIGTERM", "SIGINT"):
            self.assertTrue(signal_name in section, f"docs/PLAN.md 6.1 never names {signal_name}")

    def test_clearing_tmp_is_no_longer_called_unbuilt(self) -> None:
        unbuilt = "clearing `/tmp` at the run boundary is not built yet"
        self.assertFalse(unbuilt in self.section(), f"docs/PLAN.md 6.1 still says {unbuilt!r}")


# --- in the image, as PID 1 ----------------------------------------------------


@contextlib.contextmanager
def serving(server: _TestRunnerServer) -> Iterator[None]:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


@unittest.skipUnless(IN_IMAGE, "the wide clean-up runs only in the fetcher image's test container")
class RealTmpCleanupTest(unittest.TestCase):
    """In the image, as PID 1 with the contract's scratch directory: a run's leftovers in all of /tmp go."""

    def setUp(self) -> None:
        # A failure, not a skip: see test_fetcher_runner_sweep.IN_IMAGE.
        self.assertEqual(os.getpid(), 1, "the in-image suite must run as its container's PID 1, with no init")
        self.tmp = SCRATCH_DIR.parent
        self.clear()
        self.addCleanup(self.clear)
        self.addCleanup(kill_and_reap)
        self.log = LogCapture()
        redirect = contextlib.redirect_stderr(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.ytdlp = StubYtDlp(600, MIB, 30, sweep_strays=True)
        self.server = _TestRunnerServer(("127.0.0.1", 0), Settings(scratch_dir=SCRATCH_DIR), ytdlp=self.ytdlp,
                                        sweep_strays=True)
        context = serving(self.server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def clear(self) -> None:
        # What this test and the stub made, so that a failure here leaves no
        # full tmpfs to the rest of the suite.
        release_fifos(self.tmp / "strange_ringtone_runner")
        for name in os.listdir(self.tmp):
            if name == "strange_ringtone_runner" or name.startswith(LITTER_NAME):
                force_remove(self.tmp / name)

    def post(self, mode: str, video_id: str = VIDEO_ID) -> tuple[Response, str]:
        response, job = post_mode(self.server.server_address[1], self.ytdlp, mode, video_id)
        wait_for(lambda: not self.server.busy.locked(), "the runner to let go of its lock")
        return response, job

    def assertOnlyScratchInTmp(self, job: str | None) -> None:
        self.assertEqual(os.listdir(self.tmp), ["strange_ringtone_runner"])
        for directory in (SCRATCH_DIR, SCRATCH_DIR / "results"):
            self.assertTrue(plain_directory(directory), f"{directory} is not a plain directory")
        held = ["results", f"results/{job}.m4a"] if job else ["results"]
        self.assertEqual(tree(SCRATCH_DIR), held)
        for directory in (SCRATCH_DIR, SCRATCH_DIR / "results"):
            self.assertEqual(os.lstat(directory).st_mode & 0o777, 0o700, directory)

    def assertNextJobWorks(self) -> None:
        response, job = self.post("ok", OTHER_ID)
        self.assertEqual(response.json()["status"], "done", response.body)
        self.assertOnlyScratchInTmp(job)

    def test_litter_in_tmp_and_in_the_scratch_directory_goes_and_the_next_job_runs(self) -> None:
        response, job = self.post("litter")
        self.assertEqual(response.json()["status"], "done", response.body)
        self.assertOnlyScratchInTmp(job)
        self.assertNextJobWorks()

    def test_a_full_tmpfs_is_emptied_and_the_next_job_runs(self) -> None:
        response, job = self.post("fill")
        self.assertEqual(response.json()["status"], "done", response.body)
        self.assertOnlyScratchInTmp(job)
        self.assertNextJobWorks()

    def test_a_file_in_place_of_results_does_not_wedge_the_runner(self) -> None:
        response, _job = self.post("wedge")
        self.assertEqual(response.json(), {"status": "failed", "error": "failed"})
        self.assertOnlyScratchInTmp(None)
        self.assertNextJobWorks()

    def test_a_scratch_directory_moved_aside_is_made_again(self) -> None:
        response, _job = self.post("displace")
        self.assertEqual(response.json(), {"status": "failed", "error": "failed"})
        self.assertOnlyScratchInTmp(None)
        self.assertNextJobWorks()

    def test_a_scratch_directory_a_run_locked_is_opened_again(self) -> None:
        response, _job = self.post("lock_scratch")
        self.assertEqual(response.json(), {"status": "failed", "error": "failed"})
        self.assertOnlyScratchInTmp(None)
        self.assertNextJobWorks()


if __name__ == "__main__":
    unittest.main()

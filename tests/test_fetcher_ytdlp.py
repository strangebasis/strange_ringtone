"""The yt-dlp run (strange_ringtone_runner.ytdlp) against a stub yt-dlp (tests/stubs/ytdlp_stub.py).

yt-dlp runs only in the runner's container (docs/PLAN.md 6.1), so the run
code lives in the runner's package; the metadata whitelist and the result
types live in strange_ringtone_fetch.metadata, which the runner and the
fetcher share. The stub checks the argument list it receives and behaves by
video ID, so these tests pin the exact command, the environment, the prlimit
guards, the timeout and process-group kill, the error mapping and the
metadata whitelist without the network or yt-dlp, on Windows and Linux alike.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import math
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import helpers
from helpers import alive
from strange_ringtone_fetch.metadata import (
    MAX_START_SECONDS,
    FetchError,
    LongVideo,
    Metadata,
    clean_duration,
    clean_heatmap,
    clean_start,
    clean_title,
    clean_window_start,
    metadata_to_info,
    parse_info,
)
from strange_ringtone_runner import window, ytdlp
from strange_ringtone_runner.ytdlp import (
    DEFAULT_COMMAND,
    DENO,
    PRLIMIT,
    WINDOW_COMMAND,
    YTDLP_PYTHON,
    Limits,
    YtDlp,
    default_prlimit,
    map_failure,
    watch_url,
    window_args,
    window_ytdlp_args,
    ytdlp_args,
)

STUB = helpers.ROOT / "tests" / "stubs" / "ytdlp_stub.py"
STUB_COMMAND = (sys.executable, "-I", str(STUB))
# The stand-in for the runner's window program, the second child of a job
# with a start time (tests/stubs/window_stub.py).
WINDOW_STUB = helpers.ROOT / "tests" / "stubs" / "window_stub.py"
WINDOW_STUB_COMMAND = (sys.executable, "-I", str(WINDOW_STUB))
MODE_VAR = "STRANGE_RINGTONE_YTDLP_STUB"
WINDOW_MODE_VAR = "STRANGE_RINGTONE_WINDOW_STUB"
# One log for both stubs: a job's children in the order they ran.
LOG_VAR = "STRANGE_RINGTONE_YTDLP_STUB_LOG"
VIDEO_ID = "dQw4w9WgXcQ"
MIB = 1024 * 1024


def stub_title(video_id: str) -> str:
    return f"Stub Title {video_id} \u00e9"


class StubYtDlp(YtDlp):
    """The runner's real yt-dlp run with the stub as its program, plus a log the stub appends to.

    The run builds the child's environment from scratch, so the stub's own
    variables are added here, in the tests only. Imported by the fetcher's,
    the jobs' and the runner's tests.
    """

    def __init__(self, *args: Any, log: Path | None = None, mode: str | None = None,
                 window_mode: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("command", STUB_COMMAND)
        # Never the real window program: it would open a connection.
        kwargs.setdefault("window_command", WINDOW_STUB_COMMAND)
        super().__init__(*args, **kwargs)
        self.extra: dict[str, str] = {}
        if log is not None:
            self.extra[LOG_VAR] = str(log)
        if mode is not None:
            self.extra[MODE_VAR] = mode
        if window_mode is not None:
            self.extra[WINDOW_MODE_VAR] = window_mode

    def child_env(self, job_dir: Path) -> dict[str, str]:
        env = super().child_env(job_dir)
        env.update(self.extra)
        return env


def read_pid(pid_file: Path, timeout: float = 10) -> int | None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            text = pid_file.read_text(encoding="ascii").strip()
        except OSError:
            text = ""
        if text:
            return int(text)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass


class VideoIdTest(unittest.TestCase):
    def test_valid_ids_build_the_watch_url(self) -> None:
        for video_id in (VIDEO_ID, "a" * 11, "-_-_-_-_-_-", "AZaz09_-AZ0"):
            with self.subTest(video_id=video_id):
                self.assertEqual(watch_url(video_id), f"https://www.youtube.com/watch?v={video_id}")

    def test_anything_else_is_refused(self) -> None:
        for video_id in (
            "", "a" * 10, "a" * 12, "dQw4w9WgXc.", "..........a", "dQw4w9WgX%2", "dQw4w9WgX/c", "dQw4w9WgX c",
            "dQw4w9WgXc\n", "\ndQw4w9WgXc", "dQw4w9WgX\u0665c", "dQw4w9WgX\uff41c", "dQw4w9WgX\u00e9c",
            "dQw4w9WgXc&", "dQw4w9WgX#c", "dQw4w9WgX?c", "dQw4w9WgX=c", "-" * 10 + "\x00",
            "https://ww", None, 12345678901, b"dQw4w9WgXcQ",
        ):
            with self.subTest(video_id=video_id):
                with self.assertRaises(ValueError):
                    watch_url(video_id)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    ytdlp_args(video_id, Path("job"), 600, MIB)  # type: ignore[arg-type]


class ArgumentsTest(unittest.TestCase):
    def test_the_argument_list_is_pinned(self) -> None:
        job = Path("scratch") / "job-x"
        self.assertEqual(ytdlp_args(VIDEO_ID, job, 600, 41943040), [
            "--ignore-config", "--no-playlist", "--use-extractors", "youtube",
            "--js-runtimes", "deno:/usr/local/bin/deno", "--no-cache-dir",
            "-f", "bestaudio[ext=m4a]",
            "--break-match-filters", "duration <= 600 & !is_live",
            "--print-to-file", "pre_process:%(.{title,duration,heatmap,is_live})j", "%(id)s.pre.json",
            "--max-filesize", "41943040",
            "--write-info-json", "--quiet", "--no-warnings", "--no-progress",
            "--socket-timeout", "20",
            "-P", str(job),
            "-o", "%(id)s.%(ext)s",
            "--", f"https://www.youtube.com/watch?v={VIDEO_ID}",
        ])

    def test_the_argument_list_of_a_job_with_a_start_time_is_pinned(self) -> None:
        job = Path("scratch") / "job-x"
        self.assertEqual(window_ytdlp_args(VIDEO_ID, job), [
            "--ignore-config", "--no-playlist", "--use-extractors", "youtube",
            "--js-runtimes", "deno:/usr/local/bin/deno", "--no-cache-dir",
            "-f", "bestaudio[ext=m4a]",
            "--break-match-filters", "!is_live",
            "--skip-download",
            "--write-info-json", "--quiet", "--no-warnings", "--no-progress",
            "--socket-timeout", "20",
            "-P", str(job),
            "-o", "%(id)s.%(ext)s",
            "--", f"https://www.youtube.com/watch?v={VIDEO_ID}",
        ])

    def test_a_job_with_a_start_time_never_downloads_through_yt_dlp(self) -> None:
        args = window_ytdlp_args(VIDEO_ID, Path("job"))
        self.assertIn("--skip-download", args)
        # Every guard of the download list that is not about the download.
        download = ytdlp_args(VIDEO_ID, Path("job"), 600, MIB)
        for guard in ("--ignore-config", "--no-playlist", "--use-extractors", "--js-runtimes", "--no-cache-dir",
                      "--quiet", "--no-warnings", "--no-progress", "--socket-timeout", "--"):
            self.assertIn(guard, args)
            self.assertIn(guard, download)
        self.assertEqual(args[args.index("--use-extractors") + 1], "youtube")
        self.assertEqual(args[-2:], ["--", f"https://www.youtube.com/watch?v={VIDEO_ID}"])
        for video_id in ("", "a" * 12, "dQw4w9WgXc\n", None):
            with self.subTest(video_id=video_id), self.assertRaises(ValueError):
                window_ytdlp_args(video_id, Path("job"))  # type: ignore[arg-type]

    def test_the_window_program_s_arguments_are_three_paths_and_three_numbers(self) -> None:
        job = Path("scratch") / "job-x"
        self.assertEqual(window_args(VIDEO_ID, job, 4500, 600, 41943040), [
            str(job / f"{VIDEO_ID}.info.json"), str(job / f"{VIDEO_ID}.m4a"), str(job / f"{VIDEO_ID}.window.json"),
            "4500", "600", "41943040",
        ])
        self.assertEqual(window_args(VIDEO_ID, job, 0, 1, 1)[3:], ["0", "1", "1"])
        self.assertEqual(window_args(VIDEO_ID, job, MAX_START_SECONDS, 600, MIB)[3], "359999")

    def test_a_start_that_is_no_whole_number_of_seconds_never_becomes_an_argument(self) -> None:
        for start in (-1, MAX_START_SECONDS + 1, 1.0, 1.5, True, False, "5", "", None, [5], float("nan"),
                      10**30):
            with self.subTest(start=start), self.assertRaises(ValueError):
                window_args(VIDEO_ID, Path("job"), start, 600, MIB)  # type: ignore[arg-type]
        for video_id in ("", "a" * 12, "../../etc/pw", "dQw4w9WgXc/", None):
            with self.subTest(video_id=video_id), self.assertRaises(ValueError):
                window_args(video_id, Path("job"), 5, 600, MIB)  # type: ignore[arg-type]

    def test_the_fields_written_before_the_filter_are_pinned(self) -> None:
        args = ytdlp_args(VIDEO_ID, Path("job"), 600, MIB)
        at = args.index("--print-to-file")
        # At the pre_process stage, which runs before the filter, and into a
        # file named after the ID only, in the job directory (-P).
        self.assertEqual(args[at + 1], "pre_process:%(.{title,duration,heatmap,is_live})j")
        self.assertEqual(args[at + 2], "%(id)s.pre.json")
        # --print would make the run a simulation: nothing would be downloaded.
        self.assertNotIn("--print", args)
        self.assertNotIn("-O", args)
        self.assertNotIn("--print-to-file", window_ytdlp_args(VIDEO_ID, Path("job")))

    def test_the_url_comes_last_after_the_option_guard(self) -> None:
        args = ytdlp_args("-_-_-_-_-_-", Path("job"), 1, 1)
        self.assertEqual(args[-2:], ["--", "https://www.youtube.com/watch?v=-_-_-_-_-_-"])
        # The only argument that holds the ID, and only inside the fixed URL.
        self.assertEqual([arg for arg in args if "-_-_-_-_-_-" in arg], [args[-1]])

    def test_a_plain_match_filter_is_never_used(self) -> None:
        # A plain --match-filters skip exits 0 having written nothing, which
        # would hide a long or live video behind failed.
        for args in (ytdlp_args(VIDEO_ID, Path("job"), 600, MIB), window_ytdlp_args(VIDEO_ID, Path("job"))):
            self.assertNotIn("--match-filters", args)
            self.assertNotIn("--match-filter", args)

    def test_fixed_program_paths(self) -> None:
        self.assertEqual(PRLIMIT, "/usr/bin/prlimit")
        self.assertEqual(DENO, "/usr/local/bin/deno")
        # I1: yt-dlp runs from the image's venv, never from the server's own
        # interpreter, whose site-packages must hold no third-party wheel.
        self.assertEqual(YTDLP_PYTHON, "/opt/yt-dlp/bin/python")
        self.assertEqual(DEFAULT_COMMAND, (YTDLP_PYTHON, "-I", "-m", "yt_dlp"))

    def test_the_window_program_runs_by_path_on_the_runner_s_own_interpreter(self) -> None:
        # Never yt-dlp's venv: the program is stdlib only, and the base
        # interpreter's site-packages hold nothing (I1).
        # Beside ytdlp.py, wherever the package lies: the checkout, or
        # /app/fetcher in the image.
        program = Path(ytdlp.__file__).with_name("window.py")
        self.assertEqual(program, Path(window.__file__))
        self.assertEqual(WINDOW_COMMAND, (sys.executable, "-I", str(program)))
        self.assertTrue(program.is_file())
        self.assertNotIn(YTDLP_PYTHON, WINDOW_COMMAND)
        self.assertEqual(YtDlp(600, MIB, 7).window_command, WINDOW_COMMAND)

    def test_the_window_program_s_statuses_map_to_the_fixed_codes(self) -> None:
        # ytdlp.py does not import the program, so the numbers are written
        # out there; this keeps the two files in step.
        self.assertEqual(ytdlp.WINDOW_FAILURES, {
            window.EXIT_PAST_END: "past_end", window.EXIT_BLOCKED: "blocked", window.EXIT_TIMED_OUT: "timeout",
        })
        for code in ytdlp.WINDOW_FAILURES.values():
            self.assertIn(code, ytdlp.ERROR_CODES)
        self.assertNotIn(window.EXIT_OK, ytdlp.WINDOW_FAILURES)
        self.assertNotIn(window.EXIT_FAILED, ytdlp.WINDOW_FAILURES)
        self.assertNotIn(window.EXIT_USAGE, ytdlp.WINDOW_FAILURES)


class CommandTest(unittest.TestCase):
    def test_full_command_with_prlimit(self) -> None:
        # I1: the venv's interpreter, written out, after prlimit's guard.
        runner = YtDlp(600, 41943040, 120, prlimit=(PRLIMIT,))
        self.assertEqual(
            runner.full_command(["--ignore-config"]),
            ["/usr/bin/prlimit", "--cpu=120", "--fsize=42991616", "--nofile=256", "--nproc=512", "--",
             "/opt/yt-dlp/bin/python", "-I", "-m", "yt_dlp", "--ignore-config"],
        )

    def test_no_address_space_limit(self) -> None:
        # Deno reserves a huge virtual range at startup; RLIMIT_AS would kill it.
        command = YtDlp(600, MIB, 120, prlimit=(PRLIMIT,)).full_command([])
        self.assertFalse([part for part in command if part.startswith("--as")])

    def test_limits_follow_the_settings_unless_given(self) -> None:
        self.assertEqual(YtDlp(600, 2 * MIB, 7, prlimit=(PRLIMIT,)).full_command([])[1:3],
                         ["--cpu=7", f"--fsize={3 * MIB}"])
        limits = Limits(cpu_seconds=1, file_size_bytes=2, open_files=3, processes=4)
        self.assertEqual(
            YtDlp(600, MIB, 7, command=("yt",), prlimit=(PRLIMIT,), limits=limits).full_command([]),
            [PRLIMIT, "--cpu=1", "--fsize=2", "--nofile=3", "--nproc=4", "--", "yt"],
        )

    def test_full_command_without_prlimit(self) -> None:
        self.assertEqual(YtDlp(600, MIB, 7, command=STUB_COMMAND).full_command(["-a"]), [*STUB_COMMAND, "-a"])

    def test_the_window_program_runs_under_the_same_limits(self) -> None:
        runner = YtDlp(600, 41943040, 120, prlimit=(PRLIMIT,), window_command=("python", "-I", "window.py"))
        self.assertEqual(
            runner.full_command(["a", "b"], runner.window_command),
            ["/usr/bin/prlimit", "--cpu=120", "--fsize=42991616", "--nofile=256", "--nproc=512", "--",
             "python", "-I", "window.py", "a", "b"],
        )

    def test_prlimit_always_on_linux_and_never_elsewhere(self) -> None:
        with mock.patch.object(ytdlp.sys, "platform", "linux"), \
                mock.patch("os.path.exists", side_effect=AssertionError("looked on disk")):
            self.assertEqual(default_prlimit(), (PRLIMIT,))
        with mock.patch.object(ytdlp.sys, "platform", "win32"):
            self.assertIsNone(default_prlimit())

    def test_construction_touches_no_file(self) -> None:
        with mock.patch("os.stat", side_effect=AssertionError("stat")), \
                mock.patch("os.path.exists", side_effect=AssertionError("exists")):
            YtDlp(600, MIB, 7, command=("/nonexistent/python",), prlimit=("/nonexistent/prlimit",))

    def test_bad_construction(self) -> None:
        for args in ((0, MIB, 7), (600, 0, 7), (600, MIB, 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                YtDlp(*args)
        with self.assertRaises(ValueError):
            YtDlp(600, MIB, 7, command=())
        with self.assertRaises(ValueError):
            YtDlp(600, MIB, 7, window_command=())


class EnvironmentTest(unittest.TestCase):
    def expected(self, job: Path, **extra: str) -> dict[str, str]:
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(job),
            "DENO_DIR": str(job / "deno"),
            "DENO_NO_UPDATE_CHECK": "1",
            "NO_COLOR": "1",
            "LC_ALL": "C.UTF-8",
        }
        env.update(extra)
        if sys.platform == "win32":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        return env

    def test_the_environment_is_built_from_scratch(self) -> None:
        job = Path("scratch") / "job-x"
        secrets = {"API_TOKEN": "s3cr3t", "PYTHONPATH": "/evil", "HTTPS_PROXY": "http://unset.example.internal:1",
                   "LD_PRELOAD": "/evil.so", "DENO_DIR": "/evil"}
        with mock.patch.dict(os.environ, secrets):
            self.assertEqual(YtDlp(600, MIB, 7).child_env(job), self.expected(job))

    def test_only_the_configured_proxies_are_added(self) -> None:
        job = Path("job")
        proxies = (("HTTPS_PROXY", "http://proxy.example.internal:3128"), ("NO_PROXY", "localhost"))
        self.assertEqual(
            YtDlp(600, MIB, 7, proxies=proxies).child_env(job),
            self.expected(job, HTTPS_PROXY="http://proxy.example.internal:3128", NO_PROXY="localhost"),
        )

    def test_the_child_sees_exactly_that_environment(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            job = Path(temp) / "job"
            job.mkdir()
            log = Path(temp) / "runs.jsonl"
            runner = StubYtDlp(600, MIB, 30, log=log)
            with mock.patch.dict(os.environ, {"API_TOKEN": "s3cr3t", "STRANGE_RINGTONE_SECRET": "x"}):
                runner.fetch(VIDEO_ID, job)
            entry = json.loads(log.read_text(encoding="utf-8"))
        env = entry["env"]
        if sys.platform == "win32":
            env = {key.upper(): value for key, value in env.items()}
        self.assertEqual(env, self.expected(job, **{LOG_VAR: str(log)}))
        self.assertNotIn("s3cr3t", json.dumps(entry))


class MappingTest(unittest.TestCase):
    def test_exit_101_is_never_decided_from_what_was_printed(self) -> None:
        # A filter's refusal: the run's own files say why (read_rejection),
        # or the run has only one filter. Nothing printed turns it into a
        # refusal by YouTube.
        self.assertEqual(map_failure(101, b""), "failed")
        self.assertEqual(map_failure(101, b"ERROR: Sign in to confirm you're not a bot"), "failed")
        self.assertEqual(map_failure(101, b"Video unavailable"), "failed")

    def test_blocked_phrases(self) -> None:
        for text in (
            "ERROR: [youtube] x: Sign in to confirm you're not a bot",
            # Each phrase on its own, so no alternative of the pattern
            # hides behind another one in the same message.
            "ERROR: [youtube] x: Sign in to confirm your age",
            "ERROR: are you NOT A BOT",
            "ERROR: captcha required",
            "ERROR: PO Token required",
            "ERROR: unable to download: HTTP Error 403: Forbidden",
            "ERROR: This content isn't available",
            "ERROR: This content isn\u2019t available",
        ):
            with self.subTest(text=text):
                self.assertEqual(map_failure(1, text.encode("utf-8")), "blocked")

    def test_not_found_phrases(self) -> None:
        for text in ("Video unavailable", "This is a private video", "has been removed by the uploader",
                     "this video does not exist"):
            with self.subTest(text=text):
                self.assertEqual(map_failure(1, text.encode("utf-8")), "not_found")

    def test_everything_else_is_failed(self) -> None:
        for code, text in ((1, b"Requested format is not available"), (2, b""), (-9, b""), (1, b"\xff\xfe"),
                           (0, b"File is larger than max-filesize"), (0, b"Sign in to confirm")):
            with self.subTest(code=code, text=text):
                self.assertEqual(map_failure(code, text), "failed")

    def test_error_codes_are_the_fixed_set(self) -> None:
        self.assertEqual(ytdlp.ERROR_CODES, ("blocked", "live", "not_found", "past_end", "timeout", "failed"))
        with self.assertRaises(ValueError):
            FetchError("Stub Title")


class MetadataTest(unittest.TestCase):
    def test_title_is_cleaned(self) -> None:
        cases = {
            "Plain": "Plain",
            "  Lots \t of\n\nspace  ": "Lots of space",
            "Bad\x00\x07\x1b[31mRed": "Bad[31mRed",
            "\u202eevil\u202c \u200bzero\ufeff": "evil zero",
            "line\u2028two\u2029three": "line two three",
            "e\u0301": "\u00e9",
            "\ue000private\U000f0000": "private",
            "\ud800lone": "lone",
            "x" * 300: "x" * 200,
            "x" * 199 + " y": "x" * 199,
            # A tab between words is a space, not nothing.
            "a	b": "a b",
            "a b": "a b",
            # Unassigned code points (Cn) go too.
            "a͸b󠂀": "ab",
        }
        for raw, cleaned in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(clean_title(raw), cleaned)

    def test_missing_or_empty_titles_are_omitted(self) -> None:
        for raw in (None, 5, ["t"], {"t": 1}, "", "  ", "\x00\u200b"):
            with self.subTest(raw=raw):
                self.assertIsNone(clean_title(raw))

    def test_duration(self) -> None:
        self.assertEqual(clean_duration(215), 215)
        self.assertIsInstance(clean_duration(215), int)
        self.assertEqual(clean_duration(215.5), 215.5)
        for raw in (0, -1, math.inf, math.nan, True, "215", None, [215]):
            with self.subTest(raw=raw):
                self.assertIsNone(clean_duration(raw))

    def point(self, start: Any = 0.0, end: Any = 1.0, value: Any = 0.5) -> dict[str, Any]:
        return {"start_time": start, "end_time": end, "value": value}

    def test_heatmap_is_re_emitted(self) -> None:
        raw = [self.point(0, 1.5, 0), self.point(1.5, 3, 1), {**self.point(2.5, 3, 0.25), "extra": "x"}]
        self.assertEqual(clean_heatmap(raw, 3), (
            {"start": 0.0, "end": 1.5, "value": 0.0},
            {"start": 1.5, "end": 3.0, "value": 1.0},
            {"start": 2.5, "end": 3.0, "value": 0.25},
        ))

    def test_heatmap_may_overrun_whole_second_duration_slightly(self) -> None:
        # yt-dlp's duration is whole seconds; the last point ends at the exact length.
        self.assertEqual(clean_heatmap([self.point(200, 215.4, 1)], 215),
                         ({"start": 200.0, "end": 215.0, "value": 1.0},))
        self.assertIsNone(clean_heatmap([self.point(200, 216.5, 1)], 215))
        # A point wholly inside the overrun covers none of the duration: it is
        # left out rather than sent as a zero-length point, which the page
        # would refuse together with the rest of the list.
        self.assertEqual(clean_heatmap([self.point(200, 215, 1), self.point(215, 215.4, 0.5)], 215),
                         ({"start": 200.0, "end": 215.0, "value": 1.0},))
        self.assertEqual(clean_heatmap([self.point(214, 215.2, 1), self.point(215.2, 215.9, 0.5)], 215),
                         ({"start": 214.0, "end": 215.0, "value": 1.0},))
        self.assertIsNone(clean_heatmap([self.point(215, 215.4, 1)], 215))

    def test_every_emitted_heatmap_is_one_the_page_accepts(self) -> None:
        # The conditions of validHeatmap in core/static/audio/suggest.js,
        # against the reported case: 100 even segments of a 45.9 s video
        # whose yt-dlp duration is 45, so the last ones run past it.
        for length, duration in ((45.9, 45), (30.99, 30), (215.4, 215), (60.0, 60)):
            raw = [self.point(i * length / 100, (i + 1) * length / 100, 0.5) for i in range(100)]
            with self.subTest(length=length):
                points = clean_heatmap(raw, duration)
                assert points is not None
                self.assertLessEqual(len(points), 200)
                for point in points:
                    self.assertEqual(set(point), {"start", "end", "value"})
                    self.assertTrue(0 <= point["start"] < point["end"] <= duration, point)
                    self.assertTrue(0 <= point["value"] <= 1, point)

    def test_bad_heatmaps_are_dropped_whole(self) -> None:
        good = self.point()
        cases = {
            "not a list": {"0": good},
            "empty": [],
            "too long": [good] * 201,
            "not a dict": [good, [0, 1, 0.5]],
            "missing key": [good, {"start_time": 0, "end_time": 1}],
            "negative start": [good, self.point(-0.1, 1)],
            "end before start": [good, self.point(2, 1)],
            # The page refuses a list with one zero-length point (suggest.js).
            "zero length": [good, self.point(5, 5)],
            "zero length at the end": [good, self.point(10, 10)],
            "past the duration": [good, self.point(1, 12)],
            "value above 1": [good, self.point(value=1.01)],
            "value below 0": [good, self.point(value=-0.01)],
            "nan": [good, self.point(value=math.nan)],
            "inf": [good, self.point(end=math.inf)],
            "bool": [good, self.point(value=True)],
            "string": [good, self.point(start="0")],
        }
        for label, raw in cases.items():
            with self.subTest(label):
                self.assertIsNone(clean_heatmap(raw, 10))
        self.assertIsNone(clean_heatmap([good], None))
        self.assertEqual(len(clean_heatmap([good] * 200, 10) or ()), 200)

    def test_only_three_keys_are_kept(self) -> None:
        info = {"title": "T", "duration": 10, "heatmap": [self.point()], "uploader": "U", "description": "D",
                "url": "https://example.internal/x", "formats": [], "id": VIDEO_ID}
        metadata = parse_info(info)
        self.assertEqual(metadata, ytdlp.Metadata(title="T", duration=10,
                                                  heatmap=({"start": 0.0, "end": 1.0, "value": 0.5},)))

    def test_a_duration_that_is_not_a_positive_number_is_dropped(self) -> None:
        # The status answer promises a number; with no duration the heatmap,
        # which is checked against it, goes too.
        for raw in ("600", -5, 0, True, math.inf, math.nan, None, [600]):
            with self.subTest(raw=raw):
                metadata = parse_info({"title": "T", "duration": raw, "heatmap": [self.point()]})
                self.assertEqual(metadata, ytdlp.Metadata(title="T", duration=None, heatmap=None))
        self.assertEqual(parse_info({"duration": 215}).duration, 215)

    def test_info_that_is_not_an_object_fails(self) -> None:
        for info in ([], "x", None, 5):
            with self.subTest(info=info), self.assertRaises(FetchError) as raised:
                parse_info(info)
            self.assertEqual(raised.exception.code, "failed")


def _stub_module() -> Any:
    # The stub's own info builder, so the round trip starts from exactly what
    # the stub (and so the runner's tests) hands the runner.
    spec = importlib.util.spec_from_file_location("ytdlp_stub_for_metadata", STUB)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MetadataRoundTripTest(unittest.TestCase):
    """S1/R1: the runner answers metadata_to_info(parse_info(info)) and the fetcher runs parse_info on it again.

    One whitelist applied on both sides only works if the second pass reads
    back exactly what the first one kept, through the JSON that crosses `jobs`.
    """

    def point(self, start: Any, end: Any, value: Any) -> dict[str, Any]:
        return {"start_time": start, "end_time": end, "value": value}

    def infos(self) -> dict[str, Any]:
        stub = _stub_module()
        duration = stub.DURATION
        return {
            "the stub's info": {
                "id": VIDEO_ID, "title": stub_title(VIDEO_ID), "duration": duration,
                "heatmap": stub.heatmap(duration, stub.HEATMAP_POINTS),
                "uploader": "Stub Uploader", "description": "stub description",
                "webpage_url": f"https://www.youtube.com/watch?v={VIDEO_ID}", "formats": [],
            },
            # Whole-second duration, last points past it: cleaning clips one
            # and drops the other, and the clipped list must read back as is.
            "a clipped heatmap": {
                "title": "T", "duration": 215,
                "heatmap": [self.point(200, 214, 0.5), self.point(214, 215.2, 1), self.point(215.2, 215.9, 0.5)],
            },
            "a title only": {"title": "  Only ‮a​ title\n"},
            "nothing kept": {"id": VIDEO_ID, "uploader": "U"},
        }

    def test_metadata_to_info_round_trips_through_parse_info(self) -> None:
        for label, info in self.infos().items():
            with self.subTest(label):
                metadata = parse_info(info)
                answered = metadata_to_info(metadata)
                self.assertEqual(parse_info(answered), metadata)
                # What really crosses `jobs` is JSON: the tuple of points
                # becomes a list, and the fetcher must still read it back.
                wire = json.loads(json.dumps(answered))
                self.assertEqual(parse_info(wire), metadata)
                if metadata.duration is not None:
                    # A whole-second duration stays an int, as the page gets it today.
                    self.assertIs(type(parse_info(wire).duration), type(metadata.duration))
        empty = Metadata(None, None, None)
        self.assertEqual(metadata_to_info(empty), {})
        self.assertEqual(parse_info(metadata_to_info(empty)), empty)
        clipped = parse_info(self.infos()["a clipped heatmap"])
        assert clipped.heatmap is not None
        self.assertEqual(clipped.heatmap[-1], {"start": 214.0, "end": 215.0, "value": 1.0})

    def test_metadata_to_info_emits_only_the_three_keys_in_yt_dlps_names(self) -> None:
        metadata = parse_info(self.infos()["the stub's info"])
        answered = metadata_to_info(metadata)
        self.assertEqual(set(answered), {"title", "duration", "heatmap"})
        self.assertEqual(answered["title"], stub_title(VIDEO_ID))
        self.assertEqual(answered["duration"], 215.5)
        assert metadata.heatmap is not None
        self.assertEqual(len(answered["heatmap"]), len(metadata.heatmap))
        for sent, kept in zip(answered["heatmap"], metadata.heatmap, strict=True):
            # yt-dlp's names, so parse_info reads them; never the page's.
            self.assertEqual(set(sent), {"start_time", "end_time", "value"})
            self.assertEqual((sent["start_time"], sent["end_time"], sent["value"]),
                             (kept["start"], kept["end"], kept["value"]))
        text = json.dumps(answered)
        for never in ("uploader", "description", "webpage_url", "formats", '"id"', "Stub Uploader"):
            self.assertNotIn(never, text)
        # A value that is None is left out, not sent as null.
        self.assertEqual(metadata_to_info(Metadata("T", None, None)), {"title": "T"})
        self.assertEqual(metadata_to_info(Metadata(None, 10, None)), {"duration": 10})


class StubTestCase(unittest.TestCase):
    """A job directory and a log of every stub run."""

    def setUp(self) -> None:
        # ignore_cleanup_errors: on Windows a killed run's grandchild may still
        # hold the directory as its working directory for a moment.
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.job = self.temp / "job"
        self.job.mkdir()
        self.runs_log = self.temp / "runs.jsonl"

    def runner(self, timeout: int = 30, max_bytes: int = MIB, **kwargs: Any) -> StubYtDlp:
        return StubYtDlp(600, max_bytes, timeout, log=self.runs_log, **kwargs)

    def runs(self) -> list[dict[str, Any]]:
        if not self.runs_log.exists():
            return []
        return [json.loads(line) for line in self.runs_log.read_text(encoding="utf-8").splitlines()]

    def job_names(self) -> list[str]:
        return sorted(p.name for p in self.job.iterdir())

    def fetch_error(self, video_id: str, **kwargs: Any) -> str:
        with self.assertRaises(FetchError) as raised:
            self.runner(**kwargs).fetch(video_id, self.job)
        return raised.exception.code


class FetchTest(StubTestCase):
    def test_ok(self) -> None:
        download = self.runner().fetch(VIDEO_ID, self.job)
        self.assertEqual(download.audio, self.job / f"{VIDEO_ID}.m4a")
        self.assertEqual(download.audio.read_bytes(), b"STUB-M4A\n" + VIDEO_ID.encode("ascii"))
        metadata = download.metadata
        self.assertEqual(metadata.title, stub_title(VIDEO_ID))
        self.assertEqual(metadata.duration, 215.5)
        assert metadata.heatmap is not None
        self.assertEqual(len(metadata.heatmap), 100)
        for point in metadata.heatmap:
            self.assertEqual(sorted(point), ["end", "start", "value"])
            self.assertTrue(0 <= point["start"] <= point["end"] <= 215.5)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.runs()[0]["argv"], ytdlp_args(VIDEO_ID, self.job, 600, MIB))

    def test_failures_map_to_the_fixed_codes(self) -> None:
        cases = {
            "liveStream_": "live",
            # A filter's refusal that left no word about why.
            "rejected___": "failed",
            "blocked____": "blocked",
            "notFound___": "not_found",
            "failing____": "failed",
            # Exit 0 is not success on its own: the files decide.
            "oversize___": "failed",
            "noAudio____": "failed",
            "notADict___": "failed",
        }
        for video_id, code in cases.items():
            with self.subTest(video_id=video_id):
                self.assertEqual(self.fetch_error(video_id, max_bytes=1024), code)

    def test_a_failed_exit_is_failure_even_with_both_files(self) -> None:
        # Success needs exit 0 and the files: a rejected or failed run that
        # still left <id>.m4a and <id>.info.json behind is not a download.
        for video_id, code in (("tooLongFile", "failed"), ("failedFiles", "failed")):
            with self.subTest(video_id=video_id):
                self.assertEqual(self.fetch_error(video_id), code)
                self.assertTrue((self.job / f"{video_id}.m4a").is_file())
                self.assertTrue((self.job / f"{video_id}.info.json").is_file())

    def test_a_long_video_is_not_downloaded_and_not_an_error(self) -> None:
        with self.assertRaises(LongVideo) as raised:
            self.runner().fetch("tooLong____", self.job)
        metadata = raised.exception.metadata
        self.assertEqual(metadata.title, stub_title("tooLong____"))
        self.assertEqual(metadata.duration, 3600)
        assert metadata.heatmap is not None
        self.assertEqual(len(metadata.heatmap), 100)
        # Only what yt-dlp wrote before its filter: no audio, no info JSON.
        self.assertEqual(self.job_names(), ["tooLong____.pre.json"])
        # One run: the long video costs no second look here.
        self.assertEqual(len(self.runs()), 1)
        self.assertNotIsInstance(raised.exception, FetchError)

    def test_a_live_stream_and_a_bare_refusal_write_no_audio(self) -> None:
        self.assertEqual(self.fetch_error("liveStream_"), "live")
        self.assertEqual(self.job_names(), ["liveStream_.pre.json"])
        self.assertEqual(self.fetch_error("rejected___"), "failed")
        self.assertEqual(self.job_names(), ["liveStream_.pre.json"])

    def test_the_length_that_makes_a_video_long_is_the_runner_s_own(self) -> None:
        # The stub's long video lasts an hour; a video of 215.5 s is long
        # only for a runner that fetches less. The stub does not apply the
        # filter itself, so this shows what read_rejection makes of exit 101.
        self.assertEqual(self.fetch_error("tooLongFile"), "failed")
        runner = StubYtDlp(200, MIB, 30, log=self.runs_log)
        with self.assertRaises(LongVideo) as raised:
            runner.fetch("tooLongFile", self.job)
        self.assertEqual(raised.exception.metadata.duration, 215.5)

    def test_metadata_problems_drop_only_that_value(self) -> None:
        runner = self.runner()
        self.assertEqual(runner.fetch("ctrlTitle__", self.job).metadata.title, "Bad Title line two")
        self.assertIsNone(runner.fetch("hugeHeatmap", self.job).metadata.heatmap)
        self.assertIsNone(runner.fetch("badHeatmap_", self.job).metadata.heatmap)
        self.assertEqual(runner.fetch("bareInfo___", self.job).metadata, ytdlp.Metadata(None, None, None))

    def test_wrong_arguments_fail(self) -> None:
        # The stub refuses any list but the pinned one (exit 2).
        with mock.patch.object(ytdlp, "ytdlp_args", lambda *a: [*ytdlp_args(*a)[:-1], "https://example.internal/"]):
            self.assertEqual(self.fetch_error(VIDEO_ID), "failed")
        self.assertEqual(self.runs(), [])

    def test_missing_program_fails_cleanly(self) -> None:
        runner = YtDlp(600, MIB, 30, command=(str(self.temp / "missing-python"),))
        with self.assertRaises(FetchError) as raised:
            runner.fetch(VIDEO_ID, self.job)
        self.assertEqual(raised.exception.code, "failed")

    def test_output_never_leaves_the_runner(self) -> None:
        # The stub prints the title on stdout and stderr, as yt-dlp prints the
        # destination; none of it may reach this process's streams or an error.
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.runner().fetch(VIDEO_ID, self.job)
            for video_id in ("blocked____", "failing____", "notFound___"):
                with self.assertRaises(FetchError) as raised:
                    self.runner().fetch(video_id, self.job)
                self.assertNotIn("Stub", str(raised.exception))
                self.assertEqual(raised.exception.args, (raised.exception.code,))
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_success_needs_regular_files_within_their_limits(self) -> None:
        runner = self.runner()
        runner.fetch(VIDEO_ID, self.job)
        audio = self.job / f"{VIDEO_ID}.m4a"
        info = self.job / f"{VIDEO_ID}.info.json"
        # An empty audio file.
        audio.write_bytes(b"")
        with self.assertRaises(FetchError):
            ytdlp.read_result(self.job, VIDEO_ID, MIB)
        audio.write_bytes(b"x" * 11)
        self.assertEqual(ytdlp.read_result(self.job, VIDEO_ID, 11).audio, audio)
        with self.assertRaises(FetchError):
            ytdlp.read_result(self.job, VIDEO_ID, 10)
        # An info JSON past its size limit is never parsed: 4 MiB, a few times
        # what a real one holds. Valid JSON, so only the cap refuses it.
        self.assertEqual(ytdlp.INFO_MAX_BYTES, 4 * MIB)
        info.write_bytes(b'{"title": "T"}' + b" " * (4 * MIB + 1 - 14))
        self.assertEqual(info.stat().st_size, 4 * MIB + 1)
        with self.assertRaises(FetchError) as raised:
            ytdlp.read_result(self.job, VIDEO_ID, MIB)
        self.assertEqual(raised.exception.code, "failed")
        info.write_bytes(b'{"title": "T"}' + b" " * (4 * MIB - 14))
        self.assertEqual(ytdlp.read_result(self.job, VIDEO_ID, MIB).metadata.title, "T")
        with mock.patch.object(ytdlp, "INFO_MAX_BYTES", info.stat().st_size - 1), self.assertRaises(FetchError):
            ytdlp.read_result(self.job, VIDEO_ID, MIB)
        # A directory where the audio should be.
        audio.unlink()
        audio.mkdir()
        with self.assertRaises(FetchError):
            ytdlp.read_result(self.job, VIDEO_ID, MIB)
        audio.rmdir()
        audio.write_bytes(b"x")
        for bad in (b"{", b"[1, 2]", b"\xff", b"[" * 100000 + b"]" * 100000):
            with self.subTest(info=bad[:10]):
                info.write_bytes(bad)
                with self.assertRaises(FetchError):
                    ytdlp.read_result(self.job, VIDEO_ID, MIB)

    def test_a_path_that_is_a_link_is_refused_before_it_is_opened(self) -> None:
        # Windows has no O_NOFOLLOW, so the lstat is what refuses a symlink
        # there; reported as one here, whatever the platform.
        self.runner().fetch(VIDEO_ID, self.job)
        link = os.stat_result((stat.S_IFLNK | 0o777,) + tuple(os.lstat(self.job / f"{VIDEO_ID}.m4a"))[1:])
        with mock.patch.object(ytdlp.os, "lstat", return_value=link), \
                mock.patch.object(ytdlp.os, "open", side_effect=AssertionError("opened")):
            with self.assertRaises(FetchError):
                ytdlp.read_result(self.job, VIDEO_ID, MIB)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW is POSIX")
    def test_the_open_itself_never_follows_a_symlink(self) -> None:
        # As if the link appeared between the lstat and the open.
        self.runner().fetch(VIDEO_ID, self.job)
        audio = self.job / f"{VIDEO_ID}.m4a"
        outside = self.temp / "outside.m4a"
        os.replace(audio, outside)
        os.symlink(outside, audio)
        regular = os.lstat(outside)
        with mock.patch.object(ytdlp.os, "lstat", return_value=regular), self.assertRaises(FetchError):
            ytdlp.read_result(self.job, VIDEO_ID, MIB)

    @unittest.skipUnless(os.name == "posix", "a directory opens for reading on POSIX only")
    def test_the_open_file_must_be_regular_too(self) -> None:
        self.runner().fetch(VIDEO_ID, self.job)
        audio = self.job / f"{VIDEO_ID}.m4a"
        regular = os.lstat(audio)
        audio.unlink()
        audio.mkdir()
        with mock.patch.object(ytdlp.os, "lstat", return_value=regular), self.assertRaises(FetchError):
            ytdlp.read_result(self.job, VIDEO_ID, MIB)

    def test_a_symlinked_audio_file_is_refused(self) -> None:
        try:
            os.symlink(self.temp / "nowhere", self.temp / "probe")
        except (OSError, NotImplementedError):
            self.skipTest("this account can't create symlinks")
        self.assertEqual(self.fetch_error("symlinked__"), "failed")


class SubprocessGuardsTest(StubTestCase):
    """The guards CLAUDE.md requires of every subprocess: list, no shell, fixed program, timeout."""

    def spy(self) -> mock.MagicMock:
        spy = mock.MagicMock(wraps=subprocess.Popen)
        patcher = mock.patch.object(ytdlp.subprocess, "Popen", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return spy

    def test_popen_gets_a_list_no_shell_and_a_fresh_environment(self) -> None:
        spy = self.spy()
        runner = self.runner()
        runner.fetch(VIDEO_ID, self.job)
        self.assertEqual(spy.call_count, 1)
        (command,), kwargs = spy.call_args
        self.assertIsInstance(command, list)
        self.assertTrue(all(isinstance(part, str) for part in command))
        self.assertEqual(command, runner.full_command(ytdlp_args(VIDEO_ID, self.job, 600, MIB)))
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("executable", kwargs)
        self.assertNotIn("preexec_fn", kwargs)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        # Nothing reads yt-dlp's stdout (map_failure looks at stderr, and
        # read_result at the files), so it is never piped to the runner.
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.PIPE)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(kwargs["cwd"], self.job)
        self.assertEqual(kwargs["env"], runner.child_env(self.job))

    def test_the_program_is_the_fixed_path(self) -> None:
        # I1: what Popen really gets is the venv's interpreter, alone or
        # behind prlimit, followed by exactly DEFAULT_COMMAND's options.
        spy = mock.MagicMock(side_effect=OSError("not here"))
        for prlimit, program in ((None, YTDLP_PYTHON), ((PRLIMIT,), PRLIMIT)):
            with self.subTest(prlimit=prlimit), mock.patch.object(ytdlp.subprocess, "Popen", spy):
                with self.assertRaises(FetchError):
                    YtDlp(600, MIB, 30, prlimit=prlimit).fetch(VIDEO_ID, self.job)
                (command,), _ = spy.call_args
                self.assertEqual(command[0], program)
                # assertIn first, so a command without the venv fails here
                # rather than erroring in index().
                self.assertIn(YTDLP_PYTHON, command)
                self.assertEqual(command[command.index(YTDLP_PYTHON):][:4], [YTDLP_PYTHON, "-I", "-m", "yt_dlp"])

    def test_the_default_command_never_uses_the_servers_interpreter(self) -> None:
        # I1: the server's interpreter holds no third-party wheel (pip is
        # removed from it), so a command built on sys.executable could not
        # import yt-dlp; and if it could, the wheel's .pth lines and scripts
        # would be code in the server. Both the constant and the runner the
        # server builds by default are checked, so a default computed
        # elsewhere can't slip back to sys.executable.
        self.assertNotIn(sys.executable, DEFAULT_COMMAND)
        self.assertNotIn(sys.executable, YtDlp(600, MIB, 30).command)
        self.assertNotIn(sys.executable, YtDlp(600, MIB, 30, prlimit=(PRLIMIT,)).full_command([]))

    def test_the_run_has_the_wall_clock_timeout(self) -> None:
        # Pinned by what happens, not by the call that waits: the run is
        # given exactly timeout_seconds, neither less nor unbounded.
        started = time.monotonic()
        self.assertEqual(self.fetch_error("hanging____", timeout=2), "timeout")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 2)
        self.assertLess(elapsed, 2 + ytdlp.DRAIN_SECONDS + 3)


class TimeoutTest(StubTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(ytdlp, "DRAIN_SECONDS", 0.5)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.procs: list[subprocess.Popen[bytes]] = []
        self.popen = subprocess.Popen

        def recording(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            proc = self.popen(*args, **kwargs)
            self.procs.append(proc)
            return proc

        patcher = mock.patch.object(ytdlp.subprocess, "Popen", recording)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertReaped(self, pipes_closed: bool = True) -> None:
        self.assertEqual(len(self.procs), 1)
        proc = self.procs[0]
        self.assertIsNotNone(proc.returncode, "the timed-out run was never reaped")
        if pipes_closed:
            # stdout is not a pipe at all (SubprocessGuardsTest); stderr is,
            # and must be closed once the run is over.
            self.assertIsNone(proc.stdout)
            assert proc.stderr is not None
            deadline = time.monotonic() + 5
            while not proc.stderr.closed and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(proc.stderr.closed, "stderr left open")

    def test_a_hung_run_times_out(self) -> None:
        started = time.monotonic()
        self.assertEqual(self.fetch_error("hanging____", timeout=1), "timeout")
        self.assertLess(time.monotonic() - started, 1 + 0.5 + 5)
        self.assertReaped()

    def test_the_kill_is_followed_by_a_bounded_drain(self) -> None:
        # A grandchild that holds stderr open past the kill (here the kill
        # reaches the direct child only) keeps the pipe from ever reaching
        # EOF: the drain gives up after DRAIN_SECONDS (0.5 here) and the run
        # still ends as a timeout.
        pid_file = self.job / "grandchild.pid"
        started = time.monotonic()
        with mock.patch.object(ytdlp, "_kill_group", lambda proc: proc.kill()):
            self.assertEqual(self.fetch_error("grandchild_", timeout=1), "timeout")
        elapsed = time.monotonic() - started
        grandchild = read_pid(pid_file, timeout=5)
        self.assertIsNotNone(grandchild, "the stub never started its grandchild")
        assert grandchild is not None
        kill_quietly(grandchild)
        self.assertGreaterEqual(elapsed, 1)
        self.assertLess(elapsed, 1 + 0.5 + 3)
        # Once the grandchild is gone, the pipe reaches EOF and is closed.
        self.assertReaped()

    def test_the_whole_process_group_dies(self) -> None:
        pid_file = self.job / "grandchild.pid"
        self.assertEqual(self.fetch_error("grandchild_", timeout=2), "timeout")
        grandchild = read_pid(pid_file, timeout=5)
        self.assertIsNotNone(grandchild, "the stub never started its grandchild")
        assert grandchild is not None
        self.addCleanup(kill_quietly, grandchild)
        if not hasattr(os, "killpg"):
            # Windows has no process groups; only the direct child can be ended.
            self.assertReaped(pipes_closed=False)
            return
        self.assertReaped()
        deadline = time.monotonic() + 5
        while alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(grandchild), "the grandchild survived the timeout")

    def test_an_interrupted_wait_kills_the_group_and_propagates(self) -> None:
        pid_file = self.job / "grandchild.pid"
        real_wait = self.popen.wait
        real_communicate = self.popen.communicate
        for error in (KeyboardInterrupt, OSError):
            with self.subTest(error=error.__name__):
                self.procs.clear()
                pid_file.unlink(missing_ok=True)

                # The first wait for the run, whichever call makes it
                # (communicate() or wait()), is interrupted; the waits of the
                # clean-up after it are the real ones.
                interruptions = [error]

                def interrupted(proc: subprocess.Popen[bytes], input: Any = None, timeout: float | None = None,
                                interruptions: list[type[BaseException]] = interruptions) -> Any:
                    if interruptions:
                        read_pid(pid_file)
                        raise interruptions.pop()()
                    return real_communicate(proc, input, timeout)

                def interrupted_wait(proc: subprocess.Popen[bytes], timeout: float | None = None,
                                     interruptions: list[type[BaseException]] = interruptions) -> Any:
                    if interruptions:
                        read_pid(pid_file)
                        raise interruptions.pop()()
                    return real_wait(proc, timeout)

                started = time.monotonic()
                with mock.patch.object(self.popen, "communicate", interrupted), \
                        mock.patch.object(self.popen, "wait", interrupted_wait):
                    with self.assertRaises(error):
                        self.runner().fetch("grandchild_", self.job)
                grandchild = read_pid(pid_file, timeout=0)
                self.assertIsNotNone(grandchild, "the stub never started its grandchild")
                assert grandchild is not None
                self.addCleanup(kill_quietly, grandchild)
                self.assertLess(time.monotonic() - started, 10 + 5)
                self.assertReaped()
                if not hasattr(os, "killpg"):
                    continue
                self.assertEqual(self.procs[0].returncode, -signal.SIGKILL)
                deadline = time.monotonic() + 5
                while alive(grandchild) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(alive(grandchild), "the grandchild survived the interrupted run")

    def test_a_survivor_holding_the_pipes_cannot_hang_the_fetch(self) -> None:
        started = time.monotonic()
        with mock.patch.object(ytdlp, "_kill_group", lambda proc: None):
            self.assertEqual(self.fetch_error("hanging____", timeout=1), "timeout")
        self.assertLess(time.monotonic() - started, 1 + 0.5 + 5)
        self.assertReaped()

    @unittest.skipUnless(hasattr(os, "killpg"), "process groups are POSIX")
    def test_the_kill_targets_the_group_not_just_the_child(self) -> None:
        with mock.patch.object(ytdlp.os, "killpg", wraps=os.killpg) as killpg:
            self.assertEqual(self.fetch_error("hanging____", timeout=1), "timeout")
        self.assertEqual(killpg.call_count, 1)
        self.assertEqual(killpg.call_args[0][1], signal.SIGKILL)

    def test_cancel_kills_a_running_fetch(self) -> None:
        runner = self.runner(timeout=60)
        failures: list[str] = []

        def run() -> None:
            try:
                runner.fetch("hanging____", self.job)
            except FetchError as error:
                failures.append(error.code)

        thread = threading.Thread(target=run)
        started = time.monotonic()
        thread.start()
        deadline = time.monotonic() + 10
        while not self.runs() and time.monotonic() < deadline:
            time.sleep(0.05)
        runner.cancel()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(failures, ["failed"])
        self.assertReaped()


@unittest.skipUnless(hasattr(os, "killpg"), "process groups are POSIX")
class RunEndKillTest(StubTestCase):
    """R3: nothing a run started outlives it, even when the run succeeds."""

    def test_a_child_left_behind_by_a_successful_run_is_killed(self) -> None:
        # A child that closed its streams lets communicate() return at once;
        # without the group kill at the end of every run it would keep the
        # runner's reach between jobs and could edit the next job's files.
        download = self.runner().fetch("survivor___", self.job)
        # Acceptance: the run itself is a normal success.
        self.assertEqual(download.audio, self.job / "survivor___.m4a")
        self.assertEqual(download.metadata.title, stub_title("survivor___"))
        survivor = read_pid(self.job / "survivor.pid", timeout=5)
        self.assertIsNotNone(survivor, "the stub never started its survivor")
        assert survivor is not None
        self.addCleanup(kill_quietly, survivor)
        deadline = time.monotonic() + 2
        while alive(survivor) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(survivor), "a child of a successful run outlived it")

    def test_the_group_is_killed_once_per_run(self) -> None:
        # One kill on success as on the other paths (TimeoutTest pins those),
        # and with the whole group's signal, not the leader's alone.
        with mock.patch.object(ytdlp.os, "killpg", wraps=os.killpg) as killpg:
            self.runner().fetch(VIDEO_ID, self.job)
            with self.assertRaises(FetchError):
                self.runner().fetch("failing____", self.job)
        self.assertEqual(killpg.call_count, 2)
        for call in killpg.call_args_list:
            self.assertEqual(call[0][1], signal.SIGKILL)


@unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are POSIX")
class FifoResultTest(StubTestCase):
    """R3: a FIFO where the audio should be is refused at once, never waited on."""

    def test_open_regular_never_blocks_on_a_fifo(self) -> None:
        # As if the FIFO replaced the file between the lstat and the open:
        # without O_NONBLOCK the open would wait for a writer, holding the
        # run, and with it the runner's lock, for good.
        regular_file = self.temp / "regular"
        regular_file.write_bytes(b"x")
        regular = os.lstat(regular_file)
        fifo = self.job / "fifo.m4a"
        os.mkfifo(fifo)
        outcome: list[Any] = []

        def run() -> None:
            outcome.append(ytdlp._open_regular(fifo, MIB))

        with mock.patch.object(ytdlp.os, "lstat", return_value=regular):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(1.0)
        if thread.is_alive():
            # Free the blocked open, so the thread ends and the directory can go.
            writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(writer)
            thread.join(5.0)
            self.fail("_open_regular blocked on a FIFO")
        self.assertEqual(outcome, [None])
        # Acceptance: the same call opens the regular file.
        opened = ytdlp._open_regular(regular_file, MIB)
        self.assertIsNotNone(opened)
        assert opened is not None
        os.close(opened[0])
        self.assertEqual(opened[1], 1)


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.exists(PRLIMIT), "util-linux prlimit")
class PrlimitTest(StubTestCase):
    def test_the_run_has_the_limits(self) -> None:
        import resource

        self.runner(timeout=20, max_bytes=2 * MIB, prlimit=(PRLIMIT,)).fetch(VIDEO_ID, self.job)
        (run,) = self.runs()
        self.assertEqual(run["limits"], {
            # No address-space limit, on purpose (Deno).
            "RLIMIT_AS": resource.RLIM_INFINITY,
            "RLIMIT_CPU": 20,
            "RLIMIT_FSIZE": 3 * MIB,
            "RLIMIT_NOFILE": 256,
            "RLIMIT_NPROC": 512,
        })


class OutputBoundTest(StubTestCase):
    """What a run prints costs the runner a bounded tail of stderr, never the whole output.

    The runner is its container's PID 1 under a 512 MB memory limit: holding
    a run's whole output, as communicate() does, lets a run that prints a few
    hundred megabytes end the runner. The stub's loud modes print far more
    than the tail on both streams.
    """

    BLOCKED_LINE = b"Sign in to confirm you're not a bot\n"

    def tail_bytes(self) -> int:
        tail = getattr(ytdlp, "STDERR_TAIL_BYTES", None)
        if tail is None:
            raise AssertionError("strange_ringtone_runner.ytdlp.STDERR_TAIL_BYTES is missing")
        return tail

    def test_the_tail_is_64_kib(self) -> None:
        # Enough for the last lines map_failure reads, which yt-dlp prints
        # last; far less than the stub's floods.
        self.assertEqual(self.tail_bytes(), 64 * 1024)
        self.assertGreater(_stub_module().LOUD_BYTES, 100 * self.tail_bytes())

    def test_memory_stays_bounded_and_the_failure_is_mapped_as_before(self) -> None:
        self.tail_bytes()
        tracemalloc.start()
        try:
            code = self.fetch_error(VIDEO_ID, mode="loud")
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(code, "blocked")
        # The stub printed 16 MiB on each stream; the run may keep about
        # the tail plus a read buffer or two.
        self.assertLess(peak, 4 * MIB, f"the run held {peak} bytes of a run's output")

    def test_map_failure_sees_only_the_tail_and_its_end(self) -> None:
        tail = self.tail_bytes()
        seen: list[bytes] = []
        real = ytdlp.map_failure

        def recording(returncode: int, stderr: bytes) -> str:
            seen.append(bytes(stderr))
            return real(returncode, stderr)

        with mock.patch.object(ytdlp, "map_failure", recording):
            self.assertEqual(self.fetch_error(VIDEO_ID, mode="loud"), "blocked")
        self.assertEqual(len(seen), 1)
        # Exactly the last STDERR_TAIL_BYTES the stub printed: its error
        # line last, after the flood.
        self.assertEqual(len(seen[0]), tail)
        self.assertTrue(seen[0].endswith(self.BLOCKED_LINE), seen[0][-200:])
        self.assertIn(_stub_module().LOUD_LINE, seen[0])

    def test_a_flood_never_blocks_a_successful_run(self) -> None:
        # Both pipes are read (or not piped) to the end while the run goes
        # on: a child that fills a pipe nobody reads would hang until the
        # timeout.
        self.tail_bytes()
        started = time.monotonic()
        tracemalloc.start()
        try:
            download = self.runner(timeout=60, mode="loud_ok").fetch(VIDEO_ID, self.job)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(download.audio.read_bytes(), b"STUB-M4A\n" + VIDEO_ID.encode("ascii"))
        self.assertLess(time.monotonic() - started, 30)
        self.assertLess(peak, 4 * MIB, f"the run held {peak} bytes of a run's output")


class StartTest(unittest.TestCase):
    def test_a_start_is_a_whole_number_of_seconds_within_the_bound(self) -> None:
        for value in (0, 1, 600, 4500, MAX_START_SECONDS):
            with self.subTest(value=value):
                self.assertEqual(clean_start(value), value)
        # 99:59:59.
        self.assertEqual(MAX_START_SECONDS, 99 * 3600 + 59 * 60 + 59)

    def test_anything_else_is_no_start(self) -> None:
        for value in (-1, MAX_START_SECONDS + 1, 10**30, True, False, 0.0, 12.0, 12.5, float("nan"), float("inf"),
                      "12", "", b"12", None, [12], {"start": 12}):
            with self.subTest(value=value):
                self.assertIsNone(clean_start(value))

    def test_a_window_begins_at_or_before_the_start_asked_for(self) -> None:
        self.assertEqual(clean_window_start(120.0, 125, 600), 120.0)
        self.assertEqual(clean_window_start(0, 0, 600), 0.0)
        self.assertEqual(clean_window_start(0, 125, 600), 0.0)
        self.assertEqual(clean_window_start(125, 125, 600), 125.0)
        # One second of slack, for rounding and audio that begins just after zero.
        self.assertEqual(clean_window_start(126.0, 125, 600), 126.0)
        for value in (126.01, 500, -0.001, -1, float("nan"), float("inf"), "120", True, None, [120], 10**400):
            with self.subTest(value=value):
                self.assertIsNone(clean_window_start(value, 125, 600))

    def test_a_window_begins_less_than_its_longest_length_before_the_start(self) -> None:
        # A window holds its start, so one that began any earlier would have to be longer than allowed.
        self.assertEqual(clean_window_start(2400, 3000, 600), 2400.0)
        self.assertEqual(clean_window_start(2990.5, 3000, 10), 2990.5)
        self.assertEqual(clean_window_start(0, 600, 600), 0.0)
        for value, start, max_seconds in ((2399.99, 3000, 600), (0, 3000, 600), (2989.9, 3000, 10), (0, 601, 600)):
            with self.subTest(value=value, start=start, max_seconds=max_seconds):
                self.assertIsNone(clean_window_start(value, start, max_seconds))

    def test_a_long_video_always_has_a_length(self) -> None:
        self.assertEqual(LongVideo(Metadata("T", 3600, None)).metadata, Metadata("T", 3600, None))
        with self.assertRaises(ValueError):
            LongVideo(Metadata("T", None, None))


class RejectionTest(unittest.TestCase):
    """What a download run's exit 101 means, from the file yt-dlp wrote before its filter looked."""

    HEATMAP = [{"start_time": 0.0, "end_time": 36.0, "value": 0.5},
               {"start_time": 36.0, "end_time": 72.0, "value": 1.0}]

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.job = Path(temp.name)
        self.pre = self.job / f"{VIDEO_ID}.pre.json"

    def write(self, content: Any) -> None:
        self.pre.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode("utf-8"))

    def outcome(self, max_seconds: int = 600) -> str:
        result = ytdlp.read_rejection(self.job, VIDEO_ID, max_seconds)
        if isinstance(result, LongVideo):
            return "long"
        assert isinstance(result, FetchError)
        return result.code

    def test_a_video_longer_than_the_runner_fetches_is_long(self) -> None:
        self.write({"title": "  A \x07 title\n", "duration": 3600, "heatmap": self.HEATMAP, "is_live": False})
        result = ytdlp.read_rejection(self.job, VIDEO_ID, 600)
        assert isinstance(result, LongVideo)
        self.assertEqual(result.metadata.title, "A title")
        self.assertEqual(result.metadata.duration, 3600)
        self.assertEqual(result.metadata.heatmap, (
            {"start": 0.0, "end": 36.0, "value": 0.5}, {"start": 36.0, "end": 72.0, "value": 1.0},
        ))

    def test_yt_dlp_s_line_end_after_the_object_is_no_problem(self) -> None:
        self.write(json.dumps({"duration": 3600, "is_live": False}).encode("ascii") + os.linesep.encode("ascii"))
        self.assertEqual(self.outcome(), "long")

    def test_the_length_is_compared_with_the_runner_s_own_limit(self) -> None:
        for duration, max_seconds, outcome in ((600, 600, "failed"), (600.5, 600, "long"), (601, 600, "long"),
                                               (215.5, 600, "failed"), (215.5, 200, "long"), (1, 1, "failed"),
                                               (2, 1, "long")):
            with self.subTest(duration=duration, max_seconds=max_seconds):
                self.write({"duration": duration, "is_live": False})
                self.assertEqual(self.outcome(max_seconds), outcome)

    def test_a_live_stream_is_live_whatever_else_the_file_says(self) -> None:
        for pre in ({"is_live": True}, {"is_live": True, "duration": None}, {"is_live": True, "duration": 3600},
                    {"is_live": True, "duration": 5, "title": "T"}):
            with self.subTest(pre=pre):
                self.write(pre)
                self.assertEqual(self.outcome(), "live")

    def test_only_the_json_true_means_live(self) -> None:
        for value in ("true", 1, "yes", [True], {"live": True}, None, False):
            with self.subTest(value=value):
                self.write({"is_live": value, "duration": 3600})
                self.assertEqual(self.outcome(), "long")
                self.write({"is_live": value, "duration": 100})
                self.assertEqual(self.outcome(), "failed")

    def test_a_file_that_names_neither_reason_is_a_failure(self) -> None:
        for pre in ({}, {"duration": None}, {"duration": "3600"}, {"duration": -5}, {"duration": 0}, {"duration": True},
                    {"title": "long"}, [{"duration": 3600}], "long", 3600, None, True):
            with self.subTest(pre=pre):
                self.write(pre)
                self.assertEqual(self.outcome(), "failed")
        for raw in (b'{"duration": NaN}', b'{"duration": Infinity}', b'{"duration": 1e999}'):
            with self.subTest(raw=raw):
                self.write(raw)
                self.assertEqual(self.outcome(), "failed")

    def test_a_file_that_cannot_be_read_is_a_failure(self) -> None:
        self.assertEqual(self.outcome(), "failed")  # no file
        for raw in (b"", b"{", b"not json", b'{"duration": 3600}\n{"duration": 3600}\n', b"\xff\xfe",
                    b"[" * 100_000):
            with self.subTest(raw=raw[:20]):
                self.write(raw)
                self.assertEqual(self.outcome(), "failed")
        self.write(json.dumps({"duration": 3600, "title": "x" * ytdlp.PRE_MAX_BYTES}).encode("ascii"))
        self.assertEqual(self.outcome(), "failed")
        self.pre.unlink()
        self.pre.mkdir()
        self.assertEqual(self.outcome(), "failed")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW is POSIX")
    def test_a_link_is_not_followed(self) -> None:
        real = self.job / "real.json"
        real.write_bytes(json.dumps({"duration": 3600}).encode("ascii"))
        os.symlink(real, self.pre)
        self.assertEqual(self.outcome(), "failed")

    def test_only_the_whitelisted_metadata_is_kept(self) -> None:
        self.write({"title": "T", "duration": 3600, "heatmap": [{"start_time": 0, "end_time": 5, "value": 9}],
                    "url": "https://example.invalid/x", "formats": [{"url": "x"}], "uploader": "U"})
        result = ytdlp.read_rejection(self.job, VIDEO_ID, 600)
        assert isinstance(result, LongVideo)
        self.assertEqual(result.metadata, Metadata("T", 3600, None))


class WindowResultTest(unittest.TestCase):
    """What the runner accepts as a window job's result, from the three files in the job directory."""

    INFO = {"title": "T", "duration": 3600, "url": "https://example.invalid/x"}

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.job = Path(temp.name)
        self.audio = self.job / f"{VIDEO_ID}.m4a"
        self.info = self.job / f"{VIDEO_ID}.info.json"
        self.result = self.job / f"{VIDEO_ID}.window.json"
        self.audio.write_bytes(b"audio")
        self.info.write_bytes(json.dumps(self.INFO).encode("ascii"))

    def write(self, result: Any) -> None:
        self.result.write_bytes(result if isinstance(result, bytes) else json.dumps(result).encode("ascii"))

    def read(self, start: int = 1234, max_seconds: int = 600, max_bytes: int = MIB) -> Any:
        return ytdlp.read_window_result(self.job, VIDEO_ID, start, max_seconds, max_bytes)

    def refused(self, **kwargs: Any) -> None:
        with self.assertRaises(FetchError) as raised:
            self.read(**kwargs)
        self.assertEqual(raised.exception.code, "failed")

    def test_the_result_is_the_audio_the_metadata_and_where_the_window_begins(self) -> None:
        self.write({"start": 1229.875, "duration": 599.075})
        download = self.read()
        self.assertEqual(download.audio, self.audio)
        self.assertEqual(download.metadata, Metadata("T", 3600, None))
        self.assertEqual(download.window_start, 1229.875)

    def test_the_bounds_are_the_start_asked_for_and_the_longest_window(self) -> None:
        for result in ({"start": 1234, "duration": 600}, {"start": 1235.0, "duration": 600.0},
                       {"start": 634, "duration": 0.001}, {"start": 634.0, "duration": 600}):
            with self.subTest(result=result):
                self.write(result)
                self.assertEqual(self.read().window_start, float(result["start"]))
        # Both bounds are the runner's own limit, not fixed numbers.
        self.write({"start": 0, "duration": 300})
        self.assertEqual(self.read(start=0, max_seconds=300).window_start, 0.0)
        self.assertEqual(self.read(start=300, max_seconds=300).window_start, 0.0)
        self.refused(start=301, max_seconds=300)
        self.write({"start": 0, "duration": 300.001})
        self.refused(start=0, max_seconds=300)

    def test_a_result_no_window_could_have_is_refused(self) -> None:
        for result in (
            {"start": 1235.01, "duration": 600}, {"start": 5000, "duration": 600}, {"start": -0.5, "duration": 600},
            {"start": 1230, "duration": 600.001}, {"start": 1230, "duration": 0}, {"start": 1230, "duration": -1},
            {"start": 633.99, "duration": 600}, {"start": 0, "duration": 600},
            {"start": "1230", "duration": 600}, {"start": 1230, "duration": "600"}, {"start": True, "duration": 600},
            {"start": 1230, "duration": True}, {"start": None, "duration": 600}, {"start": 1230, "duration": None},
            {"start": 1230}, {"duration": 600}, {}, {"start": 1230, "duration": 600, "address": "x"},
            [1230, 600], "1230", 1230, None,
        ):
            with self.subTest(result=result):
                self.write(result)
                self.refused()
        for raw in (b"", b"{not json", b'{"start": NaN, "duration": 600}', b'{"start": 1230, "duration": NaN}',
                    b'{"start": 1230, "duration": Infinity}', b'{"start": 1230, "duration": 600}{}',
                    json.dumps({"start": 1230, "duration": 600}).encode("ascii") + b" " * 1024):
            with self.subTest(raw=raw[:30]):
                self.write(raw)
                self.refused()

    def test_every_one_of_the_three_files_must_be_there(self) -> None:
        self.write({"start": 1230, "duration": 600})
        self.read()
        self.result.unlink()
        self.refused()
        self.write({"start": 1230, "duration": 600})
        self.info.unlink()
        self.refused()
        self.info.write_bytes(json.dumps(self.INFO).encode("ascii"))
        self.audio.unlink()
        self.refused()
        self.audio.write_bytes(b"")
        self.refused()
        self.audio.write_bytes(b"x" * 11)
        self.assertEqual(self.read(max_bytes=11).audio, self.audio)
        self.refused(max_bytes=10)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW is POSIX")
    def test_no_file_is_read_through_a_link(self) -> None:
        self.write({"start": 1230, "duration": 600})
        for path in (self.audio, self.result, self.info):
            with self.subTest(path=path.name):
                real = self.job / f"real-{path.name}"
                os.replace(path, real)
                os.symlink(real, path)
                self.refused()
                path.unlink()
                os.replace(real, path)
        self.read()


class WindowFetchTest(StubTestCase):
    """A job with a start time: yt-dlp with --skip-download, then the window program, both stubs here."""

    LONG_ID = "tooLong____"

    def window_error(self, video_id: str, start: int = 1234, **kwargs: Any) -> str:
        with self.assertRaises(FetchError) as raised:
            self.runner(**kwargs).fetch_window(video_id, self.job, start)
        return raised.exception.code

    def test_it_runs_yt_dlp_for_the_address_and_then_the_window_program(self) -> None:
        download = self.runner().fetch_window(self.LONG_ID, self.job, 1234)
        self.assertEqual(download.audio, self.job / f"{self.LONG_ID}.m4a")
        self.assertEqual(download.audio.read_bytes(), b"STUB-WINDOW\n" + f"{self.LONG_ID}@1234".encode("ascii"))
        self.assertEqual(download.window_start, 1230.0)
        self.assertEqual(download.metadata.title, stub_title(self.LONG_ID))
        self.assertEqual(download.metadata.duration, 3600)
        first, second = self.runs()
        self.assertEqual(first["argv"], window_ytdlp_args(self.LONG_ID, self.job))
        self.assertEqual(second["argv"], window_args(self.LONG_ID, self.job, 1234, 600, MIB))
        # The same fence for both: the working directory and the environment.
        self.assertEqual(os.path.normcase(first["cwd"]), os.path.normcase(second["cwd"]))
        self.assertEqual(first["env"], second["env"])
        self.assertEqual(self.job_names(), sorted(f"{self.LONG_ID}.{suffix}" for suffix in
                                                  ("info.json", "m4a", "window.json")))

    def test_a_download_has_no_window_start(self) -> None:
        self.assertIsNone(self.runner().fetch(VIDEO_ID, self.job).window_start)

    def test_a_window_of_a_video_that_is_not_long_is_fetched_the_same_way(self) -> None:
        # The stub's ordinary video lasts 215.5 s.
        download = self.runner().fetch_window(VIDEO_ID, self.job, 125)
        self.assertEqual(download.window_start, 120.0)
        self.assertEqual(download.audio.read_bytes(), b"STUB-WINDOW\n" + f"{VIDEO_ID}@125".encode("ascii"))

    def test_the_first_child_s_failures_map_to_the_fixed_codes_and_end_the_job(self) -> None:
        cases = {
            "liveStream_": "live",
            # This run's only filter is !is_live.
            "rejected___": "live",
            "blocked____": "blocked",
            "notFound___": "not_found",
            "failing____": "failed",
            "notADict___": "failed",
        }
        for count, (video_id, code) in enumerate(cases.items(), start=1):
            with self.subTest(video_id=video_id):
                self.assertEqual(self.window_error(video_id), code)
                # The window program never ran.
                self.assertEqual(len(self.runs()), count)
                self.assertFalse((self.job / f"{video_id}.m4a").exists())

    def test_exit_0_without_the_info_json_starts_no_window_program(self) -> None:
        self.assertEqual(self.window_error(VIDEO_ID, mode="no_info"), "failed")
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.job_names(), [])

    def test_the_window_program_s_statuses_map_to_the_fixed_codes(self) -> None:
        cases = {"pastEnd____": "past_end", "winBlocked_": "blocked", "winTimeout_": "timeout",
                 "winFailing_": "failed"}
        for video_id, code in cases.items():
            with self.subTest(video_id=video_id):
                self.assertEqual(self.window_error(video_id), code)
        # A start at or after the end of the stub's 215.5 s video.
        self.assertEqual(self.window_error(VIDEO_ID, start=216), "past_end")
        self.assertEqual(self.window_error(VIDEO_ID, start=MAX_START_SECONDS), "past_end")

    def test_exit_0_is_not_success_on_its_own(self) -> None:
        modes = ["no_result", "no_audio", "late", "early", "too_long", "not_json", "extra_key", "files_1"]
        if hasattr(os, "O_NOFOLLOW"):
            modes.append("symlink")
        for mode in modes:
            with self.subTest(mode=mode):
                job = self.temp / f"job-{mode}"
                job.mkdir()
                with self.assertRaises(FetchError) as raised:
                    self.runner(window_mode=mode).fetch_window(self.LONG_ID, job, 1234)
                self.assertEqual(raised.exception.code, "failed")
        job = self.temp / "job-oversize"
        job.mkdir()
        with self.assertRaises(FetchError) as raised:
            self.runner(max_bytes=1024, window_mode="oversize").fetch_window(self.LONG_ID, job, 1234)
        self.assertEqual(raised.exception.code, "failed")

    def test_other_arguments_than_the_six_fail_the_job(self) -> None:
        # The stub refuses any list but the pinned one (exit 2).
        with mock.patch.object(ytdlp, "window_args", lambda *a: [*window_args(*a), "--verbose"]):
            self.assertEqual(self.window_error(self.LONG_ID), "failed")
        with mock.patch.object(ytdlp, "window_ytdlp_args",
                               lambda *a: [arg for arg in window_ytdlp_args(*a) if arg != "--skip-download"]):
            self.assertEqual(self.window_error(self.LONG_ID), "failed")

    def test_a_value_that_is_no_id_or_no_start_runs_nothing(self) -> None:
        runner = self.runner()
        for video_id, start in (("../../etc/pw", 5), ("a" * 12, 5), (VIDEO_ID, -1), (VIDEO_ID, MAX_START_SECONDS + 1),
                                (VIDEO_ID, 1.5), (VIDEO_ID, True), (VIDEO_ID, "5"), (VIDEO_ID, None)):
            with self.subTest(video_id=video_id, start=start), self.assertRaises(ValueError):
                runner.fetch_window(video_id, self.job, start)  # type: ignore[arg-type]
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.job_names(), [])

    def test_the_two_children_share_one_time_budget(self) -> None:
        timeouts: list[float] = []

        class Recording(StubYtDlp):
            def _run_child(self, command: list[str], job_dir: Path, timeout: float,
                           cancels: int) -> tuple[int, bytes]:
                timeouts.append(timeout)
                return super()._run_child(command, job_dir, timeout, cancels)

        Recording(600, MIB, 30, log=self.runs_log).fetch_window(self.LONG_ID, self.job, 1234)
        self.assertEqual(len(timeouts), 2)
        self.assertEqual(timeouts[0], 30)
        # What the first child left of the 30 s, never the whole 30 s again.
        self.assertTrue(0 < timeouts[1] < 30, timeouts)

    def test_a_budget_the_first_child_used_up_starts_no_second_child(self) -> None:
        started: list[float] = []

        class Slow(StubYtDlp):
            def _run_child(self, command: list[str], job_dir: Path, timeout: float,
                           cancels: int) -> tuple[int, bytes]:
                started.append(timeout)
                result = super()._run_child(command, job_dir, timeout, cancels)
                # As if yt-dlp had taken all of the job's time and a little more.
                time.sleep(self.timeout_seconds + 0.2)
                return result

        with self.assertRaises(FetchError) as raised:
            Slow(600, MIB, 3, log=self.runs_log).fetch_window(self.LONG_ID, self.job, 1234)
        self.assertEqual(raised.exception.code, "timeout")
        # Counted where a child is started, not in the stub's log: a second
        # child given no time at all would be killed before it could write
        # its line there.
        self.assertEqual(started, [3])
        self.assertEqual(len(self.runs()), 1)

    def test_a_cancel_between_the_two_children_starts_no_second_child(self) -> None:
        asked: list[list[str]] = []

        class Stopped(StubYtDlp):
            def _run_child(self, command: list[str], job_dir: Path, timeout: float,
                           cancels: int) -> tuple[int, bytes]:
                asked.append(command)
                result = super()._run_child(command, job_dir, timeout, cancels)
                if len(asked) == 1:
                    # The server stops in the moment between the two
                    # children: nothing runs, so cancel() finds nothing to kill.
                    self.cancel()
                return result

        runner = Stopped(600, MIB, 30, log=self.runs_log)
        with self.assertRaises(FetchError) as raised:
            runner.fetch_window(self.LONG_ID, self.job, 1234)
        self.assertEqual(raised.exception.code, "failed")
        # The second child was asked for and never started.
        self.assertEqual(len(asked), 2)
        self.assertEqual(len(self.runs()), 1)
        self.assertFalse((self.job / f"{self.LONG_ID}.m4a").exists())
        # The cancel ended that job, not the next one: a job that begins
        # after it runs both of its children.
        with tempfile.TemporaryDirectory(dir=self.job.parent) as other:
            download = runner.fetch_window(self.LONG_ID, Path(other), 1234)
            self.assertEqual(download.window_start, 1230.0)
        self.assertEqual(len(self.runs()), 3)

    def test_a_cancel_before_a_job_does_not_stop_it(self) -> None:
        runner = self.runner()
        runner.cancel()
        self.assertEqual(runner.fetch_window(self.LONG_ID, self.job, 1234).window_start, 1230.0)
        self.assertEqual(len(self.runs()), 2)

    def test_a_window_program_that_hangs_is_killed_at_the_timeout(self) -> None:
        started = time.monotonic()
        self.assertEqual(self.window_error("winHanging_", timeout=3), "timeout")
        self.assertLess(time.monotonic() - started, 3 + ytdlp.DRAIN_SECONDS + 10)
        self.assertEqual(len(self.runs()), 2)
        self.assertFalse((self.job / "winHanging_.m4a").exists())

    def test_a_first_child_that_hangs_times_out_and_starts_no_second(self) -> None:
        self.assertEqual(self.window_error(self.LONG_ID, timeout=2, mode="hang"), "timeout")
        self.assertEqual(len(self.runs()), 1)

    def test_nothing_either_child_prints_leaves_the_runner(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.runner().fetch_window(self.LONG_ID, self.job, 1234)
            for video_id in ("winFailing_", "pastEnd____", "blocked____"):
                with self.assertRaises(FetchError) as raised:
                    self.runner().fetch_window(video_id, self.job, 1234)
                self.assertEqual(raised.exception.args, (raised.exception.code,))
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_a_flood_from_the_window_program_never_blocks_the_run(self) -> None:
        started = time.monotonic()
        download = self.runner(timeout=60, window_mode="loud").fetch_window(self.LONG_ID, self.job, 1234)
        self.assertEqual(download.window_start, 1230.0)
        self.assertLess(time.monotonic() - started, 30)

    def test_cancel_ends_a_running_window_program(self) -> None:
        runner = self.runner(timeout=60)
        outcome: list[str] = []

        def run() -> None:
            try:
                runner.fetch_window("winHanging_", self.job, 1234)
            except FetchError as error:
                outcome.append(error.code)

        thread = threading.Thread(target=run, daemon=True)
        started = time.monotonic()
        thread.start()
        # Both children have logged their run: the second one is the one that hangs.
        while len(self.runs()) < 2 and time.monotonic() - started < 30:
            time.sleep(0.05)
        self.assertEqual(len(self.runs()), 2)
        runner.cancel()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["failed"])
        self.assertLess(time.monotonic() - started, 40)


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.exists(PRLIMIT), "util-linux prlimit")
class WindowPrlimitTest(StubTestCase):
    def test_the_window_program_runs_under_the_same_limits_as_yt_dlp(self) -> None:
        import resource

        self.runner(timeout=20, max_bytes=2 * MIB, prlimit=(PRLIMIT,)).fetch_window("tooLong____", self.job, 1234)
        first, second = self.runs()
        self.assertEqual(second["limits"], {
            "RLIMIT_AS": resource.RLIM_INFINITY,
            "RLIMIT_CPU": 20,
            "RLIMIT_FSIZE": 3 * MIB,
            "RLIMIT_NOFILE": 256,
            "RLIMIT_NPROC": 512,
        })
        self.assertEqual(first["limits"], second["limits"])


if __name__ == "__main__":
    unittest.main()

"""The yt-dlp runner (strange_ringtone_fetch.ytdlp) against a stub yt-dlp (tests/stubs/ytdlp_stub.py).

The stub checks the argument list it receives and behaves by video ID, so
these tests pin the exact command, the environment, the prlimit guards, the
timeout and process-group kill, the error mapping and the metadata whitelist
without the network or yt-dlp, on Windows and Linux alike.
"""
from __future__ import annotations

import contextlib
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
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import helpers
from strange_ringtone_fetch import ytdlp
from strange_ringtone_fetch.ytdlp import (
    DEFAULT_COMMAND,
    DENO,
    PRLIMIT,
    FetchError,
    Limits,
    YtDlp,
    clean_duration,
    clean_heatmap,
    clean_title,
    default_prlimit,
    map_failure,
    parse_info,
    watch_url,
    ytdlp_args,
)

STUB = helpers.ROOT / "tests" / "stubs" / "ytdlp_stub.py"
STUB_COMMAND = (sys.executable, "-I", str(STUB))
MODE_VAR = "STRANGE_RINGTONE_YTDLP_STUB"
LOG_VAR = "STRANGE_RINGTONE_YTDLP_STUB_LOG"
VIDEO_ID = "dQw4w9WgXcQ"
MIB = 1024 * 1024


def stub_title(video_id: str) -> str:
    return f"Stub Title {video_id} \u00e9"


class StubYtDlp(YtDlp):
    """The real runner with the stub as its program, plus a log the stub appends to.

    The runner builds the child's environment from scratch, so the stub's
    own variables are added here, in the tests only.
    """

    def __init__(self, *args: Any, log: Path | None = None, mode: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("command", STUB_COMMAND)
        super().__init__(*args, **kwargs)
        self.extra: dict[str, str] = {}
        if log is not None:
            self.extra[LOG_VAR] = str(log)
        if mode is not None:
            self.extra[MODE_VAR] = mode

    def child_env(self, job_dir: Path) -> dict[str, str]:
        env = super().child_env(job_dir)
        env.update(self.extra)
        return env


def alive(pid: int) -> bool:
    """Whether pid still runs. A zombie (killed, not yet reaped) counts as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


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
            "--max-filesize", "41943040",
            "--write-info-json", "--quiet", "--no-warnings", "--no-progress",
            "--socket-timeout", "20",
            "-P", str(job),
            "-o", "%(id)s.%(ext)s",
            "--", f"https://www.youtube.com/watch?v={VIDEO_ID}",
        ])

    def test_the_url_comes_last_after_the_option_guard(self) -> None:
        args = ytdlp_args("-_-_-_-_-_-", Path("job"), 1, 1)
        self.assertEqual(args[-2:], ["--", "https://www.youtube.com/watch?v=-_-_-_-_-_-"])
        # The only argument that holds the ID, and only inside the fixed URL.
        self.assertEqual([arg for arg in args if "-_-_-_-_-_-" in arg], [args[-1]])

    def test_a_plain_match_filter_is_never_used(self) -> None:
        # A plain --match-filters skip exits 0 having written nothing, which
        # would hide too_long behind failed.
        args = ytdlp_args(VIDEO_ID, Path("job"), 600, MIB)
        self.assertNotIn("--match-filters", args)
        self.assertNotIn("--match-filter", args)

    def test_fixed_program_paths(self) -> None:
        self.assertEqual(PRLIMIT, "/usr/bin/prlimit")
        self.assertEqual(DENO, "/usr/local/bin/deno")
        self.assertEqual(DEFAULT_COMMAND, (sys.executable, "-I", "-m", "yt_dlp"))


class CommandTest(unittest.TestCase):
    def test_full_command_with_prlimit(self) -> None:
        runner = YtDlp(600, 41943040, 120, prlimit=(PRLIMIT,))
        self.assertEqual(
            runner.full_command(["--ignore-config"]),
            ["/usr/bin/prlimit", "--cpu=120", "--fsize=42991616", "--nofile=256", "--nproc=512", "--",
             sys.executable, "-I", "-m", "yt_dlp", "--ignore-config"],
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
    def test_exit_101_is_too_long(self) -> None:
        self.assertEqual(map_failure(101, b""), "too_long")
        self.assertEqual(map_failure(101, b"ERROR: Sign in to confirm you're not a bot"), "too_long")

    def test_blocked_phrases(self) -> None:
        for text in (
            "ERROR: [youtube] x: Sign in to confirm you're not a bot",
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
        self.assertEqual(ytdlp.ERROR_CODES, ("blocked", "too_long", "not_found", "timeout", "failed"))
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

    def test_info_that_is_not_an_object_fails(self) -> None:
        for info in ([], "x", None, 5):
            with self.subTest(info=info), self.assertRaises(FetchError) as raised:
                parse_info(info)
            self.assertEqual(raised.exception.code, "failed")


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
            "tooLong____": "too_long",
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

    def test_too_long_writes_nothing(self) -> None:
        self.assertEqual(self.fetch_error("tooLong____"), "too_long")
        self.assertEqual(self.job_names(), [])

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
        # An info JSON past its size limit is never parsed.
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
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.PIPE)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(kwargs["cwd"], self.job)
        self.assertEqual(kwargs["env"], runner.child_env(self.job))

    def test_the_program_is_the_fixed_path(self) -> None:
        spy = mock.MagicMock(side_effect=OSError("not here"))
        for prlimit, program in ((None, sys.executable), ((PRLIMIT,), PRLIMIT)):
            with self.subTest(prlimit=prlimit), mock.patch.object(ytdlp.subprocess, "Popen", spy):
                with self.assertRaises(FetchError):
                    YtDlp(600, MIB, 30, prlimit=prlimit).fetch(VIDEO_ID, self.job)
                (command,), _ = spy.call_args
                self.assertEqual(command[0], program)
                self.assertEqual(command[command.index(sys.executable):][:4], list(DEFAULT_COMMAND))

    def test_communicate_has_the_wall_clock_timeout(self) -> None:
        timeouts: list[float | None] = []
        real = subprocess.Popen.communicate

        def recording(proc: subprocess.Popen[bytes], input: Any = None, timeout: float | None = None) -> Any:
            timeouts.append(timeout)
            return real(proc, input, timeout)

        with mock.patch.object(subprocess.Popen, "communicate", recording):
            self.runner(timeout=17).fetch(VIDEO_ID, self.job)
        self.assertEqual(timeouts, [17])


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
            assert proc.stdout is not None and proc.stderr is not None
            deadline = time.monotonic() + 5
            while not (proc.stdout.closed and proc.stderr.closed) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(proc.stdout.closed, "stdout left open")
            self.assertTrue(proc.stderr.closed, "stderr left open")

    def test_a_hung_run_times_out(self) -> None:
        started = time.monotonic()
        self.assertEqual(self.fetch_error("hanging____", timeout=1), "timeout")
        self.assertLess(time.monotonic() - started, 1 + 0.5 + 5)
        self.assertReaped()

    def test_the_kill_is_followed_by_a_bounded_drain(self) -> None:
        timeouts: list[float | None] = []
        real = self.popen.communicate

        def recording(proc: subprocess.Popen[bytes], input: Any = None, timeout: float | None = None) -> Any:
            timeouts.append(timeout)
            return real(proc, input, timeout)

        with mock.patch.object(self.popen, "communicate", recording):
            self.assertEqual(self.fetch_error("hanging____", timeout=1), "timeout")
        self.assertEqual(timeouts, [1, 0.5])
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
        for error in (KeyboardInterrupt, OSError):
            with self.subTest(error=error.__name__):
                self.procs.clear()
                pid_file.unlink(missing_ok=True)

                def interrupted(proc: subprocess.Popen[bytes], input: Any = None, timeout: float | None = None,
                                error: type[BaseException] = error) -> Any:
                    read_pid(pid_file)
                    raise error()

                started = time.monotonic()
                with mock.patch.object(self.popen, "communicate", interrupted):
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


if __name__ == "__main__":
    unittest.main()

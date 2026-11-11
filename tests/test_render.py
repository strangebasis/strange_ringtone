"""The renderer against a stub ffmpeg (tests/stubs/ffmpeg_stub.py).

The stub checks the argument list it receives and reports loudness derived
from it, so these tests pin the exact commands, the measure / render /
correct sequence, the timeout and process-group kill, and the cleanup, on
Windows and Linux alike. tests/image/test_render_real.py runs the real ffmpeg.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
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
from corpus.build import valid_wav
from helpers import alive
from strange_ringtone import render
from strange_ringtone.render import (
    FFMPEG,
    PRLIMIT,
    Limits,
    Renderer,
    RenderError,
    RenderParams,
    clamp_gain,
    default_prlimit,
    filter_chain,
    measure_args,
    parse_summary,
    render_args,
)

STUB = helpers.ROOT / "tests" / "stubs" / "ffmpeg_stub.py"
STUB_COMMAND = (sys.executable, "-I", str(STUB))
MODE_VAR = "STRANGE_RINGTONE_FFMPEG_STUB"
LOG_VAR = "STRANGE_RINGTONE_FFMPEG_STUB_LOG"
STUB_MAGIC = b"STUB-IPOD\n"

CHAIN = (
    "volume=9.00dB,alimiter=limit=0.8414:level=false:latency=true,"
    "afade=t=in:st=0:d=0.200:curve=qsin,afade=t=out:st=8.500:d=1.500:curve=qsin,ebur128=peak=true"
)

# What the image's ffmpeg 9.0.2 printed for a two-second two-tone clip,
# trimmed to the last frame line.
REAL_SUMMARY = (
    "[Parsed_ebur128_0 @ 0x763e78002480] t: 1.999977   TARGET:-23 LUFS    M: -23.5 S:-120.7     I: -23.5 LUFS"
    "       LRA:   0.0 LU  FTPK: -21.1 -21.1 dBFS  TPK: -21.1 -21.1 dBFS\n"
    "[Parsed_ebur128_0 @ 0x763e78002480] Summary:\n"
    "\n"
    "  Integrated loudness:\n"
    "    I:         -23.5 LUFS\n"
    "    Threshold: -33.5 LUFS\n"
    "\n"
    "  Loudness range:\n"
    "    LRA:         0.0 LU\n"
    "    Threshold:   0.0 LUFS\n"
    "    LRA low:     0.0 LUFS\n"
    "    LRA high:    0.0 LUFS\n"
    "\n"
    "  True peak:\n"
    "    Peak:      -21.1 dBFS\n"
    "[out#0/null @ 0x630d71c89980] video:0KiB audio:345KiB subtitle:0KiB other streams:0KiB\n"
)


def params(**overrides: Any) -> RenderParams:
    values: dict[str, Any] = {
        "fade_in": 0.2, "fade_out": 1.5, "normalize": True, "target_lufs": -14.0, "title": "My Tone", "duration": 10.0,
    }
    values.update(overrides)
    return RenderParams(**values)


def read_pid(pid_file: Path, timeout: float = 10) -> int | None:
    """The PID in pid_file once it holds one, or None after timeout.

    Waits for the content, not just the file: a file that exists may still be
    empty under load, and int("") would fail the test for the wrong reason.
    """
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
    # On Windows os.kill with SIGTERM is TerminateProcess.
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass


class ArgumentsTest(unittest.TestCase):
    def test_measure_args(self) -> None:
        self.assertEqual(
            measure_args(Path("job") / "in.wav"),
            ["-nostdin", "-hide_banner", "-nostats", "-threads", "1", "-filter_threads", "1",
             "-protocol_whitelist", "file", "-f", "wav", "-i", str(Path("job") / "in.wav"),
             "-af", "ebur128=peak=true", "-f", "null", "-"],
        )

    def test_render_args(self) -> None:
        source, output = Path("job") / "in.wav", Path("job") / "out.m4a"
        self.assertEqual(
            render_args(source, output, params(), 9.0),
            ["-nostdin", "-hide_banner", "-nostats", "-threads", "1", "-filter_threads", "1",
             "-protocol_whitelist", "file", "-y",
             "-f", "wav", "-i", str(source), "-af", CHAIN, "-ar", "44100", "-map_metadata", "-1",
             "-fflags", "+bitexact", "-metadata", "title=My Tone", "-c:a", "aac", "-b:a", "256k",
             "-movflags", "+faststart", "-f", "ipod", str(output)],
        )

    def test_limiter_is_always_level_false_at_minus_1_5_dbfs(self) -> None:
        # level=true (alimiter's default) re-normalises and undoes the gain.
        self.assertEqual(render.LIMITER_CEILING_DB, -1.5)
        self.assertEqual(render.LIMITER_LIMIT, "0.8414")
        for case in (params(), params(normalize=False), params(fade_in=0, fade_out=0)):
            with self.subTest(case=case):
                stages = filter_chain(case, 0.0).split(",")
                self.assertEqual(stages[1], "alimiter=limit=0.8414:level=false:latency=true")

    def test_ebur128_measures_the_end_of_the_chain(self) -> None:
        for case in (params(), params(fade_in=0, fade_out=0)):
            with self.subTest(case=case):
                self.assertEqual(filter_chain(case, 1.0).split(",")[-1], "ebur128=peak=true")

    def test_zero_fades_are_left_out(self) -> None:
        # afade with d=0 would be a one-second fade, not none.
        self.assertEqual(
            filter_chain(params(fade_in=0, fade_out=0), -3.5),
            "volume=-3.50dB,alimiter=limit=0.8414:level=false:latency=true,ebur128=peak=true",
        )
        self.assertNotIn("t=out", filter_chain(params(fade_out=0), 0.0))
        self.assertNotIn("t=in", filter_chain(params(fade_in=0), 0.0))
        # Rounded to the printed precision before the zero check.
        self.assertNotIn("afade", filter_chain(params(fade_in=0.0004, fade_out=0.0004), 0.0))

    def test_fades_are_clamped_to_the_clip(self) -> None:
        chain = filter_chain(params(fade_in=5.0, fade_out=5.0, duration=2.0), 0.0)
        self.assertIn("afade=t=in:st=0:d=2.000:curve=qsin", chain)
        self.assertIn("afade=t=out:st=0.000:d=2.000:curve=qsin", chain)

    def test_non_finite_numbers_never_reach_the_chain(self) -> None:
        for case, gain in ((params(fade_in=float("nan")), 0.0), (params(duration=float("inf")), 0.0),
                           (params(), float("nan"))):
            with self.subTest(case=case, gain=gain), self.assertRaises(ValueError):
                filter_chain(case, gain)

    def test_title_is_one_argument_whatever_it_holds(self) -> None:
        for title in ("-y", "x -i /etc/passwd", "$(touch pwned); `id` | cat", "Ünïcødé 🎵"):
            with self.subTest(title=title):
                args = render_args(Path("in.wav"), Path("out.m4a"), params(title=title), 0.0)
                self.assertEqual(args[args.index("-metadata") + 1], f"title={title}")
                self.assertEqual(len(args), len(render_args(Path("in.wav"), Path("out.m4a"), params(), 0.0)))

    def test_every_run_is_local_files_only_without_stdin(self) -> None:
        for args in (measure_args(Path("in.wav")), render_args(Path("in.wav"), Path("out.m4a"), params(), 0.0)):
            with self.subTest(args=args[-1]):
                # One thread for the codecs and one for the filter graph,
                # which would otherwise start a thread per usable CPU: the
                # core's pids_limit counts a fixed number of threads per run.
                self.assertEqual(args[:9], ["-nostdin", "-hide_banner", "-nostats", "-threads", "1",
                                            "-filter_threads", "1", "-protocol_whitelist", "file"])
                # The demuxer is named, never guessed from the content.
                self.assertEqual(args[args.index("-i") - 2: args.index("-i")], ["-f", "wav"])
        self.assertNotIn("-y", measure_args(Path("in.wav")))
        self.assertIn("-y", render_args(Path("in.wav"), Path("out.m4a"), params(), 0.0))

    def test_gain_clamp(self) -> None:
        self.assertEqual(render.GAIN_CLAMP_DB, 20.0)
        self.assertEqual(clamp_gain(56.0), 20.0)
        self.assertEqual(clamp_gain(-25.0), -20.0)
        self.assertEqual(clamp_gain(-3.25), -3.25)


class CommandTest(unittest.TestCase):
    def test_full_command_with_prlimit(self) -> None:
        renderer = Renderer((FFMPEG,), 20, prlimit=(PRLIMIT,))
        self.assertEqual(
            renderer.full_command(["-nostdin"]),
            ["/usr/bin/prlimit", "--as=536870912", "--cpu=20", "--fsize=4194304", "--nofile=64", "--",
             "/usr/local/bin/ffmpeg", "-nostdin"],
        )

    def test_the_file_size_limit_fits_the_largest_output_and_no_more(self) -> None:
        # The largest legitimate output: MAX_CLIP_SECONDS at its ceiling of
        # 40, at the contract's sizing of 33,000 bytes per second (the 256k
        # bitrate plus the container). At least twice that, so a real render
        # never hits it, and a few MiB at most, so one runaway output can't
        # fill the tmpfs every result shares.
        largest = 40 * 33_000
        self.assertEqual(render.BITRATE, "256k")
        self.assertGreaterEqual(Limits().file_size_bytes, 2 * largest)
        self.assertLessEqual(Limits().file_size_bytes, 8 * 1024 * 1024)
        # The renderer's own default is the same limit.
        self.assertEqual(Renderer((FFMPEG,), 20).limits.file_size_bytes, Limits().file_size_bytes)

    def test_cpu_limit_follows_the_timeout_unless_given(self) -> None:
        self.assertIn("--cpu=7", Renderer((FFMPEG,), 7, prlimit=(PRLIMIT,)).full_command([]))
        limits = Limits(address_space_bytes=1, cpu_seconds=2, file_size_bytes=3, open_files=4)
        self.assertEqual(
            Renderer((FFMPEG,), 7, prlimit=(PRLIMIT,), limits=limits).full_command([]),
            [PRLIMIT, "--as=1", "--cpu=2", "--fsize=3", "--nofile=4", "--", FFMPEG],
        )

    def test_full_command_without_prlimit(self) -> None:
        self.assertEqual(Renderer(STUB_COMMAND, 20).full_command(["-a"]), [*STUB_COMMAND, "-a"])

    def test_fixed_program_paths(self) -> None:
        self.assertEqual(FFMPEG, "/usr/local/bin/ffmpeg")
        self.assertEqual(PRLIMIT, "/usr/bin/prlimit")

    def test_prlimit_always_on_linux_and_never_elsewhere(self) -> None:
        # Not "when the file exists": a Linux host without prlimit fails
        # renders instead of running ffmpeg unlimited.
        with mock.patch.object(render.sys, "platform", "linux"), \
                mock.patch("os.path.exists", side_effect=AssertionError("looked on disk")):
            self.assertEqual(default_prlimit(), (PRLIMIT,))
        with mock.patch.object(render.sys, "platform", "win32"):
            self.assertIsNone(default_prlimit())

    def test_construction_touches_no_file(self) -> None:
        with mock.patch("os.stat", side_effect=AssertionError("stat")), \
                mock.patch("os.path.exists", side_effect=AssertionError("exists")):
            Renderer(("/nonexistent/ffmpeg",), 20, prlimit=("/nonexistent/prlimit",))

    def test_bad_construction(self) -> None:
        with self.assertRaises(ValueError):
            Renderer((), 20)
        with self.assertRaises(ValueError):
            Renderer((FFMPEG,), 0)


class SummaryTest(unittest.TestCase):
    def test_real_summary(self) -> None:
        loudness = parse_summary(REAL_SUMMARY)
        assert loudness is not None
        self.assertEqual((loudness.integrated, loudness.true_peak), (-23.5, -21.1))

    def test_frame_lines_are_not_the_summary(self) -> None:
        frames_only = REAL_SUMMARY.split("[Parsed_ebur128_0 @ 0x763e78002480] Summary:")[0]
        self.assertIsNone(parse_summary(frames_only))

    def test_silence_has_a_minus_inf_peak(self) -> None:
        text = REAL_SUMMARY.replace("-23.5 LUFS\n", "-70.0 LUFS\n").replace("-21.1 dBFS\n", " -inf dBFS\n")
        loudness = parse_summary(text)
        assert loudness is not None
        self.assertEqual(loudness.integrated, -70.0)
        self.assertEqual(loudness.true_peak, float("-inf"))

    def test_crlf_is_accepted(self) -> None:
        self.assertIsNotNone(parse_summary(REAL_SUMMARY.replace("\n", "\r\n")))

    def test_anything_else_is_refused(self) -> None:
        summary = REAL_SUMMARY.split("] Summary:", 1)[1]
        cases = {
            "empty": "",
            "two summaries": REAL_SUMMARY + REAL_SUMMARY,
            "truncated": REAL_SUMMARY.split("  True peak:")[0],
            "no peak section": REAL_SUMMARY.replace("  True peak:\n", ""),
            "integrated -inf": REAL_SUMMARY.replace("-23.5 LUFS\n", " -inf LUFS\n"),
            "nan": REAL_SUMMARY.replace("-23.5 LUFS\n", "  nan LUFS\n"),
            "exponent": REAL_SUMMARY.replace("-23.5 LUFS\n", "-2e1 LUFS\n"),
            "reordered": "Summary:" + summary.replace("  Integrated loudness:\n", "", 1),
            "sample peak instead": REAL_SUMMARY.replace("  True peak:", "  Sample peak:"),
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertIsNone(parse_summary(text))


class StubTestCase(unittest.TestCase):
    """A job directory with in.wav and a log of every stub run."""

    def setUp(self) -> None:
        # ignore_cleanup_errors: on Windows a killed run's grandchild may still
        # hold the directory as its working directory for a moment.
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.job = self.temp / "job"
        self.job.mkdir()
        valid_wav(self.job / "in.wav", 1.0)
        self.runs_log = self.temp / "runs.jsonl"
        self.set_mode("ok")

    def set_mode(self, mode: str) -> None:
        patcher = mock.patch.dict(os.environ, {MODE_VAR: mode, LOG_VAR: str(self.runs_log)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def runs(self) -> list[dict[str, Any]]:
        if not self.runs_log.exists():
            return []
        return [json.loads(line) for line in self.runs_log.read_text(encoding="utf-8").splitlines()]

    def kinds(self) -> list[str]:
        return ["render" if "-y" in run["argv"] else "measure" for run in self.runs()]

    def job_names(self) -> list[str]:
        return sorted(p.name for p in self.job.iterdir())


class RenderTest(StubTestCase):
    def test_measure_then_render(self) -> None:
        result = Renderer(STUB_COMMAND, 20).render(self.job, params(duration=1.0))
        self.assertEqual(self.kinds(), ["measure", "render"])
        # The stub's upload measures -23 LUFS.
        self.assertEqual(result.applied_gain_db, 9.0)
        self.assertEqual(result.measured_lufs, -14.0)
        self.assertEqual(result.duration, 1.0)
        self.assertEqual(result.output, self.job / "out.m4a")
        self.assertTrue(result.output.read_bytes().startswith(STUB_MAGIC))
        self.assertEqual(self.job_names(), ["in.wav", "out.m4a"])
        # Every run works inside the job directory.
        self.assertEqual({Path(run["cwd"]) for run in self.runs()}, {self.job})

    def test_the_render_pass_uses_the_measured_gain(self) -> None:
        Renderer(STUB_COMMAND, 20).render(self.job, params(target_lufs=-11.0, duration=1.0))
        render_run = self.runs()[1]["argv"]
        self.assertTrue(render_run[render_run.index("-af") + 1].startswith("volume=12.00dB,"))

    def test_normalize_false_runs_no_measure_pass_and_no_correction(self) -> None:
        result = Renderer(STUB_COMMAND, 20).render(self.job, params(normalize=False, duration=1.0))
        self.assertEqual(self.kinds(), ["render"])
        self.assertEqual(result.applied_gain_db, 0.0)
        # 9 LU from the target, yet no corrective pass: the user asked for none.
        self.assertEqual(result.measured_lufs, -23.0)

    def test_a_miss_gets_one_corrective_pass(self) -> None:
        self.set_mode("offset")
        result = Renderer(STUB_COMMAND, 20).render(self.job, params(duration=1.0))
        self.assertEqual(self.kinds(), ["measure", "render", "render"])
        self.assertEqual(result.applied_gain_db, 10.0)
        self.assertEqual(result.measured_lufs, -14.0)
        last = self.runs()[2]["argv"]
        self.assertTrue(last[last.index("-af") + 1].startswith("volume=10.00dB,"))
        # The corrective pass reads the original upload again.
        self.assertEqual(last[last.index("-i") + 1], str(self.job / "in.wav"))

    def test_a_small_miss_is_left_alone(self) -> None:
        # -14.4 against -14: inside the 0.5 LU threshold.
        result = Renderer(STUB_COMMAND, 20).render(self.job, params(target_lufs=-14.4, duration=1.0))
        self.assertEqual(self.kinds(), ["measure", "render"])
        self.assertEqual(render.CORRECTION_THRESHOLD_LU, 0.5)
        self.assertAlmostEqual(result.measured_lufs, -14.4)

    def test_only_one_correction_even_if_it_still_misses(self) -> None:
        self.set_mode("stubborn")
        result = Renderer(STUB_COMMAND, 20).render(self.job, params(duration=1.0))
        self.assertEqual(self.kinds(), ["measure", "render", "render"])
        # 9 + (-14 - -30) = 25, clamped to 20.
        self.assertEqual(result.applied_gain_db, 20.0)
        self.assertEqual(result.measured_lufs, -30.0)

    def test_silence_clamps_the_gain_and_skips_a_useless_correction(self) -> None:
        self.set_mode("silence")
        result = Renderer(STUB_COMMAND, 20).render(self.job, params(duration=1.0))
        self.assertEqual(result.applied_gain_db, 20.0)
        self.assertEqual(result.measured_lufs, -50.0)
        # The corrected gain would be clamped to the same 20 dB.
        self.assertEqual(self.kinds(), ["measure", "render"])

    def test_unparsable_measure_fails_and_writes_nothing(self) -> None:
        self.set_mode("unparsable")
        with self.assertRaises(RenderError) as raised:
            Renderer(STUB_COMMAND, 20).render(self.job, params(duration=1.0))
        self.assertEqual(raised.exception.code, "measure_failed")
        self.assertEqual(self.job_names(), ["in.wav"])

    def test_unparsable_render_fails_and_removes_its_output(self) -> None:
        self.set_mode("unparsable")
        with self.assertRaises(RenderError) as raised:
            Renderer(STUB_COMMAND, 20).render(self.job, params(normalize=False, duration=1.0))
        self.assertEqual(raised.exception.code, "render_failed")
        # The stub wrote out.m4a before failing to report loudness.
        self.assertEqual(self.kinds(), ["render"])
        self.assertEqual(self.job_names(), ["in.wav"])

    def test_non_zero_exit_fails(self) -> None:
        self.set_mode("fail")
        for normalize, code in ((True, "measure_failed"), (False, "render_failed")):
            with self.subTest(normalize=normalize):
                with self.assertRaises(RenderError) as raised:
                    Renderer(STUB_COMMAND, 20).render(self.job, params(normalize=normalize, duration=1.0))
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.job_names(), ["in.wav"])

    def test_non_zero_exit_fails_even_with_a_valid_summary(self) -> None:
        # The summary parses and a render pass leaves out.m4a, so only the exit
        # status says the run failed: a partial or damaged file is never a result.
        self.set_mode("late_fail")
        for normalize, code, kinds in ((True, "measure_failed", ["measure"]), (False, "render_failed", ["render"])):
            with self.subTest(normalize=normalize):
                self.runs_log.unlink(missing_ok=True)
                with self.assertRaises(RenderError) as raised:
                    Renderer(STUB_COMMAND, 20).render(self.job, params(normalize=normalize, duration=1.0))
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.kinds(), kinds)
                self.assertEqual(self.job_names(), ["in.wav"])

    def test_a_render_that_writes_no_output_fails(self) -> None:
        # Exit 0 and a summary, but no file: never reported as a result.
        real_popen = subprocess.Popen

        def without_output(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
            proc = real_popen(command, **kwargs)
            if "-y" in command:
                proc.wait()
                (self.job / "out.m4a").unlink()
            return proc

        with mock.patch.object(render.subprocess, "Popen", without_output):
            with self.assertRaises(RenderError) as raised:
                Renderer(STUB_COMMAND, 20).render(self.job, params(normalize=False, duration=1.0))
        self.assertEqual(raised.exception.code, "render_failed")

    def test_wrong_arguments_fail(self) -> None:
        # The stub refuses anything but the pinned list, so a renderer that
        # dropped, say, level=false would fail here as well as above.
        with mock.patch.object(render, "LIMITER_LIMIT", "1.0"):
            with self.assertRaises(RenderError):
                Renderer(STUB_COMMAND, 20).render(self.job, params(normalize=False, duration=1.0))

    def test_missing_program_fails_cleanly(self) -> None:
        missing = str(self.temp / "no-such-ffmpeg")
        with self.assertRaises(RenderError) as raised:
            Renderer((missing,), 20).render(self.job, params(duration=1.0))
        self.assertEqual(raised.exception.code, "measure_failed")
        with self.assertRaises(RenderError) as raised:
            Renderer(STUB_COMMAND, 20, prlimit=(missing,)).render(self.job, params(duration=1.0))
        self.assertEqual(raised.exception.code, "measure_failed")
        self.assertEqual(self.job_names(), ["in.wav"])

    def test_stderr_never_leaves_the_renderer(self) -> None:
        # The stub echoes the title on stderr, as ffmpeg does.
        captured = io.StringIO()
        for mode in ("ok", "unparsable", "fail"):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                with contextlib.redirect_stderr(captured), contextlib.redirect_stdout(captured):
                    try:
                        Renderer(STUB_COMMAND, 20).render(self.job, params(title="SecretTitle", duration=1.0))
                    except RenderError as error:
                        self.assertNotIn("SecretTitle", str(error))
                        self.assertNotIn("SecretTitle", repr(error.args))
                        self.assertIsNone(error.__cause__)
        self.assertEqual(captured.getvalue(), "")

    def test_the_title_reaches_ffmpeg_literally(self) -> None:
        # No shell: metacharacters are just characters of one argument.
        title = "$(touch pwned) `touch pwned2` ; | & ' \" -y"
        result = Renderer(STUB_COMMAND, 20).render(self.job, params(title=title, normalize=False, duration=1.0))
        content = json.loads(result.output.read_bytes()[len(STUB_MAGIC):])
        self.assertEqual(content["title"], title)
        self.assertEqual(self.job_names(), ["in.wav", "out.m4a"])


class SubprocessGuardsTest(StubTestCase):
    """The guards CLAUDE.md requires of every subprocess: list, no shell, fixed program, timeout."""

    def spy(self) -> mock.MagicMock:
        spy = mock.MagicMock(wraps=subprocess.Popen)
        patcher = mock.patch.object(render.subprocess, "Popen", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return spy

    def test_popen_gets_a_list_and_no_shell(self) -> None:
        spy = self.spy()
        Renderer(STUB_COMMAND, 20).render(self.job, params(duration=1.0))
        self.assertEqual(spy.call_count, 2)
        for call in spy.call_args_list:
            (command,), kwargs = call
            self.assertIsInstance(command, list)
            self.assertTrue(all(isinstance(part, str) for part in command))
            self.assertNotIn("shell", kwargs)
            self.assertNotIn("executable", kwargs)
            self.assertNotIn("preexec_fn", kwargs)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], subprocess.PIPE)
            self.assertIs(kwargs["stderr"], subprocess.PIPE)
            self.assertIs(kwargs["start_new_session"], True)
            self.assertEqual(kwargs["cwd"], self.job)

    def test_the_program_is_the_fixed_path(self) -> None:
        # The production renderer's first argument is FFMPEG or PRLIMIT, from
        # code; nothing the client sends can change it.
        spy = mock.MagicMock(side_effect=OSError("not here"))
        for prlimit, program in ((None, FFMPEG), ((PRLIMIT,), PRLIMIT)):
            with self.subTest(prlimit=prlimit), mock.patch.object(render.subprocess, "Popen", spy):
                with self.assertRaises(RenderError):
                    Renderer((FFMPEG,), 20, prlimit=prlimit).render(self.job, params(title="--evil", duration=1.0))
                (command,), _ = spy.call_args
                self.assertEqual(command[0], program)
                self.assertIn(FFMPEG, command)

    def test_the_wait_has_the_wall_clock_timeout(self) -> None:
        # render._collect(proc, timeout) reads both pipes and waits for the
        # run, in place of communicate(), which would keep all of both.
        timeouts: list[float] = []
        real = render._collect

        def recording(proc: subprocess.Popen[bytes], timeout: float) -> bytes:
            timeouts.append(timeout)
            return real(proc, timeout)

        with mock.patch.object(render, "_collect", recording):
            Renderer(STUB_COMMAND, 17).render(self.job, params(duration=1.0))
        self.assertEqual(timeouts, [17, 17])


class TimeoutTest(StubTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Shorter than the default, so a pipe held by a survivor on Windows
        # costs half a second, not five.
        patcher = mock.patch.object(render, "DRAIN_SECONDS", 0.5)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Every run the renderer starts, so a test can check it was reaped.
        self.procs: list[subprocess.Popen[bytes]] = []
        # The class itself, kept before the patch below replaces the name.
        self.popen = subprocess.Popen

        def recording(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            proc = self.popen(*args, **kwargs)
            self.procs.append(proc)
            return proc

        patcher = mock.patch.object(render.subprocess, "Popen", recording)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertReaped(self, pipes_closed: bool = True) -> None:
        self.assertEqual(len(self.procs), 1)
        proc = self.procs[0]
        # Killed and waited for, not left a zombie for the GC to find.
        self.assertIsNotNone(proc.returncode, "the timed-out run was never reaped")
        if pipes_closed:
            assert proc.stdout is not None and proc.stderr is not None
            # On POSIX the renderer closes them before it raises. On Windows a
            # reader thread closes its pipe once the killed run's end of it is
            # gone, which can trail the raise by a moment.
            deadline = time.monotonic() + 5
            while not (proc.stdout.closed and proc.stderr.closed) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(proc.stdout.closed, "stdout left open")
            self.assertTrue(proc.stderr.closed, "stderr left open")

    def test_a_hung_run_times_out(self) -> None:
        self.set_mode("hang")
        started = time.monotonic()
        with self.assertRaises(RenderError) as raised:
            Renderer(STUB_COMMAND, 1).render(self.job, params(duration=1.0))
        self.assertEqual(raised.exception.code, "render_timeout")
        self.assertLess(time.monotonic() - started, 1 + 0.5 + 5)
        self.assertEqual(self.job_names(), ["in.wav"])
        self.assertReaped()

    def test_the_kill_is_followed_by_a_bounded_drain(self) -> None:
        # The killed run is read to EOF and reaped, with the drain's own bound.
        self.set_mode("hang")
        timeouts: list[float] = []
        real = render._collect

        def recording(proc: subprocess.Popen[bytes], timeout: float) -> bytes:
            timeouts.append(timeout)
            return real(proc, timeout)

        with mock.patch.object(render, "_collect", recording):
            with self.assertRaises(RenderError):
                Renderer(STUB_COMMAND, 1).render(self.job, params(duration=1.0))
        self.assertEqual(timeouts, [1, 0.5])
        self.assertReaped()

    def test_the_whole_process_group_dies(self) -> None:
        self.set_mode("grandchild")
        pid_file = self.job / "grandchild.pid"
        with self.assertRaises(RenderError) as raised:
            Renderer(STUB_COMMAND, 2).render(self.job, params(duration=1.0))
        self.assertEqual(raised.exception.code, "render_timeout")
        grandchild = read_pid(pid_file, timeout=5)
        self.assertIsNotNone(grandchild, "the stub never started its grandchild")
        assert grandchild is not None
        # Registered after the job directory, so it runs first: no sleeper
        # outlives the test on any platform, and on Windows the directory it
        # sits in can be removed.
        self.addCleanup(kill_quietly, grandchild)
        if not hasattr(os, "killpg"):
            # Windows has no process groups to kill; there the renderer can
            # only end the direct child, and the surviving grandchild still
            # holds the pipes its reader threads are blocked on.
            self.assertReaped(pipes_closed=False)
            return
        self.assertReaped()
        deadline = time.monotonic() + 5
        while alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(grandchild), "the grandchild survived the timeout")

    def test_an_interrupted_wait_kills_the_group_and_propagates(self) -> None:
        # Anything but a timeout escaping communicate() (a KeyboardInterrupt at
        # shutdown, an OSError from a pipe) must still take the run and what it
        # started down with it, then re-raise rather than become a result.
        self.set_mode("grandchild")
        pid_file = self.job / "grandchild.pid"
        for error in (KeyboardInterrupt, OSError):
            with self.subTest(error=error.__name__):
                self.procs.clear()
                pid_file.unlink(missing_ok=True)

                def interrupted(proc: subprocess.Popen[bytes], timeout: float,
                                error: type[BaseException] = error) -> bytes:
                    # Only once the grandchild exists, so there is a group to kill.
                    read_pid(pid_file)
                    raise error()

                started = time.monotonic()
                with mock.patch.object(render, "_collect", interrupted):
                    with self.assertRaises(error):
                        Renderer(STUB_COMMAND, 20).render(self.job, params(duration=1.0))
                grandchild = read_pid(pid_file, timeout=0)
                self.assertIsNotNone(grandchild, "the stub never started its grandchild")
                assert grandchild is not None
                self.addCleanup(kill_quietly, grandchild)
                # Killed, not waited out: the stub would sleep for 60 s.
                self.assertLess(time.monotonic() - started, 10 + 5)
                self.assertReaped()
                if not hasattr(os, "killpg"):
                    # Windows: only the direct child can be ended (see above).
                    continue
                self.assertEqual(self.procs[0].returncode, -signal.SIGKILL)
                deadline = time.monotonic() + 5
                while alive(grandchild) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(alive(grandchild), "the grandchild survived the interrupted run")

    def test_an_interrupted_render_removes_its_output(self) -> None:
        # Not only a RenderError: whatever escapes the encoder after it has
        # written out.m4a (an interrupt at shutdown, an OSError from a pipe)
        # leaves no half-written output behind for anything to serve.
        real_collect = render._collect
        written: list[bool] = []
        for error in (KeyboardInterrupt, OSError):
            with self.subTest(error=error.__name__):
                self.procs.clear()
                written.clear()

                def interrupted(proc: subprocess.Popen[bytes], timeout: float,
                                error: type[BaseException] = error) -> bytes:
                    # The run completes and writes its output first.
                    real_collect(proc, timeout)
                    written.append((self.job / "out.m4a").is_file())
                    raise error()

                with mock.patch.object(render, "_collect", interrupted):
                    with self.assertRaises(error):
                        Renderer(STUB_COMMAND, 20).render(self.job, params(normalize=False, duration=1.0))
                self.assertEqual(written, [True], "the stub never wrote out.m4a")
                self.assertReaped()
                self.assertEqual(self.job_names(), ["in.wav"])

    def test_a_survivor_holding_the_pipes_cannot_hang_the_request(self) -> None:
        # As if the kill had missed: the run still holds stdout and stderr
        # open. The bounded drain gives up instead of waiting for it.
        self.set_mode("hang")
        started = time.monotonic()
        with mock.patch.object(render, "_kill_group", lambda proc: None):
            with self.assertRaises(RenderError) as raised:
                Renderer(STUB_COMMAND, 1).render(self.job, params(duration=1.0))
        self.assertEqual(raised.exception.code, "render_timeout")
        self.assertLess(time.monotonic() - started, 1 + 0.5 + 5)
        # The fallback kill ends the run, and its pipes are closed explicitly
        # rather than left for the garbage collector.
        self.assertReaped()

    @unittest.skipUnless(hasattr(os, "killpg"), "process groups are POSIX")
    def test_the_kill_targets_the_group_not_just_the_child(self) -> None:
        self.set_mode("hang")
        with mock.patch.object(render.os, "killpg", wraps=os.killpg) as killpg:
            with self.assertRaises(RenderError):
                Renderer(STUB_COMMAND, 1).render(self.job, params(duration=1.0))
        self.assertEqual(killpg.call_count, 1)
        self.assertEqual(killpg.call_args[0][1], signal.SIGKILL)


class RunEndKillTest(StubTestCase):
    """Every run's process group is killed exactly once, on every path, before anything of the run is used."""

    def record(self) -> list[str]:
        """The kills and the summary reads of every run, in order; the real ones still happen."""
        events: list[str] = []
        real_kill, real_parse = render._kill_group, render.parse_summary

        def kill(proc: subprocess.Popen[bytes]) -> None:
            events.append("kill")
            real_kill(proc)

        def parse(stderr: str) -> Any:
            events.append("parse")
            return real_parse(stderr)

        for patcher in (mock.patch.object(render, "_kill_group", kill),
                        mock.patch.object(render, "parse_summary", parse),
                        mock.patch.object(render, "DRAIN_SECONDS", 0.5)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return events

    def test_the_group_is_killed_before_the_summary_is_read(self) -> None:
        # A run that exited by itself may have left a child that closed its
        # pipes; nothing of the run is read while that child could still
        # write to the job directory.
        events = self.record()
        Renderer(STUB_COMMAND, 20).render(self.job, params(duration=1.0))
        self.assertEqual(self.kinds(), ["measure", "render"])
        self.assertEqual(events, ["kill", "parse", "kill", "parse"])

    def test_the_group_is_killed_once_on_every_path(self) -> None:
        cases = (
            # mode, timeout, normalize, runs, the error the render ends with
            ("ok", 20, True, 2, None),
            ("unparsable", 20, False, 1, "render_failed"),
            ("fail", 20, False, 1, "render_failed"),
            ("late_fail", 20, False, 1, "render_failed"),
            ("hang", 1, False, 1, "render_timeout"),
        )
        events = self.record()
        for mode, timeout, normalize, runs, code in cases:
            with self.subTest(mode=mode):
                events.clear()
                self.set_mode(mode)
                try:
                    Renderer(STUB_COMMAND, timeout).render(self.job, params(normalize=normalize, duration=1.0))
                except RenderError as error:
                    self.assertEqual(error.code, code)
                else:
                    self.assertIsNone(code)
                self.assertEqual(events.count("kill"), runs, events)
                self.assertEqual(events[0], "kill", events)

    @unittest.skipUnless(hasattr(os, "killpg"), "process groups are POSIX")
    def test_a_child_left_behind_by_a_successful_run_is_killed(self) -> None:
        # The stub leaves a sleeping child in its group after each pass, with
        # its streams on the null device, so the pipes reach EOF at once.
        self.set_mode("survivor")
        result = Renderer(STUB_COMMAND, 20).render(self.job, params(duration=1.0))
        # Acceptance: the render itself is a normal success.
        self.assertEqual(result.measured_lufs, -14.0)
        self.assertTrue(result.output.read_bytes().startswith(STUB_MAGIC))
        pid_file = self.job / "survivors.pid"
        self.assertTrue(pid_file.is_file(), "the stub never started its survivors")
        survivors = [int(line) for line in pid_file.read_text(encoding="ascii").split()]
        for pid in survivors:
            self.addCleanup(kill_quietly, pid)
        self.assertEqual(len(survivors), 2)
        deadline = time.monotonic() + 2
        while any(alive(pid) for pid in survivors) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([pid for pid in survivors if alive(pid)], [], "a child of a successful run outlived it")


class GroupCollectionTest(StubTestCase):
    """After each group kill the dead members of that run's own group are collected, and no other process."""

    def record(self) -> list[tuple[str, int | None]]:
        """Each kill and each collection, with the leader's exit status at that moment; the real ones still run."""
        events: list[tuple[str, int | None]] = []
        real_kill, real_collect = render._kill_group, render._collect_group

        def kill(proc: subprocess.Popen[bytes]) -> None:
            events.append(("kill", proc.returncode))
            real_kill(proc)

        def collect(proc: subprocess.Popen[bytes]) -> None:
            events.append(("collect", proc.returncode))
            real_collect(proc)

        for patcher in (mock.patch.object(render, "_kill_group", kill),
                        mock.patch.object(render, "_collect_group", collect),
                        mock.patch.object(render, "DRAIN_SECONDS", 0.5)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return events

    def assertCollectedAfterEachKill(self, events: list[tuple[str, int | None]], runs: int) -> None:
        self.assertEqual([name for name, _code in events], ["kill", "collect"] * runs, events)
        for name, code in events:
            if name == "collect":
                # Popen has the leader's status already, so no collection can
                # take it from Popen.
                self.assertIsNotNone(code, "a group was collected before its leader was waited for")

    def test_every_kill_is_followed_by_a_collection_once_the_leader_is_waited_for(self) -> None:
        cases = (
            # mode, timeout, normalize, runs, the error the render ends with
            ("ok", 20, True, 2, None),
            ("unparsable", 20, False, 1, "render_failed"),
            ("fail", 20, False, 1, "render_failed"),
            ("late_fail", 20, False, 1, "render_failed"),
            ("hang", 1, False, 1, "render_timeout"),
        )
        events = self.record()
        for mode, timeout, normalize, runs, code in cases:
            with self.subTest(mode=mode):
                events.clear()
                self.set_mode(mode)
                try:
                    Renderer(STUB_COMMAND, timeout).render(self.job, params(normalize=normalize, duration=1.0))
                except RenderError as error:
                    self.assertEqual(error.code, code)
                else:
                    self.assertIsNone(code)
                self.assertCollectedAfterEachKill(events, runs)

    def test_an_interrupted_run_is_collected_too(self) -> None:
        events = self.record()

        def interrupted(proc: subprocess.Popen[bytes], timeout: float) -> bytes:
            raise KeyboardInterrupt()

        self.set_mode("hang")
        with mock.patch.object(render, "_collect", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                Renderer(STUB_COMMAND, 20).render(self.job, params(normalize=False, duration=1.0))
        self.assertCollectedAfterEachKill(events, 1)

    def test_nothing_is_collected_where_orphans_go_elsewhere(self) -> None:
        # Not PID 1, no child subreaper, or not Linux: what a run leaves
        # behind is another process's to collect, and the renderer doesn't
        # even ask.
        self.set_mode("survivor")
        with mock.patch.object(render, "_orphans_come_here", return_value=False), \
                mock.patch.object(render.os, "waitid", side_effect=AssertionError("waitid called"), create=True):
            Renderer(STUB_COMMAND, 20).render(self.job, params(duration=1.0))
        pid_file = self.job / "survivors.pid"
        if pid_file.is_file():
            for line in pid_file.read_text(encoding="ascii").split():
                self.addCleanup(kill_quietly, int(line))

    def test_where_orphans_come_to(self) -> None:
        probe = render._orphans_come_here.__wrapped__
        with mock.patch.object(render.sys, "platform", "win32"):
            self.assertFalse(probe())
        with mock.patch.object(render.sys, "platform", "linux"):
            # PID 1 of its namespace (the compose example's server) inherits
            # every orphan, and nothing else needs asking.
            with mock.patch.object(render.os, "getpid", return_value=1), \
                    mock.patch.object(render, "_is_child_subreaper", side_effect=AssertionError("asked")):
                self.assertTrue(probe())
            for subreaper in (False, True):
                with self.subTest(subreaper=subreaper), \
                        mock.patch.object(render.os, "getpid", return_value=4321), \
                        mock.patch.object(render, "_is_child_subreaper", return_value=subreaper):
                    self.assertEqual(probe(), subreaper)

    @unittest.skipUnless(sys.platform.startswith("linux"), "a child subreaper is Linux's")
    def test_the_killed_members_are_collected_where_orphans_come_to_the_server(self) -> None:
        # In the compose example the server is its container's PID 1, so what
        # a run leaves behind becomes the server's child once the run's
        # leader exits. Killed, such a child stays a zombie, holding a slot of
        # pids_limit, until its parent collects it. A child subreaper inherits
        # orphans the same way, so the render runs in one, which then reports
        # whatever child the render left it.
        for mode, timeout, outcome in (("survivor", 20, "ok"), ("grandchild", 1, "render_timeout")):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                for name in ("survivors.pid", "grandchild.pid", "out.m4a"):
                    (self.job / name).unlink(missing_ok=True)
                result = subprocess.run(
                    [sys.executable, "-I", "-c", SUBREAPER_HELPER, str(helpers.CORE), str(self.job),
                     json.dumps(STUB_COMMAND), str(timeout)],
                    capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["outcome"], outcome)
                self.assertTrue(report["started"], "the stub left no child behind")
                self.assertEqual(report["left"], [], "the render left the server a child it never collected")

    @unittest.skipUnless(sys.platform.startswith("linux"), "waitid on a process group, as Linux has it")
    def test_a_render_running_alongside_keeps_its_exit_status(self) -> None:
        # The core runs renders side by side. Another render's ffmpeg that
        # has exited and not yet been waited for is a child of the server
        # too. A collection that took any child could take it, and that
        # render would then read the status as 0, which is what Popen
        # answers for a child already gone: a failed run would pass.
        other = self.temp / "other"
        other.mkdir()
        valid_wav(other / "in.wav", 1.0)
        exited, finished = threading.Event(), threading.Event()
        outcome: list[str] = []
        real_collect = render._collect

        def collect(proc: subprocess.Popen[bytes], timeout: float) -> bytes:
            if threading.current_thread() is alongside and not exited.is_set():
                # Wait until the run has exited, without collecting it, and
                # hold it there while the other render runs and collects.
                os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
                exited.set()
                finished.wait(60)
            return real_collect(proc, timeout)

        def render_alongside() -> None:
            try:
                Renderer(STUB_COMMAND, 20).render(other, params(normalize=False, duration=1.0))
            except RenderError as error:
                outcome.append(error.code)
            else:
                outcome.append("ok")

        alongside = threading.Thread(target=render_alongside)
        # Its run writes a summary and an output, then exits 1: only the
        # exit status says it failed.
        self.set_mode("late_fail")
        with mock.patch.object(render, "_collect", collect), \
                mock.patch.object(render, "_orphans_come_here", return_value=True):
            alongside.start()
            try:
                self.assertTrue(exited.wait(30), "the render alongside never ran")
                self.set_mode("survivor")
                Renderer(STUB_COMMAND, 20).render(self.job, params(duration=1.0))
            finally:
                finished.set()
                alongside.join(60)
        pid_file = self.job / "survivors.pid"
        if pid_file.is_file():
            for line in pid_file.read_text(encoding="ascii").split():
                self.addCleanup(kill_quietly, int(line))
        self.assertEqual(outcome, ["render_failed"], "the render alongside lost its exit status")

    def test_a_member_that_stays_alive_does_not_hold_the_request(self) -> None:
        # A member the group kill hasn't ended yet is never reported by
        # waitid, which answers None for as long as it lives. The collection
        # gives up after COLLECT_SECONDS, so such a member can't hang the
        # request whose run left it.
        class StillWaiting(Exception):
            pass

        proc = mock.Mock(spec=subprocess.Popen, pid=4321, returncode=0)
        # Windows has none of these; their Linux values stand in there.
        p_pgid = getattr(os, "P_PGID", 2)
        asked: set[tuple[int, int]] = set()
        started = time.monotonic()

        def waitid(idtype: int, ident: int, options: int) -> None:
            asked.add((idtype, ident))
            if time.monotonic() - started > render.COLLECT_SECONDS + 5:
                # Ends a collection that has no bound, so the test fails
                # instead of hanging.
                raise StillWaiting()
            return None

        with mock.patch.object(render, "_orphans_come_here", return_value=True), \
                mock.patch.object(render.os, "waitid", waitid, create=True), \
                mock.patch.object(render.os, "P_PGID", p_pgid, create=True), \
                mock.patch.object(render.os, "WEXITED", getattr(os, "WEXITED", 4), create=True), \
                mock.patch.object(render.os, "WNOHANG", getattr(os, "WNOHANG", 1), create=True):
            try:
                render._collect_group(proc)
            except StillWaiting:
                self.fail("the collection kept waiting for a live member past its bound")
        self.assertLess(time.monotonic() - started, render.COLLECT_SECONDS + 0.5)
        # Only the run's own group was asked about.
        self.assertEqual(asked, {(p_pgid, 4321)})

    @unittest.skipUnless(sys.platform.startswith("linux"), "waitid on a process group, as Linux has it")
    def test_a_leader_not_yet_waited_for_keeps_its_exit_status(self) -> None:
        # The leader is a member of its own group. Called before Popen has
        # waited for it, a collection of the group could take the leader's
        # status, and Popen would then answer 0 for a child already gone:
        # a failed run would pass. So the collection leaves such a group alone.
        proc = subprocess.Popen([sys.executable, "-I", "-c", "raise SystemExit(3)"], start_new_session=True)
        self.addCleanup(proc.wait, 10)
        # Exited, and not waited for.
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
        self.assertIsNone(proc.returncode)
        with mock.patch.object(render, "_orphans_come_here", return_value=True):
            render._collect_group(proc)
        self.assertEqual(proc.wait(timeout=10), 3)


# Runs one render as a child subreaper (prctl PR_SET_CHILD_SUBREAPER, 36), then
# reports any child it still has: a zombie the render didn't collect, or a
# process it didn't kill. Arguments: core/, the job directory, the command as
# JSON, the timeout in seconds.
SUBREAPER_HELPER = """
import ctypes, json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from strange_ringtone.render import Renderer, RenderError, RenderParams
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:
    raise SystemExit("prctl(PR_SET_CHILD_SUBREAPER) failed")
job = Path(sys.argv[2])
params = RenderParams(fade_in=0.0, fade_out=0.0, normalize=True, target_lufs=-14.0, title="t", duration=1.0)
try:
    Renderer(tuple(json.loads(sys.argv[3])), int(sys.argv[4])).render(job, params)
    outcome = "ok"
except RenderError as error:
    outcome = error.code
started = (job / "survivors.pid").exists() or (job / "grandchild.pid").exists()
left = []
deadline = time.monotonic() + 5
while True:
    try:
        found = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        break
    if found is not None:
        left.append("zombie")
        break
    if time.monotonic() >= deadline:
        left.append("alive")
        break
    time.sleep(0.05)
print(json.dumps({"outcome": outcome, "started": started, "left": left}))
"""


class OutputKindTest(StubTestCase):
    """Only a regular file is a result: anything else in its place is refused and removed, a link's target untouched."""

    def render_with(self, make_output: str) -> RenderError:
        # A run that ends like a good one, summary and all, but leaves
        # something of its own choosing at the output's path.
        script = f"import os, sys; out = sys.argv[-1]; {make_output}; sys.stderr.write({REAL_SUMMARY!r})"
        renderer = Renderer((sys.executable, "-I", "-c", script), 20)
        with self.assertRaises(RenderError) as raised:
            renderer.render(self.job, params(normalize=False, duration=1.0))
        return raised.exception

    def test_a_link_as_the_output_is_refused(self) -> None:
        # The job directory is writable by the UID ffmpeg runs as. A link
        # there that the store kept would be opened by the server on
        # download: its own /proc/self/maps, say, which no other process of
        # its UID may read. The target here is a small regular file, so only
        # the kind of the output itself can refuse it.
        target = self.temp / "elsewhere.txt"
        target.write_bytes(b"not the run's output\n")
        probe = self.temp / "probe-link"
        try:
            os.symlink(target, probe)
        except (OSError, NotImplementedError):
            self.skipTest("this account can't create symlinks")
        probe.unlink()
        error = self.render_with(f"os.symlink({str(target)!r}, out)")
        self.assertEqual(error.code, "render_failed")
        self.assertEqual(self.job_names(), ["in.wav"])
        self.assertEqual(target.read_bytes(), b"not the run's output\n")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are POSIX")
    def test_a_fifo_as_the_output_is_refused(self) -> None:
        error = self.render_with("os.mkfifo(out)")
        self.assertEqual(error.code, "render_failed")
        self.assertEqual(self.job_names(), ["in.wav"])


class OutputSizeTest(StubTestCase):
    """An output over the file-size limit is refused and removed, also where prlimit is not in force."""

    def test_an_output_over_the_limit_is_refused_and_removed(self) -> None:
        # No prlimit here, as on Windows: only the renderer's own check after
        # the run stands between an oversized file and the results store.
        first = Renderer(STUB_COMMAND, 20).render(self.job, params(normalize=False, duration=1.0))
        size = first.output.stat().st_size
        first.output.unlink()
        self.assertLess(size, Limits().file_size_bytes)
        # Exactly at the limit is what RLIMIT_FSIZE itself allows.
        exact = Renderer(STUB_COMMAND, 20, limits=Limits(cpu_seconds=20, file_size_bytes=size))
        self.assertEqual(exact.render(self.job, params(normalize=False, duration=1.0)).output.stat().st_size, size)
        (self.job / "out.m4a").unlink()
        small = Renderer(STUB_COMMAND, 20, limits=Limits(cpu_seconds=20, file_size_bytes=size - 1))
        for normalize in (False, True):
            with self.subTest(normalize=normalize):
                with self.assertRaises(RenderError) as raised:
                    small.render(self.job, params(normalize=normalize, duration=1.0))
                self.assertEqual(raised.exception.code, "render_failed")
                self.assertEqual(self.job_names(), ["in.wav"])


class OutputBoundTest(StubTestCase):
    """stdout is read and dropped and only a bounded tail of stderr is kept, so a flood can't fill memory."""

    # The bound's own range: the ebur128 summary (some 600 bytes) and the
    # lines after it fit many times over, and a megabyte is far more than a
    # 40 s pass prints.
    LEAST, MOST = 4 * 1024, 1024 * 1024

    def test_the_bound_is_a_named_constant(self) -> None:
        bound = getattr(render, "STDERR_TAIL_BYTES", None)
        self.assertIsInstance(bound, int, "render.STDERR_TAIL_BYTES, the bytes of stderr kept, is missing")
        assert isinstance(bound, int)
        self.assertGreaterEqual(bound, self.LEAST)
        self.assertLessEqual(bound, self.MOST)

    def test_a_flood_is_read_to_its_end_but_only_the_tail_is_kept(self) -> None:
        # The stub writes 24 MiB to each pipe in turns, then the summary. The
        # render succeeds only if both pipes are read at once, and the summary
        # is found in what was kept.
        self.set_mode("flood")
        kept: list[int] = []
        real_parse = render.parse_summary

        def recording(stderr: str) -> Any:
            kept.append(len(stderr))
            return real_parse(stderr)

        with mock.patch.object(render, "parse_summary", recording):
            result = Renderer(STUB_COMMAND, 20).render(self.job, params(duration=1.0))
        self.assertEqual(self.kinds(), ["measure", "render"])
        self.assertEqual(result.measured_lufs, -14.0)
        self.assertEqual(len(kept), 2)
        for length in kept:
            self.assertLessEqual(length, self.MOST, f"kept {length} characters of stderr")
        bound = getattr(render, "STDERR_TAIL_BYTES", None)
        self.assertIsNotNone(bound, "render.STDERR_TAIL_BYTES is missing")
        for length in kept:
            self.assertLessEqual(length, bound)

    def test_memory_stays_bounded_while_a_flood_is_read(self) -> None:
        # Measured, not inferred from the code: whatever reads the pipes, the
        # Python memory the render allocates stays far under one stream's
        # flood.
        if tracemalloc.is_tracing():
            self.skipTest("tracemalloc is already in use")
        self.set_mode("flood")
        tracemalloc.start()
        try:
            Renderer(STUB_COMMAND, 20).render(self.job, params(normalize=False, duration=1.0))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * 1024 * 1024, f"the render held {peak} bytes while reading the flood")


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.exists(PRLIMIT), "util-linux prlimit")
class PrlimitTest(StubTestCase):
    def test_the_run_has_the_limits(self) -> None:
        renderer = Renderer(STUB_COMMAND, 20, prlimit=(PRLIMIT,))
        renderer.render(self.job, params(duration=1.0))
        for run in self.runs():
            self.assertEqual(run["limits"], {
                "RLIMIT_AS": 512 * 1024 * 1024,
                "RLIMIT_CPU": 20,
                "RLIMIT_FSIZE": 4 * 1024 * 1024,
                "RLIMIT_NOFILE": 64,
            })


class CollectedRunTest(StubTestCase):
    """A run that ended by itself is reaped and leaves no pipe open, so a busy server doesn't leak descriptors."""

    def test_every_run_is_reaped_with_its_pipes_closed(self) -> None:
        procs: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def recording(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            proc = real_popen(*args, **kwargs)
            procs.append(proc)
            return proc

        for mode in ("ok", "flood"):
            with self.subTest(mode=mode):
                procs.clear()
                self.set_mode(mode)
                with mock.patch.object(render.subprocess, "Popen", recording):
                    Renderer(STUB_COMMAND, 20).render(self.job, params(duration=1.0))
                self.assertEqual(len(procs), 2)
                for proc in procs:
                    self.assertEqual(proc.returncode, 0)
                    assert proc.stdout is not None and proc.stderr is not None
                    self.assertTrue(proc.stdout.closed, "stdout left open")
                    self.assertTrue(proc.stderr.closed, "stderr left open")

    def test_only_the_end_of_stderr_is_kept(self) -> None:
        # The tail, not the head: what precedes the bound is dropped, and the
        # last byte the run wrote is the last byte kept.
        script = (
            "import sys; err = sys.stderr.buffer; "
            "err.write(b'a' * 100000); err.write(b'b' * 1000); err.write(b'END'); err.flush()"
        )
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", script],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        kept = render._collect(proc, 20)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(kept), render.STDERR_TAIL_BYTES)
        self.assertTrue(kept.endswith(b"b" * 1000 + b"END"))


if __name__ == "__main__":
    unittest.main()

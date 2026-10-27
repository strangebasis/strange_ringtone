"""The web app's pure audio modules (core/static/audio/), from stdlib unittest.

The JS unit tests are node:test files under tests/js/. They run here when
`node` is on PATH, so `python -I -m unittest discover -s tests` stays the one
command for every test, and they are skipped, saying why, when it is not.
There is no npm, no node_modules and no lockfile: the modules have no
dependencies, and core/static/package.json only marks them as ES modules.

Two checks tie the client to the server instead of trusting that they agree:
the browser's WAV encoder must produce files wav.py accepts up to exactly
MAX_CLIP_SECONDS and refuses one frame later, and every error code the server
can answer with must have its own plain words in request.js. The same holds
for the fetcher and fetch.js, once fetcher/ is in the checkout.
"""
from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

import helpers
from strange_ringtone import wav
from strange_ringtone.config import Settings
from strange_ringtone.wav import WavError, inspect_wav

ROOT = helpers.ROOT
JS_TESTS = ROOT / "tests" / "js"
MAKE_WAV = JS_TESTS / "make_wav.mjs"
AUDIO = ROOT / "core" / "static" / "audio"
REQUEST_JS = AUDIO / "request.js"
FETCH_JS = ROOT / "core" / "static" / "fetch.js"
SERVER_SOURCES = sorted((ROOT / "core" / "strange_ringtone").glob("*.py"))
FETCHER_PACKAGE = ROOT / "fetcher" / "strange_ringtone_fetch"
# The fetcher refuses requests with the core's shared guards as well as its own.
HTTP_GUARD = ROOT / "core" / "strange_ringtone" / "http_guard.py"
NO_FETCHER = "fetcher/strange_ringtone_fetch/ is not in this checkout"
# A job's outcome travels in the status JSON's "error" field rather than as a
# refusal, so the source patterns below need not see it (docs/PLAN.md 6).
FETCH_JOB_ERRORS = frozenset({"blocked", "too_long", "not_found", "timeout", "failed"})
# The fetcher's request refusals, pinned with its API for milestone 5.
FETCH_REFUSALS = frozenset({
    "bad_json", "bad_video_id", "bad_origin", "too_large", "unsupported_media_type", "transfer_encoding",
    "bad_length", "length_required", "rate_limited", "busy", "not_found", "method_not_allowed",
})
NODE = shutil.which("node")
NO_NODE = "node is not on PATH; the JS unit tests need Node 20 or newer (no npm install)"
RATE = 44100
MAX_CLIP_SECONDS = Settings(public_origin="https://tones.example.internal").max_clip_seconds

# Every way the server source spells an error code it sends.
_CODE_PATTERNS = (
    re.compile(r'GuardError\(\s*\d{3},\s*"([a-z_]+)"'),
    re.compile(r'send_json_error\(\s*\d{3},\s*"([a-z_]+)"'),
    re.compile(r'"error":\s*"([a-z_]+)"'),
)


def codes_in(paths: list[Path]) -> set[str]:
    codes: set[str] = set()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for pattern in _CODE_PATTERNS:
            codes.update(pattern.findall(text))
    return codes


def server_error_codes() -> set[str]:
    return codes_in(SERVER_SOURCES)


def fetcher_sources() -> list[Path]:
    return sorted(FETCHER_PACKAGE.glob("*.py"))


def fetcher_error_codes() -> set[str]:
    return codes_in([*fetcher_sources(), HTTP_GUARD]) | FETCH_JOB_ERRORS


def message_keys(path: Path) -> set[str]:
    """The keys of the `const MESSAGES = {...};` table in a client module."""
    text = path.read_text(encoding="utf-8")
    block = re.search(r"const MESSAGES = \{(.*?)\n\};", text, re.S)
    if block is None:
        raise AssertionError(f"no MESSAGES table in {path.name}")
    return set(re.findall(r"^\s+([a-z_]+):", block.group(1), re.M))


def run_node(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    # An argument list, never a shell; node is resolved once by shutil.which.
    return subprocess.run(
        [NODE, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=False,
    )


@unittest.skipIf(NODE is None, NO_NODE)
class NodeUnitTests(unittest.TestCase):
    def test_js_unit_tests_pass(self) -> None:
        # Files, not a directory: what `node --test <dir>` means changed
        # between Node versions.
        files = sorted(p.relative_to(ROOT).as_posix() for p in JS_TESTS.glob("*.test.mjs"))
        self.assertGreaterEqual(len(files), 6, files)
        proc = run_node(["--test", *files], timeout=120)
        self.assertEqual(proc.returncode, 0, f"node --test failed:\n{proc.stdout}\n{proc.stderr}")


@unittest.skipIf(NODE is None, NO_NODE)
class EncoderAcceptedByServerTest(unittest.TestCase):
    """make_wav.mjs uses the browser's encodeWav; wav.py must agree with it to the frame."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def make(self, frames: int, channels: int) -> Path:
        path = self.dir / f"{frames}-{channels}.wav"
        proc = run_node([str(MAKE_WAV), str(path), str(frames), str(channels)], timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return path

    def assert_refused(self, path: Path, code: str) -> None:
        with self.assertRaises(WavError) as raised:
            inspect_wav(path, MAX_CLIP_SECONDS)
        self.assertEqual(raised.exception.code, code)

    def test_exactly_max_clip_seconds_is_accepted_and_one_frame_more_is_too_long(self) -> None:
        frames = MAX_CLIP_SECONDS * RATE
        for channels in (1, 2):
            with self.subTest(channels=channels):
                info = inspect_wav(self.make(frames, channels), MAX_CLIP_SECONDS)
                self.assertEqual((info.channels, info.sample_rate, info.frames), (channels, RATE, frames))
                self.assert_refused(self.make(frames + 1, channels), "too_long")

    def test_half_a_second_is_accepted_and_one_frame_less_is_too_short(self) -> None:
        for channels in (1, 2):
            with self.subTest(channels=channels):
                info = inspect_wav(self.make(RATE // 2, channels), MAX_CLIP_SECONDS)
                self.assertEqual(info.frames, RATE // 2)
                self.assert_refused(self.make(RATE // 2 - 1, channels), "too_short")

    def test_header_is_the_one_shape_the_riff_walk_accepts(self) -> None:
        for frames, channels in ((RATE, 1), (RATE + 1, 2), (RATE * 2 + 7, 1)):
            with self.subTest(frames=frames, channels=channels):
                path = self.make(frames, channels)
                # Read and closed before the TemporaryDirectory is removed:
                # Windows cannot delete a file that is still open.
                with open(path, "rb") as handle:
                    content = handle.read()
                riff, riff_size, form, fmt_id, fmt_size = struct.unpack_from("<4sI4s4sI", content, 0)
                tag, count, rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", content, 20)
                data_id, data_size = struct.unpack_from("<4sI", content, 36)
                self.assertEqual((riff, form, fmt_id, data_id), (b"RIFF", b"WAVE", b"fmt ", b"data"))
                self.assertEqual(riff_size, len(content) - 8)
                self.assertEqual(fmt_size, wav.FMT_SIZE)
                self.assertEqual(tag, wav.WAVE_FORMAT_PCM)
                self.assertEqual((count, rate, bits), (channels, RATE, wav.BITS_PER_SAMPLE))
                self.assertEqual(block_align, channels * 2)
                self.assertEqual(byte_rate, RATE * block_align)
                self.assertEqual(data_size, frames * block_align)
                self.assertEqual(data_size % 2, 0)
                # A 44-byte header, then the samples, then nothing.
                self.assertEqual(len(content), 44 + data_size)
                self.assertNotEqual(content[44:], bytes(data_size))
        self.assertEqual(len(list(self.dir.iterdir())), 3)


class ErrorMessagesCoverServerCodesTest(unittest.TestCase):
    def test_the_server_code_scan_finds_the_codes(self) -> None:
        # Guards the scan itself: if the source patterns stop matching, the
        # coverage test below would pass on an empty set.
        codes = server_error_codes()
        self.assertGreaterEqual(len(codes), 15, codes)
        self.assertTrue({"bad_origin", "too_large", "bad_wav", "busy", "not_found"} <= codes, codes)

    def test_every_server_code_is_a_key_in_request_js(self) -> None:
        text = REQUEST_JS.read_text(encoding="utf-8")
        block = re.search(r"const MESSAGES = \{(.*?)\n\};", text, re.S)
        self.assertIsNotNone(block)
        assert block is not None
        keys = set(re.findall(r"^\s+([a-z_]+):", block.group(1), re.M))
        self.assertEqual(sorted(server_error_codes() - keys), [])

    @unittest.skipIf(NODE is None, NO_NODE)
    def test_error_message_gives_every_code_and_wav_detail_its_own_words(self) -> None:
        codes = sorted(server_error_codes())
        details = sorted(wav.CODES)
        cases = [[400, code, None] for code in codes] + [[400, "bad_wav", detail] for detail in details]
        cases.append([400, "zz_unknown_code", None])
        script = (
            "import { pathToFileURL } from 'node:url';"
            "const [path, payload] = process.argv.slice(1);"
            "const { errorMessage } = await import(pathToFileURL(path).href);"
            "process.stdout.write(JSON.stringify(JSON.parse(payload).map(([s, c, d]) => errorMessage(s, c, d))));"
        )
        proc = run_node(["--input-type=module", "-e", script, str(REQUEST_JS), json.dumps(cases)], timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        messages = json.loads(proc.stdout)
        fallback = messages.pop()
        for (_, code, detail), message in zip(cases[:-1], messages, strict=True):
            with self.subTest(code=code, detail=detail):
                self.assertNotEqual(message, fallback.replace("zz_unknown_code", code))
                self.assertGreater(len(message), 10)
                if detail is not None:
                    # Either its own words or the detail named for a bug report.
                    self.assertTrue(detail in message or detail in ("too_short", "too_long"), message)


class FetchMessagesCoverFetcherCodesTest(unittest.TestCase):
    """fetch.js has plain words for every code the fetcher answers with."""

    def test_fetch_js_has_words_for_the_pinned_codes(self) -> None:
        # Runs without fetcher/ too: the API was pinned before the server existed.
        keys = message_keys(FETCH_JS)
        self.assertEqual(sorted((FETCH_JOB_ERRORS | FETCH_REFUSALS) - keys), [])

    @unittest.skipUnless(FETCHER_PACKAGE.is_dir(), NO_FETCHER)
    def test_the_fetcher_code_scan_finds_the_codes(self) -> None:
        # Guards the scan itself, as the core's test does: if the patterns
        # stop matching the fetcher's source, coverage would pass on the
        # pinned sets alone.
        own = codes_in(fetcher_sources())
        self.assertTrue(own, "no error code found in the fetcher's source")
        # Each job outcome is spelled out in the fetcher's source, so the
        # fixed set above is the one it actually uses.
        text = "\n".join(path.read_text(encoding="utf-8") for path in fetcher_sources())
        for code in sorted(FETCH_JOB_ERRORS):
            with self.subTest(code=code):
                self.assertRegex(text, rf"""["']{code}["']""")

    @unittest.skipUnless(FETCHER_PACKAGE.is_dir(), NO_FETCHER)
    def test_every_fetcher_code_is_a_key_in_fetch_js(self) -> None:
        self.assertEqual(sorted(fetcher_error_codes() - message_keys(FETCH_JS)), [])

    @unittest.skipIf(NODE is None, NO_NODE)
    def test_fetch_error_message_gives_every_code_its_own_words(self) -> None:
        codes = sorted(FETCH_JOB_ERRORS | FETCH_REFUSALS | (fetcher_error_codes() if FETCHER_PACKAGE.is_dir() else set()))
        script = (
            "import { pathToFileURL } from 'node:url';"
            "const [path, payload] = process.argv.slice(1);"
            "const { fetchErrorMessage } = await import(pathToFileURL(path).href);"
            "process.stdout.write(JSON.stringify(JSON.parse(payload).map((code) => fetchErrorMessage(code))));"
        )
        proc = run_node(["--input-type=module", "-e", script, str(FETCH_JS), json.dumps([*codes, "zz_unknown_code"])],
                        timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        messages = json.loads(proc.stdout)
        fallback = messages.pop()
        for code, message in zip(codes, messages, strict=True):
            with self.subTest(code=code):
                self.assertNotEqual(message, fallback)
                self.assertGreater(len(message), 10)
                # Its own words, not the http_<status> fallback either.
                self.assertNotIn("(HTTP ", message)


if __name__ == "__main__":
    unittest.main()

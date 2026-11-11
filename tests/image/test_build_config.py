"""The image's ffmpeg is exactly the committed minimal build (docs/PLAN.md 5.3).

The binary is compared with core/ffmpeg/configure.txt and components.txt,
which the image carries under /app/ffmpeg. A component that appears in the
binary without a line in components.txt fails here, so the build can't grow
without someone writing down why.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

FFMPEG = "/usr/local/bin/ffmpeg"
BUILD_INFO = Path("/app/ffmpeg")
KINDS = ("protocol", "demuxer", "decoder", "encoder", "muxer", "bsf", "parser", "filter")

# " D   wav             WAV / WAVE ..." and "  E  ipod ...": three flag
# columns, then the name (several names are comma-separated, as in mov,mp4).
_FORMAT_LINE = re.compile(r"^ [D ][E ][d ] (\S+)")
# " A....D aac                  AAC ...": six flag columns, then the name.
_CODEC_LINE = re.compile(r"^ [VASDT.][F.][S.][X.][B.][D.] (\S+)")
# " T. afade             A->A       Fade ...": flags, name, then the pad types.
_FILTER_LINE = re.compile(r"^ [A-Z.]{2,3} (\S+)\s+[AVN|]+->[AVN|]+(?:\s|$)")
_PARSER_ENTRY = re.compile(r"&ff_([a-z0-9_]+)_parser,")


def run_ffmpeg(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [FFMPEG, "-hide_banner", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def listing(option: str) -> str:
    proc = run_ffmpeg(option)
    if proc.returncode != 0:
        raise AssertionError(f"ffmpeg {option} exited {proc.returncode}: {proc.stderr}")
    return proc.stdout


def committed_flags() -> list[str]:
    text = (BUILD_INFO / "configure.txt").read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def allowlist() -> dict[str, set[str]]:
    """components.txt as {kind: names}; a malformed or duplicate line fails."""
    allowed: dict[str, set[str]] = {kind: set() for kind in KINDS}
    text = (BUILD_INFO / "components.txt").read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split(None, 2)
        if len(fields) != 3 or fields[0] not in allowed:
            raise AssertionError(f"components.txt line {number} is not '<kind> <name> <why>': {line!r}")
        kind, name, _why = fields
        if name in allowed[kind]:
            raise AssertionError(f"components.txt line {number} repeats {kind} {name}")
        allowed[kind].add(name)
    return allowed


def _lines_after(text: str, separator: re.Pattern[str]) -> list[str]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if separator.fullmatch(line):
            return lines[index + 1:]
    raise AssertionError(f"no separator line in:\n{text}")


def parse_protocols(text: str) -> set[str]:
    # "Input:" then names, "Output:" then names; both directions count.
    return {line.strip() for line in text.splitlines()[1:] if line.startswith("  ") and line.strip()}


def parse_formats(text: str) -> set[str]:
    names: set[str] = set()
    for line in _lines_after(text, re.compile(r"\s*-{2,}\s*")):
        match = _FORMAT_LINE.match(line)
        if match is None:
            raise AssertionError(f"unexpected format line: {line!r}")
        names.update(match[1].split(","))
    return names


def parse_codecs(text: str) -> set[str]:
    names: set[str] = set()
    for line in _lines_after(text, re.compile(r"\s*-{6}\s*")):
        match = _CODEC_LINE.match(line)
        if match is None:
            raise AssertionError(f"unexpected codec line: {line!r}")
        names.add(match[1])
    return names


def parse_bsfs(text: str) -> set[str]:
    return {line.strip() for line in _lines_after(text, re.compile(r"Bitstream filters:")) if line.strip()}


def parse_filters(text: str) -> set[str]:
    names: set[str] = set()
    for line in _lines_after(text, re.compile(r"\s*-{6}\s*")):
        match = _FILTER_LINE.match(line)
        if match is None:
            raise AssertionError(f"unexpected filter line: {line!r}")
        names.add(match[1])
    return names


def built_components() -> dict[str, set[str]]:
    """Every component in the binary, by kind."""
    return {
        "protocol": parse_protocols(listing("-protocols")),
        "demuxer": parse_formats(listing("-demuxers")),
        "decoder": parse_codecs(listing("-decoders")),
        "encoder": parse_codecs(listing("-encoders")),
        "muxer": parse_formats(listing("-muxers")),
        "bsf": parse_bsfs(listing("-bsfs")),
        # ffmpeg has no -parsers option. The build's generated parser list is
        # what libavcodec was compiled with (core/Dockerfile copies it).
        "parser": set(_PARSER_ENTRY.findall((BUILD_INFO / "parser_list.c").read_text(encoding="utf-8"))),
        "filter": parse_filters(listing("-filters")),
    }


class BuildConfigurationTest(unittest.TestCase):
    def test_buildconf_is_the_committed_configure_line(self) -> None:
        text = listing("-buildconf")
        _, marker, after = text.partition("configuration:")
        self.assertTrue(marker, text)
        reported = [line.strip() for line in after.splitlines() if line.strip().startswith("--")]
        # A list, not a set: the same flags in the same order, and none twice.
        self.assertEqual(reported, committed_flags())

    def test_configure_line_starts_from_nothing(self) -> None:
        # The allowlist below only means something on top of these.
        flags = committed_flags()
        for flag in ("--disable-everything", "--disable-network", "--disable-autodetect"):
            with self.subTest(flag=flag):
                self.assertIn(flag, flags)
        self.assertFalse([flag for flag in flags if flag.startswith("--enable-") and "gpl" in flag])

    def test_configure_line_asks_for_the_hardened_toolchain(self) -> None:
        # ffmpeg parses bytes a client sent: PIE, full RELRO, a stack
        # protector and fortified calls make a memory-safety bug there harder
        # to exploit. tests/image/test_ffmpeg_hardening.py checks the linked
        # binary itself; this pins the request in the committed line and in
        # what the binary reports it was configured with.
        self.assertIn("--toolchain=hardened", committed_flags())
        text = listing("-buildconf")
        reported = [line.strip() for line in text.partition("configuration:")[2].splitlines()]
        self.assertIn("--toolchain=hardened", reported)

    def test_every_enabled_component_is_in_the_allowlist(self) -> None:
        allowed = allowlist()
        for flag in committed_flags():
            match = re.fullmatch(r"--enable-(protocol|demuxer|decoder|encoder|muxer|bsf|parser|filter)=(.+)", flag)
            if match is None:
                continue
            for name in match[2].split(","):
                with self.subTest(flag=flag):
                    self.assertIn(name, allowed[match[1]])

    def test_binary_holds_exactly_the_allowlist(self) -> None:
        allowed = allowlist()
        built = built_components()
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(
                    sorted(built[kind] - allowed[kind]), [], f"{kind}s in the binary but not in components.txt"
                )
                self.assertEqual(
                    sorted(allowed[kind] - built[kind]), [], f"{kind}s in components.txt but not in the binary"
                )

    def test_listing_parsers_reject_unexpected_lines(self) -> None:
        # A format change in a future ffmpeg must fail loudly, not parse as an
        # empty listing that trivially matches.
        with self.assertRaises(AssertionError):
            parse_codecs(" ------\n this is not a codec line\n")
        with self.assertRaises(AssertionError):
            parse_formats("no separator here\n")


class NetworkProtocolTest(unittest.TestCase):
    def test_binary_has_no_protocol_but_file(self) -> None:
        # Without -protocol_whitelist, so this proves the binary itself, not
        # the renderer's arguments.
        for url in (
            "http://192.0.2.1/in.wav",
            "https://192.0.2.1/in.wav",
            "tcp://192.0.2.1:80",
            "udp://192.0.2.1:80",
            "pipe:0",
            "data:audio/wav;base64,UklGRg==",
            "concat:/tmp/a.wav|/tmp/b.wav",
        ):
            with self.subTest(url=url):
                proc = run_ffmpeg("-nostdin", "-i", url, "-f", "null", "-")
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("Protocol not found", proc.stderr)


if __name__ == "__main__":
    unittest.main()

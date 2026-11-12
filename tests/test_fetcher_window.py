"""The runner's window program (strange_ringtone_runner.window), with no network and no real audio.

The program reads two things it must not trust: yt-dlp's info JSON and the
bytes a media server answers with (docs/PLAN.md 6.2). These tests pin every
check it makes on both: the address and the headers it accepts, what a byte
range answer must look like, the one MP4 layout it reads, how the window is
planned and cut, and that a refused run leaves no file behind. The server is
a local one on the loopback interface, and the files come from
tests/fmp4_fixture.py.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import http.server
import io
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import urllib.parse
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import fmp4_fixture
import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from fmp4_fixture import Layout, build
from strange_ringtone_runner import window
from strange_ringtone_runner.window import Mp4Error, PastEnd, Ranges, WindowError

# The file the runner runs, wherever the package lies: the checkout, or /app/fetcher in the image.
PROGRAM = Path(window.__file__)
GOOD_URL = "https://rr3---sn-4g5e6nsz.googlevideo.com/videoplayback?expire=1&id=o-abc&itag=140"
GOOD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (test)", "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en-us,en;q=0.5",
    "Sec-Fetch-Mode": "navigate",
}
MIB = 1024 * 1024


def good_info(**changes: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": "dQw4w9WgXcQ", "title": "A title that must never be printed", "url": GOOD_URL,
        "http_headers": dict(GOOD_HEADERS), "protocol": "https", "ext": "m4a", "duration": 3600,
    }
    info.update(changes)
    return info


class AddressTest(unittest.TestCase):
    def test_an_https_address_on_a_googlevideo_subdomain_is_accepted(self) -> None:
        for url in (
            GOOD_URL,
            "https://a.googlevideo.com/",
            "https://rr1---sn-ab5l6nrz.googlevideo.com:443/videoplayback?x=%2F",
            "https://a.b.c.googlevideo.com/videoplayback",
            "https://xn--abc.googlevideo.com/x",
        ):
            with self.subTest(url=url):
                self.assertEqual(window.checked_address(url), url)

    def test_anything_else_is_refused(self) -> None:
        for url in (
            "http://a.googlevideo.com/x",  # not https
            "ftp://a.googlevideo.com/x",
            "file:///etc/passwd",
            "https://googlevideo.com/x",  # no subdomain
            "https://a.googlevideo.com.example.invalid/x",  # a look-alike
            "https://agooglevideo.com/x",
            "https://a.googlevideo.co/x",
            "https://a.youtube.com/x",  # the forwarder allows it; this program needs only the media host
            "https://a.googlevideo.com:8443/x",  # another port
            "https://a.googlevideo.com:/x",
            "https://user@a.googlevideo.com/x",  # a user name
            "https://user:pass@a.googlevideo.com/x",
            "https://@a.googlevideo.com/x",
            "https://example.invalid\\@a.googlevideo.com/x",
            "https://example.invalid#@a.googlevideo.com/x",
            "https://A.googlevideo.com/x",  # upper case
            "HTTPS://a.GOOGLEVIDEO.com/x",
            "https://a.googlevideo.com./x",  # a trailing dot
            "https://-a.googlevideo.com/x",
            "https://a..googlevideo.com/x",
            "https://a%2egooglevideo.com/x",
            "https://[::1]/x",
            "https://[::1/x",  # no address at all: the parser itself refuses it
            "https://127.0.0.1/x",
            "https://a.googlevideo.com/x#fragment",
            "https://a.googlevideo.com/x y",  # a space
            "https://a.googlevideo.com/x\n",
            "https://a.googlevideo.com/é",
            " https://a.googlevideo.com/x",
            "//a.googlevideo.com/x",
            "a.googlevideo.com/x",
            "",
            "https://a.googlevideo.com/" + "x" * window.URL_MAX_CHARS,
            None, 5, b"https://a.googlevideo.com/x", ["https://a.googlevideo.com/x"],
        ):
            with self.subTest(url=url):
                with self.assertRaises(WindowError) as raised:
                    window.checked_address(url)
                self.assertEqual(raised.exception.code, window.EXIT_FAILED)
                # The refusal never repeats what it refused.
                self.assertEqual(str(raised.exception), "no usable address")


class HeadersTest(unittest.TestCase):
    def test_only_the_four_known_headers_are_sent_on(self) -> None:
        given = dict(GOOD_HEADERS, Cookie="a=b", Authorization="Bearer x", Host="example.invalid", Range="bytes=0-")
        self.assertEqual(window.checked_headers(given), GOOD_HEADERS)
        self.assertEqual(window.checked_headers({"User-Agent": "x"}), {"User-Agent": "x"})
        self.assertEqual(window.checked_headers({}), {})
        self.assertEqual(window.checked_headers(None), {})
        self.assertEqual(window.SENT_HEADERS, ("User-Agent", "Accept", "Accept-Language", "Sec-Fetch-Mode"))

    def test_a_value_that_is_not_one_plain_line_refuses_the_run(self) -> None:
        for name in window.SENT_HEADERS:
            for value in ("a\r\nX-Injected: 1", "a\nb", "a\rb", "a\x00b", "a\tb", "café", "", "x" * 513, 5, None,
                          ["a"]):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(WindowError):
                        window.checked_headers({name: value})

    def test_headers_that_are_no_mapping_refuse_the_run(self) -> None:
        for value in ("User-Agent: x", ["User-Agent"], 5, True):
            with self.subTest(value=value):
                with self.assertRaises(WindowError):
                    window.checked_headers(value)


class ReleaseWaitTest(unittest.TestCase):
    def test_the_wait_is_the_time_left_until_the_release(self) -> None:
        self.assertEqual(window.release_wait(None, 1000.0), 0.0)
        self.assertEqual(window.release_wait(990, 1000.0), 0.0)
        self.assertEqual(window.release_wait(1000, 1000.0), 0.0)
        self.assertEqual(window.release_wait(1006, 1000.0), 6.0)
        self.assertEqual(window.release_wait(1006.5, 1000.0), 6.5)
        self.assertEqual(window.release_wait(1000 + window.RELEASE_WAIT_MAX_SECONDS, 1000.0),
                         window.RELEASE_WAIT_MAX_SECONDS)

    def test_a_release_too_far_off_is_refused_at_once(self) -> None:
        with self.assertRaises(WindowError) as raised:
            window.release_wait(1000 + window.RELEASE_WAIT_MAX_SECONDS + 1, 1000.0)
        self.assertEqual(raised.exception.code, window.EXIT_TIMED_OUT)

    def test_a_release_time_that_is_no_number_is_refused(self) -> None:
        for value in ("1006", True, [1006], {"at": 1}, float("nan"), float("inf"), -float("inf"), 10**400):
            with self.subTest(value=value):
                with self.assertRaises(WindowError) as raised:
                    window.release_wait(value, 1000.0)
                self.assertEqual(raised.exception.code, window.EXIT_FAILED)


class SourceTest(unittest.TestCase):
    def test_the_chosen_format_gives_address_headers_and_wait(self) -> None:
        source = window.read_source(good_info(available_at=1006), 1000.0)
        self.assertEqual(source.url, GOOD_URL)
        self.assertEqual(source.headers, GOOD_HEADERS)
        self.assertEqual(source.wait, 6.0)
        self.assertEqual(window.read_source(good_info(http_headers=None), 1000.0).headers, {})

    def test_only_one_plain_m4a_file_over_https_is_a_source(self) -> None:
        for changes in (
            {"protocol": "m3u8_native"}, {"protocol": "http_dash_segments"}, {"protocol": "http"}, {"protocol": None},
            {"ext": "webm"}, {"ext": "mp4"}, {"ext": None}, {"url": None}, {"url": "http://a.googlevideo.com/x"},
            {"http_headers": "x"}, {"available_at": "soon"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(WindowError):
                    window.read_source(good_info(**changes), 1000.0)
        for info in (None, [], "info", 5, [good_info()]):
            with self.subTest(info=info):
                with self.assertRaises(WindowError):
                    window.read_source(info, 1000.0)

    def test_an_address_among_the_formats_is_never_used(self) -> None:
        # Only the top level counts: what yt-dlp chose. A list of formats
        # with addresses of their own does not make up for a missing one.
        info = good_info(formats=[{"format_id": "140", "url": GOOD_URL, "protocol": "https", "ext": "m4a"}])
        del info["url"]
        with self.assertRaises(WindowError):
            window.read_source(info, 1000.0)


def read(built: fmp4_fixture.Built) -> window.Index:
    return window.read_index(built.data[: built.index_end])


class Stored:
    """In place of Ranges for the cut's tests: bytes in memory that stand at base in a file.

    copy hands over all it has from the offset on, in small pieces, whatever
    size was asked for: the one who writes the bytes counts them.
    """

    def __init__(self, data: bytes | bytearray, base: int = 0, piece_bytes: int = 700) -> None:
        self.data = bytes(data)
        self.base = base
        self.piece_bytes = piece_bytes

    def copy(self, offset: int, size: int, sink: Callable[[bytes], object]) -> int:
        data = self.data[offset - self.base:]
        for at in range(0, len(data), self.piece_bytes):
            sink(data[at: at + self.piece_bytes])
        return len(data)


def cut_window(prefix: bytes | bytearray, fragments: bytes | bytearray, index: window.Index,
               plan: window.Plan) -> bytes:
    """The window's file, written as the program writes it, from fragments held in memory."""
    out = io.BytesIO()
    window.write_window(out, prefix, index, plan, Stored(fragments, plan.offset))
    return out.getvalue()


def fetch_window(reader: Any, start: int, seconds: int, max_bytes: int) -> tuple[bytes, float, float]:
    """(the window's file, where it begins in the video, how long it is), the entry's two steps into memory."""
    prefix, index, plan = window.locate(reader, start, seconds, max_bytes)
    out = io.BytesIO()
    window.write_window(out, prefix, index, plan, reader)
    return out.getvalue(), plan.begin / index.timescale, plan.duration / index.timescale


class IndexTest(unittest.TestCase):
    def test_the_index_lists_every_fragment(self) -> None:
        built = build()
        index = read(built)
        self.assertEqual(index.timescale, 44100)
        self.assertEqual(index.media_timescale, 44100)
        self.assertEqual(index.movie_timescale, 1000)
        self.assertEqual(index.init_end, built.init_end)
        self.assertEqual([(s.begin, s.duration, s.offset, s.size) for s in index.segments], built.segments)
        # The fixture is laid out as the real files are: the fragments follow the index.
        self.assertEqual(built.segments[0][2], built.index_end)
        self.assertEqual(built.segments[-1][2] + built.segments[-1][3], len(built.data))

    def test_version_1_boxes_and_another_timescale_are_read(self) -> None:
        built = build(Layout(header_version=1, index_version=1, index_timescale=1000, movie_timescale=600,
                             with_mehd=True))
        index = read(built)
        self.assertEqual((index.timescale, index.media_timescale, index.movie_timescale), (1000, 44100, 600))
        self.assertEqual([(s.begin, s.duration, s.offset, s.size) for s in index.segments], built.segments)
        self.assertEqual(index.segments[1].begin, 10_000)

    def test_an_offset_before_the_first_fragment_and_an_earliest_time_are_kept(self) -> None:
        built = build(Layout(earliest=4410, first_offset=32))
        index = read(built)
        self.assertEqual(index.segments[0].begin, 4410)
        self.assertEqual(index.segments[0].offset, built.index_end + 32)
        self.assertEqual([(s.begin, s.duration, s.offset, s.size) for s in index.segments], built.segments)

    def test_a_64_bit_box_size_is_read(self) -> None:
        built = build()
        ftyp = fmp4_fixture.large_box(b"ftyp", built.data[8:24])
        index = window.read_index(ftyp + built.data[24: built.index_end])
        self.assertEqual(index.init_end, built.init_end + 8)
        self.assertEqual(index.segments[0].offset, built.segments[0][2] + 8)

    def test_the_end_of_the_index_needs_only_the_three_headers(self) -> None:
        built = build()
        for length in (built.init_end + 8, built.init_end + 20, built.index_end, len(built.data)):
            with self.subTest(length=length):
                self.assertEqual(window.index_end(built.data[:length]), built.index_end)
        for length in (0, 7, 24, built.init_end, built.init_end + 7):
            with self.subTest(length=length):
                with self.assertRaises(Mp4Error):
                    window.index_end(built.data[:length])

    def test_an_index_that_is_not_whole_is_refused(self) -> None:
        built = build()
        for length in (built.index_end - 1, built.index_end - 12, built.init_end + 20):
            with self.subTest(length=length):
                with self.assertRaises(Mp4Error):
                    window.read_index(built.data[:length])

    def mutated(self, change: Callable[[bytearray, fmp4_fixture.Built], bytes | bytearray | None],
                layout: Layout | None = None) -> bytes:
        built = build(layout)
        data = bytearray(built.data[: built.index_end])
        changed = change(data, built)
        return bytes(changed if changed is not None else data)

    def test_any_other_layout_is_refused(self) -> None:
        def at(data: bytearray, kind: bytes) -> int:
            return fmp4_fixture.find(bytes(data), kind)

        def put(layout: str, offset_of: Callable[[bytearray, fmp4_fixture.Built], int],
                value: int) -> Callable[..., None]:
            def change(data: bytearray, built: fmp4_fixture.Built) -> None:
                struct.pack_into(layout, data, offset_of(data, built), value)
            return change

        sidx = lambda data, built: built.init_end  # noqa: E731
        first_entry = lambda data, built: built.init_end + 8 + 4 + 8 + 8 + 4  # noqa: E731
        cases: dict[str, tuple[Callable[..., Any], Layout | None]] = {
            "moov first": (lambda data, built: data[24: built.init_end] + data[:24] + data[built.init_end:], None),
            "a box between moov and the index":
                (lambda data, built: data[: built.init_end] + fmp4_fixture.box(b"free", b"") + data[built.init_end:],
                 None),
            "no index": (lambda data, built: data[: built.init_end] + fmp4_fixture.box(b"moof", bytes(16)), None),
            "two tracks": (lambda data, built: None, Layout(tracks=2)),
            "no track": (lambda data, built: None, Layout(tracks=0)),
            "no mvhd": (lambda data, built: data.replace(b"mvhd", b"xvhd"), None),
            "no mdhd": (lambda data, built: data.replace(b"mdhd", b"xdhd"), None),
            "no mdia": (lambda data, built: data.replace(b"mdia", b"xdia"), None),
            "an mvhd timescale of zero": (put(">I", lambda data, built: at(data, b"mvhd") + 8 + 12, 0), None),
            "an mdhd timescale of zero": (put(">I", lambda data, built: at(data, b"mdhd") + 8 + 12, 0), None),
            "an mvhd of version 2": (put(">B", lambda data, built: at(data, b"mvhd") + 8, 2), None),
            "an mdhd of version 2": (put(">B", lambda data, built: at(data, b"mdhd") + 8, 2), None),
            "an index of version 2": (put(">B", lambda data, built: sidx(data, built) + 8, 2), None),
            "an index timescale of zero": (put(">I", lambda data, built: sidx(data, built) + 8 + 8, 0), None),
            "a count one too high": (put(">H", lambda data, built: first_entry(data, built) - 2, 13), None),
            "a count one too low": (put(">H", lambda data, built: first_entry(data, built) - 2, 11), None),
            "a count of zero": (put(">H", lambda data, built: first_entry(data, built) - 2, 0), None),
            "an index of indexes": (put(">I", first_entry, 0x80000000 | 4000), None),
            "an entry smaller than two headers": (put(">I", first_entry, 15), None),
            "an entry of no length": (put(">I", lambda data, built: first_entry(data, built) + 4, 0), None),
            "a box smaller than its header":
                (put(">I", lambda data, built: at(data, b"mvex"), 7), None),
            "a box that runs to the end of the file": (put(">I", lambda data, built: 0, 0), None),
            "a track box larger than moov": (put(">I", lambda data, built: at(data, b"trak"), 100_000), None),
        }
        for name, (change, layout) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(Mp4Error):
                    window.read_index(self.mutated(change, layout))

    def test_the_fixture_itself_passes_before_each_mutation(self) -> None:
        # So that a refusal above is the mutation's doing.
        self.assertEqual(len(window.read_index(self.mutated(lambda data, built: None)).segments), 12)

    def test_a_box_is_never_smaller_than_its_own_header(self) -> None:
        # A size of 0 means "to the end of the file", and a walk through
        # boxes of no size would never move on.
        for size in (0, 2, 7):
            with self.subTest(size=size):
                with self.assertRaises(Mp4Error):
                    window._box_at(struct.pack(">I4s", size, b"free") + bytes(8), 0, 16)
        # With a 64-bit size the header takes sixteen bytes.
        for size in (0, 8, 15):
            with self.subTest(size_in_64_bits=size):
                with self.assertRaises(Mp4Error):
                    window._box_at(struct.pack(">I4sQ", 1, b"free", size), 0, 16)
        self.assertEqual(window._box_at(struct.pack(">I4s", 8, b"free"), 0, 8).end, 8)
        self.assertEqual(window._box_at(struct.pack(">I4sQ", 1, b"free", 16), 0, 16).end, 16)

    def test_the_file_must_begin_ftyp_moov_sidx(self) -> None:
        built = build()
        head = built.data[: built.index_end]
        for kind in (b"ftyp", b"moov", b"sidx"):
            # The same bytes with one of the three boxes under another name.
            renamed = head.replace(kind, b"free", 1)
            self.assertEqual(len(renamed), len(head))
            for reader in (window.index_end, window.read_index):
                with self.subTest(renamed=kind, reader=reader.__name__):
                    with self.assertRaises(Mp4Error) as raised:
                        reader(renamed)
                    self.assertEqual(str(raised.exception), "the file does not begin ftyp, moov, sidx")

    def test_a_box_header_cut_off_before_its_64_bit_size_is_refused(self) -> None:
        # A size of 1 says the real size follows in eight more bytes, and they are not there.
        cut_off = struct.pack(">I4s", 1, b"ftyp") + bytes(4)
        with self.assertRaises(Mp4Error):
            window.index_end(cut_off)
        with self.assertRaises(Mp4Error):
            window.read_index(cut_off)

    def test_a_header_box_that_is_there_twice_is_refused(self) -> None:
        # Each rename makes a neighbour a second box of the kind, at no change in size.
        for name, old, new in (("two mvhd", b"udta", b"mvhd"), ("two mdhd", b"hdlr", b"mdhd"),
                               ("two mdia", b"tkhd", b"mdia")):
            with self.subTest(case=name):
                with self.assertRaises(Mp4Error):
                    window.read_index(self.mutated(lambda data, built, old=old, new=new: data.replace(old, new)))

    def test_a_header_box_too_short_for_its_timescale_and_length_is_refused(self) -> None:
        def header(kind: bytes, version: int, length: int) -> bytes:
            # The timescale itself is in place, so only the box's length can be the reason.
            fields = (bytes(12) if version == 0 else b"\x01" + bytes(19)) + struct.pack(">I", 44100)
            return fmp4_fixture.box(kind, fields + bytes(length - len(fields)))

        for kind, option in ((b"mvhd", "mvhd_box"), (b"mdhd", "mdhd_box")):
            for version, least in ((0, 20), (1, 32)):
                with self.subTest(kind=kind, version=version):
                    index = read(build(Layout(**{option: header(kind, version, least)})))
                    self.assertEqual(len(index.segments), 12)
                    with self.assertRaises(Mp4Error):
                        read(build(Layout(**{option: header(kind, version, least - 1)})))

    def test_an_index_too_short_for_its_fields_is_refused(self) -> None:
        built = build()
        head = struct.pack(">II", 1, 44100)
        for name, sidx in (
            ("no room for the timescale", fmp4_fixture.box(b"sidx", bytes(11))),
            ("version 0 without its count", fmp4_fixture.full_box(b"sidx", 0, head + bytes(8))),
            ("version 1 without its count", fmp4_fixture.full_box(b"sidx", 1, head + bytes(16))),
            # Its length fits its count, and its count is zero.
            ("no entry at all", fmp4_fixture.full_box(b"sidx", 0, head + bytes(8) + struct.pack(">HH", 0, 0))),
        ):
            with self.subTest(case=name):
                with self.assertRaises(Mp4Error):
                    window.read_index(built.data[: built.init_end] + sidx)

    def test_a_track_with_an_edit_list_is_refused(self) -> None:
        # An edit list moves the track's timeline: with one, a fragment's
        # decode time is no longer the time the index lists it under.
        elst = fmp4_fixture.full_box(b"elst", 0, struct.pack(">IIiI", 1, 0, 1024, 0x10000))
        with self.assertRaises(Mp4Error):
            read(build(Layout(trak_extra=fmp4_fixture.box(b"edts", elst))))
        # Another box in the same place is no reason to refuse.
        self.assertEqual(len(read(build(Layout(trak_extra=fmp4_fixture.box(b"free", elst)))).segments), 12)


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.built = build()  # 11 fragments of 10 s and one of about 4.5 s: 114.5 s
        self.index = read(self.built)

    def plan(self, start: int, seconds: int = 600, max_bytes: int = 100 * MIB) -> window.Plan:
        return window.plan_window(self.index, start, seconds, max_bytes)

    def test_the_window_begins_at_the_fragment_that_holds_the_start(self) -> None:
        for start, first in ((0, 0), (1, 0), (9, 0), (10, 1), (19, 1), (25, 2), (30, 3), (109, 10), (110, 11),
                             (114, 11)):
            with self.subTest(start=start):
                plan = self.plan(start)
                self.assertEqual(plan.first, first)
                self.assertEqual(plan.begin, self.built.segments[first][0])
                self.assertEqual(plan.offset, self.built.segments[first][2])
                self.assertLessEqual(plan.begin, start * 44100)

    def test_a_window_that_reaches_the_end_stops_there(self) -> None:
        plan = self.plan(100)
        self.assertEqual((plan.first, plan.last), (10, 11))
        self.assertEqual(plan.duration, 441_000 + 200_000)
        self.assertEqual(plan.offset + plan.size, len(self.built.data))

    def test_the_window_is_never_longer_than_asked(self) -> None:
        for seconds, fragments in ((10, 1), (19, 1), (20, 2), (35, 3), (600, 12)):
            with self.subTest(seconds=seconds):
                plan = self.plan(0, seconds)
                self.assertEqual(plan.last - plan.first + 1, fragments)
                self.assertLessEqual(plan.duration, seconds * 44100)

    def test_the_window_is_never_larger_than_asked(self) -> None:
        sizes = [size for _begin, _duration, _offset, size in self.built.segments]
        for fragments in (1, 2, 5):
            room = sum(sizes[:fragments])
            for extra in (0, sizes[fragments] - 1):
                with self.subTest(fragments=fragments, extra=extra):
                    plan = self.plan(0, 600, room + extra)
                    self.assertEqual(plan.last - plan.first + 1, fragments)
                    self.assertEqual(plan.size, room)

    def test_no_window_passes_either_limit(self) -> None:
        end = self.built.segments[-1][0] + self.built.segments[-1][1]
        for start in range(0, 115, 7):
            for seconds in (10, 25, 60, 600):
                for max_bytes in (1700, 5000, 100 * MIB):
                    with self.subTest(start=start, seconds=seconds, max_bytes=max_bytes):
                        plan = self.plan(start, seconds, max_bytes)
                        self.assertLessEqual(plan.duration, seconds * 44100)
                        self.assertLessEqual(plan.size, max_bytes)
                        self.assertLessEqual(plan.begin + plan.duration, end)
                        self.assertEqual(plan.size, sum(s[3] for s in self.built.segments[plan.first: plan.last + 1]))
                        self.assertEqual(plan.duration,
                                         sum(s[1] for s in self.built.segments[plan.first: plan.last + 1]))

    def test_a_start_at_or_after_the_end_is_past_the_end(self) -> None:
        # The audio is 114.535 s long: second 114 is inside it, 115 is not.
        self.assertEqual(self.plan(114).first, 11)
        for start in (115, 116, 600, 10**9):
            with self.subTest(start=start):
                with self.assertRaises(PastEnd) as raised:
                    self.plan(start)
                self.assertEqual(raised.exception.code, window.EXIT_PAST_END)

    def test_a_first_fragment_that_does_not_fit_is_refused(self) -> None:
        with self.assertRaises(Mp4Error):
            self.plan(0, 9)  # a fragment lasts 10 s
        with self.assertRaises(Mp4Error):
            self.plan(0, 600, self.built.segments[0][3] - 1)

    def test_a_start_before_the_first_fragment_takes_the_first(self) -> None:
        built = build(Layout(earliest=44100))  # the audio begins at second 1
        plan = window.plan_window(read(built), 0, 600, 100 * MIB)
        self.assertEqual((plan.first, plan.begin), (0, 44100))

    def test_limits_that_are_no_limits_are_a_programming_error(self) -> None:
        for arguments in ((-1, 600, MIB), (0, 0, MIB), (0, 600, 0)):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    window.plan_window(self.index, *arguments)


def box_kinds(data: bytes | bytearray) -> list[bytes]:
    return [box.kind for box in window.boxes(data, 0, len(data))]


def decode_times(data: bytes | bytearray) -> list[int]:
    times = []
    for box in window.boxes(data, 0, len(data)):
        if box.kind != b"moof":
            continue
        traf = window.child(data, box, b"traf")
        tfdt = window.child(data, traf, b"tfdt")
        layout = ">I" if data[tfdt.body] == 0 else ">Q"
        times.append(struct.unpack_from(layout, data, tfdt.body + 4)[0])
    return times


def field_at(data: bytes | bytearray, kind: bytes, offset: int, layout: str = ">I") -> int:
    return struct.unpack_from(layout, data, fmp4_fixture.find(bytes(data), kind) + 8 + offset)[0]


class CutTest(unittest.TestCase):
    def cut(self, layout: Layout | None = None, start: int = 25, seconds: int = 30
            ) -> tuple[fmp4_fixture.Built, window.Index, window.Plan, bytes]:
        built = build(layout)
        index = read(built)
        plan = window.plan_window(index, start, seconds, 100 * MIB)
        fragments = built.data[plan.offset: plan.offset + plan.size]
        return built, index, plan, cut_window(built.data[: built.index_end], fragments, index, plan)

    def test_the_cut_is_the_init_part_and_the_window_s_fragments(self) -> None:
        built, index, plan, result = self.cut()
        self.assertEqual((plan.first, plan.last), (2, 4))
        self.assertEqual(box_kinds(result), [b"ftyp", b"moov"] + [b"moof", b"mdat"] * 3)
        self.assertEqual(len(result), built.init_end + plan.size)
        # Which fragments were kept: each mdat repeats its fragment's number.
        payloads = [bytes(result[box.body: box.body + 1]) for box in window.boxes(result, 0, len(result))
                    if box.kind == b"mdat"]
        self.assertEqual(payloads, [b"\x02", b"\x03", b"\x04"])

    def test_the_decode_times_start_at_zero(self) -> None:
        built, _index, plan, result = self.cut()
        self.assertEqual(decode_times(built.data[plan.offset: plan.offset + plan.size]),
                         [882_000, 1_323_000, 1_764_000])
        self.assertEqual(decode_times(result), [0, 441_000, 882_000])

    def test_the_header_states_the_window_s_length(self) -> None:
        built, _index, _plan, result = self.cut(Layout(with_mehd=True))
        # 30 s: 30,000 in the file's timescale of 1000, 1,323,000 in the track's of 44100.
        self.assertEqual(field_at(built.data, b"mvhd", 16), 114_535)
        self.assertEqual(field_at(result, b"mvhd", 16), 30_000)
        self.assertEqual(field_at(result, b"tkhd", 20), 30_000)
        self.assertEqual(field_at(result, b"mehd", 4), 30_000)
        self.assertEqual(field_at(result, b"mdhd", 16), 1_323_000)

    def test_a_length_that_is_no_whole_number_is_rounded_up(self) -> None:
        # The last two fragments: 641,000 ticks of 44100 are 14535.15 ms.
        _built, _index, plan, result = self.cut(start=100)
        self.assertEqual(plan.duration, 641_000)
        self.assertEqual(field_at(result, b"mvhd", 16), 14_536)
        self.assertEqual(field_at(result, b"mdhd", 16), 641_000)

    def test_nothing_else_changes(self) -> None:
        built, index, plan, result = self.cut(Layout(with_mehd=True))
        expected = bytearray(built.data[: built.init_end] + built.data[plan.offset: plan.offset + plan.size])
        # The four lengths in the init part, and each fragment's decode time.
        for kind, offset in ((b"mvhd", 16), (b"tkhd", 20), (b"mehd", 4), (b"mdhd", 16)):
            at = fmp4_fixture.find(bytes(expected), kind) + 8 + offset
            expected[at: at + 4] = result[at: at + 4]
        at = 0
        while True:
            try:
                at = expected.index(b"tfdt", at) + 4 + 4
            except ValueError:
                break
            expected[at: at + 4] = result[at: at + 4]
        self.assertEqual(bytes(result), bytes(expected))

    def test_version_1_boxes_are_rewritten_too(self) -> None:
        built, _index, _plan, result = self.cut(Layout(header_version=1, tfdt_version=1, with_mehd=True))
        self.assertEqual(decode_times(result), [0, 441_000, 882_000])
        self.assertEqual(field_at(built.data, b"mvhd", 24, ">Q"), 114_535)
        self.assertEqual(field_at(result, b"mvhd", 24, ">Q"), 30_000)
        self.assertEqual(field_at(result, b"tkhd", 28, ">Q"), 30_000)
        self.assertEqual(field_at(result, b"mehd", 4, ">Q"), 30_000)
        self.assertEqual(field_at(result, b"mdhd", 24, ">Q"), 1_323_000)

    def test_an_index_in_another_timescale_moves_the_times_in_the_track_s(self) -> None:
        _built, _index, plan, result = self.cut(Layout(index_timescale=1000))
        self.assertEqual(plan.begin, 20_000)
        self.assertEqual(decode_times(result), [0, 441_000, 882_000])
        self.assertEqual(field_at(result, b"mdhd", 16), 1_323_000)

    def refused(self, change: Callable[[bytearray, window.Index, window.Plan], Any],
                layout: Layout | None = None) -> None:
        built = build(layout)
        index = read(built)
        plan = window.plan_window(index, 25, 30, 100 * MIB)
        fragments = bytearray(built.data[plan.offset: plan.offset + plan.size])
        changed = change(fragments, index, plan)
        if isinstance(changed, tuple):
            fragments, index, plan = changed
        with self.assertRaises(Mp4Error):
            cut_window(built.data[: built.index_end], fragments, index, plan)

    def test_fragments_that_are_not_what_the_index_says_are_refused(self) -> None:
        first_size = build().segments[2][3]

        def rename(old: bytes, new: bytes, count: int = 1) -> Callable[..., None]:
            def change(fragments: bytearray, index: window.Index, plan: window.Plan) -> None:
                fragments[:] = fragments.replace(old, new, count)
            return change

        def set_time(which: int, value: int) -> Callable[..., None]:
            def change(fragments: bytearray, index: window.Index, plan: window.Plan) -> None:
                at = -1
                for _ in range(which + 1):
                    at = fragments.index(b"tfdt", at + 1)
                struct.pack_into(">I", fragments, at + 8, value)
            return change

        cases: dict[str, Callable[..., Any]] = {
            "a fragment that begins with another box": rename(b"moof", b"free"),
            "a fragment without its mdat": rename(b"mdat", b"skip"),
            "a moof that claims its mdat's bytes":
                lambda fragments, index, plan: struct.pack_into(">I", fragments, 0, first_size),
            "a moof larger than its fragment":
                lambda fragments, index, plan: struct.pack_into(">I", fragments, 0, first_size + 1),
            "a moof cut short, so a third box appears":
                lambda fragments, index, plan: struct.pack_into(">I", fragments, 0, 16),
            "no decode time": rename(b"tfdt", b"xfdt"),
            "no track fragment": rename(b"traf", b"xraf"),
            "a decode time of version 2":
                lambda fragments, index, plan: struct.pack_into(">B", fragments, fragments.index(b"tfdt") + 4, 2),
            # The index puts the three fragments at ticks 882,000, 1,323,000 and 1,764,000.
            "a first fragment one tick early": set_time(0, 881_999),
            "a first fragment one tick late": set_time(0, 882_001),
            "a fragment at the same time as the one before": set_time(1, 882_000),
            "a later fragment one tick early": set_time(1, 1_322_999),
            "a later fragment one tick late": set_time(1, 1_323_001),
            "a fragment from an hour further on": set_time(2, 1_764_000 + 3600 * 44_100),
            "a fragment earlier than the one before": set_time(2, 1_000_000),
            "one byte too few": lambda fragments, index, plan: (fragments[:-1], index, plan),
            "one byte too many": lambda fragments, index, plan: (fragments + b"\0", index, plan),
            "a length the header cannot hold":
                lambda fragments, index, plan: (fragments, dataclasses.replace(index, movie_timescale=10**9), plan),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.refused(change)

    def one_fragment(self, fragment: bytes, begin: int = 0) -> tuple[bytes, window.Index, window.Plan]:
        """A hand-made fragment as the whole window of the fixture's file, at tick begin in the index."""
        index = read(build())
        segment = window.Segment(begin, 441_000, index.segments[0].offset, len(fragment))
        plan = window.Plan(0, 0, segment.offset, len(fragment), begin, 441_000)
        return fragment, dataclasses.replace(index, segments=(segment,)), plan

    def test_two_tracks_in_one_fragment_are_refused(self) -> None:
        with self.assertRaises(Mp4Error):
            cut_window(build().data, *self.one_fragment(fmp4_fixture.fragment(0, 0, 100, trafs=2)))
        cut_window(build().data, *self.one_fragment(fmp4_fixture.fragment(0, 0, 100)))

    def test_a_fragment_ends_exactly_where_its_mdat_ends(self) -> None:
        whole = fmp4_fixture.fragment(0, 0, 100)
        cut_window(build().data, *self.one_fragment(whole))
        for name, fragment in (
            ("a box behind the mdat", whole + fmp4_fixture.box(b"free", bytes(8))),
            ("bytes behind the mdat", whole + bytes(4)),
            ("an mdat that claims more than the fragment holds", whole[:-1]),
        ):
            with self.subTest(case=name):
                with self.assertRaises(Mp4Error):
                    cut_window(build().data, *self.one_fragment(fragment))

    def test_a_decode_time_that_is_there_twice_is_refused(self) -> None:
        twice = fmp4_fixture.fragment(0, 0, 100, traf_extra=fmp4_fixture.tfdt(0, 0))
        with self.assertRaises(Mp4Error):
            cut_window(build().data, *self.one_fragment(twice))

    def with_decode_time_box(self, tfdt: bytes) -> bytes:
        """A fragment whose traf holds this box where the decode time belongs, and behind it a trun of 24 bytes."""
        trun = fmp4_fixture.full_box(b"trun", 0, bytes(12))
        self.assertEqual(len(trun), 24)
        moof = fmp4_fixture.box(b"moof", fmp4_fixture.full_box(b"mfhd", 0, bytes(4))
                                + fmp4_fixture.box(b"traf", tfdt + trun))
        return moof + fmp4_fixture.box(b"mdat", bytes(100))

    def test_a_decode_time_box_too_short_for_its_time_is_refused(self) -> None:
        # The box ends before its time does, and the four bytes behind it
        # are the next box's size, 24. Read on regardless, they would pass
        # for the time of a fragment the index puts at tick 24, and the new
        # time would be written over that size.
        for name, tfdt in (("version 0", fmp4_fixture.full_box(b"tfdt", 0, b"")),
                           ("version 1", fmp4_fixture.full_box(b"tfdt", 1, bytes(4))),
                           ("no room for a version", fmp4_fixture.box(b"tfdt", b""))):
            with self.subTest(case=name):
                with self.assertRaises(Mp4Error):
                    cut_window(build().data, *self.one_fragment(self.with_decode_time_box(tfdt), begin=24))

    def test_a_decode_time_box_of_an_unknown_version_is_refused(self) -> None:
        # Long enough for either version and holding the right time: only the version is wrong.
        right = struct.pack(">Q", 441_000)

        def fragment(version: int) -> tuple[bytes, window.Index, window.Plan]:
            box = fmp4_fixture.full_box(b"tfdt", version, right)
            return self.one_fragment(self.with_decode_time_box(box), begin=441_000)

        self.assertEqual(decode_times(cut_window(build().data, *fragment(1))[build().init_end:]), [0])
        for version in (2, 255):
            with self.subTest(version=version):
                with self.assertRaises(Mp4Error):
                    cut_window(build().data, *fragment(version))

    def test_the_init_part_must_be_the_one_the_index_was_read_from(self) -> None:
        built = build()
        index = read(built)
        plan = window.plan_window(index, 25, 30, 100 * MIB)
        fragments = built.data[plan.offset: plan.offset + plan.size]
        cut_window(built.data, fragments, index, plan)
        # Another file's start: its moov is sixteen bytes longer, or is no moov at all.
        other = build(Layout(with_mehd=True))
        self.assertEqual(other.init_end, built.init_end + 16)
        for name, prefix in (("a moov that ends elsewhere", other.data),
                             ("no moov", built.data.replace(b"moov", b"free", 1))):
            with self.subTest(case=name):
                with self.assertRaises(Mp4Error):
                    cut_window(prefix, fragments, index, plan)

    def test_a_moof_too_large_to_be_a_sample_table_is_refused_from_its_header(self) -> None:
        # A media server could answer a "fragment" of millions of tiny boxes;
        # walking them would cost the child its memory and its time.
        inner = (fmp4_fixture.full_box(b"mfhd", 0, bytes(4))
                 + fmp4_fixture.box(b"traf", fmp4_fixture.full_box(b"tfhd", 0, bytes(4)) + fmp4_fixture.tfdt(0, 0)))
        padding = fmp4_fixture.box(b"free", b"")
        mdat = fmp4_fixture.box(b"mdat", bytes(100))
        most = (window.MOOF_MAX_BYTES - 8 - len(inner)) // len(padding)
        at_the_cap = fmp4_fixture.box(b"moof", inner + padding * most)
        self.assertEqual(len(at_the_cap), window.MOOF_MAX_BYTES)
        cut_window(build().data, *self.one_fragment(at_the_cap + mdat))
        over = fmp4_fixture.box(b"moof", inner + padding * (most + 1)) + mdat
        real = window.boxes
        walked: list[int] = []

        def watched(data: Any, start: int, end: int) -> Any:
            walked.append(len(data))
            return real(data, start, end)

        with mock.patch.object(window, "boxes", watched), self.assertRaises(Mp4Error):
            cut_window(build().data, *self.one_fragment(over))
        # Only the init part's boxes were walked, never the fragment's.
        self.assertTrue(walked)
        self.assertTrue(all(length < 4096 for length in walked), walked)
        self.assertEqual(window.MOOF_MAX_BYTES, 64 * 1024)

    def test_a_header_box_too_short_for_the_length_is_refused(self) -> None:
        def layout(option: str, kind: bytes, version: int, length: int) -> Layout:
            # 30 s are 7680 ticks of 256, a number whose last byte is zero:
            # written one byte past the box it would change nothing, so only
            # the check itself can be what refuses.
            return Layout(movie_timescale=256, **{option: fmp4_fixture.box(kind, bytes([version]) + bytes(length - 1))})

        # The least body that holds the length, for version 0 and for version 1.
        for kind, option, least_v0, least_v1 in ((b"tkhd", "tkhd_box", 24, 36), (b"mehd", "mehd_box", 8, 12)):
            for version, least in ((0, least_v0), (1, least_v1)):
                with self.subTest(kind=kind, version=version):
                    _built, _index, _plan, result = self.cut(layout(option, kind, version, least))
                    at = fmp4_fixture.find(result, kind) + 8 + least - (4 if version == 0 else 8)
                    self.assertEqual(int.from_bytes(result[at: at + (4 if version == 0 else 8)], "big"), 7680)
                    with self.assertRaises(Mp4Error):
                        self.cut(layout(option, kind, version, least - 1))
            with self.subTest(kind=kind, version=2):
                with self.assertRaises(Mp4Error):
                    self.cut(layout(option, kind, 2, 64))

    def test_the_file_never_grows_past_the_planned_size(self) -> None:
        built = build()
        index = read(built)
        plan = window.plan_window(index, 25, 30, 100 * MIB)
        # A reader that hands over the rest of the file, far more than was asked for.
        out = io.BytesIO()
        with self.assertRaises(Mp4Error):
            window.write_window(out, built.data, index, plan, Stored(built.data[plan.offset:], plan.offset))
        self.assertLessEqual(len(out.getvalue()), built.init_end + plan.size)
        self.assertGreater(len(built.data) - plan.offset, plan.size + 700)

    def test_a_start_that_is_no_whole_number_in_the_track_s_timescale_is_refused(self) -> None:
        # One millisecond is 44.1 ticks of 44100.
        self.refused(lambda fragments, index, plan: None, Layout(index_timescale=1000, earliest=1))

    def test_the_fixture_itself_is_cut_before_each_mutation(self) -> None:
        built = build()
        index = read(built)
        plan = window.plan_window(index, 25, 30, 100 * MIB)
        cut_window(built.data[: built.index_end], built.data[plan.offset: plan.offset + plan.size], index, plan)


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves the server's data by byte range, or misbehaves as server.mode says."""

    server: RangeServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def answer(self, status: int, headers: dict[str, str] | list[tuple[str, str]],
               body: bytes | memoryview = b"") -> None:
        self.send_response(status)
        for name, value in headers.items() if isinstance(headers, dict) else headers:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def pour(self) -> None:
        """Send the server's whole_bytes of zeros as a body, or as many as the client takes before it hangs up."""
        server = self.server
        block = bytes(65536)
        try:
            while server.whole_sent < server.whole_bytes:
                self.wfile.write(block)
                server.whole_sent += len(block)
        except OSError:
            pass
        server.whole_done.set()

    def do_GET(self) -> None:
        server = self.server
        with server.lock:
            server.seen.append({"path": self.path, "headers": dict(self.headers.items())})
            number = len(server.seen)
        mode, data = server.mode, server.data
        if self.path.startswith("/hop"):
            locations = [server.location] if server.locations is None else server.locations
            self.send_response(server.hop_status)
            for location in locations:
                self.send_header("Location", location)
            self.send_header("Content-Length", str(server.whole_bytes if server.hop_body else 0))
            self.end_headers()
            if server.hop_body:
                self.pour()
            return
        first, last = (int(text) for text in self.headers["Range"].removeprefix("bytes=").split("-"))
        last = min(last, len(data) - 1)
        total = len(data)
        if mode == "stall":
            time.sleep(server.stall_seconds)
        if mode == "whole":
            self.send_response(200)
            self.send_header("Content-Length", str(server.whole_bytes))
            self.end_headers()
            self.pour()
            return
        if mode.startswith("status:"):
            self.answer(int(mode.removeprefix("status:")), {"Content-Length": "0"})
            return
        if mode == "crumbs":
            last = first
        if mode == "total_grows":
            total += number
        # A view, not a copy: the memory test measures the program, and the
        # server's own copy of a whole range would drown that out.
        body: bytes | memoryview = memoryview(data)[first: last + 1]
        content_range = f"bytes {first}-{last}/{total}"
        if mode == "other_start":
            content_range = f"bytes {first + 1}-{last}/{total}"
        elif mode == "past_the_range":
            body = bytes(body) + b"\0"
            content_range = f"bytes {first}-{last + 1}/{total + 5}"
        elif mode == "end_at_total":
            content_range = f"bytes {first}-{last}/{last}"
        elif mode == "bytes_star":
            content_range = f"bytes {first}-{last}/*"
        headers = [("Content-Length", str(len(body))), ("Content-Range", content_range)]
        if mode == "no_content_range":
            headers = headers[:1]
        elif mode == "two_content_ranges":
            headers.append(("Content-Range", content_range))
        elif mode == "gzip":
            headers.append(("Content-Encoding", "gzip"))
        elif mode == "identity":
            headers.append(("Content-Encoding", "identity"))
        elif mode == "short_body":
            body = body[: len(body) // 2]
        self.answer(200 if mode == "plain_200" else 206, headers, body)


class RangeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, data: bytes) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.data = data
        self.mode = "ok"
        # What a request to /hop is answered with: a redirect to location
        # (or to each of locations), with a body of whole_bytes when
        # hop_body is set.
        self.location = ""
        self.locations: list[str] | None = None
        self.hop_status = 302
        self.hop_body = False
        self.stall_seconds = 0.0
        self.whole_bytes = 256 * MIB
        self.whole_sent = 0
        self.whole_done = threading.Event()
        self.lock = threading.Lock()
        self.seen: list[dict[str, Any]] = []

    @property
    def netloc(self) -> str:
        return f"127.0.0.1:{self.server_address[1]}"

    def url(self, path: str = "/videoplayback?expire=1") -> str:
        return f"http://{self.netloc}{path}"

    def allowed(self, url: object) -> str:
        """The test's stand-in for checked_address: this server's own address, by its exact netloc."""
        if not isinstance(url, str):
            raise WindowError("no usable address")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "http" or parts.netloc != self.netloc:
            raise WindowError("no usable address")
        return url

    def ranges(self, path: str = "/videoplayback?expire=1", **options: Any) -> Ranges:
        options.setdefault("proxies", {})
        options.setdefault("allowed", self.allowed)
        return Ranges(self.url(path), dict(GOOD_HEADERS), **options)


@contextlib.contextmanager
def serving(data: bytes) -> Iterator[RangeServer]:
    server = RangeServer(data)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


class RangesTest(unittest.TestCase):
    DATA = bytes(range(256)) * 400  # 102,400 bytes, every position recognisable

    def test_a_range_is_read_in_requests_of_at_most_the_chunk(self) -> None:
        with serving(self.DATA) as server:
            ranges = server.ranges(chunk_bytes=30_000)
            self.assertEqual(bytes(ranges.read(1000, 70_000)), self.DATA[1000:71_000])
            self.assertEqual(ranges.total, len(self.DATA))
            self.assertEqual([seen["headers"]["Range"] for seen in server.seen],
                             ["bytes=1000-30999", "bytes=31000-60999", "bytes=61000-70999"])
            self.assertEqual(ranges.requests, 3)

    def test_each_request_carries_the_headers_and_asks_for_no_encoding(self) -> None:
        with serving(self.DATA) as server:
            server.ranges().read(0, 10)
            sent = server.seen[0]["headers"]
            for name, value in GOOD_HEADERS.items():
                self.assertEqual(sent[name], value)
            self.assertEqual(sent["Accept-Encoding"], "identity")
            self.assertEqual(sent["Connection"], "close")
            self.assertEqual(server.seen[0]["path"], "/videoplayback?expire=1")

    def test_a_read_past_the_end_returns_what_the_file_has(self) -> None:
        with serving(self.DATA) as server:
            ranges = server.ranges(chunk_bytes=30_000)
            self.assertEqual(bytes(ranges.read(len(self.DATA) - 100, 5000)), self.DATA[-100:])
            # The size is known now, so nothing more is asked for.
            asked = len(server.seen)
            self.assertEqual(bytes(ranges.read(len(self.DATA), 10)), b"")
            self.assertEqual(len(server.seen), asked)

    def test_a_server_that_ignores_the_range_is_refused_unread(self) -> None:
        with serving(self.DATA) as server:
            server.mode = "whole"
            with self.assertRaises(WindowError) as raised:
                server.ranges().read(0, 1000)
            self.assertEqual(str(raised.exception), "the server ignored the range")
            self.assertEqual(raised.exception.code, window.EXIT_FAILED)
            # The server tried to send 256 MiB and was cut off: the body was never read.
            self.assertTrue(server.whole_done.wait(30))
            self.assertLess(server.whole_sent, server.whole_bytes)

    def test_answers_that_are_not_the_range_asked_for_are_refused(self) -> None:
        # Each with its own reason, so that no other check can stand in for the one meant.
        reasons = {
            "plain_200": "the server ignored the range",
            "other_start": "the server answered another range",
            "past_the_range": "the server answered another range",
            "end_at_total": "the server answered another range",
            "bytes_star": "no usable Content-Range",
            "no_content_range": "no usable Content-Range",
            "two_content_ranges": "no usable Content-Range",
            "gzip": "the answer is encoded",
            "short_body": "the answer ended early",
            "total_grows": "the file changed size between requests",
        }
        for mode, reason in reasons.items():
            with self.subTest(mode=mode), serving(self.DATA) as server:
                server.mode = mode
                with self.assertRaises(WindowError) as raised:
                    server.ranges(chunk_bytes=1000).read(100, 3000)
                self.assertEqual(str(raised.exception), reason)
                self.assertEqual(raised.exception.code, window.EXIT_FAILED)
                # Refused with the first answer that shows it: the file's
                # size can only be seen to change with the second.
                self.assertEqual(len(server.seen), 2 if mode == "total_grows" else 1)

    def test_an_answer_that_names_no_encoding_or_identity_is_read(self) -> None:
        with serving(self.DATA) as server:
            server.mode = "identity"
            self.assertEqual(bytes(server.ranges().read(5, 10)), self.DATA[5:15])

    def test_a_refusal_by_the_server_is_blocked_and_other_errors_are_failed(self) -> None:
        for status, code in ((403, window.EXIT_BLOCKED), (404, window.EXIT_FAILED), (410, window.EXIT_FAILED),
                             (416, window.EXIT_FAILED), (429, window.EXIT_FAILED), (500, window.EXIT_FAILED),
                             (503, window.EXIT_FAILED), (204, window.EXIT_FAILED)):
            with self.subTest(status=status), serving(self.DATA) as server:
                server.mode = f"status:{status}"
                with self.assertRaises(WindowError) as raised:
                    server.ranges().read(0, 10)
                self.assertEqual(raised.exception.code, code)

    def test_a_server_that_answers_in_crumbs_is_not_waited_for(self) -> None:
        with serving(self.DATA) as server:
            server.mode = "crumbs"
            with self.assertRaises(WindowError):
                server.ranges(chunk_bytes=1000).read(0, 2500)
            # 2500 bytes in chunks of 1000 take 3 requests; 2 more are allowed.
            self.assertEqual(len(server.seen), 5)

    def test_a_request_that_stalls_times_out(self) -> None:
        with serving(self.DATA) as server:
            server.mode = "stall"
            server.stall_seconds = 3.0
            started = time.monotonic()
            with self.assertRaises(WindowError) as raised:
                server.ranges(timeout=0.3).read(0, 10)
            self.assertEqual(raised.exception.code, window.EXIT_TIMED_OUT)
            self.assertLess(time.monotonic() - started, 2.5)

    def test_a_server_that_is_not_there_fails(self) -> None:
        with serving(self.DATA) as server:
            ranges = server.ranges()
        with self.assertRaises(WindowError) as raised:
            ranges.read(0, 10)
        self.assertEqual(raised.exception.code, window.EXIT_FAILED)
        self.assertEqual(str(raised.exception), "the request failed")

    def test_a_redirect_is_followed_only_to_an_allowed_address(self) -> None:
        with serving(self.DATA) as server:
            server.location = server.url("/moved?expire=2")
            self.assertEqual(bytes(server.ranges("/hop").read(10, 20)), self.DATA[10:30])
            # The second request kept the range and the headers.
            self.assertEqual([seen["path"] for seen in server.seen], ["/hop", "/moved?expire=2"])
            self.assertEqual(server.seen[1]["headers"]["Range"], "bytes=10-29")
            self.assertEqual(server.seen[1]["headers"]["User-Agent"], GOOD_HEADERS["User-Agent"])
        for location in ("http://localhost:{port}/x", "http://127.0.0.1:{port}@example.invalid/x",
                         "https://127.0.0.1:{port}/x", "ftp://127.0.0.1:{port}/x", "file:///etc/passwd"):
            with self.subTest(location=location), serving(self.DATA) as server:
                server.location = location.format(port=server.server_address[1])
                with self.assertRaises(WindowError):
                    server.ranges("/hop").read(10, 20)
                # Refused before any request to the new address.
                self.assertEqual([seen["path"] for seen in server.seen], ["/hop"])

    def test_a_chain_of_redirects_ends(self) -> None:
        with serving(self.DATA) as server:
            server.location = server.url("/hop2")
            with self.assertRaises(WindowError) as raised:
                server.ranges("/hop").read(10, 20)
            self.assertEqual(str(raised.exception), "too many redirects")
            self.assertEqual(len(server.seen), window.MAX_REDIRECTS + 1)
        self.assertEqual(window.MAX_REDIRECTS, 3)

    def test_the_body_of_a_redirect_is_never_read(self) -> None:
        with serving(self.DATA) as server:
            server.location = server.url("/moved")
            server.hop_body = True
            self.assertEqual(bytes(server.ranges("/hop").read(10, 20)), self.DATA[10:30])
            # The server had 256 MiB to send with its redirect and was cut off.
            self.assertTrue(server.whole_done.wait(30))
            self.assertLess(server.whole_sent, server.whole_bytes)

    def test_the_body_of_an_error_is_never_read(self) -> None:
        for status, code in ((403, window.EXIT_BLOCKED), (500, window.EXIT_FAILED)):
            with self.subTest(status=status), serving(self.DATA) as server:
                server.hop_status, server.hop_body = status, True
                with self.assertRaises(WindowError) as raised:
                    server.ranges("/hop").read(10, 20)
                self.assertEqual(raised.exception.code, code)
                self.assertTrue(server.whole_done.wait(30))
                self.assertLess(server.whole_sent, server.whole_bytes)

    def test_each_kind_of_redirect_is_followed_and_no_other_answer(self) -> None:
        self.assertEqual(window.REDIRECT_STATUSES, (301, 302, 303, 307, 308))
        for status in window.REDIRECT_STATUSES:
            with self.subTest(status=status), serving(self.DATA) as server:
                server.location, server.hop_status = server.url("/moved"), status
                self.assertEqual(bytes(server.ranges("/hop").read(10, 20)), self.DATA[10:30])
                self.assertEqual([seen["path"] for seen in server.seen], ["/hop", "/moved"])
        for status in (300, 304, 305, 306):
            with self.subTest(status=status), serving(self.DATA) as server:
                server.location, server.hop_status = server.url("/moved"), status
                with self.assertRaises(WindowError) as raised:
                    server.ranges("/hop").read(10, 20)
                self.assertEqual(raised.exception.code, window.EXIT_FAILED)
                self.assertEqual([seen["path"] for seen in server.seen], ["/hop"])

    def test_a_redirect_must_name_exactly_one_address(self) -> None:
        for locations in ([], ["/moved", "/moved"], ["/moved", "http://example.invalid/x"]):
            with self.subTest(locations=locations), serving(self.DATA) as server:
                server.locations = locations
                with self.assertRaises(WindowError) as raised:
                    server.ranges("/hop").read(10, 20)
                self.assertEqual(str(raised.exception), "a redirect without one address")
                self.assertEqual([seen["path"] for seen in server.seen], ["/hop"])

    def test_a_relative_redirect_is_read_against_the_address_it_came_from(self) -> None:
        with serving(self.DATA) as server:
            server.location = "/moved?expire=2"
            self.assertEqual(bytes(server.ranges("/hop").read(10, 20)), self.DATA[10:30])
            self.assertEqual([seen["path"] for seen in server.seen], ["/hop", "/moved?expire=2"])
        # What is checked is the address the next request would go to, not
        # the header's own text: these name another host, or none at all.
        for location in ("//example.invalid/x", "//127.0.0.1:1/x", "http://[::1/x"):
            with self.subTest(location=location), serving(self.DATA) as server:
                server.location = location
                with self.assertRaises(WindowError) as raised:
                    server.ranges("/hop").read(10, 20)
                self.assertEqual(str(raised.exception), "no usable address")
                self.assertEqual([seen["path"] for seen in server.seen], ["/hop"])

    def test_a_range_is_handed_on_in_small_pieces(self) -> None:
        with serving(self.DATA) as server:
            pieces: list[bytes] = []
            with mock.patch.object(window, "READ_BYTES", 4096):
                count = server.ranges(chunk_bytes=30_000).copy(1000, 70_000, pieces.append)
            self.assertEqual(count, 70_000)
            self.assertEqual(b"".join(pieces), self.DATA[1000:71_000])
            self.assertTrue(all(len(piece) <= 4096 for piece in pieces))
            self.assertGreaterEqual(len(pieces), 18)
        self.assertEqual(window.READ_BYTES, 64 * 1024)

    def test_a_sink_that_refuses_ends_the_copy(self) -> None:
        def sink(piece: bytes) -> None:
            raise Mp4Error("enough")

        with serving(self.DATA) as server:
            with self.assertRaises(Mp4Error):
                server.ranges(chunk_bytes=30_000).copy(0, 90_000, sink)
            self.assertEqual(len(server.seen), 1)

    def test_the_first_address_is_checked_before_any_request(self) -> None:
        for url in ("http://a.googlevideo.com/x", "https://example.invalid/x", "file:///etc/passwd", None):
            with self.subTest(url=url):
                with self.assertRaises(WindowError):
                    Ranges(url, {})  # type: ignore[arg-type]

    def test_only_http_and_https_can_be_opened_at_all(self) -> None:
        # Even with a check that lets everything through, the opener itself
        # has no handler for a file, an ftp or a data address.
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "secret"
            target.write_bytes(b"not for reading")
            for url in (target.as_uri(), "ftp://127.0.0.1/x", "data:text/plain,hello"):
                with self.subTest(url=url):
                    ranges = Ranges(url, {}, proxies={}, allowed=lambda address: str(address))
                    with self.assertRaises(WindowError):
                        ranges.read(0, 10)

    def test_no_proxy_is_read_from_the_environment_when_a_mapping_is_given(self) -> None:
        with serving(self.DATA) as server, mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9"}):
            self.assertEqual(bytes(server.ranges().read(0, 4)), self.DATA[:4])

    def test_the_environment_s_proxy_is_used_by_default(self) -> None:
        # The proxy here is the test server itself: it sees the absolute
        # address a client sends to a proxy for plain http.
        with serving(self.DATA) as server:
            with mock.patch.dict(os.environ, {"http_proxy": f"http://{server.netloc}", "no_proxy": ""}):
                ranges = Ranges("http://media.example.invalid/videoplayback", {}, allowed=lambda address: str(address))
                self.assertEqual(bytes(ranges.read(0, 4)), self.DATA[:4])
            self.assertEqual(server.seen[0]["path"], "http://media.example.invalid/videoplayback")


class FetchWindowTest(unittest.TestCase):
    def test_the_window_is_fetched_in_ranges_and_cut(self) -> None:
        built = build()
        first_read = built.index_end + 100
        with serving(built.data) as server, mock.patch.object(window, "PREFIX_BYTES", first_read):
            ranges = server.ranges(chunk_bytes=2000)
            data, begin, duration = fetch_window(ranges, 25, 30, 100 * MIB)
        index = read(built)
        plan = window.plan_window(index, 25, 30, 100 * MIB)
        expected = cut_window(built.data[: built.index_end], built.data[plan.offset: plan.offset + plan.size],
                              index, plan)
        self.assertEqual(data, expected)
        self.assertEqual((begin, duration), (20.0, 30.0))
        asked = [tuple(int(text) for text in seen["headers"]["Range"].removeprefix("bytes=").split("-"))
                 for seen in server.seen]
        # The start of the file first, then only the window's own bytes, in
        # requests of at most the chunk size, with nothing asked for twice.
        self.assertEqual(asked[0], (0, first_read - 1))
        self.assertEqual(asked[1][0], plan.offset)
        self.assertEqual(asked[-1][1], plan.offset + plan.size - 1)
        self.assertEqual(len(asked), 1 + -(-plan.size // 2000))
        for (_first, last), (following, _last) in zip(asked[1:], asked[2:], strict=False):
            self.assertEqual(following, last + 1)
        self.assertTrue(all(last - first < 2000 for first, last in asked[1:]))

    def test_an_index_longer_than_the_first_read_is_read_in_a_second_one(self) -> None:
        built = build(Layout(fragments=400, payload_bytes=64))
        self.assertGreater(built.index_end, 4000)
        with serving(built.data) as server, mock.patch.object(window, "PREFIX_BYTES", 1000):
            data, begin, _duration = fetch_window(server.ranges(), 3000, 20, 100 * MIB)
            asked = [seen["headers"]["Range"] for seen in server.seen]
        self.assertEqual(asked[:2], ["bytes=0-999", f"bytes=1000-{built.index_end - 1}"])
        self.assertEqual(begin, 3000.0)
        self.assertEqual(box_kinds(data), [b"ftyp", b"moov"] + [b"moof", b"mdat"] * 2)

    def test_an_index_larger_than_the_program_reads_is_refused(self) -> None:
        built = build(Layout(fragments=400, payload_bytes=64))
        with serving(built.data) as server, mock.patch.object(window, "PREFIX_BYTES", 1000), \
                mock.patch.object(window, "PREFIX_MAX_BYTES", built.index_end - 1):
            with self.assertRaises(Mp4Error):
                fetch_window(server.ranges(), 0, 20, 100 * MIB)
            # Refused from the headers alone: the rest of the index was never asked for.
            self.assertEqual(len(server.seen), 1)

    def test_a_file_shorter_than_its_index_says_is_refused(self) -> None:
        built = build()
        for length in (built.index_end - 10, built.index_end, len(built.data) - 1):
            with self.subTest(length=length), serving(built.data[:length]) as server:
                with self.assertRaises(Mp4Error):
                    fetch_window(server.ranges(), 0, 20, 100 * MIB)

    def test_the_file_written_fits_the_byte_limit_with_its_init_part(self) -> None:
        built = build()
        sizes = [size for _begin, _duration, _offset, size in built.segments]
        with serving(built.data) as server:
            limit = built.init_end + sizes[0] + sizes[1]
            data, _begin, duration = fetch_window(server.ranges(), 0, 600, limit)
            self.assertEqual(len(data), limit)
            self.assertEqual(duration, 20.0)
            data, _begin, duration = fetch_window(server.ranges(), 0, 600, limit - 1)
            self.assertEqual(duration, 10.0)
            for too_small in (built.init_end + sizes[0] - 1, built.init_end, 1):
                with self.subTest(limit=too_small):
                    with self.assertRaises(Mp4Error):
                        fetch_window(server.ranges(), 0, 600, too_small)

    def test_a_start_past_the_end_is_told_apart(self) -> None:
        built = build()
        with serving(built.data) as server:
            with self.assertRaises(PastEnd):
                fetch_window(server.ranges(), 115, 600, 100 * MIB)
            # Known from the index alone: no fragment was asked for.
            self.assertEqual(len(server.seen), 1)

    def test_garbage_instead_of_a_file_is_refused(self) -> None:
        for data in (b"", b"\0" * 5000, b"<html>" + b"x" * 5000, os.urandom(5000), build().data[24:]):
            with self.subTest(length=len(data)), serving(data or b"x") as server:
                with self.assertRaises(WindowError):
                    fetch_window(server.ranges(), 0, 20, 100 * MIB)


class FakeRanges:
    """In place of Ranges for the entry's tests: serves a file from memory and records how it was made."""

    made: list[tuple[str, dict[str, str]]] = []
    data = b""
    error: BaseException | None = None
    # When set, a copy breaks off once this many bytes went to the sink.
    break_after: int | None = None

    def __init__(self, url: str, headers: dict[str, str]) -> None:
        type(self).made.append((url, headers))
        self.total: int | None = None

    def read(self, offset: int, size: int) -> bytearray:
        if type(self).error is not None:
            raise type(self).error
        self.total = len(type(self).data)
        return bytearray(type(self).data[offset: offset + size])

    def copy(self, offset: int, size: int, sink: Callable[[bytes], object]) -> int:
        data = self.read(offset, size)
        for at in range(0, len(data), 500):
            if type(self).break_after is not None and at >= type(self).break_after:
                raise WindowError("the request timed out", window.EXIT_TIMED_OUT)
            sink(bytes(data[at: at + 500]))
        return len(data)


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.job = Path(temp.name)
        self.info = self.job / "dQw4w9WgXcQ.info.json"
        self.audio = self.job / "dQw4w9WgXcQ.m4a"
        self.result = self.job / "dQw4w9WgXcQ.window.json"
        self.built = build()
        FakeRanges.made = []
        FakeRanges.data = self.built.data
        FakeRanges.error = None
        FakeRanges.break_after = None
        patcher = mock.patch.object(window, "Ranges", FakeRanges)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.slept: list[float] = []
        patcher = mock.patch.object(window.time, "sleep", self.slept.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_info(self, info: Any) -> None:
        self.info.write_bytes(info if isinstance(info, bytes) else json.dumps(info).encode("utf-8"))

    def run_main(self, start: str = "25", seconds: str = "30", max_bytes: str = str(100 * MIB),
                 stderr: Any = None) -> int:
        with mock.patch.object(sys, "stderr", stderr if stderr is not None else open(os.devnull, "w")) as stream:
            try:
                return window.main([str(self.info), str(self.audio), str(self.result), start, seconds, max_bytes])
            finally:
                if stderr is None:
                    stream.close()

    def left(self) -> list[str]:
        return sorted(path.name for path in self.job.iterdir())

    def test_a_run_writes_the_window_and_where_it_begins(self) -> None:
        self.write_info(good_info())
        self.assertEqual(self.run_main(), window.EXIT_OK)
        index = read(self.built)
        plan = window.plan_window(index, 25, 30, 100 * MIB)
        expected = cut_window(self.built.data[: self.built.index_end],
                              self.built.data[plan.offset: plan.offset + plan.size], index, plan)
        self.assertEqual(self.audio.read_bytes(), expected)
        self.assertEqual(json.loads(self.result.read_bytes()), {"start": 20.0, "duration": 30.0})
        self.assertEqual(self.result.read_bytes()[-1:], b"\n")
        self.assertEqual(FakeRanges.made, [(GOOD_URL, GOOD_HEADERS)])
        self.assertEqual(self.left(), sorted([self.audio.name, self.info.name, self.result.name]))
        self.assertEqual(self.slept, [])
        if os.name == "posix":
            self.assertEqual(os.stat(self.audio).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(self.result).st_mode & 0o777, 0o600)

    def test_it_waits_for_the_audio_s_release_first(self) -> None:
        self.write_info(good_info(available_at=time.time() + 6))
        self.assertEqual(self.run_main(), window.EXIT_OK)
        self.assertEqual(len(self.slept), 1)
        self.assertTrue(5.0 < self.slept[0] <= 6.0, self.slept)

    def test_each_outcome_has_its_own_status_and_leaves_no_file(self) -> None:
        cases: dict[str, tuple[Callable[[], Any], int]] = {
            "a start past the end": (lambda: None, window.EXIT_PAST_END),
            "a refusal by the server":
                (lambda: setattr(FakeRanges, "error", WindowError("the server refused the request",
                                                                  window.EXIT_BLOCKED)), window.EXIT_BLOCKED),
            "a request that timed out":
                (lambda: setattr(FakeRanges, "error", WindowError("the request timed out", window.EXIT_TIMED_OUT)),
                 window.EXIT_TIMED_OUT),
            "a release too far off":
                (lambda: self.write_info(good_info(available_at=time.time() + 10_000)), window.EXIT_TIMED_OUT),
            "garbage for a file": (lambda: setattr(FakeRanges, "data", b"\0" * 9000), window.EXIT_FAILED),
            "an address that is not the media server's":
                (lambda: self.write_info(good_info(url="https://example.invalid/x")), window.EXIT_FAILED),
            "a manifest, not a file": (lambda: self.write_info(good_info(protocol="m3u8_native")), window.EXIT_FAILED),
            "an info JSON that is no JSON": (lambda: self.write_info(b"{not json"), window.EXIT_FAILED),
            "an info JSON that is no object": (lambda: self.write_info([good_info()]), window.EXIT_FAILED),
            "an info JSON that is not UTF-8": (lambda: self.write_info(b'{"title": "\xff"}'), window.EXIT_FAILED),
            "an empty info JSON": (lambda: self.write_info(b""), window.EXIT_FAILED),
            "an info JSON nested too deep": (lambda: self.write_info(b"[" * 100_000), window.EXIT_FAILED),
            "no info JSON": (lambda: self.info.unlink(), window.EXIT_FAILED),
            "a directory for an info JSON": (lambda: (self.info.unlink(), self.info.mkdir()), window.EXIT_FAILED),
            "an error nobody expected": (lambda: setattr(FakeRanges, "error", RuntimeError(GOOD_URL)),
                                         window.EXIT_FAILED),
        }
        for name, (arrange, status) in cases.items():
            with self.subTest(case=name):
                self.write_info(good_info())
                FakeRanges.data, FakeRanges.error = self.built.data, None
                arrange()
                start = "115" if name == "a start past the end" else "25"
                self.assertEqual(self.run_main(start=start), status)
                self.assertFalse(self.audio.exists())
                self.assertFalse(self.result.exists())
                if self.info.is_dir():
                    self.info.rmdir()

    def test_a_window_that_breaks_off_half_written_leaves_no_file(self) -> None:
        self.write_info(good_info())
        FakeRanges.break_after = 2000
        self.assertEqual(self.run_main(), window.EXIT_TIMED_OUT)
        self.assertEqual(self.left(), [self.info.name])

    def test_fragments_that_are_not_what_the_index_says_leave_no_file(self) -> None:
        # Known only once the fragments are in the file: one of them says it plays a tick late.
        self.write_info(good_info())
        data = bytearray(self.built.data)
        plan = window.plan_window(read(self.built), 25, 30, 100 * MIB)
        struct.pack_into(">I", data, data.index(b"tfdt", plan.offset) + 8, 882_001)
        FakeRanges.data = bytes(data)
        self.assertEqual(self.run_main(), window.EXIT_FAILED)
        self.assertEqual(self.left(), [self.info.name])

    def test_an_info_json_over_the_cap_is_never_parsed(self) -> None:
        room = window.INFO_MAX_BYTES - len(json.dumps(good_info(padding="")))
        self.write_info(json.dumps(good_info(padding="x" * (room + 1))).encode("ascii"))
        self.assertEqual(self.info.stat().st_size, window.INFO_MAX_BYTES + 1)
        with mock.patch.object(window.json, "loads", side_effect=AssertionError("parsed")) as loads:
            self.assertEqual(self.run_main(), window.EXIT_FAILED)
        loads.assert_not_called()
        self.assertEqual(FakeRanges.made, [])
        # One byte less, and it is at the cap and read.
        self.write_info(json.dumps(good_info(padding="x" * room)).encode("ascii"))
        self.assertEqual(self.info.stat().st_size, window.INFO_MAX_BYTES)
        self.assertEqual(self.run_main(), window.EXIT_OK)

    def test_an_info_json_that_is_no_regular_file_is_not_read(self) -> None:
        # A pipe or a device at the name could have no end to read to. The
        # file here is an ordinary one that the system reports as a pipe.
        self.write_info(good_info())
        real = os.fstat

        def as_a_pipe(fd: int) -> os.stat_result:
            fields = list(real(fd))
            fields[0] = stat.S_IFIFO | 0o600
            return os.stat_result(fields)

        with mock.patch.object(window.os, "fstat", as_a_pipe), \
                mock.patch.object(window.os, "read", side_effect=AssertionError("read")) as read_it:
            self.assertEqual(self.run_main(), window.EXIT_FAILED)
        read_it.assert_not_called()
        self.assertEqual(FakeRanges.made, [])
        self.assertEqual(self.run_main(), window.EXIT_OK)

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "needs symlinks")
    def test_an_info_json_behind_a_link_is_not_read(self) -> None:
        real = self.job / "real.json"
        real.write_bytes(json.dumps(good_info()).encode("ascii"))
        os.symlink(real, self.info)
        self.assertEqual(self.run_main(), window.EXIT_FAILED)
        self.assertEqual(FakeRanges.made, [])

    def test_nothing_printed_names_the_address_the_title_or_the_error(self) -> None:
        # An error of the stdlib's that quotes the address, a "file" made of
        # the title, and a start past the end: three refusals, each printed.
        cases: tuple[tuple[Callable[[], Any], str, int], ...] = (
            (lambda: setattr(FakeRanges, "error", RuntimeError(f"cannot open {GOOD_URL}")), "25", window.EXIT_FAILED),
            (lambda: setattr(FakeRanges, "data", b"A title that must never be printed" * 300), "25",
             window.EXIT_FAILED),
            (lambda: None, "115", window.EXIT_PAST_END),
        )
        for arrange, start, status in cases:
            self.write_info(good_info())
            FakeRanges.data, FakeRanges.error = self.built.data, None
            arrange()
            stream = io.StringIO()
            self.assertEqual(self.run_main(start=start, stderr=stream), status)
            printed = stream.getvalue()
            self.assertTrue(printed.startswith("window: "), printed)
            self.assertEqual(printed.count("\n"), 1)
            for secret in ("googlevideo", "videoplayback", "A title", "expire", "RuntimeError"):
                self.assertNotIn(secret, printed)

    def test_what_is_already_at_the_audio_s_name_is_left_alone_and_fails_the_run(self) -> None:
        self.write_info(good_info())
        self.audio.write_bytes(b"not ours")
        self.assertEqual(self.run_main(), window.EXIT_FAILED)
        self.assertEqual(self.audio.read_bytes(), b"not ours")
        self.assertFalse(self.result.exists())

    def test_without_the_result_file_the_audio_goes_too(self) -> None:
        self.write_info(good_info())
        self.result.write_bytes(b"not ours")
        self.assertEqual(self.run_main(), window.EXIT_FAILED)
        self.assertFalse(self.audio.exists())
        self.assertEqual(self.result.read_bytes(), b"not ours")

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "needs symlinks")
    def test_the_audio_is_never_written_through_a_link(self) -> None:
        self.write_info(good_info())
        outside = self.job / "outside"
        os.symlink(outside, self.audio)
        self.assertEqual(self.run_main(), window.EXIT_FAILED)
        self.assertFalse(outside.exists())

    def test_arguments_that_are_not_the_six_the_runner_sends_are_a_usage_error(self) -> None:
        self.write_info(good_info())
        good = [str(self.info), str(self.audio), str(self.result), "25", "30", str(100 * MIB)]
        for argv in (good[:5], good + ["x"], [], good[:3] + ["-1", "30", "1000"], good[:3] + ["2.5", "30", "1000"],
                     good[:3] + ["25", "0", "1000"], good[:3] + ["25", "30", "0"], good[:3] + ["25", "30", ""],
                     good[:3] + [" 25", "30", "1000"], good[:3] + ["25", "3_0", "1000"],
                     good[:3] + ["٢٥", "30", "1000"], good[:3] + ["1" * 11, "30", "1000"]):
            with self.subTest(argv=argv[3:]):
                self.assertEqual(window.main(argv), window.EXIT_USAGE)
        self.assertEqual(FakeRanges.made, [])
        self.assertEqual(self.left(), [self.info.name])


class CreatedTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.path = self.directory / "new"

    def test_the_file_is_written_read_back_and_kept(self) -> None:
        with window.created(str(self.path)) as out:
            out.write(b"abcdef")
            out.seek(2)
            self.assertEqual(out.read(2), b"cd")
            out.seek(2)
            out.write(b"XY")
        self.assertEqual(self.path.read_bytes(), b"abXYef")
        if os.name == "posix":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_block_that_fails_takes_the_file_with_it(self) -> None:
        for error in (WindowError("refused"), OSError("no space left"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    with window.created(str(self.path)) as out:
                        out.write(b"half")
                        out.flush()
                        self.assertTrue(self.path.exists())
                        raise error
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_what_is_already_at_the_name_is_left_alone(self) -> None:
        self.path.write_bytes(b"not ours")
        with self.assertRaises(FileExistsError):
            with window.created(str(self.path)):
                self.fail("opened a file that was already there")
        self.assertEqual(self.path.read_bytes(), b"not ours")

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "needs symlinks")
    def test_a_link_is_never_written_through(self) -> None:
        target = self.directory / "target"
        for there in (True, False):
            with self.subTest(target_exists=there):
                if there:
                    target.write_bytes(b"not ours")
                os.symlink(target, self.path)
                with self.assertRaises(OSError):
                    with window.created(str(self.path)) as out:
                        out.write(b"through the link")
                self.assertTrue(self.path.is_symlink())
                if there:
                    self.assertEqual(target.read_bytes(), b"not ours")
                    target.unlink()
                else:
                    self.assertFalse(target.exists())
                self.path.unlink()


class WholeRunTest(unittest.TestCase):
    """The entry with the real client against the local server, into real files."""

    def run_main(self, job: Path, server: RangeServer, start: str, **options: Any) -> int:
        (job / "v.info.json").write_bytes(json.dumps(good_info()).encode("ascii"))
        with mock.patch.object(window, "Ranges", lambda url, headers: server.ranges(**options)), \
                open(os.devnull, "w") as quiet, mock.patch.object(sys, "stderr", quiet):
            return window.main([str(job / "v.info.json"), str(job / "v.m4a"), str(job / "v.window.json"), start, "30",
                                str(100 * MIB)])

    def test_the_window_goes_from_the_server_into_the_file(self) -> None:
        built = build()
        with tempfile.TemporaryDirectory() as directory, serving(built.data) as server:
            job = Path(directory)
            self.assertEqual(self.run_main(job, server, "25", chunk_bytes=2000), window.EXIT_OK)
            index = read(built)
            plan = window.plan_window(index, 25, 30, 100 * MIB)
            expected = cut_window(built.data, built.data[plan.offset: plan.offset + plan.size], index, plan)
            self.assertEqual((job / "v.m4a").read_bytes(), expected)
            self.assertEqual(json.loads((job / "v.window.json").read_bytes()), {"start": 20.0, "duration": 30.0})
            self.assertEqual(sorted(path.name for path in job.iterdir()), ["v.info.json", "v.m4a", "v.window.json"])

    def test_a_server_that_fails_half_way_leaves_no_file(self) -> None:
        built = build()
        with tempfile.TemporaryDirectory() as directory, serving(built.data) as server:
            job = Path(directory)
            real = _Handler.do_GET

            def failing(handler: _Handler) -> None:
                # The start of the file and the window's first range are served; the next request is refused.
                if len(server.seen) >= 2:
                    server.mode = "status:403"
                real(handler)

            with mock.patch.object(_Handler, "do_GET", failing):
                self.assertEqual(self.run_main(job, server, "25", chunk_bytes=2000), window.EXIT_BLOCKED)
            self.assertGreaterEqual(len(server.seen), 3)
            self.assertEqual(sorted(path.name for path in job.iterdir()), ["v.info.json"])

    def test_the_memory_it_takes_does_not_grow_with_the_window(self) -> None:
        # Eighteen megabytes of fragments through the real client into a real
        # file, in requests of 8 MiB: the program may hold the start of the
        # file and a piece at a time, never a request's worth or the window.
        built = build(Layout(fragments=10, payload_bytes=2 * MIB, last_ticks=None))
        with tempfile.TemporaryDirectory() as directory, serving(built.data) as server:
            reader = server.ranges()
            tracemalloc.start()
            try:
                prefix, index, plan = window.locate(reader, 10, 600, 100 * MIB)
                with window.created(str(Path(directory) / "audio")) as out:
                    window.write_window(out, prefix, index, plan, reader)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertGreater(plan.size, 16 * MIB)
            self.assertEqual((Path(directory) / "audio").stat().st_size, built.init_end + plan.size)
            self.assertLess(peak, 2 * MIB)
        self.assertEqual(window.PREFIX_BYTES, 256 * 1024)


class ProgramTest(unittest.TestCase):
    """The file itself, run the way the runner runs it: by path, isolated, on this interpreter."""

    def run_program(self, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-I", str(PROGRAM), *arguments], cwd=cwd, capture_output=True, timeout=60, check=False,
            env={"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
        )

    def test_it_runs_by_path_and_refuses_a_bad_address_without_a_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            (job / "v.info.json").write_bytes(json.dumps(good_info(url="https://example.invalid/x")).encode("ascii"))
            done = self.run_program(str(job / "v.info.json"), str(job / "v.m4a"), str(job / "v.window.json"), "0",
                                    "600", str(40 * MIB), cwd=job)
            self.assertEqual(done.returncode, window.EXIT_FAILED)
            self.assertEqual(done.stdout, b"")
            self.assertEqual(done.stderr.replace(b"\r\n", b"\n"), b"window: no usable address\n")
            self.assertEqual(sorted(path.name for path in job.iterdir()), ["v.info.json"])

    def test_it_exits_with_the_usage_status_for_other_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            done = self.run_program("only", "three", "arguments", cwd=Path(directory))
            self.assertEqual(done.returncode, window.EXIT_USAGE)
            self.assertEqual((done.stdout, done.stderr), (b"", b""))

    def test_it_imports_nothing_but_the_standard_library(self) -> None:
        with open(PROGRAM, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "a relative import")
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported)
        self.assertEqual(sorted(imported - set(sys.stdlib_module_names)), [])
        # None of the modules that would let it do more than read an address
        # and write two files.
        self.assertEqual(sorted(imported & {"subprocess", "socket", "ctypes", "shutil", "pickle", "importlib"}), [])

    def test_the_exit_statuses_are_distinct_and_small(self) -> None:
        statuses = [window.EXIT_OK, window.EXIT_FAILED, window.EXIT_USAGE, window.EXIT_PAST_END, window.EXIT_BLOCKED,
                    window.EXIT_TIMED_OUT]
        self.assertEqual(statuses, [0, 1, 2, 3, 4, 5])
        # 101 is yt-dlp's status for a filter's refusal, which the runner reads too.
        self.assertNotIn(101, statuses)

    def test_one_request_stays_under_the_forwarder_s_tunnel_budget(self) -> None:
        from strange_ringtone_forwarder import policy

        budget = policy.DEFAULT_LIMITS.download_bytes_per_tunnel
        # A request's answer is the bytes asked for plus TLS and HTTP
        # overhead, and every request opens a tunnel of its own.
        self.assertLessEqual(window.CHUNK_BYTES + MIB, budget)
        self.assertLessEqual(window.PREFIX_MAX_BYTES + MIB, budget)
        self.assertLessEqual(window.PREFIX_BYTES, window.PREFIX_MAX_BYTES)


if __name__ == "__main__":
    unittest.main()

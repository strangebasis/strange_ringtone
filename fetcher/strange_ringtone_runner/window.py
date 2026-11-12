"""Fetch one time window of a YouTube audio file by byte range (docs/PLAN.md 6.2).

The runner starts this program as its second child process, after yt-dlp has
run with --skip-download and left the info JSON of the chosen audio format in
the job directory:

    python -I window.py <info.json> <audio> <result> <start> <max seconds> <max bytes>

It never downloads the file whole. YouTube serves its AAC audio as one
fragmented MP4: ftyp, moov, one sidx that lists every fragment's length and
size, then moof/mdat pairs. The sidx maps time to bytes, so the window is the
fragments from the one that holds <start>, as many as fit in <max seconds> and
<max bytes>. The program fetches the start of the file, reads that index,
fetches exactly those fragments, and writes them behind the file's own ftyp
and moov as a file of its own: every fragment's decode time is moved so the
window starts at zero, and the header states the window's length. <result>
gets one JSON line saying where in the video the window begins and how long
it is.

Everything it reads comes from outside: the info JSON from yt-dlp, the bytes
from the network. So:

- the address is used only when it is https on a subdomain of googlevideo.com,
  on the default port, and a redirect must pass the same check before
  anything is sent to it (the forwarder's allowlist is the second check, not
  the only one); the body of a redirect or of an error is never read;
- every request is a byte range of at most CHUNK_BYTES, and an answer is read
  only when it is a 206 for the range asked for, so a server that ignores the
  range is refused before its body is read;
- every size and count in the file is checked against what was really read
  before it is used, every fragment must be one moof and one mdat at exactly
  the time the index gives it, and any layout other than the one above fails
  closed;
- the audio goes from the network to the file in small pieces: whatever the
  window's size, the program holds the start of the file (at most
  PREFIX_MAX_BYTES) and one fragment's table (at most MOOF_MAX_BYTES);
- nothing it prints names the address, the title or a byte of the file.

What it does not read is the audio itself and each fragment's table of
samples: how long a fragment really plays is the decoder's to find out, and
the page checks the decoded length (docs/PLAN.md 6.2, "What it does not do").

Stdlib only, with no import from the project, so it runs by path on the base
interpreter (python -I), whose site-packages hold nothing. The exit status
and the two files are the whole answer; the runner maps the status to an
error code (strange_ringtone_runner.ytdlp).
"""
from __future__ import annotations

import contextlib
import http.client
import json
import math
import os
import re
import stat
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO

EXIT_OK = 0
# Anything not named below: an address or a layout this program does not
# accept, a server error, a broken answer.
EXIT_FAILED = 1
EXIT_USAGE = 2
# The start lies at or after the end of the audio.
EXIT_PAST_END = 3
# The media server refused the request (HTTP 403).
EXIT_BLOCKED = 4
# A request or the wait for the audio's release took too long.
EXIT_TIMED_OUT = 5

# A real info JSON is a few hundred kB (the runner's INFO_MAX_BYTES).
INFO_MAX_BYTES = 4 * 1024 * 1024
URL_MAX_CHARS = 8192
HEADER_MAX_CHARS = 512
# The first request. YouTube's ftyp and moov are under 1 kB, and the index
# takes 12 bytes a fragment: ten hours of audio are about 43 kB.
PREFIX_BYTES = 256 * 1024
# The most the start of a file may take, index included: about 87,000
# fragments, more than one index can list.
PREFIX_MAX_BYTES = 1024 * 1024
# Each request asks for at most this much. The forwarder lets 16 MiB through
# a tunnel (docs/PLAN.md 6.1), and every request opens a tunnel of its own.
CHUNK_BYTES = 8 * 1024 * 1024
# An answer is read, and handed on to the file, in pieces of this size.
READ_BYTES = 64 * 1024
# As yt-dlp's --socket-timeout in the runner's argument list.
SOCKET_TIMEOUT_SECONDS = 20
# YouTube releases a video's audio a few seconds after the page was fetched
# when an advertisement comes first, and yt-dlp writes that moment into the
# info JSON. A wait longer than the longest FETCH_TIMEOUT_SECONDS could never
# finish, so it is refused at once.
RELEASE_WAIT_MAX_SECONDS = 600.0
MAX_REDIRECTS = 3
# The answers that send a request on to another address.
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
# The only headers of the info JSON that are sent on: the four yt-dlp itself
# sends for YouTube's media. Anything else in http_headers stays unsent.
SENT_HEADERS = ("User-Agent", "Accept", "Accept-Language", "Sec-Fetch-Mode")

BOX_HEADER = 8
LARGE_BOX_HEADER = 16
# A fragment's moof is the table of its samples: under 2 kB for the ten
# seconds of audio YouTube puts in one. The cap bounds what is read back of a
# fragment and how many boxes it can hold, so a "fragment" made of millions
# of tiny boxes is refused from its header, not walked.
MOOF_MAX_BYTES = 64 * 1024
# The netloc of an accepted address, whole: labels of lower-case letters,
# digits and hyphens under googlevideo.com, and no port but the default. No
# user name, no upper case, no percent escape, no other character.
_NETLOC_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+googlevideo\.com(?::443)?", re.ASCII)
_CONTENT_RANGE_RE = re.compile(r"bytes ([0-9]{1,15})-([0-9]{1,15})/([0-9]{1,15})", re.ASCII)
_NUMBER_RE = re.compile(r"[0-9]{1,10}", re.ASCII)
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
# A new file only (O_EXCL), never through a link, open to write and to read
# back what was written.
_CREATE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)

Buffer = bytes | bytearray | memoryview


class WindowError(Exception):
    """The window could not be fetched. code is the exit status.

    The message is a fixed phrase, never a value read from the info JSON or
    the file, so nothing printed can carry the address, the title or audio.
    """

    def __init__(self, message: str, code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.code = code


# --- the address and the request headers ----------------------------------------


def checked_address(url: object) -> str:
    """url when it is an https address on a subdomain of googlevideo.com; WindowError otherwise."""
    if not isinstance(url, str) or not 1 <= len(url) <= URL_MAX_CHARS:
        raise WindowError("no usable address")
    # Printable ASCII with no space: nothing a parser could read two ways.
    if any(not 0x21 <= ord(ch) <= 0x7E for ch in url):
        raise WindowError("no usable address")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise WindowError("no usable address") from None
    # The whole netloc, not only the host name urlsplit takes from it: a user
    # name, a second '@', a backslash or another port never gets as far as a
    # connection, which would parse the netloc again in its own way.
    if parts.scheme != "https" or not _NETLOC_RE.fullmatch(parts.netloc) or parts.fragment:
        raise WindowError("no usable address")
    return url


def checked_headers(value: object) -> dict[str, str]:
    """The headers to send from the info JSON's http_headers: SENT_HEADERS only, each value one plain line."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WindowError("no usable headers")
    headers: dict[str, str] = {}
    for name in SENT_HEADERS:
        if name not in value:
            continue
        text = value[name]
        # No control character: a CR or LF would start a header of its own.
        if not isinstance(text, str) or not 1 <= len(text) <= HEADER_MAX_CHARS or any(
            not 0x20 <= ord(ch) <= 0x7E for ch in text
        ):
            raise WindowError("no usable headers")
        headers[name] = text
    return headers


def release_wait(value: object, now: float) -> float:
    """Seconds until the info JSON's available_at; 0 when it is absent or past; WindowError when unusable."""
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise WindowError("no usable release time")
    try:
        moment = float(value)
    except OverflowError:
        raise WindowError("no usable release time") from None
    if not math.isfinite(moment):
        raise WindowError("no usable release time")
    wait = moment - now
    if wait > RELEASE_WAIT_MAX_SECONDS:
        raise WindowError("the audio is released too late", EXIT_TIMED_OUT)
    return max(0.0, wait)


@dataclass(frozen=True)
class Source:
    url: str
    headers: dict[str, str]
    wait: float


def read_source(info: object, now: float) -> Source:
    """What to request, from yt-dlp's info JSON for one chosen format; WindowError when it is not one plain file."""
    if not isinstance(info, dict):
        raise WindowError("no usable info")
    # The chosen format's fields sit at the top level. Only one plain file
    # over https has an index to read: a manifest (m3u8, dash) has none.
    if info.get("protocol") != "https" or info.get("ext") != "m4a":
        raise WindowError("the chosen format is not one plain file")
    return Source(
        url=checked_address(info.get("url")),
        headers=checked_headers(info.get("http_headers")),
        wait=release_wait(info.get("available_at"), now),
    )


# --- byte ranges --------------------------------------------------------------------


def _request_error(error: BaseException) -> WindowError:
    """What a failed request becomes. Nothing of the error's own text is kept: it can quote the address."""
    if isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
        return WindowError("the request timed out", EXIT_TIMED_OUT)
    return WindowError("the request failed")


class Ranges:
    """Byte ranges of one address, each request on a connection of its own.

    proxies None means the environment's (HTTPS_PROXY, as for yt-dlp); the
    tests pass a mapping. allowed checks the address and every redirect.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str],
        *,
        proxies: dict[str, str] | None = None,
        allowed: Callable[[object], str] = checked_address,
        timeout: float = SOCKET_TIMEOUT_SECONDS,
        chunk_bytes: int = CHUNK_BYTES,
    ) -> None:
        self._url = allowed(url)
        self._headers = dict(headers)
        self._allowed = allowed
        self._timeout = timeout
        self._chunk_bytes = chunk_bytes
        # Built by hand. The stdlib's default opener would also speak ftp,
        # file and data, which nothing here ever needs, and it would follow
        # a redirect by itself after reading that answer's body whole. With
        # no redirect handler a redirect arrives in _open as an HTTPError,
        # unread.
        opener = urllib.request.OpenerDirector()
        for handler in (
            urllib.request.ProxyHandler(proxies),
            urllib.request.UnknownHandler(),
            urllib.request.HTTPHandler(),
            urllib.request.HTTPSHandler(),
            urllib.request.HTTPDefaultErrorHandler(),
            urllib.request.HTTPErrorProcessor(),
        ):
            opener.add_handler(handler)
        self._opener = opener
        # The file's size, as the first answer's Content-Range states it.
        self.total: int | None = None
        self.requests = 0

    def read(self, offset: int, size: int) -> bytearray:
        """The bytes from offset, size of them, or fewer only where the file ends first."""
        data = bytearray()
        self.copy(offset, size, data.extend)
        return data

    def copy(self, offset: int, size: int, sink: Callable[[bytes], object]) -> int:
        """Hand sink the bytes from offset, piece by piece: size of them, or fewer only where the file ends first.

        Returns how many. Nothing is kept here, so the memory this takes
        does not grow with size.
        """
        if offset < 0 or size < 1:
            raise ValueError("offset and size must be positive")
        at, end = offset, offset + size
        # A server may answer less than was asked; one that answers in crumbs
        # is not waited for.
        left = size // self._chunk_bytes + 3
        while at < end and (self.total is None or at < self.total):
            if left == 0:
                raise WindowError("the server answers in pieces too small")
            left -= 1
            at += self._one(at, min(end, at + self._chunk_bytes) - 1, sink)
        return at - offset

    def _open(self, first: int, last: int) -> Any:
        """The answer to one range request, a redirect followed; the caller closes it."""
        url = self._url
        for _hop in range(MAX_REDIRECTS + 1):
            self.requests += 1
            try:
                request = urllib.request.Request(url, headers={**self._headers, "Range": f"bytes={first}-{last}"})
                return self._opener.open(request, timeout=self._timeout)
            except urllib.error.HTTPError as error:
                # Closed unread. Nothing in the body of a redirect or of an
                # error is needed, and a server could make it endless.
                error.close()
                status, locations = error.code, error.headers.get_all("Location") or []
            except (OSError, http.client.HTTPException, ValueError) as error:
                # URLError is an OSError.
                raise _request_error(error) from None
            if status == 403:
                raise WindowError("the server refused the request", EXIT_BLOCKED)
            if status not in REDIRECT_STATUSES:
                raise WindowError("the server answered with an error")
            if len(locations) != 1:
                raise WindowError("a redirect without one address")
            try:
                target = urllib.parse.urljoin(url, locations[0])
            except ValueError:
                raise WindowError("no usable address") from None
            # The same check as the first address, before anything is sent there.
            url = self._allowed(target)
        raise WindowError("too many redirects")

    def _answered(self, response: Any, first: int, last: int) -> int:
        """How many bytes the answer's body holds, once the answer is the range asked for; WindowError otherwise."""
        # 206 only. A 200 is the whole file: refused here, unread.
        if response.status != 206:
            raise WindowError("the server ignored the range")
        if response.headers.get_all("Content-Encoding") not in (None, ["identity"]):
            raise WindowError("the answer is encoded")
        ranges = response.headers.get_all("Content-Range") or []
        match = _CONTENT_RANGE_RE.fullmatch(ranges[0]) if len(ranges) == 1 else None
        if match is None:
            raise WindowError("no usable Content-Range")
        start, end, total = (int(text) for text in match.groups())
        if start != first or not first <= end <= last or end >= total:
            raise WindowError("the server answered another range")
        if self.total is None:
            self.total = total
        elif total != self.total:
            raise WindowError("the file changed size between requests")
        return end - start + 1

    def _one(self, first: int, last: int, sink: Callable[[bytes], object]) -> int:
        response = self._open(first, last)
        try:
            expected = self._answered(response, first, last)
            left = expected
            while left:
                try:
                    piece = response.read(min(READ_BYTES, left))
                except (OSError, http.client.HTTPException, ValueError) as error:
                    raise _request_error(error) from None
                if not piece:
                    raise WindowError("the answer ended early")
                left -= len(piece)
                sink(piece)
            return expected
        finally:
            response.close()


# --- the fragmented MP4 -------------------------------------------------------------


class Mp4Error(WindowError):
    """The bytes are not the fragmented MP4 layout this program accepts."""


class PastEnd(WindowError):
    """The start lies at or after the end of the audio."""

    def __init__(self) -> None:
        super().__init__("the start lies at or after the end", EXIT_PAST_END)


@dataclass(frozen=True)
class Box:
    kind: bytes
    start: int  # offset of the box's header
    header: int  # length of the header: 8 or 16
    end: int  # offset one past the box, as its header declares it

    @property
    def body(self) -> int:
        return self.start + self.header


def _box_at(data: Buffer, at: int, end: int) -> Box:
    """The box whose header starts at at, read from data[at:end]; its end may lie past end."""
    if end - at < BOX_HEADER:
        raise Mp4Error("a truncated box header")
    size, kind = struct.unpack_from(">I4s", data, at)
    header = BOX_HEADER
    if size == 1:
        if end - at < LARGE_BOX_HEADER:
            raise Mp4Error("a truncated box header")
        (size,) = struct.unpack_from(">Q", data, at + BOX_HEADER)
        header = LARGE_BOX_HEADER
    # Size 0 means "to the end of the file", which no box of this layout is.
    if size < header:
        raise Mp4Error("a box smaller than its header")
    return Box(bytes(kind), at, header, at + size)


def boxes(data: Buffer, start: int, end: int) -> Iterator[Box]:
    """The boxes laid end to end in data[start:end]; Mp4Error when one does not fit exactly."""
    at = start
    while at < end:
        box = _box_at(data, at, end)
        if box.end > end:
            raise Mp4Error("a box that does not fit")
        yield box
        at = box.end


def child(data: Buffer, parent: Box, kind: bytes) -> Box:
    """The one box of this kind directly inside parent; Mp4Error when there is none or more than one."""
    found = [box for box in boxes(data, parent.body, parent.end) if box.kind == kind]
    if len(found) != 1:
        raise Mp4Error("a box is missing or there twice")
    return found[0]


def index_end(prefix: Buffer) -> int:
    """Where the index ends, from the first bytes of a file that begins ftyp, moov, sidx; Mp4Error otherwise.

    Only the three headers must lie in prefix: the index itself may run past
    it, and the caller then reads the rest.
    """
    at = 0
    for kind in (b"ftyp", b"moov", b"sidx"):
        box = _box_at(prefix, at, len(prefix))
        if box.kind != kind:
            raise Mp4Error("the file does not begin ftyp, moov, sidx")
        at = box.end
    return at


@dataclass(frozen=True)
class Segment:
    begin: int  # in the index's timescale
    duration: int
    offset: int  # absolute byte offset in the file
    size: int


@dataclass(frozen=True)
class Index:
    timescale: int  # of the index
    media_timescale: int  # of the track (mdhd), which tfdt counts in
    movie_timescale: int  # of the file (mvhd), which tkhd and mehd count in
    segments: tuple[Segment, ...]
    init_end: int  # the window's file keeps the first init_end bytes: ftyp and moov, without the index


def _version(data: Buffer, box: Box, least: int) -> int:
    if box.end - box.body < least:
        raise Mp4Error("a box too short for its fields")
    return data[box.body]


def _timescale(data: Buffer, box: Box) -> int:
    """The timescale of an mvhd or mdhd box."""
    version = _version(data, box, 4 + 16)
    if version == 0:
        (value,) = struct.unpack_from(">I", data, box.body + 12)
    elif version == 1:
        if box.end - box.body < 4 + 28:
            raise Mp4Error("a box too short for its fields")
        (value,) = struct.unpack_from(">I", data, box.body + 20)
    else:
        raise Mp4Error("a header box of an unknown version")
    if value == 0:
        raise Mp4Error("a timescale of zero")
    return value


def read_index(prefix: Buffer) -> Index:
    """The index of the file that starts with prefix, which must hold ftyp, moov and sidx whole."""
    top = []
    at = 0
    for kind in (b"ftyp", b"moov", b"sidx"):
        box = _box_at(prefix, at, len(prefix))
        if box.kind != kind or box.end > len(prefix):
            raise Mp4Error("the file does not begin ftyp, moov, sidx")
        top.append(box)
        at = box.end
    _ftyp, moov, sidx = top
    # One track only: a second one would have fragments of its own among
    # the ones the index lists.
    traks = [box for box in boxes(prefix, moov.body, moov.end) if box.kind == b"trak"]
    if len(traks) != 1:
        raise Mp4Error("not exactly one track")
    # No edit list. One would shift or cut the track's timeline, and each
    # fragment's time is compared with the index as it stands (_rebase).
    if any(box.kind == b"edts" for box in boxes(prefix, traks[0].body, traks[0].end)):
        raise Mp4Error("a track with an edit list")
    movie_timescale = _timescale(prefix, child(prefix, moov, b"mvhd"))
    media_timescale = _timescale(prefix, child(prefix, child(prefix, traks[0], b"mdia"), b"mdhd"))

    version = _version(prefix, sidx, 12)
    _reference_id, timescale = struct.unpack_from(">II", prefix, sidx.body + 4)
    at = sidx.body + 12
    if version == 0:
        if sidx.end - at < 8 + 4:
            raise Mp4Error("the index is too short")
        earliest, first_offset = struct.unpack_from(">II", prefix, at)
        at += 8
    elif version == 1:
        if sidx.end - at < 16 + 4:
            raise Mp4Error("the index is too short")
        earliest, first_offset = struct.unpack_from(">QQ", prefix, at)
        at += 16
    else:
        raise Mp4Error("an index of an unknown version")
    _reserved, count = struct.unpack_from(">HH", prefix, at)
    at += 4
    # The count is 16 bits, so one index lists at most 65,535 fragments, and
    # its length must be exactly what that count needs.
    if timescale == 0 or count < 1 or sidx.end - at != 12 * count:
        raise Mp4Error("the index's length does not match its count")
    segments: list[Segment] = []
    begin, offset = earliest, sidx.end + first_offset
    for number in range(count):
        reference, duration, _sap = struct.unpack_from(">III", prefix, at + 12 * number)
        # The top bit set means the entry points to another index.
        if reference & 0x80000000:
            raise Mp4Error("an index of indexes")
        size = reference & 0x7FFFFFFF
        # A fragment is a moof and an mdat: two headers at the least.
        if size < 2 * BOX_HEADER or duration == 0:
            raise Mp4Error("an empty index entry")
        segments.append(Segment(begin, duration, offset, size))
        begin += duration
        offset += size
    return Index(timescale, media_timescale, movie_timescale, tuple(segments), sidx.start)


@dataclass(frozen=True)
class Plan:
    first: int  # segment numbers, both included
    last: int
    offset: int  # the one byte range to fetch
    size: int
    begin: int  # in the index's timescale
    duration: int


def plan_window(index: Index, start_seconds: int, max_seconds: int, max_bytes: int) -> Plan:
    """The fragments from the one that holds start_seconds, as many as fit in max_seconds and max_bytes.

    The window begins where that fragment begins, so up to one fragment
    before the start asked for. It is never longer than max_seconds and never
    larger than max_bytes: it ends early rather than pass either. PastEnd
    when the start lies at or after the end; Mp4Error when not even the
    first fragment fits.
    """
    if start_seconds < 0 or max_seconds < 1 or max_bytes < 1:
        raise ValueError("start, length and size must be positive")
    segments = index.segments
    start = start_seconds * index.timescale
    limit = max_seconds * index.timescale
    if start >= segments[-1].begin + segments[-1].duration:
        raise PastEnd()
    # The last fragment that begins at or before the start (the first one
    # when the start lies before all of them), by bisection.
    low, high = 0, len(segments) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if segments[middle].begin <= start:
            low = middle
        else:
            high = middle - 1
    first = last = low
    duration, size = segments[first].duration, segments[first].size
    if duration > limit or size > max_bytes:
        raise Mp4Error("not even one fragment fits")
    while last + 1 < len(segments):
        following = segments[last + 1]
        if duration + following.duration > limit or size + following.size > max_bytes:
            break
        last += 1
        duration += following.duration
        size += following.size
    return Plan(first, last, segments[first].offset, size, segments[first].begin, duration)


def _set_duration(data: bytearray, box: Box, at_v0: int, at_v1: int, value: int) -> None:
    version = _version(data, box, 4)
    if version == 0:
        if box.end - box.body < at_v0 + 4 or value > 0xFFFFFFFF:
            raise Mp4Error("a header box that cannot hold the length")
        struct.pack_into(">I", data, box.body + at_v0, value)
    elif version == 1:
        if box.end - box.body < at_v1 + 8:
            raise Mp4Error("a box too short for its fields")
        struct.pack_into(">Q", data, box.body + at_v1, value)
    else:
        raise Mp4Error("a header box of an unknown version")


def _set_durations(init: bytearray, index: Index, plan: Plan) -> None:
    """Write the window's length where the init part states the whole file's."""
    ftyp = _box_at(init, 0, len(init))
    moov = _box_at(init, ftyp.end, len(init))
    if moov.kind != b"moov" or moov.end != len(init):
        raise Mp4Error("the file does not begin ftyp, moov, sidx")
    # Rounded up, so the header never states less than the fragments hold.
    movie = -(-plan.duration * index.movie_timescale // index.timescale)
    media = -(-plan.duration * index.media_timescale // index.timescale)
    _set_duration(init, child(init, moov, b"mvhd"), 16, 24, movie)
    trak = child(init, moov, b"trak")
    _set_duration(init, child(init, trak, b"tkhd"), 20, 28, movie)
    _set_duration(init, child(init, child(init, trak, b"mdia"), b"mdhd"), 16, 24, media)
    for mvex in boxes(init, moov.body, moov.end):
        if mvex.kind != b"mvex":
            continue
        for mehd in boxes(init, mvex.body, mvex.end):
            if mehd.kind == b"mehd":
                _set_duration(init, mehd, 4, 4, movie)


def _rebase(out: BinaryIO, base: int, index: Index, plan: Plan) -> None:
    """Check every fragment written from base on against the index, and move its time so the window starts at zero.

    Of each fragment only the moof and the mdat's header are read back; the
    audio stays in the file, unread.
    """
    at = base
    delta: int | None = None
    for number in range(plan.first, plan.last + 1):
        segment = index.segments[number]
        # Where the index says the fragment begins, in the track's
        # timescale, which the fragment's own decode time counts in.
        begins, rest = divmod(segment.begin * index.media_timescale, index.timescale)
        if rest:
            raise Mp4Error("a fragment's time is no whole number in the track's timescale")
        if delta is None:
            delta = begins
        out.seek(at)
        # The largest moof and the header of the mdat behind it.
        head = out.read(min(segment.size, MOOF_MAX_BYTES + LARGE_BOX_HEADER))
        # Exactly what the index says a fragment is: one moof and one mdat
        # that fill its bytes, nothing before, between or after. Read as two
        # headers, never as a list of whatever boxes the bytes may hold.
        moof = _box_at(head, 0, len(head))
        if moof.kind != b"moof" or moof.end > MOOF_MAX_BYTES:
            raise Mp4Error("a fragment that is not one moof and one mdat")
        # No header fits behind a moof that reaches the end of what was
        # read, so from here on the moof lies whole in head.
        mdat = _box_at(head, moof.end, len(head))
        if mdat.kind != b"mdat" or mdat.end != segment.size:
            raise Mp4Error("a fragment that is not one moof and one mdat")
        trafs = [box for box in boxes(head, moof.body, moof.end) if box.kind == b"traf"]
        if len(trafs) != 1:
            raise Mp4Error("a fragment with not exactly one track")
        tfdt = child(head, trafs[0], b"tfdt")
        version = _version(head, tfdt, 4)
        layout = ">I" if version == 0 else ">Q"
        if version not in (0, 1) or tfdt.end - tfdt.body < 4 + struct.calcsize(layout):
            raise Mp4Error("a decode time box of an unknown version")
        (decode_time,) = struct.unpack_from(layout, head, tfdt.body + 4)
        # The index chose these bytes for this time, and the length the
        # header is given comes from the index too. A fragment that says it
        # plays at any other time would make both untrue: a gap, an overlap,
        # or audio from elsewhere in the video.
        if decode_time != begins:
            raise Mp4Error("a fragment at another time than the index says")
        out.seek(at + tfdt.body + 4)
        out.write(struct.pack(layout, begins - delta))
        at += segment.size


def locate(reader: Any, start: int, max_seconds: int, max_bytes: int) -> tuple[bytearray, Index, Plan]:
    """(the start of the file, its index, the window's plan): all that is known before a fragment is fetched.

    reader.read(offset, size) returns the bytes there (fewer only at the
    file's end) and reader.total is then the file's size. The file to write,
    init part and planned fragments together, fits in max_bytes.
    """
    prefix = reader.read(0, PREFIX_BYTES)
    need = index_end(prefix)
    if need > PREFIX_MAX_BYTES:
        raise Mp4Error("the index is larger than this program reads")
    if need > len(prefix):
        prefix += reader.read(len(prefix), need - len(prefix))
    # A file that ends inside its index is refused there: the index must lie
    # whole in what was read.
    index = read_index(memoryview(prefix)[:need])
    total = reader.total
    last = index.segments[-1]
    # The index must describe bytes the file has.
    if total is None or last.offset + last.size > total:
        raise Mp4Error("the index describes more than the file holds")
    # The file written is the init part and the fragments together.
    room = max_bytes - index.init_end
    if room < 1:
        raise Mp4Error("not even one fragment fits")
    return prefix, index, plan_window(index, start, max_seconds, room)


def write_window(out: BinaryIO, prefix: Buffer, index: Index, plan: Plan, reader: Any) -> None:
    """Write the window to out as a file of its own: ftyp and moov with the window's length, then its fragments.

    out is a new, empty file, open to write and to read back.
    reader.copy(offset, size, sink) hands sink the bytes there, piece by
    piece. The index is left out: its offsets describe the whole file.
    """
    init = bytearray(prefix[: index.init_end])
    _set_durations(init, index, plan)
    out.write(init)
    written = 0

    def sink(piece: bytes) -> None:
        nonlocal written
        written += len(piece)
        # Counted here, whatever the reader is: the file never grows past
        # the planned size, which the byte limit was checked against.
        if written > plan.size:
            raise Mp4Error("the fragments are not the planned range")
        out.write(piece)

    reader.copy(plan.offset, plan.size, sink)
    if written != plan.size:
        raise Mp4Error("the fragments are not the planned range")
    _rebase(out, len(init), index, plan)
    out.flush()


# --- files and the entry --------------------------------------------------------------


def load_info(path: str) -> object:
    """The parsed info JSON at path: a regular file of at most INFO_MAX_BYTES, never through a link."""
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError:
        raise WindowError("no usable info") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WindowError("no usable info")
        data = bytearray()
        # Read up to the cap and one piece past it, whatever size the file
        # claims to have: what was read is what is counted.
        while len(data) <= INFO_MAX_BYTES:
            piece = os.read(fd, 65536)
            if not piece:
                break
            data += piece
    except OSError:
        raise WindowError("no usable info") from None
    finally:
        os.close(fd)
    if len(data) > INFO_MAX_BYTES:
        raise WindowError("no usable info")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise WindowError("no usable info") from None


def _create(path: str, _flags: int) -> int:
    # This program's own flags, whatever the mode string below would ask for.
    return os.open(path, _CREATE_FLAGS, 0o600)


@contextlib.contextmanager
def created(path: str) -> Iterator[BinaryIO]:
    """A new file at path, owner-only, open to write and to read back; removed again when the block fails.

    Whatever is already at the name (a link included) fails the call and is
    left alone: this program did not make it.
    """
    out = open(path, "x+b", opener=_create)
    try:
        # Closed, and with that written out, before the block counts as done
        # and before the unlink below: Windows can't delete an open file.
        with out:
            yield out
    except BaseException:
        _remove(path)
        raise


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _number(text: str, least: int, most: int) -> int:
    if not _NUMBER_RE.fullmatch(text):
        raise ValueError("not a number")
    value = int(text)
    if not least <= value <= most:
        raise ValueError("out of range")
    return value


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        return EXIT_USAGE
    info_path, audio_path, result_path = argv[:3]
    try:
        start = _number(argv[3], 0, 10**9)
        max_seconds = _number(argv[4], 1, 10**6)
        max_bytes = _number(argv[5], 1, 10**10 - 1)
    except ValueError:
        return EXIT_USAGE
    try:
        source = read_source(load_info(info_path), time.time())
        if source.wait > 0:
            time.sleep(source.wait)
        reader = Ranges(source.url, source.headers)
        # The index is read and the window planned before a file exists: a
        # start past the end, or a file this program does not read, is
        # refused with nothing written and no fragment asked for.
        prefix, index, plan = locate(reader, start, max_seconds, max_bytes)
        with created(audio_path) as out:
            write_window(out, prefix, index, plan, reader)
        try:
            result = json.dumps({
                "start": round(plan.begin / index.timescale, 3),
                "duration": round(plan.duration / index.timescale, 3),
            }) + "\n"
            with created(result_path) as out:
                out.write(result.encode("ascii"))
        except BaseException:
            # The two files are one answer: without the second, the first goes.
            _remove(audio_path)
            raise
    except WindowError as error:
        # A fixed phrase (see WindowError); the runner reads only the status.
        sys.stderr.write(f"window: {error}\n")
        return error.code
    except Exception:
        # Anything else fails closed, with nothing of its text: an exception
        # of the stdlib's can quote the address or a path.
        sys.stderr.write("window: failed\n")
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

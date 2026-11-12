"""A fragmented MP4 in the layout YouTube serves its AAC audio in, built for the window tests.

No real audio: each mdat holds bytes derived from the fragment's number, so a
test can tell which fragments a cut kept. The boxes the window program reads
(ftyp, moov with mvhd, trak/tkhd, trak/mdia/mdhd and mvex, sidx, and each
fragment's moof/traf/tfdt and mdat) have their real field layouts; the rest
(hdlr, minf, trex, udta, mfhd, tfhd, trun) are stand-ins of plausible size.
The defaults mirror what the spike measured on real files (docs/PLAN.md 6.2):
ftyp of 24 bytes, version 0 boxes, an index in the track's timescale of
44100, fragments of about 10 s.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def full_box(kind: bytes, version: int, payload: bytes, flags: int = 0) -> bytes:
    return box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def large_box(kind: bytes, payload: bytes) -> bytes:
    """The same box with a 64-bit size field."""
    return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload


def mvhd(version: int, timescale: int, duration: int) -> bytes:
    rest = bytes(4 + 2 + 10 + 36 + 24 + 4)
    if version == 0:
        return full_box(b"mvhd", 0, struct.pack(">IIII", 0, 0, timescale, duration) + rest)
    return full_box(b"mvhd", version, struct.pack(">QQIQ", 0, 0, timescale, duration) + rest)


def tkhd(version: int, duration: int) -> bytes:
    rest = bytes(8 + 2 + 2 + 2 + 2 + 36 + 4 + 4)
    if version == 0:
        return full_box(b"tkhd", 0, struct.pack(">IIIII", 0, 0, 1, 0, duration) + rest, flags=7)
    return full_box(b"tkhd", version, struct.pack(">QQIIQ", 0, 0, 1, 0, duration) + rest, flags=7)


def mdhd(version: int, timescale: int, duration: int) -> bytes:
    rest = bytes(2 + 2)
    if version == 0:
        return full_box(b"mdhd", 0, struct.pack(">IIII", 0, 0, timescale, duration) + rest)
    return full_box(b"mdhd", version, struct.pack(">QQIQ", 0, 0, timescale, duration) + rest)


def mehd(version: int, duration: int) -> bytes:
    return full_box(b"mehd", version, struct.pack(">I" if version == 0 else ">Q", duration))


def tfdt(version: int, time: int) -> bytes:
    return full_box(b"tfdt", version, struct.pack(">I" if version == 0 else ">Q", time))


def fragment(number: int, decode_time: int, payload_bytes: int, tfdt_version: int = 0, trafs: int = 1,
             with_tfdt: bool = True, traf_extra: bytes = b"") -> bytes:
    """One moof and one mdat. The mdat's bytes repeat the fragment's number."""
    traf = full_box(b"tfhd", 0, struct.pack(">I", 1))
    if with_tfdt:
        traf += tfdt(tfdt_version, decode_time)
    traf += full_box(b"trun", 0, bytes(12)) + traf_extra
    moof = box(b"moof", full_box(b"mfhd", 0, struct.pack(">I", number + 1)) + box(b"traf", traf) * trafs)
    return moof + box(b"mdat", bytes([number % 251]) * payload_bytes)


@dataclass
class Layout:
    """What to build. Lengths are in ticks of the track's timescale."""

    fragments: int = 12
    fragment_ticks: int = 441_000  # 10 s at 44100
    # The last fragment's length, when it differs (a file rarely ends on a boundary).
    last_ticks: int | None = 200_000
    payload_bytes: int = 1500
    media_timescale: int = 44100
    movie_timescale: int = 1000
    index_timescale: int = 44100
    header_version: int = 0  # mvhd, tkhd and mdhd
    index_version: int = 0
    tfdt_version: int = 0
    with_mehd: bool = False
    tracks: int = 1
    earliest: int = 0
    first_offset: int = 0
    # Boxes in place of the ones built here, and boxes added behind the usual ones.
    mvhd_box: bytes | None = None
    tkhd_box: bytes | None = None
    mdhd_box: bytes | None = None
    mehd_box: bytes | None = None
    trak_extra: bytes = b""
    traf_extra: bytes = b""


@dataclass
class Built:
    data: bytes
    init_end: int  # where ftyp and moov end and the index begins
    index_end: int
    # (begin, duration, offset, size) of each fragment, begin and duration in the index's timescale.
    segments: list[tuple[int, int, int, int]] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        begin, duration, _offset, _size = self.segments[-1]
        return (begin + duration) / self.timescale

    timescale: int = 44100


def build(layout: Layout | None = None) -> Built:
    layout = layout or Layout()
    ticks = [layout.fragment_ticks] * layout.fragments
    if layout.last_ticks is not None:
        ticks[-1] = layout.last_ticks
    total_ticks = sum(ticks)
    movie_duration = total_ticks * layout.movie_timescale // layout.media_timescale

    ftyp = box(b"ftyp", b"dash" + bytes(4) + b"iso6" + b"mp41")

    def chosen(given: bytes | None, built: bytes) -> bytes:
        return built if given is None else given

    mdia = box(b"mdia", chosen(layout.mdhd_box, mdhd(layout.header_version, layout.media_timescale, total_ticks))
               + full_box(b"hdlr", 0, bytes(20) + b"SoundHandler\0") + box(b"minf", bytes(180)))
    trak = box(b"trak", chosen(layout.tkhd_box, tkhd(layout.header_version, movie_duration)) + mdia
               + layout.trak_extra)
    mvex_inside = full_box(b"trex", 0, bytes(20))
    if layout.with_mehd or layout.mehd_box is not None:
        mvex_inside = chosen(layout.mehd_box, mehd(layout.header_version, movie_duration)) + mvex_inside
    moov = box(b"moov", chosen(layout.mvhd_box, mvhd(layout.header_version, layout.movie_timescale, movie_duration))
               + box(b"mvex", mvex_inside) + trak * layout.tracks + box(b"udta", bytes(40)))

    pieces: list[bytes] = []
    decode_time = layout.earliest * layout.media_timescale // layout.index_timescale
    for number, length in enumerate(ticks):
        pieces.append(fragment(number, decode_time, layout.payload_bytes + 7 * (number % 5), layout.tfdt_version,
                               traf_extra=layout.traf_extra))
        decode_time += length

    entries = b""
    for piece, length in zip(pieces, ticks, strict=True):
        duration = length * layout.index_timescale // layout.media_timescale
        entries += struct.pack(">III", len(piece), duration, 0x90000000)
    if layout.index_version == 0:
        head = struct.pack(">IIII", 1, layout.index_timescale, layout.earliest, layout.first_offset)
    else:
        head = struct.pack(">IIQQ", 1, layout.index_timescale, layout.earliest, layout.first_offset)
    sidx = full_box(b"sidx", layout.index_version, head + struct.pack(">HH", 0, len(pieces)) + entries)

    init_end = len(ftyp) + len(moov)
    index_end = init_end + len(sidx)
    data = ftyp + moov + sidx + bytes(layout.first_offset) + b"".join(pieces)
    segments: list[tuple[int, int, int, int]] = []
    begin, offset = layout.earliest, index_end + layout.first_offset
    for piece, length in zip(pieces, ticks, strict=True):
        duration = length * layout.index_timescale // layout.media_timescale
        segments.append((begin, duration, offset, len(piece)))
        begin += duration
        offset += len(piece)
    return Built(data=data, init_end=init_end, index_end=index_end, segments=segments,
                 timescale=layout.index_timescale)


def find(data: bytes, kind: bytes, start: int = 0) -> int:
    """The offset of the first box header of this kind at or after start (its size field's offset)."""
    at = data.index(kind, start)
    return at - 4

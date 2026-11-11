"""The image's ffmpeg is built hardened (sections 5.3 and 8 of docs/PLAN.md).

ffmpeg parses bytes a client sent, so a memory-safety bug in it is the core's
likeliest way in. core/ffmpeg/configure.txt asks for FFmpeg's hardened
toolchain, which makes such a bug harder to turn into code execution:

- a position-independent executable (ELF type ET_DYN with an interpreter),
  so the binary's own code lands at a random address;
- full RELRO: a PT_GNU_RELRO segment, and BIND_NOW so every symbol is bound
  at start-up and the whole relocation table can then be made read-only;
- a stack protector, which calls __stack_chk_fail when a canary is smashed;
- fortified libc calls (__memcpy_chk and the like), which check the length
  of a copy against the size of its buffer where the compiler knows it.

These are properties of the linked file, so the file is read directly, with
the standard library: the runtime image has no binutils. A build that loses
one of them fails here, whatever the configure line says.
"""
from __future__ import annotations

import re
import struct
import unittest
from dataclasses import dataclass, field
from pathlib import Path

FFMPEG = Path("/usr/local/bin/ffmpeg")
# A control: Debian builds util-linux with its full hardening flags, so the
# image's prlimit has every property checked below.
HARDENED_CONTROL = Path("/usr/bin/prlimit")

# ELF constants (the System V ABI and glibc's elf.h).
ET_EXEC = 2
ET_DYN = 3
PT_LOAD = 1
PT_DYNAMIC = 2
PT_INTERP = 3
PT_GNU_RELRO = 0x6474E552
DT_NULL = 0
DT_BIND_NOW = 24
DT_FLAGS = 30
DT_FLAGS_1 = 0x6FFFFFFB
DF_BIND_NOW = 0x8
DF_1_NOW = 0x1
SHT_DYNSYM = 11
SHN_UNDEF = 0

# The fortified variants glibc provides end in _chk; the stack protector's
# own two symbols do too, and don't count as fortification.
_FORTIFIED = re.compile(r"__[a-z0-9_]+_chk")
_STACK_PROTECTOR = {"__stack_chk_fail", "__stack_chk_guard"}


@dataclass
class ElfFile:
    """What the checks below need of a 64-bit little-endian ELF file."""

    elf_type: int
    segments: list[int]
    dynamic: list[tuple[int, int]]
    imported: set[str] = field(default_factory=set)
    defined: set[str] = field(default_factory=set)


def _cstring(table: bytes, offset: int) -> str:
    end = table.index(b"\0", offset)
    return table[offset:end].decode("ascii")


def _bind_now(elf: ElfFile) -> bool:
    tags = dict(elf.dynamic)
    return bool(
        DT_BIND_NOW in tags
        or tags.get(DT_FLAGS, 0) & DF_BIND_NOW
        or tags.get(DT_FLAGS_1, 0) & DF_1_NOW
    )


def _fortified(elf: ElfFile) -> list[str]:
    return sorted(name for name in elf.imported if _FORTIFIED.fullmatch(name) and name not in _STACK_PROTECTOR)


def read_elf(path: Path) -> ElfFile:
    """Read the ELF header, the program headers, the dynamic section and the dynamic symbols."""
    data = path.read_bytes()
    if data[:4] != b"\x7fELF":
        raise AssertionError(f"{path} is not an ELF file")
    if data[4] != 2 or data[5] != 1:
        raise AssertionError(f"{path} is not 64-bit little-endian (class {data[4]}, data {data[5]})")
    (elf_type,) = struct.unpack_from("<H", data, 16)
    phoff, shoff = struct.unpack_from("<QQ", data, 32)
    phentsize, phnum, shentsize, shnum = struct.unpack_from("<HHHH", data, 54)

    segments: list[int] = []
    dynamic: list[tuple[int, int]] = []
    for index in range(phnum):
        p_type, _flags, p_offset, _vaddr, _paddr, p_filesz = struct.unpack_from(
            "<IIQQQQ", data, phoff + index * phentsize
        )
        segments.append(p_type)
        if p_type == PT_DYNAMIC:
            for position in range(p_offset, p_offset + p_filesz, 16):
                tag, value = struct.unpack_from("<qQ", data, position)
                if tag == DT_NULL:
                    break
                dynamic.append((tag, value))

    elf = ElfFile(elf_type, segments, dynamic)
    # The dynamic symbol table and its string table, found through the
    # section headers, which strip --strip-all keeps. A file without them
    # fails the symbol checks below rather than passing them empty.
    sections = [
        struct.unpack_from("<IIQQQQIIQQ", data, shoff + index * shentsize) for index in range(shnum)
    ]
    for _name, sh_type, _flags, _addr, sh_offset, sh_size, sh_link, _info, _align, sh_entsize in sections:
        if sh_type != SHT_DYNSYM:
            continue
        strings = sections[sh_link]
        table = data[strings[4]:strings[4] + strings[5]]
        for position in range(sh_offset, sh_offset + sh_size, sh_entsize or 24):
            st_name, _st_info, _st_other, st_shndx = struct.unpack_from("<IBBH", data, position)
            if st_name == 0:
                continue
            name = _cstring(table, st_name)
            (elf.imported if st_shndx == SHN_UNDEF else elf.defined).add(name)
    return elf


class FfmpegHardeningTest(unittest.TestCase):
    elf: ElfFile

    @classmethod
    def setUpClass(cls) -> None:
        cls.elf = read_elf(FFMPEG)

    def test_the_reader_sees_the_binary(self) -> None:
        # Control: the checks below mean something only if the reader found
        # the segments, the dynamic section and the libc symbols ffmpeg
        # surely imports, so a missing symbol below is missing from ffmpeg,
        # not from what the reader saw.
        self.assertIn(self.elf.elf_type, (ET_EXEC, ET_DYN))
        self.assertIn(PT_LOAD, self.elf.segments)
        self.assertIn(PT_DYNAMIC, self.elf.segments)
        self.assertTrue(self.elf.dynamic)
        # ffmpeg allocates through posix_memalign, not malloc.
        self.assertLessEqual({"__libc_start_main", "free", "pthread_create"}, self.elf.imported,
                             sorted(self.elf.imported)[:40])

    def test_the_reader_finds_hardening_where_it_is(self) -> None:
        # Control: on a binary built hardened, the same reader finds every
        # property, so a failure on ffmpeg is about ffmpeg, not the reader.
        control = read_elf(HARDENED_CONTROL)
        self.assertEqual(control.elf_type, ET_DYN)
        self.assertIn(PT_INTERP, control.segments)
        self.assertIn(PT_GNU_RELRO, control.segments)
        self.assertTrue(_bind_now(control))
        self.assertIn("__stack_chk_fail", control.imported)
        self.assertTrue(_fortified(control))

    def test_it_is_a_position_independent_executable(self) -> None:
        # ET_DYN with an interpreter is a PIE; ET_EXEC loads at a fixed
        # address, which gives an exploit every gadget's address for free.
        self.assertEqual(self.elf.elf_type, ET_DYN, f"ELF type {self.elf.elf_type}, not ET_DYN (3): not a PIE")
        self.assertIn(PT_INTERP, self.elf.segments, "ET_DYN without an interpreter is a library, not a program")

    def test_it_has_full_relro(self) -> None:
        # RELRO alone leaves the GOT writable until a symbol is first used;
        # with BIND_NOW the loader binds everything first and seals it all.
        self.assertIn(PT_GNU_RELRO, self.elf.segments, "no PT_GNU_RELRO segment")
        self.assertTrue(_bind_now(self.elf),
                        f"no BIND_NOW (DT_BIND_NOW, DF_BIND_NOW or DF_1_NOW) in {self.elf.dynamic!r}")

    def test_it_has_a_stack_protector(self) -> None:
        self.assertTrue("__stack_chk_fail" in self.elf.imported,
                        "no stack protector: __stack_chk_fail is not among the imported symbols")

    def test_its_libc_calls_are_fortified(self) -> None:
        self.assertTrue(_fortified(self.elf), "no fortified libc call (__*_chk) is imported: _FORTIFY_SOURCE is off")


if __name__ == "__main__":
    unittest.main()

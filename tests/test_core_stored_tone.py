"""A stored tone is read safely: a regular file of bounded size, or a 404 (docs/PLAN.md, section 8).

A render's ffmpeg runs as the server's own UID, and a process it leaves
behind can replace a stored tone in the results directory. The download
must then neither follow a link (to the server's /proc/self/maps, or to
/dev/zero, which would read until the server runs out of memory), nor block
on a FIFO (which holds a handler thread until a restart), nor read more than
an output may hold. Anything but a regular file within that size is answered
as if the result were gone.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # noqa: F401  (puts core/ on sys.path)
from strange_ringtone import results as results_module
from strange_ringtone.results import Result, ResultStore
from test_server import RenderFlowTestCase

# Bytes that text-mode reading on Windows would change: a CRLF, a Ctrl-Z
# (end of file there) and a NUL.
TONE = b"tone\r\n\x1a\x00" + bytes(range(256))
# A refusal reads nothing that could take long: well under the client's
# own deadline.
QUICK_SECONDS = 2.0


def _can_symlink(directory: Path) -> bool:
    probe = directory / "probe-link"
    try:
        os.symlink(directory, probe)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


POSIX_SPECIALS = hasattr(os, "mkfifo") and os.path.exists("/dev/zero")


class ReadToneTest(unittest.TestCase):
    """ResultStore.read_tone: the bytes of a regular file of at most max_bytes, or None."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.store = ResultStore(self.temp / "results", 60, 10)
        source = self.temp / "out.m4a"
        source.write_bytes(TONE)
        self.result = self.store.put(source, "name")

    def replace_with(self, make: Callable[[Path], Any]) -> None:
        self.result.path.unlink()
        make(self.result.path)

    def read_quickly(self, max_bytes: int = 4096) -> bytes | None:
        started = time.monotonic()
        data = self.store.read_tone(self.result, max_bytes)
        self.assertLess(time.monotonic() - started, QUICK_SECONDS)
        return data

    def test_a_regular_file_is_read_whole_and_unchanged(self) -> None:
        # Windows too: the bytes come back exactly, never through text mode.
        self.assertEqual(self.read_quickly(), TONE)

    def test_the_limit_is_what_an_output_may_hold(self) -> None:
        self.assertEqual(self.store.read_tone(self.result, len(TONE)), TONE)
        self.assertIsNone(self.store.read_tone(self.result, len(TONE) - 1))

    def test_no_more_is_read_than_the_limit_allows(self) -> None:
        # A file that grows after its size was looked at: the read itself
        # stops one byte past the limit.
        self.result.path.write_bytes(b"x" * 1024 * 1024)
        limit = 1000
        real_fstat, real_read = os.fstat, os.read
        read: list[int] = []

        def small_fstat(fd: int) -> os.stat_result:
            values = list(real_fstat(fd))
            values[6] = 10  # st_size
            return os.stat_result(values)

        def counting_read(fd: int, size: int) -> bytes:
            data = real_read(fd, size)
            read.append(len(data))
            return data

        with mock.patch.object(results_module.os, "fstat", small_fstat), \
                mock.patch.object(results_module.os, "read", counting_read):
            self.assertIsNone(self.store.read_tone(self.result, limit))
        self.assertLessEqual(sum(read), limit + 1)

    def test_a_link_to_a_regular_file_is_refused_and_its_target_left_alone(self) -> None:
        if not _can_symlink(self.temp):
            self.skipTest("this account can't create symlinks")
        target = self.temp / "elsewhere.txt"
        target.write_bytes(b"not a tone\n")
        self.replace_with(lambda path: os.symlink(target, path))
        self.assertIsNone(self.read_quickly())
        self.assertEqual(target.read_bytes(), b"not a tone\n")

    def test_a_link_is_refused_where_there_is_no_nofollow_flag(self) -> None:
        # Windows has no O_NOFOLLOW; the link is looked for before the open.
        if not _can_symlink(self.temp):
            self.skipTest("this account can't create symlinks")
        target = self.temp / "elsewhere.txt"
        target.write_bytes(b"not a tone\n")
        self.replace_with(lambda path: os.symlink(target, path))
        with mock.patch.object(results_module.os, "O_NOFOLLOW", 0, create=True):
            self.assertIsNone(self.read_quickly())

    def test_a_directory_or_nothing_is_refused(self) -> None:
        self.replace_with(lambda path: path.mkdir())
        self.assertIsNone(self.read_quickly())
        self.result.path.rmdir()
        self.assertIsNone(self.read_quickly())

    @unittest.skipUnless(POSIX_SPECIALS, "FIFOs and /dev/zero are POSIX")
    def test_a_fifo_is_refused_without_blocking(self) -> None:
        self.replace_with(os.mkfifo)
        self.assertIsNone(self.read_quickly())

    @unittest.skipUnless(POSIX_SPECIALS, "FIFOs and /dev/zero are POSIX")
    def test_a_link_to_an_endless_device_is_refused(self) -> None:
        self.replace_with(lambda path: os.symlink("/dev/zero", path))
        self.assertIsNone(self.read_quickly())

    def test_the_handle_is_closed(self) -> None:
        # Windows can't delete an open file, and the sweeper must be able to.
        self.assertEqual(self.store.read_tone(self.result, 4096), TONE)
        self.result.path.unlink()
        self.assertFalse(self.result.path.exists())


class StoredToneDownloadTest(RenderFlowTestCase):
    """The download route answers 404 for a stored tone that was replaced, quickly, and keeps serving."""

    def stored(self) -> tuple[str, Result]:
        result_id = self.rendered(self.render())
        result = self.server.results.get(result_id)
        assert result is not None
        return result_id, result

    def get_quickly(self, result_id: str) -> Any:
        started = time.monotonic()
        response = self.request("GET", f"/api/result/{result_id}.m4a")
        self.assertLess(time.monotonic() - started, QUICK_SECONDS)
        return response

    def assertStillServes(self) -> None:
        result_id, result = self.stored()
        response = self.request("GET", f"/api/result/{result_id}.m4r")
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.body, result.path.read_bytes())

    def test_a_stored_tone_is_served_unchanged(self) -> None:
        result_id, result = self.stored()
        result.path.write_bytes(TONE)
        response = self.request("GET", f"/api/result/{result_id}.m4a")
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.body, TONE)

    def test_a_tone_larger_than_an_output_may_be_is_not_found(self) -> None:
        result_id, result = self.stored()
        result.path.write_bytes(b"x" * (self.server.renderer.limits.file_size_bytes + 1))
        self.assertJsonError(self.get_quickly(result_id), 404, "not_found")
        self.assertStillServes()

    def test_a_tone_replaced_by_a_link_to_a_regular_file_is_not_found(self) -> None:
        if not _can_symlink(self.temp):
            self.skipTest("this account can't create symlinks")
        target = self.temp / "elsewhere.txt"
        target.write_bytes(b"not a tone\n")
        result_id, result = self.stored()
        result.path.unlink()
        os.symlink(target, result.path)
        self.assertJsonError(self.get_quickly(result_id), 404, "not_found")
        self.assertEqual(target.read_bytes(), b"not a tone\n")
        self.assertStillServes()

    @unittest.skipUnless(POSIX_SPECIALS, "FIFOs and /dev/zero are POSIX")
    def test_a_tone_replaced_by_a_link_to_an_endless_device_is_not_found(self) -> None:
        result_id, result = self.stored()
        result.path.unlink()
        os.symlink("/dev/zero", result.path)
        self.assertJsonError(self.get_quickly(result_id), 404, "not_found")
        self.assertStillServes()

    @unittest.skipUnless(POSIX_SPECIALS, "FIFOs and /dev/zero are POSIX")
    def test_a_tone_replaced_by_a_fifo_is_not_found(self) -> None:
        result_id, result = self.stored()
        result.path.unlink()
        os.mkfifo(result.path)
        # Registered after the server's own clean-up, so it runs first: a
        # handler blocked on the FIFO (the defect) is released, and the test
        # fails instead of hanging the server's shutdown.
        self.addCleanup(self.release, result.path)
        for _ in range(3):
            self.assertJsonError(self.get_quickly(result_id), 404, "not_found")
        self.assertStillServes()

    @staticmethod
    def release(fifo: Path) -> None:
        try:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            # No reader waits on it.
            pass


if __name__ == "__main__":
    unittest.main()

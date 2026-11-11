"""Two of remove_leftovers' rules that no real tree in a test can reach (docs/PLAN.md, section 6.1, R7).

Another filesystem mounted inside the scratch directory is never entered or
removed: no test container may mount one, so the clean-up's view of the tree
is changed instead, and one directory, with everything in it, is reported on
another device, as lstat reports a mount point and what lies below it. And a
directory something takes the permissions off between its listing and the
removal of an entry gets the owner's permissions back before the one retry:
under the sweep nothing of a run is still running to do that, but without
the sweep something can.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from strange_ringtone_runner import server as runner_server
from test_fetcher_runner_leftovers import LITTER_BYTES, POSIX, ROOT_USER, TempTestCase, run_bounded, seam, tree


class LeftoverRulesTest(TempTestCase):
    def remove(self) -> bool:
        remove_leftovers = seam(runner_server, "remove_leftovers")
        return run_bounded(self, lambda: remove_leftovers(self.root), self.temp)

    def report_on_another_device(self, paths: list[Path]) -> None:
        """Every stat of the clean-up's trees says that paths lie on another device than the rest."""
        moved = {os.lstat(path).st_ino for path in paths}

        def elsewhere(result: os.stat_result) -> os.stat_result:
            if result.st_ino not in moved:
                return result
            fields = list(result[:10])
            fields[2] = result.st_dev + 1
            return os.stat_result(fields)

        for cls in (runner_server._DescriptorTree, runner_server._PathTree):
            for name in ("stat", "lstat"):
                real = cls.__dict__[name]

                def reporting(tree: Any, *args: Any, _real: Any = real) -> os.stat_result:
                    return elsewhere(_real(tree, *args))

                patcher = mock.patch.object(cls, name, reporting)
                patcher.start()
                self.addCleanup(patcher.stop)

    def test_another_filesystem_inside_is_never_entered_or_removed(self) -> None:
        mount = self.root / "job-1" / "mount"
        (mount / "sub").mkdir(parents=True)
        (mount / "file").write_bytes(b"another filesystem's")
        (mount / "sub" / "file").write_bytes(b"another filesystem's")
        (self.root / "job-1" / "litter").write_bytes(LITTER_BYTES)
        (self.root / "stray").write_bytes(LITTER_BYTES)
        self.report_on_another_device([mount, mount / "sub", mount / "file", mount / "sub" / "file"])
        # Not clean: something is left that the clean-up may not remove.
        self.assertIs(self.remove(), False)
        self.assertEqual(tree(self.root), ["job-1", "job-1/mount", "job-1/mount/file", "job-1/mount/sub",
                                           "job-1/mount/sub/file"])
        self.assertEqual((mount / "sub" / "file").read_bytes(), b"another filesystem's")

    @unittest.skipUnless(POSIX and not ROOT_USER, "POSIX permissions, as a user they bind")
    def test_a_directory_locked_while_it_is_emptied_gets_its_permissions_back(self) -> None:
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "file").write_bytes(LITTER_BYTES)
        cls = runner_server._DescriptorTree if runner_server._BY_DESCRIPTOR else runner_server._PathTree
        real = cls.__dict__["unlink"]
        locked: list[str] = []

        def locking(tree: Any, handle: Any, name: str) -> None:
            if name == "file" and not locked:
                # After the listing, before the removal: the directory loses
                # its write permission, so the first unlink is refused.
                locked.append(name)
                os.chmod(sub, 0o500)
            real(tree, handle, name)

        with mock.patch.object(cls, "unlink", locking):
            self.assertIs(self.remove(), True)
        self.assertEqual(locked, ["file"])
        self.assertEqual(tree(self.root), [])


if __name__ == "__main__":
    unittest.main()

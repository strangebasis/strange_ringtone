"""The fetcher image's build step that deletes the yt-dlp extractors it never runs (docs/PLAN.md 6.1).

The script runs as root in the build, on files a wheel put there, so these
tests pin what it refuses: a name that could leave the directory, a list
yt-dlp could not start with, a kept name that is missing or is not what the
list says, and a directory that is a link. Every refusal leaves the tree as
it was. The in-image suite checks the result in the real image.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

import helpers

SCRIPT = helpers.FETCHER / "prune_extractors.py"
KEEP_LIST = helpers.FETCHER / "ytdlp-extractors.txt"
# The standard library modules the script may import: it runs as root with
# -I -S, and nothing of the venv may be loaded.
ALLOWED_IMPORTS = {"__future__", "os", "re", "shutil", "stat", "sys"}
CAN_SYMLINK = os.name == "posix"


def _load():
    spec = importlib.util.spec_from_file_location("prune_extractors", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prune = _load()
REQUIRED = list(prune.REQUIRED)


class TreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.extractor = self.root / "extractor"
        self.extractor.mkdir()
        self.keep_file = self.root / "keep.txt"

    def put(self, name: str, content: str = "x = 1\n") -> None:
        path = self.extractor / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)

    def fill(self) -> None:
        for name in (*REQUIRED, "youtube/__init__.py", "youtube/_video.py",
                     "youtube/__pycache__/_video.cpython-312.pyc",
                     "openload.py", "espn.py", "go.py", "_extractors.py", "generic.py",
                     "__pycache__/espn.cpython-312.pyc", "__pycache__/common.cpython-312.pyc",
                     "othersite/__init__.py", "othersite/deep/module.py"):
            self.put(name)

    def write_keep(self, text: str) -> None:
        with open(self.keep_file, "w", encoding="ascii", newline="\n") as handle:
            handle.write(text)

    def tree(self) -> set[str]:
        return {path.relative_to(self.extractor).as_posix() for path in self.extractor.rglob("*")}


class PruneTest(TreeCase):
    KEEP = [*REQUIRED, "youtube/", "openload.py"]

    def test_everything_off_the_list_goes_and_everything_on_it_stays(self) -> None:
        self.fill()
        self.assertEqual(prune.prune(self.KEEP, str(self.extractor)), (6, 6))
        self.assertEqual(self.tree(), {
            *REQUIRED, "openload.py", "youtube", "youtube/__init__.py", "youtube/_video.py",
            "youtube/__pycache__", "youtube/__pycache__/_video.cpython-312.pyc",
        })

    def test_the_stale_bytecode_of_removed_modules_goes_with_them(self) -> None:
        # The top-level __pycache__ holds one file per module, kept or not;
        # the build compiles the kept ones again afterwards.
        self.fill()
        prune.prune(self.KEEP, str(self.extractor))
        self.assertFalse((self.extractor / "__pycache__").exists())

    def test_a_kept_name_that_is_missing_stops_before_anything_is_deleted(self) -> None:
        self.fill()
        before = self.tree()
        with self.assertRaises(prune.PruneError) as caught:
            prune.prune([*self.KEEP, "renamed_helper.py"], str(self.extractor))
        self.assertIn("ships no such extractor", str(caught.exception))
        self.assertEqual(self.tree(), before)

    def test_a_kept_name_of_the_wrong_kind_stops_before_anything_is_deleted(self) -> None:
        self.fill()
        before = self.tree()
        for keep in ([*REQUIRED, "youtube.py"], [*REQUIRED, "openload/"], [*REQUIRED, "othersite.py"]):
            with self.subTest(keep=keep[-1]), self.assertRaises(prune.PruneError):
                prune.prune(keep, str(self.extractor))
        self.assertEqual(self.tree(), before)

    def test_a_directory_that_is_missing_or_not_a_directory_is_refused(self) -> None:
        self.fill()
        for target in (self.root / "absent", self.extractor / "common.py"):
            with self.subTest(target=target.name), self.assertRaises(prune.PruneError):
                prune.prune(self.KEEP, str(target))
        self.assertIn("espn.py", self.tree())

    @unittest.skipUnless(CAN_SYMLINK, "needs symlinks")
    def test_links_are_removed_or_refused_and_never_followed(self) -> None:
        self.fill()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "precious.py").write_text("keep me\n", encoding="utf-8")
        # A link off the list is removed; what it points at is untouched.
        os.symlink(outside, self.extractor / "linked_dir")
        os.symlink(outside / "precious.py", self.extractor / "linked_file.py")
        prune.prune(self.KEEP, str(self.extractor))
        self.assertNotIn("linked_dir", self.tree())
        self.assertNotIn("linked_file.py", self.tree())
        self.assertEqual((outside / "precious.py").read_text(encoding="utf-8"), "keep me\n")
        # A kept name that is a link is refused: the list names real files.
        os.symlink(outside / "precious.py", self.extractor / "adobepass.py")
        os.symlink(outside, self.extractor / "pkg")
        for name in ("adobepass.py", "pkg/"):
            with self.subTest(name=name), self.assertRaises(prune.PruneError):
                prune.prune([*self.KEEP, name], str(self.extractor))
        # The directory itself given as a link is refused too.
        os.symlink(self.extractor, self.root / "alias")
        with self.assertRaises(prune.PruneError):
            prune.prune(self.KEEP, str(self.root / "alias"))
        self.assertIn("common.py", self.tree())


class KeepListTest(TreeCase):
    def test_names_comments_and_blank_lines(self) -> None:
        self.write_keep("# why\n\n" + "\n".join(REQUIRED) + "\n  youtube/  \n# more\nopenload.py\n")
        self.assertEqual(prune.read_keep(str(self.keep_file)), [*REQUIRED, "youtube/", "openload.py"])

    def test_a_name_that_is_not_one_module_or_package_is_refused(self) -> None:
        for bad in ("../outside.py", "youtube/_video.py", "/etc/passwd", "espn", "espn.txt", "Espn.py", "a b.py",
                    "..", ".", "youtube//", "*.py", "espn.py extra", "espn.pyc", "\\espn.py", "é.py"):
            with self.subTest(bad=bad):
                with open(self.keep_file, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write("\n".join(REQUIRED) + "\n" + bad + "\n")
                with self.assertRaises(prune.PruneError):
                    prune.read_keep(str(self.keep_file))

    def test_a_repeated_name_is_refused(self) -> None:
        self.write_keep("\n".join(REQUIRED) + "\ncommon.py\n")
        with self.assertRaises(prune.PruneError):
            prune.read_keep(str(self.keep_file))

    def test_a_list_yt_dlp_could_not_start_with_is_refused(self) -> None:
        for missing in REQUIRED:
            with self.subTest(missing=missing):
                self.write_keep("\n".join(name for name in REQUIRED if name != missing) + "\nyoutube/\n")
                with self.assertRaises(prune.PruneError) as caught:
                    prune.read_keep(str(self.keep_file))
                self.assertIn(missing, str(caught.exception))
        self.write_keep("")
        with self.assertRaises(prune.PruneError):
            prune.read_keep(str(self.keep_file))

    def test_an_unreadable_list_is_refused(self) -> None:
        with self.assertRaises(prune.PruneError):
            prune.read_keep(str(self.root / "absent.txt"))

    def test_the_images_list_is_youtube_and_what_it_imports(self) -> None:
        # The list the image is built with. A longer list puts other sites'
        # extractors, and what their source carries, back into the image.
        self.assertEqual(prune.read_keep(str(KEEP_LIST)), [
            "__init__.py", "extractors.py", "lazy_extractors.py", "common.py",
            "youtube/", "adobepass.py", "afreecatv.py", "openload.py",
        ])


class MainTest(TreeCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = prune.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_good_run_says_what_it_kept_and_removed(self) -> None:
        self.fill()
        self.write_keep("\n".join([*REQUIRED, "youtube/", "openload.py"]) + "\n")
        code, out, err = self.run_main([str(self.keep_file), str(self.extractor)])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "prune_extractors: kept 6 entries of yt-dlp's extractors and removed 6\n")

    def test_a_refusal_fails_the_build_and_deletes_nothing(self) -> None:
        self.fill()
        before = self.tree()
        self.write_keep("\n".join([*REQUIRED, "gone.py"]) + "\n")
        code, out, err = self.run_main([str(self.keep_file), str(self.extractor)])
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("prune_extractors: "))
        self.assertEqual(self.tree(), before)

    def test_wrong_arguments_exit_2(self) -> None:
        for argv in ([], ["one"], ["one", "two", "three"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_main(argv)[0], 2)


class ScriptTest(unittest.TestCase):
    def test_it_imports_only_the_standard_library_modules_it_is_allowed(self) -> None:
        # It runs as root beside a venv a wheel filled: nothing may make it
        # load a module from there.
        with SCRIPT.open(encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "a relative import")
                imported.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("__import__", "exec", "eval"):
                self.fail(f"a call to {node.func.id}")  # type: ignore[union-attr]
        self.assertLessEqual(imported, ALLOWED_IMPORTS)
        self.assertNotIn("sys.path", SCRIPT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

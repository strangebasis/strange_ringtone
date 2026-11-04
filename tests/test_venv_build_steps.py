"""I1 at build time: once the venv's wheels are in, no build step runs their code as root (fetcher/Dockerfile).

The site module runs every `.pth` line in a site-packages directory when an
interpreter starts with site enabled on it. A malicious yt-dlp or yt-dlp-ejs
release (docs/PLAN.md 6.1) can ship such a line, and every RUN step is root,
so a step that starts the venv's interpreter after the install would hand that
line root's rights over the base interpreter, the fetcher and the forwarder
run on, before any in-image check looks. pip's --python is allowed only for
the install itself: it starts the venv's interpreter while the venv is still
empty, and site reads `.pth` files only at startup.

This reads fetcher/Dockerfile on the host, so it runs on every PR without
Docker; the in-image suite checks the bytecode the build left behind. The
name keeps it out of tests/image_fetcher's test_fetcher_*.py glob: the image
ships no Dockerfile.
"""
from __future__ import annotations

import json
import re
import shlex
import unittest

import helpers

DOCKERFILE = helpers.ROOT / "fetcher" / "Dockerfile"
VENV = "/opt/yt-dlp"
# The install step: pip filling the venv through --python.
_INSTALL_RE = re.compile(r"--python(=|\s+)" + re.escape(VENV) + r"(/bin/python)?\b")
# Shell control operators split a RUN line into simple commands.
_OPERATORS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"}
# python, python3, python3.12 and the like, by basename.
_PYTHON_RE = re.compile(r"^python[0-9.]*$")
# Instructions whose text could put the venv on a later step's PATH,
# PYTHONPATH or shell.
_SETTING_INSTRUCTIONS = {"ENV", "ARG", "SHELL"}


def instructions(text: str) -> list[tuple[str, str]]:
    """The Dockerfile's instructions as (KEYWORD, arguments), continuation lines joined and comments dropped.

    Docker drops a comment line even inside a continued instruction, so a
    comment can't hide or fake a command here either. A parser directive
    that changes the escape character, or a heredoc, is refused: this reader
    doesn't model them, so it can't vouch for what they run.
    """
    if re.search(r"^#\s*escape\s*=", text, re.M | re.I):
        raise ValueError("an escape directive is not supported")
    found: list[tuple[str, str]] = []
    pending: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#") or (not stripped and not pending):
            continue
        if stripped.endswith("\\"):
            pending.append(stripped[:-1])
            continue
        pending.append(stripped)
        joined = " ".join(part for part in pending if part).strip()
        pending = []
        keyword, _, arguments = joined.partition(" ")
        found.append((keyword.upper(), arguments.strip()))
    if pending:
        raise ValueError("the Dockerfile ends inside a continued instruction")
    return found


def commands(arguments: str) -> list[list[str]]:
    """A RUN's simple commands, each as its argument list (exec form is one command)."""
    if arguments.startswith("["):
        argv = json.loads(arguments)
        if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
            raise ValueError(f"exec form is not a list of strings: {arguments!r}")
        return [argv]
    if "<<" in arguments:
        raise ValueError(f"a heredoc is not supported: {arguments!r}")
    lexer = shlex.shlex(arguments, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    found: list[list[str]] = []
    current: list[str] = []
    for token in lexer:
        if token in _OPERATORS:
            if current:
                found.append(current)
            current = []
        else:
            current.append(token)
    if current:
        found.append(current)
    return found


def _interpreter_flags(argv: list[str], start: int) -> set[str]:
    # The single-letter options before the first -m, -c or script argument:
    # "-I -S", "-IS" and "-SI" all give {"I", "S"}.
    flags: set[str] = set()
    for token in argv[start + 1:]:
        if token in ("-m", "-c") or not token.startswith("-") or token.startswith("--"):
            break
        flags.update(token[1:])
    return flags


def _is_site_free_compile(argv: list[str]) -> bool:
    # The one form allowed to name the venv after the install: the base
    # interpreter, isolated and without site, compiling it.
    start = next((index for index, token in enumerate(argv) if "=" not in token), None)
    if start is None or not _PYTHON_RE.match(argv[start]):
        return False
    if not {"I", "S"} <= _interpreter_flags(argv, start):
        return False
    rest = argv[start + 1:]
    if "-m" not in rest:
        return False
    module = rest[rest.index("-m") + 1] if rest.index("-m") + 1 < len(rest) else ""
    return module in ("compileall", "py_compile")


def venv_hazards(text: str) -> tuple[list[str], list[list[str]]]:
    """(problems, the site-free compiles of the venv) for every build step after the venv's install.

    A problem is any later step that names the venv's bin directory, uses
    pip's --python again, sets anything to a path in the venv, starts any
    interpreter on a file or directory in the venv other than a site-free
    compile, or compiles the venv without -I and -S.
    """
    problems: list[str] = []
    compiles: list[list[str]] = []
    installed = False
    for keyword, arguments in instructions(text):
        if not installed:
            if keyword == "RUN":
                steps = commands(arguments)
                for index, argv in enumerate(steps):
                    if _INSTALL_RE.search(" ".join(argv)) and "install" in argv:
                        installed = True
                        problems.extend(_check(steps[index + 1:], compiles))
                        break
            continue
        if keyword in _SETTING_INSTRUCTIONS and VENV in arguments:
            problems.append(f"{keyword} {arguments}: puts the venv on a later step's path")
        elif keyword == "RUN":
            problems.extend(_check(commands(arguments), compiles))
    if not installed:
        problems.append("no RUN step installs into the venv with pip --python")
    return problems, compiles


def _check(steps: list[list[str]], compiles: list[list[str]]) -> list[str]:
    problems: list[str] = []
    for argv in steps:
        line = shlex.join(argv)
        if any(token == "--python" or token.startswith("--python=") for token in argv):
            problems.append(f"{line}: pip --python starts the venv's interpreter after its wheels are in")
            continue
        if any(f"{VENV}/bin" in token for token in argv):
            problems.append(f"{line}: names the venv's bin directory, its interpreter or scripts")
            continue
        if any("=" in token and VENV in token.partition("=")[2] for token in argv if not token.startswith("-")):
            problems.append(f"{line}: sets a variable to a path in the venv")
            continue
        if not any(VENV in token for token in argv):
            continue
        compiling = any("compileall" in token or "py_compile" in token for token in argv)
        if _is_site_free_compile(argv):
            compiles.append(argv)
        elif compiling:
            problems.append(f"{line}: compiles the venv other than as the base interpreter's -I -S -m compileall")
        elif any(_PYTHON_RE.match(token.rsplit("/", 1)[-1]) for token in argv):
            problems.append(f"{line}: runs an interpreter on a path in the venv")
    return problems


def _with_compile_step(text: str, replacement: str) -> str:
    # The real Dockerfile with its venv compile step swapped for another.
    old = "    && python -I -S -m compileall -q /opt/yt-dlp/lib \\\n"
    if old not in text:
        raise AssertionError("fetcher/Dockerfile's venv compile step moved; update this test")
    return text.replace(old, replacement)


class VenvBuildStepTest(unittest.TestCase):
    """I1: after the venv's install, no build step starts an interpreter that reads its .pth files."""

    def setUp(self) -> None:
        with DOCKERFILE.open(encoding="utf-8") as handle:
            self.text = handle.read()

    def test_no_step_after_the_install_runs_the_venvs_pth_lines(self) -> None:
        problems, compiles = venv_hazards(self.text)
        self.assertEqual(problems, [])
        # Acceptance: the bytecode is still compiled, by the base interpreter
        # without site, which shares the venv interpreter's cache tag.
        self.assertEqual(compiles, [["python", "-I", "-S", "-m", "compileall", "-q", "/opt/yt-dlp/lib"]])

    def test_every_way_to_run_the_venvs_code_is_refused(self) -> None:
        # Each line takes the place of the compile step, after the install
        # in the same RUN, as the step the review found did.
        bad = {
            "the venv's interpreter compiling": "/opt/yt-dlp/bin/python -m compileall -q /opt/yt-dlp/lib",
            "the venv's python3": "/opt/yt-dlp/bin/python3 -I -S -m compileall -q /opt/yt-dlp/lib",
            "base without -S": "python -I -m compileall -q /opt/yt-dlp/lib",
            "base without -I": "python -S -m compileall -q /opt/yt-dlp/lib",
            "base, no flags": "python -m compileall -q /opt/yt-dlp/lib",
            "py_compile with site": "python -I -m py_compile /opt/yt-dlp/lib/python3.12/site-packages/x.py",
            "compileall through -c": "python -I -S -c 'import compileall; compileall.compile_dir(\"/opt/yt-dlp\")'",
            "pip --python again": "python -m pip --python /opt/yt-dlp list",
            "pip --python= again": "python -m pip --python=/opt/yt-dlp/bin/python list",
            "a venv script": "/opt/yt-dlp/bin/yt-dlp --version",
            "the venv on PATH": "PATH=/opt/yt-dlp/bin:/usr/bin python -c pass",
            "the venv on PYTHONPATH": "PYTHONPATH=/opt/yt-dlp/lib/python3.12/site-packages python -c pass",
            "a venv file run by base": "python /opt/yt-dlp/lib/python3.12/site-packages/yt_dlp/__main__.py",
            "the venv's site dir": "python -m site /opt/yt-dlp",
            "activate": ". /opt/yt-dlp/bin/activate",
        }
        for label, command in bad.items():
            with self.subTest(label=label):
                text = _with_compile_step(self.text, f"    && {command} \\\n")
                problems, _compiles = venv_hazards(text)
                self.assertNotEqual(problems, [], command)

    def test_later_instructions_are_checked_too(self) -> None:
        # The same in a RUN or setting of its own, after the install's RUN.
        anchor = "RUN python -m compileall -q /app\n"
        self.assertIn(anchor, self.text)
        for added in (
            "RUN /opt/yt-dlp/bin/python -c pass\n",
            'RUN ["/opt/yt-dlp/bin/python", "-c", "pass"]\n',
            "RUN true \\\n    # a comment line inside the step\n    && /opt/yt-dlp/bin/python -c pass\n",
            "ENV PATH=/opt/yt-dlp/bin:$PATH\n",
            "ENV PYTHONPATH=/opt/yt-dlp/lib/python3.12/site-packages\n",
            'SHELL ["/opt/yt-dlp/bin/python", "-c"]\n',
        ):
            with self.subTest(added=added):
                problems, _compiles = venv_hazards(self.text.replace(anchor, anchor + added))
                self.assertNotEqual(problems, [], added)

    def test_comments_and_steps_before_the_install_are_not_problems(self) -> None:
        # Acceptance: the venv's own creation and the install come first, and
        # a comment naming the venv's interpreter runs nothing.
        anchor = "RUN python -m compileall -q /app\n"
        text = self.text.replace(anchor, "# /opt/yt-dlp/bin/python -m compileall /opt/yt-dlp\n" + anchor)
        self.assertEqual(venv_hazards(text)[0], [])

    def test_an_unreadable_dockerfile_is_refused(self) -> None:
        # The reader fails closed on what it doesn't model, and without the
        # install step the check reports that rather than passing.
        with self.assertRaises(ValueError):
            venv_hazards("# escape=`\nFROM scratch\n")
        with self.assertRaises(ValueError):
            venv_hazards("FROM scratch\nRUN <<EOF\n/opt/yt-dlp/bin/python\nEOF\n")
        with self.assertRaises(ValueError):
            venv_hazards("FROM scratch\nRUN true \\\n")
        self.assertEqual(venv_hazards("FROM scratch\nRUN true\n")[0],
                         ["no RUN step installs into the venv with pip --python"])


if __name__ == "__main__":
    unittest.main()

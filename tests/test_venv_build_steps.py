"""I1 and I2 at build time: no build step runs the venv's code as root (fetcher/Dockerfile).

The site module runs every `.pth` line in a site-packages directory when an
interpreter starts with site enabled on it. A malicious yt-dlp or yt-dlp-ejs
release (docs/PLAN.md 6.1) can ship such a line, and every RUN step is root,
so a step that starts the venv's interpreter after the install would hand that
line root's rights over the base interpreter, the fetcher, the runner and the
forwarder run on, before any in-image check looks. So the venv is filled by
the base interpreter's pip through --prefix /opt/yt-dlp (I2), and pip never
runs under the venv's interpreter at all: a --python naming the venv is
refused in every step, the install included, because it would start the
venv's interpreter while pip writes the wheels into it. A second install into
the venv after the first is refused too: only the hashed lock goes in.

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
# The install step: the base interpreter's pip filling the venv through
# --prefix, so no interpreter has the venv on its path while the wheels go in.
_INSTALL_RE = re.compile(r"--prefix(=|\s+)" + re.escape(VENV) + r"\b")
# What venv_hazards reports when it finds no such step: fail closed.
NOT_FOUND = "no RUN step installs into the venv with pip --prefix"
# Shell control operators split a RUN line into simple commands.
_OPERATORS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"}
# python, python3, python3.12 and the like, by basename.
_PYTHON_RE = re.compile(r"^python[0-9.]*$")
# Instructions whose text could put the venv on a later step's PATH,
# PYTHONPATH or shell.
_SETTING_INSTRUCTIONS = {"ENV", "ARG", "SHELL"}
# The venv's bin directory written relative to a parent (a WORKDIR of /opt,
# say): a later step can name the venv's interpreter without its full path.
_RELATIVE_BIN = "yt-dlp/bin"


def _names_venv(text: str) -> bool:
    return VENV in text or _RELATIVE_BIN in text


def _program_index(argv: list[str]) -> int | None:
    # The first token that is not a VAR=value assignment: the program.
    return next((index for index, token in enumerate(argv) if "=" not in token), None)


def _is_install(argv: list[str]) -> bool:
    # The venv's install: a pip install naming the venv as its --prefix, run
    # by the base interpreter (a bare python, never a path into the venv).
    if "install" not in argv or not _INSTALL_RE.search(" ".join(argv)):
        return False
    start = _program_index(argv)
    return start is not None and _PYTHON_RE.match(argv[start]) is not None


def _option_values(argv: list[str], option: str) -> list[str]:
    # Every value given to a long option, as "--option value" or "--option=value".
    values: list[str] = []
    for index, token in enumerate(argv):
        if token == option and index + 1 < len(argv):
            values.append(argv[index + 1])
        elif token.startswith(option + "="):
            values.append(token.partition("=")[2])
    return values


def _pip_python_on_venv(argv: list[str]) -> bool:
    # pip --python starts the named interpreter and runs pip under it: with
    # the venv's interpreter that is the venv's site, and its .pth lines,
    # while pip writes the wheels in.
    return any(_names_venv(value) for value in _option_values(argv, "--python"))


def _installs_into_venv(argv: list[str]) -> bool:
    # --prefix or --target naming the venv, by its full path or relative to a
    # parent ("yt-dlp/..." after a cd to /opt): any second install into it.
    return any(
        _names_venv(value) or "yt-dlp" in value
        for option in ("--prefix", "--target")
        for value in _option_values(argv, option)
    )


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
    start = _program_index(argv)
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

    A problem is a pip --python naming the venv in any step up to and
    including the install (I2), and any later step that names the venv's bin
    directory, uses pip's --python at all, installs into the venv again, sets
    anything to a path in the venv, starts any interpreter on a file or
    directory in the venv other than a site-free compile, compiles the venv
    without -I and -S, or names the venv in any other way (a cd or WORKDIR
    into it, a COPY onto it): this is a tripwire for our own Dockerfile, so
    it fails closed rather than model the shell.
    """
    problems: list[str] = []
    compiles: list[list[str]] = []
    installed = False
    for keyword, arguments in instructions(text):
        if not installed:
            if keyword == "RUN":
                steps = commands(arguments)
                for index, argv in enumerate(steps):
                    if _pip_python_on_venv(argv):
                        problems.append(f"{shlex.join(argv)}: pip --python runs pip under the venv's interpreter")
                    if _is_install(argv):
                        installed = True
                        problems.extend(_check(steps[index + 1:], compiles))
                        break
            continue
        if keyword in _SETTING_INSTRUCTIONS and _names_venv(arguments):
            problems.append(f"{keyword} {arguments}: puts the venv on a later step's path")
        elif keyword != "RUN" and _names_venv(arguments):
            # WORKDIR, COPY, ADD and the rest: a working directory in the venv
            # lets a later step start its interpreter by a relative path (and
            # puts its modules first on sys.path for python -c), and nothing
            # after the install has a reason to name the venv at all.
            problems.append(f"{keyword} {arguments}: names the venv after its install")
        elif keyword == "RUN":
            problems.extend(_check(commands(arguments), compiles))
    if not installed:
        problems.append(NOT_FOUND)
    return problems, compiles


def _check(steps: list[list[str]], compiles: list[list[str]]) -> list[str]:
    problems: list[str] = []
    for argv in steps:
        line = shlex.join(argv)
        if any(token == "--python" or token.startswith("--python=") for token in argv):
            problems.append(f"{line}: pip --python starts the venv's interpreter after its wheels are in")
            continue
        if _installs_into_venv(argv):
            # Only the hashed lock goes in, once (I2).
            problems.append(f"{line}: installs into the venv a second time")
            continue
        if any(f"{VENV}/bin" in token for token in argv):
            problems.append(f"{line}: names the venv's bin directory, its interpreter or scripts")
            continue
        if any("=" in token and VENV in token.partition("=")[2] for token in argv if not token.startswith("-")):
            problems.append(f"{line}: sets a variable to a path in the venv")
            continue
        if not any(_names_venv(token) for token in argv):
            continue
        compiling = any("compileall" in token or "py_compile" in token for token in argv)
        if _is_site_free_compile(argv):
            compiles.append(argv)
        elif compiling:
            problems.append(f"{line}: compiles the venv other than as the base interpreter's -I -S -m compileall")
        elif any(_PYTHON_RE.match(token.rsplit("/", 1)[-1]) for token in argv):
            problems.append(f"{line}: runs an interpreter on a path in the venv")
        else:
            # Any other mention: a cd into the venv lets the next command
            # start bin/python, or put the venv's modules first on sys.path,
            # without naming the venv again. Fail closed; the site-free
            # compile is the only later step that needs it.
            problems.append(f"{line}: names the venv after its install")
    return problems


def _with_compile_step(text: str, replacement: str) -> str:
    # The real Dockerfile with its venv compile step swapped for another.
    old = "    && python -I -S -m compileall -q /opt/yt-dlp/lib \\\n"
    if old not in text:
        raise AssertionError("fetcher/Dockerfile's venv compile step moved; update this test")
    return text.replace(old, replacement)


def _has_option(argv: list[str], option: str, value: str) -> bool:
    # "--option value" as two tokens, or "--option=value" as one.
    pairs = zip(argv, argv[1:], strict=False)
    return (option, value) in pairs or f"{option}={value}" in argv


class VenvBuildStepTest(unittest.TestCase):
    """I1 and I2: pip never runs under the venv's interpreter, and after the install no step reads its .pth files."""

    # The install's venv option, as fetcher/Dockerfile writes it (I2).
    PREFIX = "--prefix /opt/yt-dlp"

    def setUp(self) -> None:
        with DOCKERFILE.open(encoding="utf-8") as handle:
            self.text = handle.read()

    def assertInstallFound(self, problems: list[str]) -> None:
        # A refusal must come from the step under test, never from a
        # Dockerfile whose install the reader didn't recognise.
        self.assertNotIn(NOT_FOUND, problems)

    def test_no_step_after_the_install_runs_the_venvs_pth_lines(self) -> None:
        # I1/I2 acceptance on the real Dockerfile: the install is found and
        # nothing before, in or after it is a problem.
        problems, compiles = venv_hazards(self.text)
        self.assertEqual(problems, [])
        # Acceptance: the bytecode is still compiled, by the base interpreter
        # without site, which shares the venv interpreter's cache tag.
        self.assertEqual(compiles, [["python", "-I", "-S", "-m", "compileall", "-q", "/opt/yt-dlp/lib"]])

    def test_the_install_is_the_base_interpreters_prefix_install(self) -> None:
        # I2: exactly one step installs into the venv, and it is the base
        # interpreter's pip writing to the venv's prefix with the hashed lock,
        # right after the venv is created without pip, in the same RUN.
        installs: list[tuple[list[str], list[str] | None]] = []
        for keyword, arguments in instructions(self.text):
            if keyword != "RUN":
                continue
            steps = commands(arguments)
            for index, argv in enumerate(steps):
                if "install" in argv and any(_names_venv(token) for token in argv):
                    installs.append((argv, steps[index - 1] if index else None))
        self.assertEqual(len(installs), 1, installs)
        argv, previous = installs[0]
        self.assertEqual(argv[:4], ["python", "-m", "pip", "install"], argv)
        self.assertTrue(_has_option(argv, "--prefix", VENV), argv)
        self.assertIn("--require-hashes", argv)
        self.assertIn("--no-deps", argv)
        self.assertTrue(_has_option(argv, "--only-binary", ":all:"), argv)
        self.assertTrue(_has_option(argv, "-r", "/app/fetcher/requirements.txt"), argv)
        self.assertFalse([token for token in argv if token == "--python" or token.startswith("--python=")], argv)
        self.assertEqual(previous, ["python", "-m", "venv", "--without-pip", VENV])
        # The reader recognises exactly this step as the install.
        self.assertTrue(_is_install(argv))

    def test_pip_python_on_the_venv_is_refused_anywhere(self) -> None:
        # I2: pip under the venv's interpreter is refused before, in and
        # after the install, and an install the venv's own interpreter runs
        # is not the install.
        self.assertIn(self.PREFIX, self.text, "fetcher/Dockerfile's install no longer uses --prefix; update this test")
        create = "RUN python -m venv --without-pip /opt/yt-dlp \\\n"
        install = "&& python -m pip install"
        self.assertIn(create, self.text)
        self.assertIn(install, self.text)
        cases = {
            "the install through --python": self.text.replace(self.PREFIX, "--python /opt/yt-dlp/bin/python"),
            "--python on the install too": self.text.replace(
                self.PREFIX, self.PREFIX + " --python /opt/yt-dlp/bin/python"),
            "--python= on the install too": self.text.replace(self.PREFIX, self.PREFIX + " --python=/opt/yt-dlp"),
            "--python before the install": self.text.replace(
                create, create + "    && python -m pip --python /opt/yt-dlp/bin/python list \\\n"),
            "--python by a relative path before the install": self.text.replace(
                create, create + "    && cd /opt && python -m pip --python=yt-dlp/bin/python list \\\n"),
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                problems, _compiles = venv_hazards(text)
                self.assertTrue([problem for problem in problems if "pip --python" in problem], problems)
        # The venv's own interpreter running the install: never the install,
        # so the check reports that no install was found.
        problems, _compiles = venv_hazards(self.text.replace(install, "&& /opt/yt-dlp/bin/python -m pip install"))
        self.assertIn(NOT_FOUND, problems)
        # Acceptance: a --python that names another interpreter before the
        # install starts nothing in the venv.
        text = self.text.replace(create, create + "    && python -m pip --python /usr/local/bin/python --version \\\n")
        self.assertEqual(venv_hazards(text)[0], [])

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
            "cd into the venv, then bin/python": "cd /opt/yt-dlp && bin/python -c pass",
            "cd into site-packages, then python -c": "cd /opt/yt-dlp/lib/python3.12/site-packages && python -c pass",
            "the bin directory by a relative path": "cd /opt && yt-dlp/bin/python -c pass",
            # I2: only the hashed lock goes in, once.
            "pip --prefix again": "python -m pip install --prefix /opt/yt-dlp x.whl",
            "pip --prefix= again": "python -m pip install --prefix=/opt/yt-dlp x.whl",
            "pip --target into the venv": (
                "python -m pip install --target /opt/yt-dlp/lib/python3.12/site-packages x.whl"),
            "pip --target= into the venv": (
                "python -m pip install --target=/opt/yt-dlp/lib/python3.12/site-packages x.whl"),
        }
        second_installs = {"pip --prefix again", "pip --prefix= again", "pip --target into the venv",
                           "pip --target= into the venv"}
        for label, command in bad.items():
            with self.subTest(label=label):
                text = _with_compile_step(self.text, f"    && {command} \\\n")
                problems, _compiles = venv_hazards(text)
                self.assertNotEqual(problems, [], command)
                self.assertInstallFound(problems)
                if label in second_installs:
                    self.assertTrue([problem for problem in problems if "a second time" in problem], problems)

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
            "WORKDIR /opt/yt-dlp\nRUN bin/python -c pass\n",
            "WORKDIR /opt/yt-dlp/lib/python3.12/site-packages\nRUN python -c pass\n",
            "WORKDIR /opt\nRUN yt-dlp/bin/python -c pass\n",
        ):
            with self.subTest(added=added):
                problems, _compiles = venv_hazards(self.text.replace(anchor, anchor + added))
                self.assertNotEqual(problems, [], added)
                self.assertInstallFound(problems)

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
                         ["no RUN step installs into the venv with pip --prefix"])
        # I2: the old --python install is no install at all now, and is refused.
        old = ("FROM scratch\nRUN python -m venv --without-pip /opt/yt-dlp \\\n"
               "    && python -m pip --python /opt/yt-dlp/bin/python install -r requirements.txt\n")
        problems = venv_hazards(old)[0]
        self.assertIn("no RUN step installs into the venv with pip --prefix", problems)
        self.assertTrue([problem for problem in problems if "pip --python" in problem], problems)


if __name__ == "__main__":
    unittest.main()

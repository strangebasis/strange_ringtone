# The opt-in fetcher image (docs/PLAN.md 6 and 6.1). Build from the repository root:
#   docker build -f fetcher/Dockerfile -t strange_ringtone-fetcher:local .
#
# One image, three commands: the fetcher (CMD, port 8080), which has no route
# out; the runner (python -I /app/fetcher/runner.py, port 8080), yt-dlp's
# only home; and the forwarder (python -I /app/fetcher/forwarder.py, port
# 3128), the only way to YouTube. The fetcher, the runner and the forwarder
# run on the base interpreter, which holds no third-party package. yt-dlp and
# yt-dlp-ejs from the hashed lock live in a venv of their own at /opt/yt-dlp,
# and only the yt-dlp the runner starts runs from it. Also Deno for YouTube's
# player JavaScript and the three core modules the fetcher's and the runner's
# code share. No ffmpeg: it downloads AAC in MP4 as it is, and the phone
# decodes it.

# Deno's official single-binary image (FROM scratch, the glibc build at
# /deno). A named stage pinned by digest, so Dependabot's docker updater sees
# and bumps it; nothing from it reaches the runtime but that one file.
FROM denoland/deno:bin-2.9.8@sha256:b507583d3c68ba69f3ed2ce5969ae5b8b70f43421c781279c7aea06435263543 AS deno

# The same base and digest as core/Dockerfile, so both images move together.
# util-linux's prlimit (the yt-dlp resource limits, 6) ships in this base
# image; the build fails if a base bump ever drops it.
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

# The contract's fixed, non-root UID and GID (5.7), the same as the core's.
# No home directory and no login shell: nothing runs as this user but the
# three commands and the yt-dlp the runner starts.
RUN test -x /usr/bin/prlimit \
    && groupadd --gid 10001 ringtone \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin --no-log-init ringtone

# Root owns everything copied in, so neither the servers nor yt-dlp can
# modify the code that runs, even if the root filesystem were writable. The
# fixed path is the one strange_ringtone_runner.ytdlp passes to --js-runtimes.
COPY --from=deno /deno /usr/local/bin/deno

# yt-dlp and yt-dlp-ejs live in a venv of their own (docs/PLAN.md 6.1): a
# wheel installed for the server's interpreter is code in the server (-I
# still runs .pth lines, and pip writes scripts and data files into the
# interpreter's prefix). The venv is filled by the base interpreter's pip
# through --prefix /opt/yt-dlp, which writes the wheels to the venv's own
# site-packages and their scripts to /opt/yt-dlp/bin (with the base
# interpreter's shebang; nothing runs them, and nothing puts that directory
# on a PATH), so no interpreter has the venv on its path at any point of the
# build: --python would have run pip under the venv's interpreter while it
# wrote the wheels. The venv is created without pip, so no installer ends up
# in it; --require-hashes --no-deps --only-binary :all: as before. Root owns
# it.
# The venv's bytecode is compiled now for the same reason as /app's below,
# by the base interpreter with -I -S: once the wheels are in, no build step
# may start the venv's interpreter, or any interpreter with site enabled on
# it, because site would run a malicious wheel's .pth lines here as root,
# where they could rewrite the base interpreter the fetcher, the runner and
# the forwarder run on (tests/test_venv_build_steps.py). Same binary, so the
# same cache tag. The lock stays in the image, so the in-image tests can
# compare what is installed with it (tests/image_fetcher).
COPY fetcher/requirements.txt /app/fetcher/requirements.txt
RUN python -m venv --without-pip /opt/yt-dlp \
    && python -m pip install --no-cache-dir --disable-pip-version-check \
        --prefix /opt/yt-dlp \
        --require-hashes --no-deps --only-binary :all: -r /app/fetcher/requirements.txt \
    && python -I -S -m compileall -q /opt/yt-dlp/lib \
    # The base interpreter runs the fetcher, the runner and the forwarder, so
    # it keeps no installer either: after this nothing third-party is
    # importable by it, and tests/image_fetcher proves it (I1).
    && python -m pip uninstall --yes --disable-pip-version-check pip

# Exactly the core modules the fetcher and the runner import
# (fetcher/__main__.py, fetcher/runner.py), at the path both put first on
# sys.path. The rest of the core, ffmpeg and the static files stay out:
# neither serves a page.
COPY core/strange_ringtone/__init__.py core/strange_ringtone/config.py core/strange_ringtone/http_guard.py /app/core/strange_ringtone/
COPY fetcher/__main__.py fetcher/forwarder.py fetcher/runner.py /app/fetcher/
COPY fetcher/strange_ringtone_fetch/ /app/fetcher/strange_ringtone_fetch/
# The forwarder, the image's second command (python -I /app/fetcher/forwarder.py,
# docs/PLAN.md 6.1): its own package beside the fetcher's, so the entry script
# finds it and nothing else.
COPY fetcher/strange_ringtone_forwarder/ /app/fetcher/strange_ringtone_forwarder/
# The runner, the image's third command (python -I /app/fetcher/runner.py,
# docs/PLAN.md 6.1): yt-dlp and Deno run only in its container.
COPY fetcher/strange_ringtone_runner/ /app/fetcher/strange_ringtone_runner/

# Bytecode is compiled now, because the root filesystem is read-only at run
# time and -I ignores PYTHONDONTWRITEBYTECODE (core/Dockerfile).
RUN python -m compileall -q /app

# No ENV for DENO_DIR, XDG_CACHE_HOME or proxies: the runner builds yt-dlp's
# whole environment itself (strange_ringtone_runner.ytdlp), so nothing set
# here would reach it, and an ENV would suggest the image's environment
# matters.
USER 10001:10001
# The fetcher's and the runner's port (each its own container) and the
# forwarder's; none is published here (compose.example.yaml decides who
# reaches which).
EXPOSE 8080 3128
# -I ignores PYTHON* variables and the user site, so the environment can't
# change which code runs. The directory form is explained in fetcher/__main__.py.
CMD ["python", "-I", "/app/fetcher"]

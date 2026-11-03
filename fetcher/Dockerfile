# The opt-in fetcher image (docs/PLAN.md 6). Build from the repository root:
#   docker build -f fetcher/Dockerfile -t strange_ringtone-fetcher:local .
#
# The only component with internet access. It holds Python, yt-dlp and
# yt-dlp-ejs from the hashed lock, Deno for YouTube's player JavaScript, the
# fetcher package and the two core modules it shares. No ffmpeg: it downloads
# AAC in MP4 as it is, and the phone decodes it.

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
# server and the yt-dlp it starts.
RUN test -x /usr/bin/prlimit \
    && groupadd --gid 10001 ringtone \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin --no-log-init ringtone

# Root owns everything copied in, so neither the server nor yt-dlp can modify
# the code that runs, even if the root filesystem were writable. The fixed
# path is the one strange_ringtone_fetch.ytdlp passes to --js-runtimes.
COPY --from=deno /deno /usr/local/bin/deno

# The hashed lock, installed as root before USER. --require-hashes and
# --no-deps: exactly the two locked wheels, no resolver and nothing a
# package's metadata pulls in; --only-binary: no sdist's build code runs.
# The lock stays in the image, so the in-image tests can compare what is
# installed with it (tests/image_fetcher).
COPY fetcher/requirements.txt /app/fetcher/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
        --require-hashes --no-deps --only-binary :all: -r /app/fetcher/requirements.txt

# Exactly the core modules the fetcher imports (fetcher/__main__.py), at the
# path it puts first on sys.path. The rest of the core, ffmpeg and the static
# files stay out: the fetcher serves no page.
COPY core/strange_ringtone/__init__.py core/strange_ringtone/config.py core/strange_ringtone/http_guard.py /app/core/strange_ringtone/
COPY fetcher/__main__.py /app/fetcher/
COPY fetcher/strange_ringtone_fetch/ /app/fetcher/strange_ringtone_fetch/

# Bytecode is compiled now, because the root filesystem is read-only at run
# time and -I ignores PYTHONDONTWRITEBYTECODE (core/Dockerfile).
RUN python -m compileall -q /app

# No ENV for DENO_DIR, XDG_CACHE_HOME or proxies: the server builds yt-dlp's
# whole environment itself (strange_ringtone_fetch.ytdlp), so nothing set here
# would reach it, and an ENV would suggest the image's environment matters.
USER 10001:10001
EXPOSE 8080
# -I ignores PYTHON* variables and the user site, so the environment can't
# change which code runs. The directory form is explained in fetcher/__main__.py.
CMD ["python", "-I", "/app/fetcher"]

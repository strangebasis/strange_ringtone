# The core image (docs/PLAN.md 5.3 and 5.7). Build from the repository root:
#   docker build -f core/Dockerfile -t strange_ringtone:local .
#
# Stage 1 builds a minimal ffmpeg from the pinned, verified release tarball.
# Stage 2 is the runtime: Python, the core package, that ffmpeg and nothing
# else the core needs from the network or a package manager.

# Same Debian release as the runtime stage, so the dynamically linked glibc the
# binary needs is the one the runtime has. Pinned by digest; Dependabot bumps it.
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS ffmpeg

# pipefail and -u: a failed download or a typo in a variable stops the build
# instead of configuring something else.
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

# gnupg dearmors the committed key for gpgv; xz-utils lets tar read .tar.xz.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates curl gnupg xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Only these four inputs, so the cached ffmpeg layers survive edits anywhere
# else in the repository.
COPY core/ffmpeg/VERSION core/ffmpeg/SHA256SUMS core/ffmpeg/ffmpeg-devel.asc core/ffmpeg/configure.txt ./

# The tarball is trusted only after two independent checks: the SHA-256
# committed when the version was reviewed, and the signature by FFmpeg's
# release signing key (the committed key; its primary key signs releases).
# gpgv's own exit status is not enough, because it accepts a signature from
# any key in the keyring. The VALIDSIG status line's last field is the
# primary fingerprint of whichever key made the signature, so requiring
# FFmpeg's there also covers a signing subkey added later. VERSION
# becomes part of a URL and a path, so it may hold only a release number.
RUN version="$(cat VERSION)"; \
    [[ "$version" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || { echo "bad VERSION: $version" >&2; exit 1; }; \
    tarball="ffmpeg-${version}.tar.xz"; \
    for file in "$tarball" "$tarball.asc"; do \
        curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
            --retry 3 --output "$file" "https://ffmpeg.org/releases/$file"; \
    done; \
    sha256sum --strict --check SHA256SUMS; \
    GNUPGHOME="$(mktemp -d)"; export GNUPGHOME; \
    gpg --batch --dearmor --output ffmpeg-devel.gpg ffmpeg-devel.asc; \
    gpgv --keyring /build/ffmpeg-devel.gpg --status-fd 1 "$tarball.asc" "$tarball" > gpgv.status; \
    awk '$1 == "[GNUPG:]" && $2 == "VALIDSIG" && $NF == "FCF986EA15E6E293A5644F10B4322F04D67658D8" { ok = 1 } END { exit !ok }' gpgv.status \
        || { echo "signature is not from FFmpeg's release key" >&2; cat gpgv.status >&2; exit 1; }; \
    tar -xJf "$tarball"; \
    mv "ffmpeg-${version}" src

# configure gets exactly the committed lines. Anything that is not a comment
# or a plain --flag[=value] stops the build, so configure.txt can never smuggle
# in a shell word, an environment variable or a second flag on one line.
RUN if grep -Evn '^(#.*|--[a-z0-9-]+(=[a-z0-9_,]+)?)$' configure.txt; then \
        echo "configure.txt has a line that is not a comment or a single flag" >&2; exit 1; \
    fi; \
    mapfile -t flags < <(grep -v '^#' configure.txt); \
    [[ "${#flags[@]}" -gt 0 ]]; \
    cd src; \
    ./configure "${flags[@]}"; \
    make -j"$(nproc)"; \
    strip --strip-all ffmpeg; \
    install -D -m 0755 ffmpeg /out/ffmpeg; \
    install -D -m 0644 libavcodec/parser_list.c /out/parser_list.c

# The runtime. util-linux's prlimit (the render's resource limits, 5.3) ships
# in this base image; the build fails if a base bump ever drops it.
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

# A fixed, non-root UID and GID are part of the deployment contract (5.7), so
# hosts can grant or deny file access by number. No home directory and no
# login shell: nothing runs as this user but the server.
RUN test -x /usr/bin/prlimit \
    && groupadd --gid 10001 ringtone \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin --no-log-init ringtone

# Root owns everything copied in, so the server can't modify its own code or
# its ffmpeg even if the root filesystem were writable.
COPY --from=ffmpeg /out/ffmpeg /usr/local/bin/ffmpeg
# The committed build configuration, for the in-image tests that compare the
# binary against it (tests/image/test_build_config.py). ffmpeg has no -parsers
# option, so the build's own generated parser list comes along for the test.
COPY core/ffmpeg/configure.txt core/ffmpeg/components.txt /app/ffmpeg/
COPY --from=ffmpeg /out/parser_list.c /app/ffmpeg/
COPY core/__main__.py /app/core/
COPY core/strange_ringtone/ /app/core/strange_ringtone/
COPY core/static/ /app/core/static/

# Bytecode is compiled now, because the root filesystem is read-only at run
# time. Under -I the PYTHONDONTWRITEBYTECODE variable is ignored, so without
# this every import would try, and fail, to write a .pyc.
RUN python -m compileall -q /app/core

# Late in the file so a new version or fork URL rebuilds only this layer. An
# empty SOURCE_URL means the upstream repository (config.py).
ARG VERSION=dev
ARG SOURCE_URL=
ENV VERSION=${VERSION} \
    SOURCE_URL=${SOURCE_URL}

USER 10001:10001
EXPOSE 8080
# -I ignores PYTHON* variables and the user site, so the environment can't
# change which code runs. The directory form is explained in core/__main__.py.
CMD ["python", "-I", "/app/core"]

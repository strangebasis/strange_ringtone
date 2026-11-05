# CLAUDE.md

strange_ringtone is a small, self-hostable web tool that turns an audio clip
into an iPhone ringtone. It is open source first (AGPL-3.0-or-later).
Deployments consume it as a pinned container image from their own
infrastructure repositories, so **this repository stays generic**. The design
record is `docs/PLAN.md`. Once v0.1.0 ships, the README's "Deployment contract"
is authoritative.

## Working with the owner

- Before designing anything new, interview the owner **one question at a time**.
  Each question gets a recommended option and its trade-off. Settle the stack,
  the features and the release flow before writing code.
- Security comes before convenience. When they conflict, recommend the more
  restrictive option and say what it costs.
- Branch from `main` as `feat/…` or `fix/…`, open a PR, and never push to `main`.
  Commit subjects are short, imperative, and use conventional prefixes (`feat:`,
  `fix:`, `docs:`, `ci:`, `refactor:`, `!` for breaking), because release-please
  builds the CHANGELOG from them. The body explains *why*.

## Never in this repository

- Any deployment's hostnames, IP addresses, network names, domain,
  certificates, reverse-proxy config, VPN or tailnet details, or personal
  information. That includes names and emails in User-Agents, headers and
  sample data. Use `example.internal` placeholders.
- Secrets of any kind. Settings come from environment variables, and secrets
  from `*_FILE` variables with documented placeholders in `.env.example`. Never
  ship a usable default for a secret or a signing key.

## Architecture invariants

- **Core image:** no outbound network, ever. No URL fetching, no telemetry, no
  update checks, no CDN assets. It accepts **only PCM WAV**, which the browser
  makes from the trimmed selection, and checks it by content before ffmpeg runs.
  The bundled ffmpeg is a minimal source build (WAV in, AAC/MP4 out, `file`
  protocol only), and a test fails if its build configuration grows.
- **Fetcher image** (opt-in, the owner's explicit exception for YouTube): it
  accepts only an 11-character video ID and runs as three containers of one
  image. The **fetcher** (CMD, port 8080) is the API behind the reverse
  proxy. It never runs yt-dlp: it sends the **runner**
  (`python -I /app/fetcher/runner.py`, port 8080, at `FETCH_RUNNER_URL`) only
  the validated ID and a token of its own, and trusts nothing that comes
  back: capped JSON of a fixed shape, audio of exactly the declared length up
  to `FETCH_MAX_BYTES`, per-read and total deadlines, the metadata
  re-validated, no retry. The runner is the only container that runs yt-dlp
  (from the venv, as a subprocess) and Deno. It builds the URL itself, takes
  one job at a time, deletes its job directory in `finally` and the audio
  once collected, when the next job starts or after 60 s, and shares no
  network with the reverse proxy. Neither reaches the core. The browser is
  the only bridge. The **forwarder** (`python -I /app/fetcher/forwarder.py`,
  port 3128) is the only component with egress: a stdlib `CONNECT` gate that
  allows port 443 to `youtube.com`, its subdomains and the subdomains of
  `googlevideo.com`, decides everything before DNS, resolves IPv4 only and
  connects only to global unicast addresses it checked, within fixed byte,
  time and tunnel budgets. The fetcher (on `fetcher` and `jobs`) and the
  runner (on `jobs` and `egress`) run on internal networks only, which
  Docker's isolated gateway modes (IPv4 and IPv6) keep off the host, and the
  runner reaches YouTube through `HTTPS_PROXY` pointing at the forwarder.
  Both refuse to start with a default route unless
  `FETCH_ALLOW_DEFAULT_ROUTE=true`. The runner is its container's PID 1
  (`init: false`, stated) and makes itself non-dumpable. Before and after
  each yt-dlp run it kills every other process in its container
  (`kill(-1, SIGKILL)`) and reaps them. Where it can't (anywhere but PID 1)
  it refuses to start (exit 4) unless `FETCH_ALLOW_NO_SWEEP=true`, which is
  for a local run or a host that can't run it as PID 1, never for one that
  can. No test ever sends the real `kill(-1)` outside a throwaway container.
  Every container of the fetcher image turns forwarding off on every
  interface and refuses ICMP redirects (never by writing
  `net.ipv4.ip_forward`, which resets the redirect setting), and each has a
  `pids_limit` of at least twice its worst case (docs/PLAN.md 6.1).
- **Container contract:** a fixed non-root UID/GID, `read_only`, `cap_drop: [ALL]`,
  `no-new-privileges`, and one plain-HTTP port per container (8080 for the
  core, the fetcher and the runner, 3128 for the forwarder command).
  **Stateless:** it writes only to `/tmp` (tmpfs), and the forwarder writes
  nothing; results expire after a short TTL, and the runner keeps an audio
  file only until the fetcher collects it. There is no database, data
  directory or volume, and adding one needs the owner's decision (the library
  was dropped on 2026-10-25). Tone names stay out of URLs and logs.
  `GET /healthz` (the fetcher's is `/fetch/healthz`) returns 200 and reveals
  nothing (the forwarder serves no path; its health check is a TCP connect).
  A change to the env vars, limits, UID/GID, port or writable paths needs a
  breaking-change commit, so the CHANGELOG marks it BREAKING.
- **Web:** strict CSP (no inline script, no third-party origins), `nosniff`,
  `DENY` framing, and `Referrer-Policy: same-origin`. Every state change is a
  POST that requires exactly one `Origin` equal to `PUBLIC_ORIGIN`, failing
  closed. Escape all rendered text; the client uses `textContent` only.
  `X-Forwarded-For` is trusted only from `TRUSTED_PROXIES` (empty by default;
  the compose example pins the fetcher's to empty, and it never covers the
  `jobs` network).
- **Subprocesses** (ffmpeg, yt-dlp): an argument list, never a shell. Each gets
  a wall-clock timeout, `prlimit` resource limits, a fresh per-job directory, and
  server-generated names only. Clean up in `finally`. yt-dlp runs only in the
  runner container, from the image's venv
  (`/opt/yt-dlp/bin/python -I -m yt_dlp`), never from a server's
  interpreter, whose site-packages hold nothing; the build fills the venv
  with the base interpreter's `pip install --prefix`, never `pip --python`.

## Code and tests

- Python 3.12 with no newer language features, type hints, and
  `from __future__ import annotations`. The core, the fetcher server, the
  runner server and the forwarder are **stdlib-only**. yt-dlp and yt-dlp-ejs
  come from a hashed
  pip-tools lock (`--require-hashes --no-deps --only-binary :all:`) into the
  image's venv, never into the base interpreter's site-packages, and nobody
  hand-edits `requirements.txt`.
- Tests use stdlib `unittest`, not pytest. **Every security check has a test
  that fails when the check is removed.** Malformed-input tests also assert that
  no files are left behind.
- Close every file handle explicitly before cleanup, because Windows can't
  delete a file that is still open.
- The frontend is dependency-free ES modules with no build step.
- Comments explain *why*, especially for security settings.

## Commands

```bash
python -I -m unittest discover -s tests        # all tests, stdlib only
ruff check .                                   # ruff from .github/lint/requirements.txt (--require-hashes)
python -I tools/spike/phase0.py                # regenerate the iOS test tones into spike-out/ (needs local ffmpeg)
STRANGE_RINGTONE_COMPOSE_TESTS=1 python -I -m unittest discover -s tests/compose -t tests -v   # compose topology tests (built fetcher image, Docker Engine 28.0+, Docker Compose 2.27.1+)
```

To recompile ruff's lock, edit `.github/lint/requirements.in`, then run the
command below in Git Bash from the repository root. It uses the Python of the CI
runner, 3.12.

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "$(cygpath -w "$PWD/.github/lint"):/work" -w /work python:3.12-slim sh -c 'pip install -q pip-tools==7.5.3 && pip-compile --generate-hashes --strip-extras --output-file=requirements.txt requirements.in'
```

PR titles are checked by `scripts/check_pr_title.py`. Changes to the README's
"Deployment contract" are checked by `scripts/check_contract_change.py`, which
starts enforcing once `.release-please-manifest.json` records a release.

## Supply chain and CI

- Pin Actions by full commit SHA with a `# vX.Y.Z` comment, and base images by
  digest. No `curl | sh`. Dependabot covers pip, github-actions and docker,
  with a 7-day cooldown.
- Reusable workflows owned by the `strangebasisdevs` organisation, such as
  `strangebasisdevs/strange_security/.github/workflows/semgrep.yml`, are pinned
  by full commit SHA with a `# vX.Y.Z` comment like any other action, and
  Dependabot bumps them. They are not called at `@main`.
- **Security scanning belongs to `strangebasisdevs/strange_security`,** the
  organisation's one-stop security repository. `security_gate.yml` calls its
  Semgrep, Trivy and Gitleaks workflows, and this repository runs no security
  scans of its own. Fix gaps in those scans in strange_security, never with a
  local copy here (`docs/PLAN.md`, section 9.4).
- **Semgrep suppressions are allowed, narrowly** (owner's decision, 2026-10-25):
  policy stays central in strange_security (its `docs/SUPPRESSIONS.md` is the
  source of truth, and its check runs in the security gate); this is
  per-repository triage.
  - Only Semgrep findings qualify. Trivy and Gitleaks findings never get an
    inline suppression: a secret finding means rotate first, and a reviewed
    false positive goes in `.gitleaksignore`.
  - The rules (inline only, one rule per suppression, a one-line reason
    directly above, listed in the PR description) live in strange_security's
    `docs/SUPPRESSIONS.md`; read it there rather than restating it here.
  - For a subprocess such as ffmpeg, make the call safe first, then suppress:
    an argument list with no `shell=True`, a fixed program path never taken
    from input, only validated values and server-generated paths as
    arguments, and a timeout. The reason names those guards, and a test pins
    them.
- **`main` is protected by a ruleset:** changes go through a PR, squash-merge
  only, and the required checks are Ruff, Unit tests, Image tests,
  Conventional PR title, Deployment contract change, and the gate's Semgrep,
  Trivy and Gitleaks jobs. Fetcher image tests and Compose topology tests are
  to join them (`docs/PLAN.md`, section 9.2); the owner adds them by hand.
  Renaming a job means updating the ruleset in the same change. `v*` tags can
  be created, moved or deleted only by organisation admins.
- Workflows default to `contents: read`. Never use `pull_request_target` or
  `workflow_run` with secrets. Write scopes exist only in the release-please and
  publish jobs, plus `security-events: write` in the security gate, which the
  shared workflows need to upload SARIF.
- Every PR runs ruff, the tests (including in-image runs of the core and the
  fetcher images with `--network none`), the compose topology tests, the
  organisation security gate, the PR-title check and the contract-change check.
  Releases also scan the image and publish provenance and an SBOM.

## Local machine

Development happens on Windows with Git Bash and Docker Desktop. A throwaway
`docker run --rm --network none` with a unique name and no published ports is
fine, and so is the compose topology tests' stack (a unique project name, no
published ports, removed when they finish), including its throwaway networks,
a `--network host` probe that reads the host's interfaces or listens on one
test bridge's private address, and a root helper with only `NET_ADMIN` on
throwaway bridges. Don't publish ports on the LAN from this machine.

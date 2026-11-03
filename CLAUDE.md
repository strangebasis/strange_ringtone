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
- **Fetcher image** (opt-in, the owner's explicit exception for YouTube): the
  only component with egress. It accepts only an 11-character video ID and
  builds the URL itself. It never reaches the core. The browser is the only
  bridge.
- **Container contract:** a fixed non-root UID/GID, `read_only`, `cap_drop: [ALL]`,
  `no-new-privileges`, and one plain-HTTP port. **Stateless:** it writes only to
  `/tmp` (tmpfs), and results expire after a short TTL. There is no database,
  data directory or volume, and adding one needs the owner's decision (the
  library was dropped on 2026-10-25). Tone names stay out of URLs and logs.
  `GET /healthz` returns 200 and reveals nothing. A change to the env vars, limits, UID/GID, port or writable
  paths needs a breaking-change commit, so the CHANGELOG marks it BREAKING.
- **Web:** strict CSP (no inline script, no third-party origins), `nosniff`,
  `DENY` framing, and `Referrer-Policy: same-origin`. Every state change is a
  POST that requires exactly one `Origin` equal to `PUBLIC_ORIGIN`, failing
  closed. Escape all rendered text; the client uses `textContent` only.
  `X-Forwarded-For` is trusted only from `TRUSTED_PROXIES` (empty by default).
- **Subprocesses** (ffmpeg, yt-dlp): an argument list, never a shell. Each gets
  a wall-clock timeout, `prlimit` resource limits, a fresh per-job directory, and
  server-generated names only. Clean up in `finally`.

## Code and tests

- Python 3.12 with no newer language features, type hints, and
  `from __future__ import annotations`. The core is **stdlib-only**. The
  fetcher's dependencies come from a hashed pip-tools lock
  (`--require-hashes --no-deps --only-binary :all:`), and nobody hand-edits
  `requirements.txt`.
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
  Trivy and Gitleaks jobs.
  Renaming a job means updating the ruleset in the same change. `v*` tags can
  be created, moved or deleted only by organisation admins.
- Workflows default to `contents: read`. Never use `pull_request_target` or
  `workflow_run` with secrets. Write scopes exist only in the release-please and
  publish jobs, plus `security-events: write` in the security gate, which the
  shared workflows need to upload SARIF.
- Every PR runs ruff, the tests (including an in-image run with
  `--network none`, once the image exists), the organisation security gate, the
  PR-title check and the contract-change check.
  Releases also scan the image and publish provenance and an SBOM.

## Local machine

Development happens on Windows with Git Bash and Docker Desktop. A throwaway
`docker run --rm --network none` with a unique name and no published ports is
fine. Don't publish ports on the LAN from this machine.

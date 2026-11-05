# strange_ringtone

A small, self-hostable web tool that turns an audio clip into an iPhone
ringtone. You trim, fade and level a clip in the phone's browser, then hand it to
iOS 26's **Share → Use as Ringtone**. No GarageBand, no computer and no cable.

> **Status: pre-release.** There is nothing to deploy yet. v0.1.0 is being built
> in the open, and the design record is [docs/PLAN.md](docs/PLAN.md).

## How it works

1. Open the app in Safari on an iPhone running iOS 26 or later and pick an
   audio file (up to 10 minutes). The phone decodes it; nothing is uploaded
   yet. Where the server runs the optional fetcher, you can paste or share a
   YouTube link instead ([below](#from-a-youtube-link)).
2. The app pre-selects the loudest stretch, up to `MAX_CLIP_SECONDS` (30 s by
   default), and moves its start to a quiet moment. Drag the handles or nudge
   them by 0.1 s or 1 s. **Play selection** loops it the way a ringtone
   rings, with the fade-in and fade-out (toggles and 0 to 5 s sliders) and the
   loudness (a Softer to Louder slider from -24 to -9 LUFS, measured on the
   phone) applied as you change them.
3. Type a name and tap **Hear final**. Only the selection travels to the
   server, as plain 16-bit WAV; the server measures, fades, levels and
   encodes it, and the app plays the finished file back on a loop.
4. Tap **Get on this iPhone**, then **Use as Ringtone** in the share sheet.
   Where the share sheet isn't available, or doesn't offer it, use
   **Download .m4a** or **Download .m4r** and share the file from Safari's
   downloads list. (Whether the share sheet itself offers "Use as Ringtone" is
   still to be confirmed on a phone; the download path is.)

iOS names the tone after the file, so the name you type is the name Settings
shows. Finished tones stay on the server for a few minutes only; when one has
gone, the page says so, and **Hear final** makes it again.

### From a YouTube link

Where the deployment runs the optional fetcher image and sets
`FETCH_ENABLED=true` on the core, a **From YouTube** box appears above the
file picker. Paste a link and tap **Fetch**, or share the video to the
[Make Ringtone Shortcut](#ios-shortcut-make-ringtone), which opens the app with
the link.

- The page reads the link on the phone. It takes `watch?v=`, `youtu.be/`,
  `/shorts/`, `/live/` and `/embed/` links on `youtube.com`, `m.youtube.com`
  and `music.youtube.com`, or a bare 11-character video ID. **Only the ID is
  sent** to the fetcher, which builds the YouTube address itself.
- Videos can be up to 10 minutes long (`FETCH_MAX_SECONDS`, which must not be
  set above the page's 10-minute decode limit). A longer video is refused with
  a message saying so.
- The page asks for the job's status every 2 seconds, then downloads the
  audio once (the fetcher deletes it as it sends it) and decodes it on the
  phone like a picked file. From there it is the same trimmer, with the name
  field set to the video's title.
- The selection starts at the link's `t=` timestamp when it has one.
  Otherwise it goes on YouTube's "Most Replayed" stretch when the video has
  that heatmap, and otherwise on the loudest stretch, as for a file. Either
  way its start then moves to a quiet moment within a second.

With `FETCH_ENABLED` off the box stays hidden and the page never contacts
`/fetch/`, even when it is opened from the Shortcut.

## iOS Shortcut: Make Ringtone

The Shortcut puts the app on the share sheet of the YouTube app (and of
Safari). Build it once in the **Shortcuts** app:

1. Create a shortcut named **Make Ringtone**. In its details, turn on
   **Show in Share Sheet**, so its first action reads **Receive URLs from
   Share Sheet**.
2. Add **URL Encode** with the **Shortcut Input**, so the link can be put
   after `#yt=` intact.
3. Add **Open URLs** with
   `https://ringtones.example.internal/#yt=` followed by the **URL Encoded
   Text** variable. Use your deployment's `PUBLIC_ORIGIN` in place of
   `https://ringtones.example.internal`.

Then, in the YouTube app, tap **Share**, then **More**, then **Make
Ringtone**. Safari opens the app and the fetch starts by itself.

Privacy:

- The link travels in the address's fragment (the part after `#`), which the
  browser never sends to any server, so no proxy log shows which video was
  shared. The page also removes it from the address bar and the history
  before it fetches anything.
- `si=` share tracking and every other parameter are dropped on the phone;
  only the 11-character ID leaves it.
- YouTube sees the server's IP address fetch the video, the same as watching
  it. No account or cookies are used, and the phone itself makes no request to
  YouTube.
- The video's title becomes the tone's name. Like any tone name it never goes
  into a URL or a log.

## Design

- **The browser does the decoding.** It sends the server only the trimmed
  selection, as plain PCM WAV.
- **The server is small and strict.** It checks the WAV by content and encodes
  it with a minimal ffmpeg build: WAV in, AAC/MP4 out, with no network protocols
  compiled in.
- **It keeps no state.** Results live in a tmpfs for a few minutes and then
  disappear.
- **The core image never makes an outbound connection.**
- **YouTube fetching is a separate, opt-in image.** It accepts nothing but an
  11-character video ID; yt-dlp runs in a container of its own that shares no
  network with the reverse proxy, and it reaches the internet only through
  the forwarder, a command of the same image that allows `CONNECT` to
  YouTube's names on port 443 and nothing else.

## Repository layout

| Path | What |
| --- | --- |
| `docs/PLAN.md` | Research, decisions and the v0.1.0 plan |
| `core/` | The core server: the `strange_ringtone` package and its static files, the image's `Dockerfile`, and in `core/ffmpeg/` the pinned, verified ffmpeg build inputs (version, checksum, signing key, configure line, component allowlist) |
| `fetcher/` | The optional YouTube fetcher, a second image with three commands: the fetcher server (the `strange_ringtone_fetch` package, run through `__main__.py`), its runner (the `strange_ringtone_runner` package, run through `runner.py`: yt-dlp and Deno run only there) and its egress forwarder (the stdlib `strange_ringtone_forwarder` package, run through `forwarder.py`). Also its `Dockerfile` and the hashed lock for yt-dlp and yt-dlp-ejs (`requirements.txt`, compiled from `requirements.in`), which the image installs into a venv of their own at `/opt/yt-dlp`. The fetcher shares the core's request checks by importing them from `core/`, and the runner imports the core's setting parsers and request checks and the fetcher's settings, metadata whitelist and route check, never the fetcher's server; the forwarder imports nothing from any of them |
| `.env.example` | The core's settings and the optional fetcher's, with placeholders and defaults |
| `compose.example.yaml` | A hardened Compose example, with no published ports, the fetcher and its runner on internal networks only and the forwarder the runner's one way out |
| `.dockerignore` | An allowlist: only `core/` and `fetcher/` reach the build context the two images share |
| `tools/spike/phase0.py` | Generates the test tones that established the share sheet's rules |
| `tools/dev_fetch_stack.py` | An offline development stack on loopback: the core, the fetcher, its runner with a stub yt-dlp, and one origin, for trying the YouTube flow without touching YouTube |
| `scripts/` | Stdlib checks that CI runs: the PR title and contract checks on pull requests, and the weekly FFmpeg release watch |
| `tests/` | Stdlib `unittest` tests (the in-image suites in `tests/image/` and `tests/image_fetcher/`, the compose topology tests in `tests/compose/`, the ffmpeg and yt-dlp stubs in `tests/stubs/`), plus `node:test` unit tests for the browser's code in `tests/js/` |
| `.github/` | CI workflows, Dependabot, and hashed lockfiles for the CI tools |

## Development

The code targets Python 3.12 and uses the standard library only. Run the tests:

```bash
python -I -m unittest discover -s tests
```

The same command runs the JavaScript unit tests (`tests/js/`, with Node's
built-in `node:test`) when `node` is on `PATH` (Node 20 or newer; CI's runner
has it), and skips them otherwise. There is no `npm install`: the frontend is
plain ES modules with no dependencies and no build step.

Lint with the same ruff that CI uses, installed from its hashed lockfile:

```bash
python -m pip install --require-hashes --no-deps --only-binary :all: -r .github/lint/requirements.txt
```

```bash
ruff check .
```

Regenerate the spike tones. This needs a local ffmpeg with the `aac` and
`libmp3lame` encoders and the `ebur128` and `alimiter` filters:

```bash
python -I tools/spike/phase0.py
```

**Run locally.** Start the core server on loopback, then open
<http://127.0.0.1:8080/>:

```bash
PUBLIC_ORIGIN=http://127.0.0.1:8080 python -I core --bind 127.0.0.1
```

The other settings in [.env.example](.env.example) are read from the
environment the same way, and an invalid value stops the server at startup.
Uploads are written only under `/tmp/strange_ringtone` (on Windows, which is
for development only, under the user's temporary directory); `TMPDIR` does
not move it.
Without `--bind` the server listens on `0.0.0.0:8080`, which is meant for the
container; don't expose it on your LAN. `--bind` takes an IP address, never a
host name, so starting the server makes no DNS lookup. The command passes the `core`
directory rather than `-m strange_ringtone`: `-I` leaves the current directory
off `sys.path`, so `-m` finds the package only where it is installed, while a
directory that Python runs is always on `sys.path`.

**Build the image.** From the repository root:

```bash
docker build -f core/Dockerfile -t strange_ringtone:local .
```

The first build compiles ffmpeg and takes a few minutes. It downloads the
release named in `core/ffmpeg/VERSION` and refuses it unless both its SHA-256
(`core/ffmpeg/SHA256SUMS`) and FFmpeg's release signature (made with the
committed `core/ffmpeg/ffmpeg-devel.asc`) check out, then configures it with
exactly `core/ffmpeg/configure.txt`. `--build-arg VERSION=…` and
`--build-arg SOURCE_URL=…` bake those settings in; they default to `dev` and the
upstream repository.

**Run the in-image tests** in a throwaway container with the deployment
contract's restrictions: no network, a read-only root, only `/tmp` writable
(`--ipc none` and `--ulimit msgqueue=0` take away Docker's default `/dev/shm`
and POSIX message queues), no capabilities and no published ports. In Git Bash
on Windows:

```bash
MSYS_NO_PATHCONV=1 docker run --rm --name strange_ringtone-image-tests \
  --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --ipc none --ulimit msgqueue=0 \
  -e PUBLIC_ORIGIN=https://example.internal -e STRANGE_RINGTONE_IMAGE_TESTS=1 \
  -v "$(cygpath -w "$PWD/tests"):/tests:ro" \
  strange_ringtone:local python -I -m unittest discover -s /tests/image -t /tests -v
```

On Linux or macOS, drop `MSYS_NO_PATHCONV=1` and write `"$PWD/tests"` in place
of the `cygpath` call. These are the CI job **Image tests**. They check the
binary against `core/ffmpeg/components.txt`, which names every component and
why it is there, and the container against the contract. Outside the image
(`STRANGE_RINGTONE_IMAGE_TESTS` unset), `unittest discover -s tests` skips them.
`compose.example.yaml` shows the same restrictions as a Compose service.

**The fetcher.** The optional YouTube fetcher (`fetcher/`) is a second
server, with a runner beside it that needs yt-dlp and Deno, so the pair runs
for real only in its image. To try the whole YouTube flow on this
machine without any YouTube traffic, run `python -I tools/dev_fetch_stack.py`
and open <http://127.0.0.1:8080/>: it starts the core, the runner with the
stub yt-dlp (`tests/stubs/ytdlp_stub.py`), the fetcher talking to it with its
real client, and a proxy that sends `/fetch/` to the fetcher, all on
loopback. The dev stack needs no setting for that. Started on their own as
`python -I fetcher` or `python -I fetcher/runner.py`, the fetcher and the
runner each refuse to start (exit 3) where they find a default route, which
a development machine has, or can't read `/proc/net/route`, as on Windows;
set `FETCH_ALLOW_DEFAULT_ROUTE=true` for such a run, never in a deployment
whose egress the forwarder is meant to confine. The runner also refuses
(exit 4) where it is not PID 1, which it never is on a development machine,
because it can't sweep there; set `FETCH_ALLOW_NO_SWEEP=true` for such a
run, never in a deployment that can run it as PID 1.

The runner runs from a checkout too, on the fixed port 8080, with both
overrides:

```bash
FETCH_ALLOW_DEFAULT_ROUTE=true FETCH_ALLOW_NO_SWEEP=true python -I fetcher/runner.py --bind 127.0.0.1
```

Without the image's venv it fails every job with `failed`; its tests use the
stub. As for the core, `--bind` takes an IP address only, and without it the
runner listens on `0.0.0.0`, which is meant for the container. On Linux it
makes itself non-dumpable before it serves (see Runs with), so attaching a
debugger or profiler to a local runner needs root.

The forwarder, the runner's only way out in a deployment, runs from a
checkout too, on the fixed port 3128:

```bash
python -I fetcher/forwarder.py --bind 127.0.0.1
```

It reads no setting and writes no file. As for the core, `--bind` takes an
IP address only, and without it the forwarder listens on `0.0.0.0`, which is
meant for the container. A `CONNECT` it accepts makes a real DNS lookup and a
real connection, so its tests use a fake resolver and connector instead.

Build the fetcher image from the repository root:

```bash
docker build -f fetcher/Dockerfile -t strange_ringtone-fetcher:local .
```

The build installs exactly the wheels in `fetcher/requirements.txt`, a
hashed pip-tools lock compiled from `fetcher/requirements.in`, into a venv of
their own at `/opt/yt-dlp`, filled by the base interpreter's
`pip install --prefix`, so pip never runs under the venv's interpreter and
no interpreter has the venv on its path at any point of the build; the
wheels' scripts land in `/opt/yt-dlp/bin`, where nothing runs them. It copies
Deno from the official `denoland/deno` image pinned by digest. Only yt-dlp
runs from the venv (`/opt/yt-dlp/bin/python -I -m yt_dlp`), and only in the
runner. The base interpreter, which runs the fetcher, the runner and the
forwarder, keeps no third-party distribution at all, not even pip, so no
wheel's `.pth` line, script or data file reaches them. Once the wheels are
installed, no build step starts the venv's interpreter: the base interpreter
compiles their bytecode with `-I -S`, so a wheel's `.pth` line can't run as
root during the build either. To
change a pin, edit `requirements.in` (yt-dlp-ejs must stay at the version
yt-dlp's own metadata pins), then recompile in Git Bash from the repository
root:

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "$(cygpath -w "$PWD/fetcher"):/work" -w /work python:3.12-slim sh -c 'pip install -q pip-tools==7.5.3 && pip-compile --generate-hashes --strip-extras --output-file=requirements.txt requirements.in'
```

Run its in-image tests under the contract flags. The tmpfs is 96 MB, more
than either container's `/tmp`, because one container runs the fetcher's and
the runner's suites; `--network none` matters: one test starts the real
yt-dlp with the runner's exact arguments to prove the pinned version accepts
them, and one starts the runner command, and without a network each can
only fail a real fetch at name resolution. Every other fetch in the tests is
the stub's.

```bash
MSYS_NO_PATHCONV=1 docker run --rm --name strange_ringtone-fetcher-image-tests   --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,size=96m,mode=1777   --cap-drop ALL --security-opt no-new-privileges:true   --ipc none --ulimit msgqueue=0   -e PUBLIC_ORIGIN=https://example.internal -e STRANGE_RINGTONE_FETCHER_IMAGE_TESTS=1   -v "$(cygpath -w "$PWD/tests"):/tests:ro"   strange_ringtone-fetcher:local python -I -m unittest discover -s /tests/image_fetcher -t /tests -v
```

These are the CI job **Fetcher image tests**. They check the container
contract, that the venv's yt-dlp and yt-dlp-ejs are the lock and agree with
each other, that the base interpreter holds nothing third-party, that one
yt-dlp download range plus headroom fits the forwarder's per-tunnel cap, that
yt-dlp finds Deno and Deno runs under the runner's resource limits, that the
venv's scripts went to the venv and not the base prefix, that the forwarder
command answers refusals without any DNS lookup, that the fetcher finds no
default route under `--network none` and starts, and that the runner command
starts with no default route, answers `/healthz`, fails an offline fetch and
leaves its scratch empty, and loads only its own package and the modules it
shares. They also run the fetcher's, the runner's and the forwarder's unit
tests (`tests/test_fetcher_*.py`) again under the image's Python 3.12, and
there, where the test process is the container's PID 1, the one test that
runs the runner's real sweep: a run's leftovers (in yt-dlp's process group,
in a session of their own, and one that keeps forking to a new PID) are all
killed and reaped. Everywhere else the tests replace `kill` and `waitpid`,
because `kill(-1)` outside a throwaway container would kill every process
of the user. Outside the image
(`STRANGE_RINGTONE_FETCHER_IMAGE_TESTS` unset) they are skipped. On any
Linux, in the image or not, the runner's unit tests also check that a
process of the same UID can neither read the `/proc` entries of a process
that took the runner's non-dumpable step nor ptrace it.

**Run the compose topology tests.** They bring up `compose.example.yaml`
itself with the fetcher image built above and check the layout for real: the
fetcher and the runner have no default route and resolve no external name,
the runner reaches the forwarder and the fetcher can't, the forwarder
reaches neither fetcher address, nothing on the `fetcher` network reaches
the forwarder or the runner, nothing on `jobs` reaches the forwarder, the
forwarder is the only container with a route out, the fetcher and the
runner each refuse to start on that network unless
`FETCH_ALLOW_DEFAULT_ROUTE` is set, the Docker host has no address on
`fetcher`, `jobs` or `egress` (IPv4 or IPv6, also on a throwaway internal
network with IPv6 turned on and the example's options, against a control
with the IPv4 mode alone), the fetcher, the runner and the forwarder run
with IP forwarding off on every interface (IPv6 forwarding too, on that IPv6
network as well; the global `net.ipv4.ip_forward` reads 1 by design and
isn't checked), with every IPv4 `accept_redirects` value at 0 (`all`,
`default`, `lo` and each interface), a container with the forwarder's
settings routes nothing between two throwaway bridge networks while a
control router with forwarding on does, and the example's pids cap, which is
at least twice each one's worst case computed from the code's own limits,
the running forwarder refuses what it should and logs only the name and the
reason, and (with a loopback fake DNS in a `--network none` container) the
forwarder never asks a `search` domain.
Besides the compose project they start plain `docker run` containers and
those throwaway networks, all named after the run's unique project name and
removed afterwards; a `--network host` probe reads the host's interfaces,
the IPv6 control binds a listener to the control bridge's own address
only, and the routing check's helper runs as root with `NET_ADMIN` and no
other capability, only to set its own route to a listener container. From
the repository root:

```bash
STRANGE_RINGTONE_COMPOSE_TESTS=1 python -I -m unittest discover -s tests/compose -t tests -v
```

`STRANGE_RINGTONE_COMPOSE_IMAGE=<tag>` picks another image than
`strange_ringtone-fetcher:local`; it is never pulled or built. They need
Docker Engine 28.0 or newer (a test fails on an older engine: before 26.0 it
forwards DNS from internal networks, and before 28.0, which added the
bridge driver's `isolated` gateway modes for internal networks, it can't keep
the host off them), a Docker host whose kernel has IPv6 (the IPv6 test
creates internal IPv6 networks), and Docker Compose 2.27.1 or newer, because
`compose.example.yaml` sets `driver_opts` on its services' networks (the
tests' override file's `!reset` and `!override` tags need 2.24.4). Each run
uses a unique project name and a
temporary env file, so it never reads your `.env`, publishes no port, sends
nothing to YouTube or any other external host (every name it gives the
forwarder is refused before DNS), and tears its containers and networks down
afterwards. These are the CI job **Compose topology tests**. Without
`STRANGE_RINGTONE_COMPOSE_TESTS=1` they are skipped.

**Pull requests:** branch from `main` as `feat/…` or `fix/…`. PRs are
squash-merged, so their titles must be conventional-commit subjects (`feat:`,
`fix:`, `docs:`, `ci:`, `chore:`, with `!` for a breaking change). release-please
builds the changelog from them.

## Deployment contract

Not defined yet. v0.1.0 will define it here: the image, UID/GID, port, writable
paths, environment variables and limits. The draft is in
[docs/PLAN.md](docs/PLAN.md), section 5.7. After the first release, CI refuses any
change to this section unless the PR is marked as breaking.

### Fetcher image (opt-in)

Run it only if you want YouTube links (set `FETCH_ENABLED=true` on the core
too). It is the only part that needs the internet, and it never talks to the
core: the phone downloads the audio from the fetcher and sends the core only
the trimmed WAV, as for a picked file. The image runs as three containers: the
fetcher, on internal networks only; the runner, which runs yt-dlp and shares
no network with the reverse proxy; and the forwarder, the runner's one way
out.
Draft, from [docs/PLAN.md](docs/PLAN.md) sections 6 and 6.1.

| Item | Value |
| --- | --- |
| Image | `ghcr.io/<owner>/strange_ringtone-fetcher:<semver>`, consumed by digest, linux/amd64 |
| User | UID 10001, GID 10001 (fixed, non-root), the same as the core, for all three commands |
| Commands | The image's CMD, `python -I /app/fetcher`, is the fetcher. `python -I /app/fetcher/runner.py` is the runner and `python -I /app/fetcher/forwarder.py` the forwarder, each run from the same image as a container of its own (`command:` in `compose.example.yaml`). |
| Port | 8080/tcp for the fetcher and 8080/tcp for the runner (each in its own container), plain HTTP; TLS is the proxy's job. 3128/tcp for the forwarder command, plain HTTP that answers only `CONNECT`. All fixed. |
| Paths | The fetcher serves the `/fetch/` prefix itself: `GET /fetch/healthz`, `POST /fetch/api/jobs`, `GET /fetch/api/jobs/<id>` and `GET /fetch/api/jobs/<id>/audio`. The reverse proxy forwards every path under `/fetch/` to it unchanged, on the same origin as the core (no rewriting), and everything else to the core. The runner serves `GET /healthz`, `POST /jobs` and `GET /jobs/<token>/audio`, for the fetcher only; nothing forwards to it. The forwarder serves no path, and the reverse proxy never reaches it. |
| Runs with | The fetcher: `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, `ipc: none`, `ulimits: msgqueue: 0`, and a memory limit (`mem_limit`, 256 MB in `compose.example.yaml`; its `/tmp` pages count against it). The runner: the same flags and 512 MB: yt-dlp runs under `prlimit` for CPU time, file size, open files and processes, but with no address-space limit, because Deno reserves a huge virtual range, so the cgroup limit is what bounds memory. The runner must be its container's PID 1, its command run directly, with no `init: true` and no wrapper: before and after every yt-dlp run it sends `kill(-1, SIGKILL)`, which ends every other process of UID 10001 in its container, and reaps them, so nothing a run started (a child that left yt-dlp's process group included) reaches the next job or stays a zombie that counts against `pids_limit`. A `docker exec` or health check running in that container at that moment is killed too. A run can time that on purpose, or fill `pids_limit`, so that the health check fails and the runner reads unhealthy; nothing in `compose.example.yaml` acts on the runner's health status, and nothing should: don't restart the runner, or route around it, on its health status alone. Started as anything but PID 1 (under an init, a daemon's default included, which the example's stated `init: false` overrides), the runner can't sweep and refuses to start (exit 4), unless `FETCH_ALLOW_NO_SWEEP=true` (see below), which is for a local run or a host that can't run the runner as PID 1, never for a deployment that can. As PID 1 it says so on stderr at startup; with the override and no sweep it serves without the sweep and says that on stderr instead. On Linux the runner also makes itself non-dumpable before it serves, so no process of its UID, yt-dlp included, can ptrace it or read its `/proc/1/environ`, `maps` or `mem`, whatever the host's `kernel.yama.ptrace_scope`; code planted in the runner would otherwise outlive every sweep. A `docker exec` in that container can't read those entries either; `docker inspect` on the container still shows the environment the runner was started with. The forwarder: the same flags with no `/tmp` and a smaller memory limit (128 MB in `compose.example.yaml`). All three: forwarding off on every interface, ICMP redirects refused, and a cap on processes and threads, set as [Kernel and process settings](#kernel-and-process-settings) below describes. None of these settings needs a capability. |
| Egress | The fetcher needs none: it talks only to the runner, over `jobs`, at `FETCH_RUNNER_URL`, and trusts nothing it answers (every response is capped in size and time and the metadata re-validated). The runner needs none of its own: it refuses to start (exit 3) when its container has a default route, IPv4 or IPv6, or when it can't read or parse its route tables (see `FETCH_ALLOW_DEFAULT_ROUTE` below), unless `FETCH_ALLOW_DEFAULT_ROUTE=true`, and it reaches YouTube only through `HTTPS_PROXY`, which points at the forwarder. The fetcher refuses to start under the same rule. Only `http://` proxies work: yt-dlp is installed without `requests`, so it reaches a proxy through urllib (`CONNECT` for HTTPS). The forwarder needs outbound TCP 443 to `youtube.com`, its subdomains and the subdomains of `googlevideo.com`, and DNS for those names. It connects only to IPv4 global unicast addresses it checked, and it resolves a name only after the allowlist and its budgets have passed, so a refused name never reaches DNS. It looks the name up as an absolute name (with a trailing dot), so the `search` domains in the container's `/etc/resolv.conf`, which Docker copies from the host, never turn an allowed name into one in your own zone. It is the only container with a route out. |
| Networks | `fetcher`: the reverse proxy and the fetcher. `jobs`: the fetcher and the runner, and nothing else. `egress`: the runner and the forwarder, and nothing else. `outside`: the forwarder only, the one network with a route out. Every network but `outside` is `internal: true`, the core's `core` network included, and also sets `driver_opts: {com.docker.network.bridge.gateway_mode_ipv4: isolated, com.docker.network.bridge.gateway_mode_ipv6: isolated}`: `internal: true` alone still gives the Docker host an address on the bridge, through which a container reaches every service the host listens on. Set both modes: the IPv4 one covers IPv4 only, so if IPv6 is turned on for these networks (`enable_ipv6: true`, or the daemon's `default-network-opts`), the host gets an IPv6 address on the bridge again unless the IPv6 mode is set too. The IPv6 option is accepted, and does nothing, on a network without IPv6. The host then also has no route to these networks, so the reverse proxy must be a container attached to `core` (and `fetcher`): a proxy running on the host, or with `network_mode: host`, can't reach them. A proxy kept in another Compose project declares them as external networks under their real names, the project name plus the network name (`strange_ringtone_core` and `strange_ringtone_fetcher` with the example's `name:`), and addresses the fetcher as `fetcher:8080`, which resolves to its address on `fetcher`; a proxy that discovers backends from container labels must be told to use the `fetcher` network, because the fetcher also has an address on `jobs`, which the proxy can't reach. Docker Engine 28.0 or newer: older engines forward DNS from internal networks (CVE-2024-29018, fixed in 26.0), which would let the fetcher or the runner resolve names without the forwarder, and lack the `isolated` gateway modes, which Engine 28.0 added for internal networks (moby/moby#49262). With Compose, Docker Compose 2.27.1 or newer, for the per-network `driver_opts` that turn forwarding off and refuse ICMP redirects on each interface (see Runs with). The forwarder listens on all its interfaces, so the Docker host can reach port 3128 on its `outside` address, and the runner can reach it on `egress`: nothing but the forwarder may join `outside`. The runner listens on all its interfaces too, so the forwarder can reach its port 8080 over `egress`, where it answers only a well-formed job request and runs yt-dlp through the forwarder itself. The fetcher listens on all its interfaces, so the runner, and anything a compromised yt-dlp runs in its container, can reach the fetcher's port 8080 over `jobs`: there the Origin check applies (the public origin is no secret, so it keeps browsers on other origins out, not the runner), the per-client rate limit keys on the runner's address, and no job ID is ever sent to the runner, so it can read or collect no result but its own submissions'. `compose.example.yaml` therefore sets `TRUSTED_PROXIES: ""` in the fetcher's own `environment`, which wins over the shared `.env`: the fetcher trusts no proxy and keys its rate limit on the socket peer, so behind a reverse proxy every client shares one `RATE_LIMIT_FETCHES_PER_MINUTE`. If you override that, list only the reverse proxy's own addresses and never a range that covers `jobs`, or the rate limit would trust an `X-Forwarded-For` the runner wrote; and Docker picks the subnets of `fetcher` and `jobs` from its address pools when it creates them and may pick others when it recreates them (after `docker compose down`, say), so an address that is the proxy's today can be the runner's tomorrow. What a compromised runner gains there, submitting fetch jobs that come back to itself, adds nothing to what it can already do through the forwarder; binding the fetcher to its `fetcher` address alone would need a fixed subnet and address in the example (docs/PLAN.md 6.1, Accepted risks). |
| Writable | The fetcher: `/tmp` (tmpfs) and nothing else, sized `2 × FETCH_MAX_BYTES`: one download arriving from the runner and one finished result waiting to be collected. 80 MB with the defaults. The runner: `/tmp` (tmpfs) and nothing else, sized `FETCH_MAX_BYTES + 16 MB`: one download in flight or one audio waiting to be collected (never both), the info JSON and Deno's cache. 56 MB with the defaults. The forwarder: nothing, not even `/tmp`. No volumes, and nothing survives a restart. |
| Results | One job runs at a time, with up to 4 more queued (503 `busy` beyond that). At most one finished, uncollected result is held: when a newer download finishes, an older one still waiting is deleted, and its page gets a 404. A result being downloaded by its page counts against the same budget: while one is still streaming, the next download starts only once the older waiting result is gone, and a stream that would push a third file onto `/tmp` is cut short. A result is deleted when the page collects it, and in any case `FETCH_RESULT_TTL_SECONDS` after it finished. The runner holds an audio file only until the fetcher collects it, which it does at once, and deletes it anyway when the next job starts or 60 seconds after the run. |
| Health | The fetcher: `GET /fetch/healthz` returns 200 with the body `ok` and nothing else. The runner: `GET /healthz`, the same; a check that happens to run at a run boundary is killed by the runner's sweep (see Runs with). `retries: 3` absorbs a coincidence, not a run that times its exit to the checks, so the runner's health status is a hint for a person, not a signal to act on automatically; nothing in `compose.example.yaml` acts on it. The forwarder: it accepts a TCP connection on 3128 and answers nothing until a request arrives, and a connection closed before its first byte is not logged (`compose.example.yaml` shows the check). |
| Logs | The fetcher and the runner each write one line per request: method, path with job IDs and tokens masked, status and duration, never a video ID, a title or an address; the runner never writes yt-dlp's output. The forwarder writes one stderr line per refused request or cut tunnel, `forwarder refused <reason> <name>` or `forwarder cut <reason> <name>`, and nothing for a tunnel that ends normally. `-` stands for a name that didn't parse as a host name. It never logs payload, headers or anything else a client sent. It writes at most `log_lines_per_minute` lines in any 60-second window, the `forwarder log suppressed <n>` lines included (see the limits below): it drops the rest and reports how many with one such line once the window has room for it; and an unexpected error is one `forwarder error <type>` line. It also writes `strange_ringtone_forwarder listening on <bind>:3128` when it starts, and `strange_ringtone_forwarder: cannot start: <error>` (exit 1) when it can't bind. Every service in `compose.example.yaml` rotates its log (json-file, `max-size: 10m`, `max-file: 3`), because Docker keeps every line otherwise; do the same, or set the daemon's defaults. |

`.env` is shared by the fetcher and the runner; each reads the rows that name
it.

| Variable | Default | Notes |
| --- | --- | --- |
| `PUBLIC_ORIGIN` | *(required)* | Fetcher. The same value as the core's. Every `POST` must carry exactly one `Origin` equal to it. |
| `TRUSTED_PROXIES` | empty | Fetcher. As for the core: CIDRs whose `X-Forwarded-For` is honoured for the per-client rate limit. `compose.example.yaml` sets it to empty on the fetcher service, which wins over `.env`, so the value in `.env` is the core's alone and the fetcher trusts no proxy. If you override that, never list a range that covers `jobs`, where the runner's requests come from: Docker may give `fetcher` and `jobs` other subnets when it recreates them (see Networks). |
| `FETCH_MAX_SECONDS` | 600 | Runner. Longest video, 1 to 600. Must not exceed the page's 10-minute decode limit, or a video is downloaded only for the phone to refuse it. |
| `FETCH_MAX_BYTES` | 41943040 (40 MiB) | Both. Largest audio download, 1 MiB to 512 MiB: the fetcher caps what it accepts from the runner, the runner caps what yt-dlp downloads; size each `/tmp` from it. |
| `FETCH_TIMEOUT_SECONDS` | 120 | Both. The runner's wall clock per yt-dlp run, 1 to 600; the fetcher waits that long plus 15 s for the runner's answer, so set it once, in the shared `.env`. |
| `RATE_LIMIT_FETCHES_PER_MINUTE` | 5 | Fetcher. Per client IP, counted on accepted jobs. With the example's empty `TRUSTED_PROXIES` on the fetcher, a client behind the reverse proxy counts as the proxy's address, so every client shares this limit. |
| `FETCH_RESULT_TTL_SECONDS` | 600 | Fetcher. How long a finished result waits to be collected |
| `FETCH_ALLOW_DEFAULT_ROUTE` | false | Both. `true` turns off the start check that refuses a container with a default route (IPv4 or IPv6). Set it only where egress is filtered another way (a NetworkPolicy, a firewall). The check also refuses when `/proc/net/route` is missing or can't be read or parsed, or when `/proc/net/ipv6_route` exists but can't be read or parsed (a missing `ipv6_route` means a kernel without IPv6, so no IPv6 routes). `/proc/net/route` shows the IPv4 main table only, so a default route in another table that an `ip rule` selects (policy routing, as some network plugins and VPN clients set up) passes the check; Docker's bridge driver sets up none. The check looks for a `/0` route only: routes that together cover every address without one, such as a VPN client's `0.0.0.0/1` and `128.0.0.0/1`, pass it, and an IPv4 `unreachable default` route counts as a default route (IPv6 skips reject routes). |
| `FETCH_ALLOW_NO_SWEEP` | false | Runner only. Where the runner can't kill and reap what a yt-dlp run leaves behind (it is not its container's PID 1, as under an init, or the platform lacks `kill` and `waitpid`), it refuses to start (exit 4); `true` lets it serve without that sweep instead, and it says so on stderr. Set it only for a local run or on a host that can't run the runner as PID 1, never in a deployment that can: without the sweep, a child of yt-dlp that left its process group can keep running between jobs, with the reach of the runner's UID in its container, and the zombies of killed processes count against `pids_limit` for good. As PID 1 the runner sweeps whatever this says. A value other than the boolean words is a configuration error (exit 2). `compose.example.yaml` never sets it. |
| `FETCH_RUNNER_URL` | `http://fetcher-runner:8080` | Fetcher only. Where the runner is: `http://host[:port]`, nothing else, no credentials. The default is the compose service; a Kubernetes deployment names its service here. The fetcher treats whatever answers as untrusted, so this changes where fetches go, not what the fetcher accepts. |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` | unset | Runner only. Passed to yt-dlp, and the only part of the runner's environment it sees. `compose.example.yaml` sets `HTTPS_PROXY` to `http://fetcher-forwarder:3128` on the runner service, which wins over `.env`; a deployment that gates egress another way may point it elsewhere. `http://host:port` only, with no credentials. |

#### Kernel and process settings

The fetcher, the runner and the forwarder all set these (the "Runs with" row
above); `compose.example.yaml` shows them in place.

- **Forwarding off on every interface.** A new network namespace copies the
  host's IP forwarding, which Docker turns on, and all three containers sit
  on two networks, so their kernels would otherwise route packets between
  them, around the forwarder's gate. None forwards in the kernel: the
  forwarder relays in user space. The kernel forwards a packet only when the interface
  it arrived on has forwarding on, so set `net.ipv4.conf.default.forwarding`
  (interfaces created later) and `net.ipv4.conf.lo.forwarding` to `"0"` in
  `sysctls` (or `--sysctl net.ipv4.conf.default.forwarding=0`), and each
  attached interface's value through its network attachment (below).
- **Not the global switch.** Don't set `net.ipv4.ip_forward` or
  `net.ipv4.conf.all.forwarding`: writing that global switch makes the kernel
  reset `net.ipv4.conf.all.accept_redirects` to 1, and Docker writes a
  container's sysctls in no fixed order, so it would undo the redirect refusal
  below in most starts. Inside these containers `net.ipv4.ip_forward`
  therefore reads 1 by design; check the per-interface `forwarding` values,
  which read 0. IPv6 forwarding is already off in these containers, so set no
  IPv6 sysctl: it would stop a container from starting on a kernel without
  IPv6.
- **ICMP redirects refused.** A container whose interfaces don't forward is a
  host to Linux, which follows its router's redirects (RFC 1122), and the
  kernel takes a redirect when `all` or the receiving interface says 1 (only
  from a route's current gateway: the fetcher and the runner have none, the
  forwarder's is the Docker host on `outside`). Set `net.ipv4.conf.all.accept_redirects`,
  `net.ipv4.conf.default.accept_redirects` and
  `net.ipv4.conf.lo.accept_redirects` to `"0"` in `sysctls` (or
  `--sysctl net.ipv4.conf.all.accept_redirects=0` and so on).
- **Each network attachment.** On every network the container joins, set the
  attached interface's forwarding and redirects. In Compose (Docker Compose
  2.27.1 or newer), under each of the service's `networks`:
  `driver_opts: {com.docker.network.endpoint.sysctls: "net.ipv4.conf.IFNAME.forwarding=0,net.ipv4.conf.IFNAME.accept_redirects=0"}`.
  With `docker run`, for each network:
  `--network 'name=<network>,"driver-opt=com.docker.network.endpoint.sysctls=net.ipv4.conf.IFNAME.forwarding=0,net.ipv4.conf.IFNAME.accept_redirects=0"'`
  (the inner quotes keep the comma inside one field). Docker replaces `IFNAME`
  with the interface it creates for that network, whose name follows the
  attach order. Endpoint sysctls arrived in Docker Engine 27.0.1, and an
  interface's sysctl given through `--sysctl` (`net.ipv4.conf.eth0.*`) is
  deprecated there and refused by current engines.
- **A cap on processes and threads** (`pids_limit`, or `--pids-limit`),
  because `prlimit`'s process limit for yt-dlp counts per UID across every
  container that runs as 10001, not per container. `compose.example.yaml`
  sets 64 for the fetcher, at least twice its worst case of 21: the main
  thread, 16 request workers, the job worker and the sweeper, the health
  check, and the runner client's deadline timer. It sets 160 for the runner,
  at least twice its worst case of 74: the main thread, 4 request workers,
  the result sweeper, the health check, and one yt-dlp run of up to 67
  threads with Deno, sized for a host that reports 64 CPUs; nothing of an
  earlier run adds to that, because the runner kills and reaps whatever a
  run leaves (see Runs with). It sets 320 for
  the forwarder, at least twice its
  worst case of 156: the main thread, one per open connection up to
  `connections_max`, one per lookup still running, which `tunnels_per_minute`
  bounds, and the health check. A thread that can't start fails its request
  or connection closed.

#### Forwarder limits

The forwarder reads no setting: its limits are fixed in the image,
provisional values sized with headroom that a release may change. A test
compares this table with the code.

| Limit | Value | Meaning |
| --- | --- | --- |
| `head_max_bytes` | 4096 | Largest request head in bytes, its blank line included; a longer one gets 431 |
| `head_deadline_seconds` | 10 | Seconds from accepting a connection to the end of its request head; a slower one gets 408 |
| `connections_max` | 64 | Client connections open at once, counted before the head is read; one more is closed without a response |
| `tunnels_max_concurrent` | 8 | Tunnels open at once; one more gets 503 |
| `tunnels_per_minute` | 90 | New tunnels in any 60-second window; one more gets 429 |
| `upload_bytes_per_tunnel` | 262144 | Bytes (256 KiB) a client may send through one tunnel after the head; more cuts the tunnel |
| `upload_bytes_per_minute` | 4194304 | Bytes (4 MiB) clients may send through all tunnels in any 60-second window; more cuts the tunnel |
| `download_bytes_per_tunnel` | 16777216 | Bytes (16 MiB) one tunnel may carry back from YouTube; more cuts the tunnel |
| `idle_timeout_seconds` | 30 | Seconds a tunnel may carry no byte either way before it is cut |
| `lifetime_seconds` | 300 | Seconds from accepting a connection after which its tunnel is cut, however busy |
| `resolve_deadline_seconds` | 5 | Seconds the IPv4 lookup may take; a slower one gets 504 |
| `connect_timeout_seconds` | 10 | Seconds for each connection attempt to a checked address; attempts stop at the tunnel's lifetime, and the last one gets only what is left of it. When every address fails, the answer is 504 if the last attempt timed out or the lifetime ran out, and 502 otherwise |
| `log_lines_per_minute` | 600 | Log lines in any 60-second window, `forwarder log suppressed <n>` lines included; more are dropped and counted, and one such line reports them once the window has room for it |

How these relate to `FETCH_MAX_BYTES`: the pinned yt-dlp downloads
googlevideo audio in ranges of at most 10 MiB (`http_chunk_size`), one
`CONNECT` tunnel per range because it sends `Connection: close`. So
`download_bytes_per_tunnel` is a fixed 16 MiB, one range plus headroom, which
holds any `FETCH_MAX_BYTES` up to 512 MiB. It is deliberately not sized from
`FETCH_MAX_BYTES`: a compromised runner moves at most 16 MiB per tunnel. The
in-image tests read the range size from the installed yt-dlp and fail when one
range plus headroom no longer fits the cap, so a yt-dlp bump that changes it
fails CI before it fails fetches; the remedy is to raise the cap and this
table's row together. What a larger `FETCH_MAX_BYTES` consumes is
`tunnels_per_minute`: about `FETCH_MAX_BYTES / 10 MiB` tunnels for the audio,
plus roughly 6 to 8 for the watch page, the player JavaScript and the
innertube calls of the three default clients.

## Security

See [SECURITY.md](SECURITY.md).

## License

[AGPL-3.0-or-later](LICENSE). If you run a modified version for other people,
section 13 requires you to offer them its source. The app's **Source** link
exists for that.

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
- **YouTube fetching is a separate, opt-in image.** It is the only part with
  internet access, and it accepts nothing but an 11-character video ID.

## Repository layout

| Path | What |
| --- | --- |
| `docs/PLAN.md` | Research, decisions and the v0.1.0 plan |
| `core/` | The core server: the `strange_ringtone` package and its static files, the image's `Dockerfile`, and in `core/ffmpeg/` the pinned, verified ffmpeg build inputs (version, checksum, signing key, configure line, component allowlist) |
| `.env.example` | The core's settings, with placeholders and defaults |
| `compose.example.yaml` | A hardened Compose example, with no published ports |
| `.dockerignore` | An allowlist: only `core/` reaches the image build context |
| `tools/spike/phase0.py` | Generates the test tones that established the share sheet's rules |
| `scripts/` | Stdlib checks that CI runs: the PR title and contract checks on pull requests, and the weekly FFmpeg release watch |
| `tests/` | Stdlib `unittest` tests, plus `node:test` unit tests for the browser's audio code in `tests/js/` |
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
server that needs yt-dlp and Deno, so it runs for real only in its image. To
try the whole YouTube flow on this machine without any YouTube traffic, run
`python -I tools/dev_fetch_stack.py` and open <http://127.0.0.1:8080/>: it
starts the core, the fetcher with the stub yt-dlp (`tests/stubs/ytdlp_stub.py`)
and a proxy that sends `/fetch/` to the fetcher, all on loopback. Build its
image from the repository root:

```bash
docker build -f fetcher/Dockerfile -t strange_ringtone-fetcher:local .
```

The build installs exactly the wheels in `fetcher/requirements.txt`, a
hashed pip-tools lock compiled from `fetcher/requirements.in`, and copies
Deno from the official `denoland/deno` image pinned by digest. To change a
pin, edit `requirements.in` (yt-dlp-ejs must stay at the version yt-dlp's own
metadata pins), then recompile in Git Bash from the repository root:

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "$(cygpath -w "$PWD/fetcher"):/work" -w /work python:3.12-slim sh -c 'pip install -q pip-tools==7.5.3 && pip-compile --generate-hashes --strip-extras --output-file=requirements.txt requirements.in'
```

Run its in-image tests under the fetcher's contract flags. The tmpfs is
`2 × FETCH_MAX_BYTES + 16 MB`, and `--network none` matters: one test starts
the real yt-dlp with the fetcher's exact arguments to prove the pinned
version accepts them, and without a network it can only fail at name
resolution. Every other fetch in the tests is the stub's.

```bash
MSYS_NO_PATHCONV=1 docker run --rm --name strange_ringtone-fetcher-image-tests   --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,size=96m,mode=1777   --cap-drop ALL --security-opt no-new-privileges:true   --ipc none --ulimit msgqueue=0   -e PUBLIC_ORIGIN=https://example.internal -e STRANGE_RINGTONE_FETCHER_IMAGE_TESTS=1   -v "$(cygpath -w "$PWD/tests"):/tests:ro"   strange_ringtone-fetcher:local python -I -m unittest discover -s /tests/image_fetcher -t /tests -v
```

These are the CI job **Fetcher image tests**. They check the container
contract, that the installed yt-dlp and yt-dlp-ejs are the lock and agree
with each other, that yt-dlp finds Deno and Deno runs under the fetcher's
resource limits, and they run the fetcher's unit tests (`tests/test_fetcher_*.py`)
again under the image's Python 3.12. Outside the image
(`STRANGE_RINGTONE_FETCHER_IMAGE_TESTS` unset) they are skipped.

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
the trimmed WAV, as for a picked file. Draft, from
[docs/PLAN.md](docs/PLAN.md) section 6.

| Item | Value |
| --- | --- |
| Image | `ghcr.io/<owner>/strange_ringtone-fetcher:<semver>`, consumed by digest, linux/amd64 |
| User | UID 10001, GID 10001 (fixed, non-root), the same as the core |
| Port | 8080/tcp, plain HTTP. TLS is the proxy's job. |
| Paths | The fetcher serves the `/fetch/` prefix itself: `GET /fetch/healthz`, `POST /fetch/api/jobs`, `GET /fetch/api/jobs/<id>` and `GET /fetch/api/jobs/<id>/audio`. The reverse proxy forwards every path under `/fetch/` to it unchanged, on the same origin as the core (no rewriting), and everything else to the core. |
| Runs with | `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, `ipc: none`, `ulimits: msgqueue: 0`, and a memory limit (`mem_limit`, 512 MB in `compose.example.yaml`): yt-dlp runs under `prlimit` for CPU time, file size, open files and processes, but with no address-space limit, because Deno reserves a huge virtual range, so the cgroup limit is what bounds memory. |
| Egress | Needs outbound HTTPS to YouTube's hosts and nothing else. Restricting it to them (an allowlisting proxy, firewall rules) and blocking private address ranges is the deployment's job. It never needs to reach the core, so give it a network of its own shared only with the proxy (`compose.example.yaml`). It honours `HTTPS_PROXY`, but only `http://` proxies: yt-dlp is installed without `requests`, so it reaches a proxy through urllib (CONNECT for HTTPS). |
| Writable | `/tmp` (tmpfs) and nothing else, sized `2 × FETCH_MAX_BYTES + 16 MB`: one download in flight, one finished result waiting to be collected, the info JSON and Deno's cache. 96 MB with the defaults. No volumes, and nothing survives a restart. |
| Results | One job runs at a time, with up to 4 more queued (503 `busy` beyond that). At most one finished, uncollected result is held: when a newer download finishes, an older one still waiting is deleted, and its page gets a 404. A result is deleted when the page collects it, and in any case `FETCH_RESULT_TTL_SECONDS` after it finished. |
| Health | `GET /fetch/healthz` returns 200 with the body `ok` and nothing else |

| Variable | Default | Notes |
| --- | --- | --- |
| `PUBLIC_ORIGIN` | *(required)* | The same value as the core's. Every `POST` must carry exactly one `Origin` equal to it. |
| `TRUSTED_PROXIES` | empty | As for the core: CIDRs whose `X-Forwarded-For` is honoured for the per-client rate limit. |
| `FETCH_MAX_SECONDS` | 600 | Longest video, 1 to 600. Must not exceed the page's 10-minute decode limit, or a video is downloaded only for the phone to refuse it. |
| `FETCH_MAX_BYTES` | 41943040 (40 MiB) | Largest audio download, 1 MiB to 512 MiB. Size `/tmp` from it. |
| `FETCH_TIMEOUT_SECONDS` | 120 | Wall clock per yt-dlp run, 1 to 600 |
| `RATE_LIMIT_FETCHES_PER_MINUTE` | 5 | Per client IP, counted on accepted jobs |
| `FETCH_RESULT_TTL_SECONDS` | 600 | How long a finished result waits to be collected |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` | unset | Passed to yt-dlp, and the only part of the fetcher's environment it sees. `http://host:port` only, with no credentials. |

## Security

See [SECURITY.md](SECURITY.md).

## License

[AGPL-3.0-or-later](LICENSE). If you run a modified version for other people,
section 13 requires you to offer them its source. The app's **Source** link
exists for that.

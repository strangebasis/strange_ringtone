# strange_ringtone: research and v0.1.0 plan

Status: design agreed on 2026-10-24 (decisions in section 3). Milestones 0
to 5 are merged, and so is the fetcher's egress confinement (section 6.1,
decided on 2026-11-03), built in two PRs. The owner's phone tests of the
share sheet and the Shortcut are still open (section 12). The release
(milestone 6) is next.
This file is the design record; the README's "Deployment contract" becomes the
source of truth for operators once v0.1.0 ships.

## 1. Short answer

- **iOS 26 added Share → "Use as Ringtone" for audio files.** GarageBand,
  syncing through the Music library, and a computer are no longer needed.
  Tested on 2026-10-24 (section 12, Phase 0):
  - Clips up to **30.0 s** are accepted. 30.5 s is refused with "File Too Large:
    Choose a ringtone under 30 seconds".
  - `.m4a`, `.m4r` and `.mp3` all work.
  - iOS names the tone after the **filename** and ignores the title tag.
  - Custom tones also appear in the Text Tone list.
- **Installing a tone with no taps on the phone is impossible.** No public API,
  and no Shortcuts action, installs a ringtone. Tethered computer sync (Finder or
  Music on a Mac, iTunes on Windows, iMazing) needs no phone taps, but none of
  those can be scripted, and each needs the computer and a paired phone. A few
  taps on the phone is the minimum, and this project aims to reach it.
- **Target flow on the phone:**
  1. In the YouTube app, tap Share → **Make Ringtone** (an iOS Shortcut). The
     trimmer opens with the audio loaded and the hook pre-selected. Or open the
     web app and pick a file.
  2. Adjust the selection, the fades and the loudness, and preview the result.
  3. Tap **Get on this iPhone** to open the share sheet, then **Use as Ringtone**.
     That uses Web Share, which Phase 4 still has to test on the real site. The
     fallback was confirmed in Phase 0: download the file, open Safari's
     downloads list, tap the file, then Share → Use as Ringtone.
  4. Choose it in Settings → Sounds & Haptics → Ringtone, or assign it to a
     contact.

## 2. Research: ways to get a custom tone onto an iPhone

| Path | Phone taps per tone | Needs | Can it be automated? | Notes |
| --- | --- | --- | --- | --- |
| **A. This project + iOS 26 share sheet** | about 3 to 5 after trimming | iOS 26+, an HTTPS origin the phone trusts | Everything except the final tap | Recommended |
| B. GarageBand on iPhone | about 12 to 15 | any iOS | No | Fallback for iOS < 26. Import straight from Files (no Music-library sync), then My Songs → long-press → Share → Ringtone. GarageBand caps tones at 30 s. |
| C. Mac Finder or Music: drag an `.m4r` onto the phone | 0 on the phone | Mac, cable or Wi-Fi sync | No (no CLI) | Up to 40 s. Reported broken on some macOS 15 releases, working again via the Music view on 15.7. |
| D. iMazing (Mac/PC) | 0 on the phone | computer, paired phone | GUI only | Ringtone import is free. The paired computer gets backup-level access to the phone. Closed source. |
| E. A native iOS app | n/a | Apple Developer Program | n/a | Dead end: no public API installs ringtones. |
| F. Ringtone apps and websites | same as A or B | n/a | No | Ads and tracking, and they still end in the share sheet or GarageBand. |

Another finding: custom tones reportedly don't always survive a move to a new
iPhone. The server keeps nothing (decision 3), so keep your own copies of tones
you care about, for example in Files.

### What each path costs

| Path | Money | Ongoing cost |
| --- | --- | --- |
| A (self-hosted) | US$0. Runs on an existing home server. GitHub Actions and GHCR are free for public repos. Remote access can use a free WireGuard-based VPN tier (Tailscale's Personal plan is free). | Maintenance time: yt-dlp breaks when YouTube changes, plus ffmpeg bumps and Dependabot PRs |
| B (GarageBand) | US$0 | Taps per tone |
| C (Finder) | US$0, plus a Mac | Cable or Wi-Fi sync per tone, and flaky across macOS versions |
| D (iMazing) | Ringtones are free; the full licence lists at about US$35 (deals lower) | Closed source, plus a pairing trust relationship |
| E (native app) | US$99 a year | Can't do the job |

## 3. Decisions (interview, 2026-10-24)

| # | Question | Decision | Cost accepted |
| --- | --- | --- | --- |
| 0 | Interface | Mobile web first. A TUI may come later on the same HTTP API. Targets iOS 26+. | No terminal app in v0.1.0 |
| 1 | YouTube | **Opt-in fetcher sidecar in v0.1.0.** This is the owner's explicit exception to the no-URL-fetching rule, and it applies only to a *separate* image. The core image still never fetches. | One container with internet egress. yt-dlp churn. A Deno runtime. YouTube ToS gray area. |
| 2 | Length and type | `MAX_CLIP_SECONDS` defaults to 30, with a hard ceiling of 40. One AAC encode, offered as `.m4a` (share sheet) and `.m4r` (Finder, iMazing). | v0.1.0 "done" differs from the original brief's wording, so the contract documents both names. |
| 3 | Library | **No library (revised 2026-10-25).** The core is as stateless as it can be: no database, no data directory, and results deleted after a short TTL. A shared library sits on the Later list (section 14). The first answer, on 2026-10-24, was an opt-in library in v0.1.0. | No shared gallery. Re-getting a tone means trimming it again. |
| 4 | Decoding | **The browser decodes, and the server accepts PCM WAV only** (the trimmed selection). | Needs JavaScript. Only formats the browser can decode. CLI users convert to WAV first. |
| 5 | Web layer | **Stdlib-only Python 3.12** (`http.server`, `wave`), with no third-party runtime packages in the core | Hand-written routing and limits. Relies on the reverse proxy for slow-client timeouts. |
| 6 | ffmpeg | **Minimal build from a pinned source release**, WAV in and AAC/MP4 out, with no network protocols | Dependabot can't see it, so a scheduled job flags new releases. Slower CI builds. |
| 7 | Licence | **AGPL-3.0-or-later.** The full text, with the "how to apply" lines naming this project. | Section 13 of the licence: the UI must offer users the source (section 7.5). |
| 8 | Releases | **release-please** opens release PRs from conventional commits | The release-please job needs `contents` and `pull-requests` write, and commit prefixes are mandatory (section 9.3). |
| 9 | Tone names (2026-10-25) | **Readable Unicode.** iOS shows the download's filename as the tone name (section 5.6). | A bigger sanitiser and test set. Look-alike characters are possible, which is harmless for names on your own phone. |
| 10 | Fetcher egress (2026-11-03) | **The fetcher image confines itself.** yt-dlp runs in a runner container on internal networks only, and its one way out is the image's own CONNECT forwarder, which allows YouTube's hosts on port 443 within budgets (section 6.1). No TLS interception. | Two more containers from the fetcher image. The gate confines the fetcher to Google's front ends, not to YouTube alone. Docker Engine 28.0 or newer, the first with the isolated gateway mode that keeps the host off internal networks. |

Phase 0 confirmed decision 2: the share sheet accepts `.m4a`, `.m4r` and
`.mp3` up to 30.0 s. A `MAX_CLIP_SECONDS` above 30 only helps people who
install through Finder or iMazing.

### Defaults I chose (overrule any of these before Phase 1)

- linux/amd64 only for v0.1.0. arm64 can come later, because the ffmpeg build
  under QEMU is slow.
- The fetcher is served under `/fetch/` on the **same origin** as the core (the
  proxy routes that prefix), so the strict CSP and the Origin check hold. The
  core and the fetcher never talk to each other.
- The fetcher sends no cookies and uses no PO-token provider. If YouTube blocks
  the server's IP, the fetch fails with a clear message, and cookies (a
  credential) become a separate, later decision.
- The 7-day Dependabot cooldown also applies to yt-dlp. When YouTube breaks
  extraction, the maintainer opens a manual bump PR after reading the release
  notes.
- The tone name goes to `/api/render` in an `X-Tone-Name` header
  (percent-encoded UTF-8), not in the query string. Proxy access logs usually
  record query strings, and the name is often a song title.
- Fades default to 0.2 s in and 1.5 s out, each with its own toggle and a range
  of 0 to 5 s. Ringtones loop while ringing, so a fade-out plus a short fade-in
  makes the loop seamless and avoids clicks.
- Loudness normalisation is on by default. The baseline (`DEFAULT_TARGET_LUFS`)
  is −14 LUFS, with a slider from −24 (softer) to −9 (louder) in 0.1 LU steps
  (the page rounds a baseline with more decimals to one) and a true-peak
  ceiling of −1 dBTP. Phase 0 didn't compare it by ear with a built-in tone, so
  the baseline is adjusted later through the slider or the env var.
- The frontend has no dependencies: plain ES modules, with a canvas waveform
  written for this project. wavesurfer.js can be vendored later if the polish is
  worth the extra supply chain.

## 4. Architecture

```
 iPhone Safari (HTTPS origin the phone trusts)
 ┌────────────────────────────────────────────────────────┐
 │ decode (Web Audio) → waveform → select / fade / gain   │
 │ preview locally → encode the selection as PCM WAV      │
 └──────┬─────────────────────────────────┬───────────────┘
        │ POST /api/render  (audio/wav)   │ POST /fetch/api/jobs {video_id}
        │ GET  /api/result/<id>.m4a / .m4r│ GET  /fetch/api/jobs/<id>[/audio]
        ▼                                 ▼
 ┌──────────────────────────┐   ┌───────────────────────────────┐
 │ core image               │   │ fetcher image (opt-in)        │
 │ stdlib HTTP server       │   │ stdlib HTTP server            │
 │ WAV check → ffmpeg       │   │ yt-dlp + yt-dlp-ejs + Deno    │
 │ (minimal build, file I/O)│   │ egress: YouTube only          │
 │ no egress, internal net  │   │ never reaches the core        │
 │ stateless, /tmp tmpfs    │   │ /tmp tmpfs only               │
 └──────────────────────────┘   └───────────────────────────────┘
```

The browser is the only bridge. Audio from the fetcher reaches the core only as
WAV that the browser made, so the core's input surface is the same with or
without the fetcher.

Inside the fetcher, yt-dlp moves to a container of its own whose only way out
is the image's forwarder (section 6.1, milestone 5's follow-up).

## 5. Core image

### 5.1 Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | App page. Static HTML, config in escaped `data-*` attributes, no inline script. |
| GET | `/static/<version>/…` | JS, CSS and icons from an in-memory allowlist. The version segment is the image's `VERSION` build argument (`dev` outside a release), so `immutable` caching is safe: a release changes the URLs. |
| GET | `/healthz` | `200`, empty body |
| POST | `/api/render` | Body: `audio/wav`. Query: `fade_in`, `fade_out`, `normalize`, `target_lufs`. The name goes in the `X-Tone-Name` header. Returns JSON `{result_id, duration, measured_lufs, applied_gain_db}`. |
| GET | `/api/result/<id>.m4a` and `.m4r` | The rendered tone (attachment). `<id>` is 128 random bits and expires after `RESULT_TTL_SECONDS`. |

That is the whole API. Nothing else reads or writes state.

"Hear final" fetches `/api/result/<id>.m4a` once with `fetch()`, decodes the
bytes with `decodeAudioData` and loops them through Web Audio, with no fades or
gain of its own because the file already has them. The page learns
`RESULT_TTL_SECONDS` from a data attribute and drops its download links when
that time has passed (counted from the render request, so never later than the
server), so they never point at a tone the sweeper has deleted; the file in
memory still plays and shares, and "Hear final" then renders again. A tone
finished while the page is hidden waits for the next "Hear final" instead of
starting a loop in a background tab. An `<audio>` element
pointed at the attachment URL would not work: Safari's media loader sends
`Range` (and `HEAD`) requests, and the result route supports neither. The
same bytes become the `File` for Web Share (7.3), and nothing needs a `blob:`
URL, which the CSP's `'self'` does not cover. The download links point at the
result URLs directly.

### 5.2 Input checks, in order, before ffmpeg runs

1. The method and path are allowed, and the request has exactly one `Origin`
   equal to `PUBLIC_ORIGIN`. A missing `Origin` is refused (fail closed).
2. `Content-Type` is exactly `audio/wav`. This also forces a CORS preflight for
   any cross-origin attempt.
3. There is exactly one well-formed `Content-Length` and no `Transfer-Encoding`,
   and the length is at most `MAX_BODY_BYTES`. That limit is derived from
   `MAX_CLIP_SECONDS × 48000 × 2 ch × 2 B` plus 4 KiB, which is about 5.8 MB at
   30 s.
4. The per-client rate limit and the render concurrency limit have room.
5. The body streams into a fresh per-job directory under the scratch path, with
   a server-generated name.
6. Stdlib `wave` parses it, and a strict RIFF walk rejects duplicate `fmt `/`data`
   chunks or a declared size that disagrees with the file. Allowed: PCM s16le, 1
   or 2 channels, 44.1 or 48 kHz, duration from 0.5 s to exactly
   `MAX_CLIP_SECONDS`. The frame count may not exceed seconds × rate, with no
   tolerance, because iOS accepted 30.000 s and refused 30.5 s.
7. Only then does ffmpeg run, still with explicit `-f wav`.

The job directory is deleted in a `finally`. Tests prove that nothing is left
behind on any path (section 10).

### 5.3 Render pipeline (sketch; exact flags are checked against the pinned build)

ffmpeg runs two or three times per render, each time as a subprocess with an
argument list (never a shell). Every run is wrapped in the util-linux `prlimit`
binary (address space, CPU seconds, file size, open files), which execs into
ffmpeg, so the PID is unchanged. `preexec_fn` is not used: the stdlib documents
it as unsafe in a threaded process, and this server is threaded. The child
starts in its own session (`start_new_session=True`), `communicate()` enforces
`RENDER_TIMEOUT_SECONDS`, and on timeout the whole process group is killed with
`os.killpg` and reaped. `subprocess.run(timeout=…)` alone would kill only the
direct child. Each run uses `-nostdin -hide_banner -nostats -threads 1
-protocol_whitelist file -f wav -i <job>/in.wav`, and each render run adds `-y`,
because the corrective render overwrites `out.m4a`.

1. **Measure:** `-af ebur128=peak=true -f null -`. A strict regex reads the
   integrated loudness and true peak from the summary. If parsing fails, the
   render fails. The `null` muxer still encodes what it discards, with
   `pcm_s16le` by default, so that encoder is part of the build.
2. **Render:** `-af volume=<gain>dB,alimiter=limit=<ceiling>:level=false:latency=true,afade=t=in:…,afade=t=out:…,ebur128=peak=true`,
   then `-ar 44100 -map_metadata -1 -fflags +bitexact -metadata title=<sanitised>
   -c:a aac -b:a 256k -movflags +faststart -f ipod <job>/out.m4a`.
   `alimiter` defaults to `level=true`, which re-normalises its output and
   silently undoes the gain. `latency=true` keeps the clip length exact.
   `+bitexact` drops the encoder tag, so the title is the only metadata. iOS
   ignores the title tag and names the tone after the filename (Phase 0), so
   the title only matters to other players. The trailing `ebur128` measures
   the signal after the limiter and the fades in the same run that encodes
   it. The image cannot decode its own output (WAV in, AAC out, nothing
   else), so this post-chain measurement is the authority.
3. **Correct (when needed):** if the post-chain loudness misses the target by
   more than 0.5 LU, run step 2 once more from `in.wav` with the gain
   adjusted by the error. `measured_lufs` in the response is the post-chain
   value of the final render.

The gain is the target minus the measured loudness, clamped to ±20 dB. Louder
targets push the limiter harder, and the UI warns past −11 LUFS.

Measured in the Phase 0 spike (ffmpeg 8.1.1, synthetic loop, 2026-10-24):

- **Loudness:** a single pass landed within 0.2 LU of −14 and −11 LUFS. At −9 the
  limiter cost 0.5 LU (−9.5 measured). That is why the render pass measures
  its own output and the corrective pass exists.
- **True peak:** with the limiter at −1.0 dBFS, the AAC output measured −0.8 dBTP
  at −9 LUFS, because encoding adds inter-sample overs. With the ceiling at
  −1.5 dBFS every render stayed at or below −1.3 dBTP. At −9 LUFS that took the
  corrective pass (−9.7, then −9.3 LUFS), so −1.5 dBFS is the default. The
  production image never measures the true peak after encoding, because it
  has no AAC decoder; the −1.5 dBFS ceiling is the margin, and
  `tools/spike/phase0.py` with a full ffmpeg is the check when the ceiling or
  the encoder changes.
- **Duration:** the ipod muxer reports exact durations (29.900 s in, 29.900 s
  out), so the encoder's priming samples don't push a clip over the cap. The `.m4a` and
`.m4r` downloads are the same bytes. Only the name and `Content-Type` differ
(`audio/mp4` and `audio/x-m4r`).

**Minimal ffmpeg build:** a pinned release tarball, checked against a committed
SHA-256 and the FFmpeg release signature. Configure flags:
`--disable-everything --disable-network --disable-autodetect --disable-iconv
--disable-unstable --disable-doc --disable-ffplay --disable-ffprobe
--disable-x86asm --enable-static --disable-shared`, then enable only these.
`--disable-iconv` is needed because configure probes for the C library's
iconv even under `--disable-autodetect`; `--disable-unstable` leaves out
FFmpeg's experimental code; `--disable-x86asm` means the builder needs no
nasm, and audio encoding doesn't need the hand-written SIMD.

- demuxer `wav`
- decoder `pcm_s16le`
- encoders `aac` and `pcm_s16le` (the `null` muxer needs an encoder, 5.3 step 1)
- muxers `ipod` and `null`, plus what configure pulls in for `ipod`: the `mov`
  muxer, and through its `_select` line the bitstream filters `aac_adtstoasc`
  and `vp9_superframe` and the parser `ac3`. The committed allowlist names
  each of them, with the reason, so the test documents the whole binary.
- protocol `file`
- filters `ebur128`, `volume`, `alimiter`, `afade`, `aformat`, `aresample`,
  `anull`
- what `ffmpeg_select` pulls in for the ffmpeg tool's automatic audio and
  video graphs: the filter `atrim` and the video filters `crop`, `format`,
  `hflip`, `null`, `rotate`, `transpose`, `trim` and `vflip`, which WAV input
  can't reach
- libavfilter's always-built graph ends: `abuffer` and `abuffersink`, and the
  video `buffer` and `buffersink`, which are equally unreachable

`core/ffmpeg/components.txt` gives the reason for every one of these.

The licence stays LGPL-2.1+. A CI test compares `ffmpeg -buildconf` with the
committed configure line, then runs `-protocols`, `-demuxers`, `-decoders`,
`-encoders`, `-muxers`, `-bsfs` and `-filters` in the built image, reads the
parsers from the build's generated `parser_list.c` (ffmpeg has no `-parsers`
option), and fails on anything outside the committed allowlist
(`core/ffmpeg/components.txt`) and on anything listed there that is missing.

### 5.4 Fades and loudness preview

The browser previews everything immediately. Fades are quarter-sine gain
curves, the same shape as the server's `afade=curve=qsin`, drawn with one
`setValueCurveAtTime` per loop iteration. Loudness uses a small JS BS.1770 meter (K-weighting
filters, 400 ms blocks, absolute and relative gating) applied to the selection,
so the slider moves the preview gain live. The server's measurement is the
authority. "Hear final" plays the real render.

### 5.5 Results: the only state

- A result is the rendered `.m4a`, plus its sanitised name, held under the
  scratch tmpfs. It lives only long enough to be previewed and downloaded.
- A sweeper thread deletes results after `RESULT_TTL_SECONDS` (default 600),
  and startup clears the whole scratch path.
- At most `MAX_RESULTS` (default 50) results exist at once. When the cap is
  reached a render answers 503 with `Retry-After` until the sweep frees room.
  Nothing is evicted early: a result lives exactly its TTL, so the tmpfs has
  a known upper bound (5.7).
- There is no database, no data directory and no volume. A restart forgets
  everything.
- Logs record the method, the path without its query string, the status and the
  timing. They never record tone names or audio details.

### 5.6 Tone names (the download filename)

iOS shows the download's filename as the ringtone's name (Phase 0), so the name
the user types becomes the filename. Decision 9 keeps it readable:

- The name arrives in `X-Tone-Name` as percent-encoded UTF-8. There must be
  exactly one such header, at most 512 bytes long, that decodes to valid UTF-8.
  Otherwise the render is refused. A missing header means the name `ringtone`.
- NFC-normalise the name. Then keep only:
  - letters, combining marks and digits in any script (Unicode categories L, M
    and N)
  - spaces (any `Zs` becomes one space)
  - the punctuation `- _ ( ) ' . , & !`
  - emoji (`So`), plus ZWJ (U+200D) and VS16 (U+FE0F) only where they sit next
    to an emoji
- Everything else is dropped. That includes control and format characters
  (bidi overrides among them), path separators, `" * : < > ? |`, and every other
  quote mark.
- Collapse runs of spaces, and trim spaces and dots from both ends.
- Cap the name at 40 code points, then strip any dangling ZWJ, VS16 or
  combining mark left at the cut. If nothing is left, use `ringtone`.
- The header is `Content-Disposition: attachment; filename="<ASCII fallback>.m4a";
  filename*=UTF-8''<percent-encoded name>.m4a` (RFC 6266 and 8187). For the ASCII
  fallback, fold accents (NFKD, then drop marks) and replace anything else with
  `_`.
- The name is never used as a filesystem path. Results are stored under random
  IDs.
- Adding a tone that iOS already has fails with "Duplicate Ringtone: This file
  already exists in the ringtone library". Phase 0 didn't establish whether iOS
  matches on the name or on the content. The UI tells the user to delete the old
  tone (swipe left in Settings → Sounds & Haptics → Ringtone) or to pick a new
  name.

### 5.7 Draft deployment contract (becomes the README section)

| Item | Value |
| --- | --- |
| Image | `ghcr.io/<owner>/strange_ringtone:<semver>`, consumed by digest, linux/amd64 |
| User | UID 10001, GID 10001 (fixed, non-root) |
| Port | 8080/tcp, plain HTTP. TLS is the proxy's job. |
| Runs with | `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, `ipc: none`, `ulimits: msgqueue: 0`. No egress needed (internal network). In `compose.example.yaml` (from 6.1's PR A) that network is also isolated from the Docker host (`gateway_mode_ipv4: isolated` and `gateway_mode_ipv6: isolated`, both, so the host stays off it if IPv6 is ever turned on for it; they need Docker Engine 28.0 or newer, the release that added those modes), so the reverse proxy must be a container on it, not a process on the host, and the log rotates (json-file, `max-size: 10m`, `max-file: 3`). |
| Writable | `/tmp` (tmpfs) and nothing else. Docker would also give the container a writable `/dev/shm` tmpfs and POSIX message queues in `/dev/mqueue`; `ipc: none` and a zero `msgqueue` limit remove both. No volumes, and nothing survives a restart. Size it at `(MAX_RESULTS + RENDER_CONCURRENCY) × MAX_CLIP_SECONDS × 33 kB + RENDER_CONCURRENCY × MAX_BODY_BYTES`: a result is 256 kbit/s AAC, at most 33 kB per second of clip with the MP4 overhead, and each running render also holds its upload. That is 63 MB with the defaults (use 64 MB) and 84 MB with `MAX_CLIP_SECONDS=40`. |
| Health | `GET /healthz` returns 200 with an empty body |

| Variable | Default | Notes |
| --- | --- | --- |
| `PUBLIC_ORIGIN` | *(required)* | For example `https://ringtones.example.internal`. The server refuses to start without it. |
| `TRUSTED_PROXIES` | empty | CIDRs whose `X-Forwarded-For` is honoured. The rightmost untrusted hop is the client. The walk from the right stops at the first malformed hop, and without an untrusted hop before it (or when the header is missing or lists only trusted proxies) the socket peer is the client. Behind a proxy with this empty, every client shares the proxy's address and the per-client rate limit becomes global. |
| `MAX_CLIP_SECONDS` | 30 | 1 to 40 |
| `DEFAULT_TARGET_LUFS` | −14 | −24 to −9 |
| `RENDER_TIMEOUT_SECONDS` | 20 | Wall clock, per ffmpeg run |
| `RENDER_CONCURRENCY` | 2 | |
| `RATE_LIMIT_RENDERS_PER_MINUTE` | 10 | Per client IP |
| `RESULT_TTL_SECONDS` | 600 | |
| `MAX_RESULTS` | 50 | Results held at once; 503 when full (5.5) |
| `FETCH_ENABLED` | false | Only shows the YouTube box in the UI. The core never calls the fetcher. |
| `SOURCE_URL` | baked at build | The AGPL source link. Forks override it. |
| `VERSION` | baked at build, `dev` outside a release | The `/static/<version>/` path segment (5.1): 1 to 64 letters, digits, `.`, `_` or `-`, starting with a letter or digit |

The core has no secrets in v0.1.0, so `.env.example` holds only
`PUBLIC_ORIGIN=` and commented optional settings. Any later secret arrives as
`*_FILE`, with no usable default. The same file carries the optional fetcher's
settings in a block of their own, all commented out; `compose.example.yaml`
gives the core, the fetcher and its runner that one `.env` (the forwarder
reads none), and each server reads only its own variables (`PUBLIC_ORIGIN`
is shared by the core and the fetcher; both read `TRUSTED_PROXIES`, but the
example sets the fetcher's to empty in its own `environment`, which wins
over `.env`, so the value in `.env` is the core's (6.1); the runner reads
neither).

The fetcher image is a second, separate contract with its own table in the
README (section 6, "Fetcher contract"). Nothing in it changes the core's: the
core still needs no egress, and `FETCH_ENABLED` only shows the YouTube box.

## 6. Fetcher image (opt-in)

Section 6.1, decided on 2026-11-03, moves yt-dlp into a container of its own
and gives the image its own egress gate. Its two PRs have landed: the
bullets below describe the image as of PR B, with yt-dlp in the runner.

- **Input:** JSON `{video_id}` only. The ID must match `^[A-Za-z0-9_-]{11}$`.
  The fetcher validates it and sends it to the runner, which validates it
  again and builds `https://www.youtube.com/watch?v=<id>` itself. No URL a
  user typed ever reaches yt-dlp, which removes the SSRF path (yt-dlp's
  generic extractor would otherwise fetch anything).
- **Invocation:** `/opt/yt-dlp/bin/python -I -m yt_dlp`, the interpreter of
  the image's venv (never a server's own, see Runtime), run by the runner as
  a subprocess with an argument list. The flags were checked against the pinned yt-dlp 2026.10.26, and an
  in-image test runs the real yt-dlp with the exact list (offline, so it can
  only fail at name resolution) to prove that version accepts every one:
  - `--ignore-config --no-playlist --use-extractors youtube`
  - `--js-runtimes deno:/usr/local/bin/deno` (the image's fixed path) and
    `--no-cache-dir`
  - `-f "bestaudio[ext=m4a]"` (AAC, which Safari decodes, so the fetcher needs
    no ffmpeg)
  - `--break-match-filters "duration <= FETCH_MAX_SECONDS & !is_live"`, not
    `--match-filter`: a plain filter skips the video and exits 0 having
    written nothing, while the breaking one exits 101, which is how
    `too_long` is told apart
  - `--max-filesize FETCH_MAX_BYTES` (checked before the download; a larger
    file is skipped with exit 0 and no audio, which maps to `failed`)
  - `--write-info-json --quiet --no-warnings --no-progress --socket-timeout 20`
  - `-P <job directory> -o "%(id)s.%(ext)s"`, so file names come from the
    validated ID only, then `--` and the URL the server built

  It runs in the runner container under `prlimit` and a wall-clock timeout
  (`communicate()`; on timeout the whole process group is killed and the
  pipes drained), in its own session, with every leftover killed and reaped
  at both run boundaries (6.1, the runner bullet), one job at a time: the fetcher's queue
  (`FETCH_CONCURRENCY=1`, with 4 more queued and 503 `busy` beyond, and its
  own per-IP rate limit) and the runner's own lock. The runner's `prlimit`
  set is CPU seconds, file size, open files and process count
  (512, because `RLIMIT_NPROC` counts every thread of UID 10001, V8's
  included), with no address-space limit: Deno reserves a huge virtual
  address space at startup, so `RLIMIT_AS` would kill it. Memory is bounded
  by the deployment's cgroup limit (`mem_limit` in `compose.example.yaml`),
  which the contract documents as the operator's job. yt-dlp's environment
  is built from scratch by the runner: fixed values, `HOME` and `DENO_DIR`
  inside the job directory under `/tmp` (the root filesystem is read-only,
  and nothing carries over between jobs), and `HTTPS_PROXY`, `HTTP_PROXY` and
  `NO_PROXY` when set (`compose.example.yaml` points the runner's
  `HTTPS_PROXY` at the forwarder), never the runner's own environment. yt-dlp
  hands that environment to Deno, which it runs with `--no-remote
  --cached-only`.
- **Success** is judged by the runner: exactly `<id>.m4a` (a regular file of
  at most `FETCH_MAX_BYTES`) and `<id>.info.json` being in the job directory, never
  by exit 0 alone. Exit 101 is `too_long`, a timeout is `timeout`, and other
  failures are `blocked` or `not_found` when stderr matches a few fixed
  phrases (bot checks, HTTP 403; unavailable, private or removed videos) and
  `failed` otherwise. The job directory is removed in `finally` on every path.
- **Logging:** yt-dlp runs with `--quiet --no-warnings --no-progress`, and its
  stdout and stderr are never logged; neither the runner nor the fetcher logs them:
  they carry the destination filename, which contains the title that becomes
  the tone name (CLAUDE.md: tone names stay out of logs). Failures map to a fixed error set: `blocked`, `too_long`,
  `not_found`, `timeout`, `failed`. A test with a stubbed yt-dlp asserts the
  log never contains the stub's title.
- **Output:** `POST /fetch/api/jobs` answers 202 with `{job_id}` (128 random
  bits). `GET /fetch/api/jobs/<id>` returns `status` (`queued`, `running`,
  `done` or `failed`), `error` (from the fixed set below, only when failed)
  and whitelisted metadata only:
  - `title` (sanitised, at most 200 characters)
  - `duration`
  - `heatmap`, validated as at most 200 points with times within the duration
    and values from 0 to 1

  `GET …/audio` claims the job atomically (a concurrent second request gets
  404), streams the file once as `audio/mp4` with `no-store`, then deletes it
  and forgets the job, so a later status request is a 404 too. A TTL sweep
  (`FETCH_RESULT_TTL_SECONDS` from completion) catches any file that was never
  collected. The fetcher holds **at most one** finished, uncollected result:
  `/tmp` is sized for one download in flight plus one waiting, so when a
  download finishes, an older one still waiting is deleted. A result being
  streamed counts too, because its file keeps its space until the stream
  closes it: before the next download starts, the oldest uncollected result
  goes, and if streams alone fill the budget, the oldest stream is cut at its
  next chunk and the worker waits for it to let go. This rule is the
  fetcher's only; the core's no-early-eviction rule (5.5) is unchanged.

  The fetcher gets the result from the runner over `jobs`: `POST /jobs` with
  the ID and a token the fetcher mints (never its job ID) answers, once
  yt-dlp is done, the cleaned title, duration and heatmap in yt-dlp's key
  names, which the fetcher re-validates with the same whitelist;
  `GET /jobs/<token>/audio` streams the audio once, capped at
  `FETCH_MAX_BYTES` and timed, and the runner deletes it; an uncollected
  file goes when the next job starts or after 60 s. The runner answers 503
  `busy` while a run or a stream is in progress. It lets go of that lock
  before it closes the connection, so the fetcher ends each exchange at the
  runner's close, not at the answer's last byte, and its next request never
  finds the runner busy.
- **Runtime:** the same `python:3.12-slim-bookworm` base and digest as the
  core, with its `prlimit`. Deno (2.9.8; yt-dlp needs at least 2.3.0) is
  copied from the official `denoland/deno:bin` image, a named build stage
  pinned by digest so Dependabot tracks it. yt-dlp and yt-dlp-ejs come from a
  hashed pip-tools lock (`fetcher/requirements.txt`, compiled from
  `requirements.in`; `--require-hashes --no-deps --only-binary :all:`), which
  holds exactly those two distributions: yt-dlp declares no required
  dependencies, and yt-dlp-ejs must equal the version yt-dlp's own `default`
  extra pins (an in-image test checks both, and Dependabot bumps them as one
  group). They install into a venv of their own at `/opt/yt-dlp`, root-owned,
  created `--without-pip` and filled by the base image's pip through
  `--prefix`; the base pip is then uninstalled. So the base interpreter,
  which runs the fetcher, the runner and the forwarder, holds no installed
  distribution at all and no `.pth` file, and its `sys.path` leaves out the
  venv (I1 in 6.1, checked in the image). Without `requests`, yt-dlp talks
  to a proxy through urllib, so only `http://` proxies work. The fetcher
  shares the core's `strange_ringtone.config` and
  `strange_ringtone.http_guard` (the Origin, length, `X-Forwarded-For` and
  rate-limit rules): `fetcher/__main__.py` puts `../core` first on
  `sys.path`, and the image copies exactly those modules to `/app/core`. The
  runner is the stdlib package `strange_ringtone_runner`, started by
  `/app/fetcher/runner.py`, which puts `../core` and `fetcher/` on
  `sys.path`; it imports the core's `config` and `http_guard` and the
  fetcher's `config`, `metadata` and `routes`, never its server. The
  forwarder is the stdlib package `strange_ringtone_forwarder`, started by
  `/app/fetcher/forwarder.py`, which puts only its own directory on
  `sys.path`, so its process loads neither the core nor the fetcher.
- **Contract:** the same UID, read-only and capability rules as the core. The
  fetcher needs no egress at all: it reaches the runner over `jobs` at
  `FETCH_RUNNER_URL` (default `http://fetcher-runner:8080`). The runner
  needs YouTube's hosts and no access to any private network, and it gets
  them **only through the forwarder** (6.1): it sits on `jobs` and `egress`,
  internal networks only, `compose.example.yaml` sets its `HTTPS_PROXY` to
  `http://fetcher-forwarder:3128`, and both the fetcher and the runner
  refuse to start (exit 3) when their container has a default route, IPv4
  or IPv6, or when they can't read or parse their route tables, unless
  `FETCH_ALLOW_DEFAULT_ROUTE=true` says egress is filtered another way. Docker Engine 28.0 or newer: engines before 26.0 forward DNS
  from internal networks, and the internal networks' `isolated` gateway
  modes, which keep the Docker host off them, arrived in 28.0
  (moby/moby#49262). With Compose, Docker Compose 2.27.1 or newer, for the
  per-network `driver_opts` that turn forwarding off and refuse ICMP
  redirects on each interface (6.1).
  Settings, from one shared `.env`: the fetcher reads `PUBLIC_ORIGIN`
  (required), `TRUSTED_PROXIES`, `FETCH_MAX_BYTES`, `FETCH_TIMEOUT_SECONDS`,
  `RATE_LIMIT_FETCHES_PER_MINUTE` (5), `FETCH_RESULT_TTL_SECONDS` (600),
  `FETCH_ALLOW_DEFAULT_ROUTE` and `FETCH_RUNNER_URL`
  (`http://fetcher-runner:8080`); the runner reads `FETCH_MAX_SECONDS` (600,
  at most 600: `decode.js` refuses input over 10 minutes, so a longer video
  would be downloaded only for the phone to refuse it), `FETCH_MAX_BYTES`
  (40 MiB; 10 minutes of YouTube's AAC is about 10 MB),
  `FETCH_TIMEOUT_SECONDS` (120; the fetcher waits that long plus 15 s for
  the runner's answer), `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`,
  `FETCH_ALLOW_DEFAULT_ROUTE` (false) and `FETCH_ALLOW_NO_SWEEP` (false,
  never set by the example), and nothing browser-facing. The
  forwarder reads no setting.
- **Fetcher contract (the README gets a second table):** image
  `ghcr.io/<owner>/strange_ringtone-fetcher:<semver>`, linux/amd64, UID/GID
  10001, three commands: the CMD `python -I /app/fetcher`, the fetcher on
  port 8080/tcp plain HTTP; `python -I /app/fetcher/runner.py`, the runner
  on port 8080/tcp in its own container; and
  `python -I /app/fetcher/forwarder.py`, the forwarder on port 3128/tcp. The
  fetcher serves the `/fetch/` prefix itself (`GET /fetch/healthz`,
  `POST /fetch/api/jobs`, `GET /fetch/api/jobs/<id>` and `…/audio`), and the
  proxy forwards those paths unchanged, so no rewriting is needed. Writable:
  the fetcher `/tmp` (tmpfs) only, sized `2 × FETCH_MAX_BYTES` for one
  download arriving from the runner and one finished result: 80 MB with the
  defaults; the runner `/tmp` (tmpfs) only, sized `FETCH_MAX_BYTES + 16 MB`
  for one download in flight or one audio waiting to be collected, the info
  JSON and Deno's cache: 56 MB; the forwarder nothing. Health: the fetcher's
  `GET /fetch/healthz` and the runner's `GET /healthz` answer 200 `ok`; the
  forwarder accepts a TCP connection on 3128. The forwarder's limits are named with their values in
  the README. The in-image tests live in `tests/image_fetcher/` (not
  `tests/image/`, which is the core's), gated by
  `STRANGE_RINGTONE_FETCHER_IMAGE_TESTS=1`; the compose topology tests live
  in `tests/compose/`, gated by `STRANGE_RINGTONE_COMPOSE_TESTS=1`.

### 6.1 Egress confinement (decided 2026-11-03)

The owner chose this design on 2026-11-03, after a review of five options.
Milestone 5's follow-up builds it in two PRs (section 12). Both PRs have
landed; this section is the record.

**Threat.** The likeliest compromise is a malicious yt-dlp, yt-dlp-ejs or
Deno release that arrives through a Dependabot bump and runs as the fetcher's
UID. The fetcher holds no secrets, so the design limits reach. That code must
not get to the LAN, the host, a VPN, cloud metadata, the reverse proxy, or the
app's requests and responses, and it must not turn the owner's address into a
source of abuse.

**What it rests on** (checked on 2026-11-03 in throwaway containers):

- Google's front ends route by the `Host` header, so a tunnel to
  `www.youtube.com` also reaches other Google services, including relays that
  fetch any website. A hostname allowlist therefore confines the fetcher to
  Google's front ends, not to YouTube. Only TLS interception would narrow it.
- yt-dlp follows redirects and format URLs to any host, so a response alone
  can steer it. It sends `Connection: close`, so each tunnel carries one
  request.
- Deno, as yt-dlp runs it, has no access to files, the environment,
  subprocesses or the network.
- `python -I` still runs `.pth` lines from its site-packages (`-S` doesn't),
  and pip writes a wheel's scripts and data files into the interpreter's
  prefix. A wheel installed for the server's interpreter is code in the
  server. The same holds at build time: any interpreter that starts with
  site enabled on the venv (the venv's own `python`, for one) after its
  wheels are in runs their `.pth` lines as root, where they could rewrite
  the base interpreter. pip's `--python` re-runs pip under the venv's
  interpreter, but that interpreter starts while the venv is empty, and
  site reads `.pth` files only at startup.
- A container on an `internal: true` network has no default route. From
  Docker Engine 26.0 it also has no external DNS (CVE-2024-29018 stopped DNS
  forwarding from internal networks).
- `internal: true` alone does not keep the host out: the bridge still has a
  host address, and a container on it reaches every service the host listens
  on at that address or on all addresses (SSH, a TCP Docker API, admin
  ports). The bridge driver's `gateway_mode_ipv4: isolated` leaves the
  bridge without an IPv4 address, and `gateway_mode_ipv6: isolated` without
  an IPv6 one. Engine 28.0.0 added both modes, accepted only on an internal
  network (its release notes, moby/moby#49262). Connections to the host
  then fail, while container-to-container traffic and the embedded DNS keep
  working (checked on Engine 29.8 by the refute stage, 2026-11-03).
- The modes are per address family. On an internal network with IPv6
  turned on (`enable_ipv6: true`, or a daemon `default-network-opts` that
  turns it on for every new network) and the IPv4 mode alone, the host
  gets an IPv6 address on the bridge again, and a container there reaches
  the host's services on `::`. With both modes the bridge holds no IPv6
  address at all. The IPv6 mode is accepted on a network without IPv6, where
  the bridge keeps at most a link-local `fe80::` address and Docker turns
  IPv6 off on the containers' interfaces (checked on Engine 29.8 by the
  refute stage, 2026-11-04).
- Inside every container of the stack `net.ipv4.ip_forward` is 1 (a new
  network namespace copies it from the host's, where dockerd turns it on),
  so the forwarder, with an interface on `egress` and on `outside`, would
  route a packet that reached it for an off-link destination, the runner
  would route between `jobs` and `egress`, and the fetcher between `fetcher`
  and `jobs`. Nothing can hand them one today (the fetcher's and the
  runner's only routes are their on-link subnets, they have no capability
  to add a route or craft a frame, and the host has no address on the
  isolated networks), but that is one capability or one route away from a
  path around the gate. So every container of the fetcher image turns
  forwarding off (owner, 2026-11-04, for the fetcher and the forwarder; the
  runner the same from PR B), on every interface rather than with the
  global switch (owner, 2026-11-04, after the race below).
- The kernel forwards a packet only when the interface it arrived on has
  forwarding on (`Documentation/networking/ip-sysctl.rst`,
  `conf/interface/forwarding`). So the fetcher, the runner and the forwarder set
  `net.ipv4.conf.default.forwarding` (every interface created later) and
  `net.ipv4.conf.lo.forwarding` to 0 in `sysctls`, and each interface
  Docker attaches through its endpoint sysctl on every network attachment
  (`driver_opts: {com.docker.network.endpoint.sysctls:
  "net.ipv4.conf.IFNAME.forwarding=0,net.ipv4.conf.IFNAME.accept_redirects=0"}`;
  Docker replaces `IFNAME` with the interface it creates, whose name follows
  the attach order). With every interface at 0 nothing is forwarded, while
  the global switch, `net.ipv4.ip_forward` (the same value as
  `conf/all/forwarding`), still reads 1 inside, by design. A compose test
  routes datagrams from a helper on a stand-in for `egress` through a
  container with the forwarder's settings toward a listener on a stand-in
  for `outside`: none arrive, the router's own counters show it received
  them and forwarded none, and a control router with forwarding on delivers
  them (checked on Engine 29.8 by the fix stage, 2026-11-04). Docker writes
  these namespaced sysctls as it creates the container, so they need no
  capability; they work under `read_only` and `cap_drop: [ALL]`, and UID
  10001 can't change them back, because `/proc/sys` is read-only inside
  (EROFS). IPv6 forwarding already reads 0 in these containers, on the
  example's networks and on an internal network with IPv6 turned on, so no
  IPv6 sysctl is set: it would stop a container from starting on a kernel
  without IPv6.
- Why the global switch is left alone: writing `net.ipv4.ip_forward` (or
  `conf/all/forwarding`) runs the kernel's forwarding change, which sets
  `net.ipv4.conf.all.accept_redirects` to 1 (RFC 1122's host defaults), and
  Docker passes a container's sysctls as a map and writes them in no fixed
  order. With `ip_forward` 0 beside `all.accept_redirects` 0, `all` read 1
  in 10 of 12 `docker run` starts and in 5 of 6 fetcher and 1 of 6
  forwarder Compose starts on Engine 29.8. With forwarding off per
  interface and `ip_forward` never written, every `accept_redirects` and
  every interface's `forwarding` read 0 in 12 of 12 `docker run` and 6 of 6
  Compose starts, both services healthy (redirects stage, 2026-11-04), and
  the stack test passed 5 of 5 runs (fix stage). Anyone who checks only
  `ip_forward` sees 1: read the per-interface values instead.
- A namespace whose interfaces don't forward is a host, and a host follows
  its router's ICMP redirects (RFC 1122); the kernel takes a redirect when
  `all` or the receiving interface says 1, and only from a route's current
  gateway: the fetcher and the runner have none, and the forwarder's is the
  Docker host on `outside`. All three refuse them anyway (owner, 2026-11-04;
  the runner from PR B): `all`, `default`
  and `lo` at 0 in `sysctls`, and each attached interface through the same
  endpoint sysctl. Engine 29.8 attaches the interfaces after it writes the
  container's sysctls, so they take `default`'s 0 anyway (with a network's
  option removed, its interface still read 0); the per-attachment option
  sets each one whatever the engine's order. `lo` exists before `default`
  is written, so it needs its own sysctls. An endpoint sysctl must name
  `IFNAME` (the engine refuses `all`, `default`, `lo` and
  `net.ipv4.ip_forward` there), and the engine refuses an interface's
  sysctl through `--sysctl` (`eth0`) but not `lo`'s. Nothing for IPv6, as
  for forwarding. Checked on Engine 29.8 and Compose 5.5 (redirects stage,
  2026-11-04).
- Versions, from Docker's documentation (checked 2026-11-04): endpoint
  sysctls (`com.docker.network.endpoint.sysctls`) first appear in the
  Docker Engine 27.0.1 release notes (2024-06-24), below the 28.0 floor,
  and the same notes say that a per-interface `--sysctl` such as
  `net.ipv4.conf.eth0.*` will be rejected in a future release (Engine 29.8
  does reject it), which is why the per-attachment option is the way to set
  an interface. The Compose Specification gives Docker Compose v2.27.1 for
  `driver_opts` on a service's network (compose-spec commit d04baf3,
  2024-07-18), so 2.27.1 is the Compose floor.
- On an internal network, Docker's own host rules drop a packet whose
  destination is off the network's subnet before it reaches any container
  on the bridge: a router container with forwarding on received none of the
  datagrams sent through it (Engine 29.8, fix stage, 2026-11-04). That is a
  second layer on `egress` today, not one the layout relies on: it depends
  on the engine's firewall rules, and the per-interface settings hold
  without it.
- prlimit's `--nproc` for yt-dlp is `RLIMIT_NPROC`, which the kernel counts
  per UID across every container that runs as 10001 (without user-namespace
  remapping), so it is no per-container bound. The cgroup's pids controller
  is: `pids_limit` caps a container's processes and threads, and a thread
  that can't start then fails its request or connection closed. Deno, as
  yt-dlp runs it, starts its main thread, 2 tokio threads and one V8 worker
  per usable CPU up to 4 (7 threads with 4 CPUs or more, measured offline in
  the image), and yt-dlp's Python 1 thread. The runner's `pids_limit: 160`
  is at least twice its worst case of 74: the main thread, 4 request workers
  (`RunnerServer.MAX_WORKERS`), the result sweeper, the health check, and
  one yt-dlp run counted as 67 (V8's workers as 63, for a host that reports
  64 CPUs, in case the cap of 4 doesn't hold there). The fetcher, which
  runs no yt-dlp, has `pids_limit: 64`, at least twice its worst case of
  21: the main thread, 16 request workers (`FetchServer.MAX_WORKERS`), the
  job worker and the sweeper, the health check, and the runner client's
  deadline timer.
- A container's PID 1 inherits every orphan and must reap it. Measured as
  PID 1 of a throwaway container of the fetcher image (2026-11-05): a
  grandchild killed with its process group stays a zombie of PID 1; one
  that called `setsid()` survives `killpg`; one that also forks to a new PID
  every 10 ms left 49 zombies in half a second; then `kill(-1, SIGKILL)`
  and `waitpid(-1, WNOHANG)` killed and reaped all 51 and left nothing.
  The kernel sends `kill(-1)` to every process the caller may signal in its
  PID namespace but PID 1 and the caller, under a lock `fork()` can't slip
  past.
- Python 3.12's `ipaddress` calls `64:ff9b::a9fe:a9fe` (NAT64 for
  169.254.169.254) and `::169.254.169.254` global.
- A child with the same UID can read its parent's `/proc/<pid>/environ`, and
  it can ptrace the parent where Yama's `ptrace_scope` is 0 (the kernel's
  default; Docker's default seccomp profile allows `ptrace` on kernels 4.8
  and newer).
- A non-dumpable process (`prctl(PR_SET_DUMPABLE, 0)`) can't be ptraced,
  and its `/proc/<pid>/environ`, `maps` and `mem` can't be read, by a
  process without `CAP_SYS_PTRACE`, whatever `ptrace_scope` says; `execve`
  makes a process dumpable again. Measured as PID 1 of a throwaway container
  of the fetcher image under the contract flags (2026-11-05): before the
  call, a same-UID child read `/proc/1/environ` and `/proc/1/maps`; after
  it, both, `process_vm_readv` and `PTRACE_ATTACH` were refused.
- glibc's resolver retries a relative name under each `search` domain of
  `/etc/resolv.conf`, which Docker copies from the host, after an NXDOMAIN,
  so `nosuch.youtube.com` can become `nosuch.youtube.com.<search domain>`.
  A trailing dot makes the name absolute and stops that (checked in the
  fetcher image with a loopback fake DNS by the refute stage, 2026-11-03).

**Layout.** The fetcher image runs as three containers. Every network but
`outside` is `internal: true` and isolated from the host
(`gateway_mode_ipv4: isolated` and `gateway_mode_ipv6: isolated`, so turning
IPv6 on for them doesn't bring the host back); `outside` keeps its gateway,
the forwarder's route out. Every service rotates its log (json-file, `max-size` and
`max-file`), so a flood of refusals can't fill the host's disk. Every
container of the fetcher image (the fetcher, the runner and the forwarder)
turns forwarding off on every interface (`forwarding`
0 for `default`, `lo` and, on each network attachment, its interface, and
never the global `ip_forward`, which reads 1 inside by design), so none of
them routes between its networks, refuses ICMP redirects (`accept_redirects`
0 for `all`, `default`, `lo` and each attachment's interface), and sets a
`pids_limit` of at least twice its worst case.

```
 reverse proxy
      │  fetcher network
      ▼
 fetcher            our server only, on the base interpreter
      │  jobs network
      ▼
 fetcher-runner     yt-dlp and Deno, from the venv
      │  egress network (HTTPS_PROXY)
      ▼
 fetcher-forwarder  the CONNECT gate, on the base interpreter
      │  outside network: the only route out
      ▼
 YouTube's hosts, port 443
```

- **fetcher** keeps the reverse-proxy side: the API, the queue, the rate
  limit and the result store of section 6. It sends the runner a validated
  video ID and treats everything that comes back as untrusted, with timeouts,
  size caps on the JSON and the audio, and the same metadata whitelist.
  The proxy's routing stays as it is. The fetcher trusts no proxy in the
  example: `compose.example.yaml` sets its `TRUSTED_PROXIES` to empty, which
  wins over the shared `.env`, so it keys its rate limit on the socket peer
  and behind a reverse proxy every client shares one limit. The runner
  reaches it over `jobs`, and `TRUSTED_PROXIES` must never cover `jobs`,
  whose subnet Docker may change when it recreates the network (owner,
  2026-11-05). It finds the
  runner at `FETCH_RUNNER_URL`, sends it the ID and a token of its own, and
  caps the answer: JSON ≤ 256 KiB of a fixed shape, audio of exactly the
  declared length up to `FETCH_MAX_BYTES`, 30 s per read and a total
  deadline per exchange; a runner that misbehaves fails the job, never a
  retry.
- **fetcher-runner** runs yt-dlp as section 6 describes (the argument list,
  `prlimit`, the timeout, an environment built from scratch) behind a small
  stdlib server (`POST /jobs`, `GET /jobs/<token>/audio`, `GET /healthz`)
  that takes one job at a time, deletes its job directory in `finally`, and
  keeps an audio file only until it is collected, the next job starts, or
  60 s pass. It shares no network with the reverse proxy, never sees a
  browser's request and can't set a response header on the app's origin.
  It is its container's PID 1 (no `init`, no wrapper), and under its lock,
  before each run starts and after each run ends (before it answers the
  `POST`), it sends `kill(-1, SIGKILL)`, which ends every other process of
  UID 10001 in its container, a child that left yt-dlp's process group with
  `setsid()` or one that keeps forking to a new PID included, then reaps
  with `waitpid(-1, WNOHANG)` for about 2 s, never while a `Popen` is in
  flight. So nothing of one run reaches the next one's files, and no orphan
  stays a zombie that counts against `pids_limit`. Only the entry turns it
  on, and only as PID 1: anywhere else `kill(-1)` would kill every process
  of the user (owner, 2026-11-05). The example states `init: false`, since
  an unset `init` takes the daemon's default, which may be an init. Where
  the sweep can't run (not PID 1, as under an init, or no `kill` and
  `waitpid`), the entry refuses to start (exit 4) unless
  `FETCH_ALLOW_NO_SWEEP=true`, which is for a local run or a host that
  can't run the runner as PID 1, never for a deployment that can; with it,
  the runner serves without the sweep and says so on stderr. As PID 1 it
  sweeps whatever the variable says (owner, 2026-11-05). Before it serves, it makes
  itself non-dumpable: yt-dlp runs as its UID and is not its ancestor, so
  where `ptrace_scope` is 0 it could otherwise attach to the runner and
  plant code there that `kill(-1)`, which spares the caller, never ends.
- **fetcher-forwarder** is the only container with a route out.
- The core image doesn't change. Its example network becomes isolated from
  the host too (both isolated gateway modes, Engine 28.0), so the reverse
  proxy must be a container on it, not a process on the host, and its log
  rotates. A proxy kept in another Compose project joins the networks as
  external ones under their project-scoped names (`strange_ringtone_core`,
  `strange_ringtone_fetcher`) and is told to use the fetcher's address on
  `fetcher`, not on `jobs`.

**The forwarder.** Stdlib Python on one plain port, 3128. It reads one
request head per connection and never parses anything after it. In order:

1. Read the head within a size cap and a deadline. Accept only
   `CONNECT <name>:443`. Refuse plain-HTTP proxying, other methods, other
   ports, IP literals, and malformed or non-ASCII names.
2. Match the lowercased name, by whole labels, against the allowlist:
   `youtube.com` and its subdomains, and the subdomains of `googlevideo.com`.
   Suffixes cost nothing here, because every allowed name reaches the same
   front ends, and they keep churn low. Piped's proxy allowlists the same way.
3. Check the budgets: concurrent tunnels and new tunnels per minute.
4. Only then resolve the name, IPv4 only and within a deadline, as an
   absolute name (a trailing dot), so no `search` domain is ever tried.
   Refuse unless every address is global unicast. IPv4 only sidesteps the
   NAT64 and IPv4-compatible forms above.
5. Connect to an address that passed, without resolving again, and answer
   `200`. The attempts stop at the tunnel's lifetime, since the tunnel's
   slot is already held. Then copy bytes both ways within caps: upload bytes per tunnel and
   per minute (yt-dlp sends requests, never large bodies), download bytes per
   tunnel, an idle timeout and a lifetime.
6. Log refusals and cut tunnels with the name and the reason only, never
   payload, at most `log_lines_per_minute` lines per minute: refusals cost a
   client no budget, so the log has its own, and one
   `forwarder log suppressed <n>` line, itself within that budget, counts
   what it dropped.

Steps 1 to 3 refuse before any DNS lookup, so a refused name can't leak
through DNS. A hard cap on open connections before the head comes on top, so
a flood of half-open connections can't exhaust threads before step 3's
budgets apply.

The forwarder's container has the contract's flags, nothing writable, a
128 MB memory limit, IP forwarding off on every interface (it relays in
user space, so its kernel never routes from `egress` to `outside`; the
global `ip_forward` is left at 1, see What it rests on), ICMP redirects
refused on every interface (with its interfaces not forwarding, its one
gateway, the Docker host on `outside`, could otherwise redirect its routes),
and `pids_limit: 320`. That
is at least twice its worst case of 156 threads: the main thread, one per
open connection (`connections_max`, 64), the lookups (each in a thread that
lives until glibc gives up, at most 10 s with the `resolv.conf` Docker
writes for these networks, 5 s timeout and 2 attempts, measured at 10.0 s;
each started by a tunnel in that time, so `tunnels_per_minute`, 90, bounds
them), and the health check. Measured with `--pids-limit 320`: 64 idle
connections make 65 threads, and the next connection is closed by the
connection gate (`forwarder refused connections -`), not by a failed thread
start; 16 clients hammering an allowed name against a DNS server that never
answers peak at 29 threads. With a limit too small, a thread that can't
start costs one `forwarder error RuntimeError` line and a closed
connection, gives back its slots, and the forwarder serves the next
request. The budgets are sized with headroom from one measured fetch,
or set generously if the owner declines that fetch, and they become contract
limits. PR A ships provisional values, named with their meaning in the
README's "Forwarder limits" table, which a test compares with the code. The
per-tunnel download cap is a fixed 16 MiB, one 10 MiB yt-dlp range plus
headroom, never sized from `FETCH_MAX_BYTES` (owner, 2026-11-03); an in-image
test fails when the pinned yt-dlp's range no longer fits it.

**Fail closed.** The fetcher and the runner refuse to start when their container has a default route, IPv4 or IPv6, or when the
route tables can't be read or parsed (a missing `ipv6_route` alone means a
kernel without IPv6), because something other than the forwarder could then
carry traffic out. `/proc/net/route` lists the IPv4 main table only, so a
default route in a table that an `ip rule` selects passes unseen; Docker's
bridge driver sets up no such rule, and `cap_drop: [ALL]` keeps the container
from adding one. The check looks for a `/0` route only: routes that together
cover every address without one (a VPN client's `0.0.0.0/1` and
`128.0.0.0/1`) pass it, and an IPv4 `unreachable default` counts as a
default route while IPv6 skips reject routes. Neither is reachable by the
attacker in scope, who can't add routes and runs only after the check. `FETCH_ALLOW_DEFAULT_ROUTE=true` turns the check off
where egress is filtered some other way (a Kubernetes NetworkPolicy, for
example). The offline dev stack starts the fetcher and the runner without
their entry points' checks, so it needs nothing. The check replaces requiring `HTTPS_PROXY`, which
a malicious yt-dlp would ignore.

**Image.** yt-dlp and yt-dlp-ejs install into a venv from the same hashed
lock, and only yt-dlp runs from it, in the runner. The fetcher, the runner
and the forwarder run the base interpreter, whose site-packages hold no
third-party distribution, so a wheel's `.pth`, scripts and data files can't
reach them. The venv is filled by the base interpreter's `pip install
--prefix /opt/yt-dlp`, so pip never runs under the venv's interpreter: with
`--python` it did, while it wrote the wheels (owner-approved change after
PR A's review). The wheels' scripts land in `/opt/yt-dlp/bin` with the base
interpreter's shebang; nothing runs them and nothing puts that directory on
a PATH. Once the wheels are in, no build step starts the venv's
interpreter: the base interpreter compiles the venv's bytecode with
`-I -S`, so no `.pth` line runs as root during the build either.
Keeping one image leaves CI, Trivy, publishing, provenance and Dependabot as
they are.

**Contract changes**, none of which needs a breaking marker before v0.1.0:

- shipped in PR A:
  - the forwarder command (`python -I /app/fetcher/forwarder.py`, port 3128,
    nothing writable)
  - the `egress` network, which now joins the fetcher to the forwarder
    (internal), and the `outside` network, the forwarder's only way out
  - `FETCH_ALLOW_DEFAULT_ROUTE`
  - `HTTPS_PROXY` pointing at the forwarder (set on the fetcher service by
    `compose.example.yaml`)
  - the forwarder's limits, named in the README (its log budget included)
  - `gateway_mode_ipv4: isolated` and `gateway_mode_ipv6: isolated` on the
    internal networks, and log rotation on every service
  - IP forwarding off per interface and ICMP redirects refused on the
    fetcher and the forwarder: `net.ipv4.conf.{default,lo}.forwarding: "0"`
    and `net.ipv4.conf.{all,default,lo}.accept_redirects: "0"` in `sysctls`,
    and `com.docker.network.endpoint.sysctls:
    "net.ipv4.conf.IFNAME.forwarding=0,net.ipv4.conf.IFNAME.accept_redirects=0"`
    in the `driver_opts` of each of their network attachments, with
    `net.ipv4.ip_forward` and `conf/all/forwarding` never set (so
    `ip_forward` reads 1 inside by design), no capability needed (owner,
    2026-11-04)
  - a `pids_limit` on the fetcher (200) and the forwarder (320), no
    capability needed (owner, 2026-11-04)
  - Docker Engine 28.0 or newer (the first release with the isolated gateway
    modes; endpoint sysctls date from 27.0.1), and Docker Compose 2.27.1 or
    newer for a service network's `driver_opts`
- shipped in PR B:
  - the runner command (`python -I /app/fetcher/runner.py`, port 8080, a
    `/tmp` of `FETCH_MAX_BYTES + 16 MB`, 56 MB with the defaults)
  - the `jobs` network between the fetcher and the runner (internal, both
    isolated gateway modes); `egress` moves from the fetcher to the runner
  - `FETCH_RUNNER_URL` (default `http://fetcher-runner:8080`), how the
    fetcher finds the runner
  - `HTTPS_PROXY` moved to the runner service
  - the fetcher's `/tmp` at `2 × FETCH_MAX_BYTES` (80 MB with the defaults)
  - `mem_limit` 256 MB for the fetcher (512 MB for the runner)
  - `pids_limit` 64 for the fetcher and 160 for the runner
  - the kernel settings (forwarding off per interface, ICMP redirects
    refused) on the runner
  - the runner as its container's PID 1 (`init: false`, stated, and no
    wrapper), which its kill-and-reap sweep needs; a `docker exec` or health
    check running at a run boundary is killed too
  - `FETCH_ALLOW_NO_SWEEP` (runner only, default false): where the sweep
    can't run, the runner refuses to start (exit 4) unless it is `true`
    (owner, 2026-11-05)
  - the runner non-dumpable, so a `docker exec` as its UID can't read its
    `/proc/1/environ`, `maps` or `mem`
  - `TRUSTED_PROXIES: ""` on the fetcher service, which wins over `.env`

**Dropped, and why.**

- **TLS interception.** It would narrow the gate from Google's front ends to
  YouTube, but the fetcher holds nothing worth stealing, traffic relayed
  through Google reaches its target from Google's addresses, and YouTube
  itself stays a command channel (descriptions, comments, an attacker's
  account) that no `Host` check closes. It would put a TLS server, an HTTP
  parser and a CA in the path, and replace yt-dlp's TLS fingerprint, which
  rules out curl_cffi impersonation if YouTube ever fingerprints TLS.
- **A "front" container before the fetcher.** The runner split keeps
  third-party code off the reverse proxy's network without a new image or a
  second `X-Forwarded-For` hop.
- **Requiring `HTTPS_PROXY` at startup.** The route check replaces it.
- **Making the fetcher's server non-dumpable.** Moot: yt-dlp runs in
  another container. The runner, PID 1 of yt-dlp's container, does make
  itself non-dumpable (see fetcher-runner above).
- **An off-the-shelf gate.** Smokescreen (Stripe's egress proxy) came closest,
  but it has no port rule or byte budgets, accepts clients without
  certificates only through a YAML setting, and its release tags are years
  apart. Squid, tinyproxy, Envoy and mitmproxy fit worse: an audit backlog; a
  critical header-parsing bug and no address check after DNS; warnings about
  confused-deputy attacks from untrusted clients; and a large dependency tree.
- **An environment variable that extends the allowlist.** A new YouTube host
  means a release. PR A ships this default; the owner can still overrule it.

**Accepted risks.** A compromised runner can still reach other Google
services through the shared front ends, within the forwarder's budgets.
YouTube stays a command channel. Docker Engine before 26.0 leaks DNS from
internal networks, and before 28.0 has no way to keep the host off them
(28.0 added the isolated gateway mode), which is why the contract requires
28.0. The forwarder is our own code at the boundary, so it stays small. It
listens on all its interfaces, `outside` included, where the Docker host can
reach port 3128 (Engine 28 and newer drop direct routing to unpublished
ports from elsewhere); nothing else may join `outside`. A compromised
forwarder shares `egress` with the runner and can reach its port 8080 there,
where the runner answers only a well-formed job request and runs yt-dlp
through the forwarder itself. A compromised runner shares `jobs` with the
fetcher and can reach its port 8080 there: the Origin check needs only the
public origin, which is no secret, the rate limit keys on the runner's
address (so `TRUSTED_PROXIES` must never cover `jobs`; the example sets
the fetcher's to empty, because Docker may give `jobs` another subnet when
it recreates the network), and it learns the job IDs of its own
submissions, never a browser's, so it can submit jobs that come back to
itself and nothing more, which adds nothing to what the forwarder already
gives it. Binding the
fetcher to its `fetcher` address alone would need a fixed subnet and
address in the example; the owner declined that on 2026-11-05 and accepts
this path as a documented risk. Nothing a yt-dlp run starts outlives it:
the runner kills the run's process group when the run ends, and its
kill-and-reap sweep at both run boundaries ends and reaps what left the
group, so a run's child can't keep the runner's reach between jobs, touch
the next job's files or hold a share of `pids_limit`; and the runner is
non-dumpable, so such a child can't hide in it by ptrace either, whatever
the host's `ptrace_scope`. The sweep's cost is
accepted (owner, 2026-11-05): a `docker exec` or health check running in
the runner's container at a run boundary is killed too, and the runner must
be that container's PID 1. The health check's retries absorb a coincidence,
not an adversary: a run can time its exit to the checks, or fill
`pids_limit`, and make the runner read unhealthy, so nothing in the example
acts on the runner's health status and nothing should restart the runner
on it. A runner that can't sweep refuses to start; an operator who sets
`FETCH_ALLOW_NO_SWEEP=true` accepts the residual the sweep removes (a child
of a run that left its process group keeps running between jobs with the
runner's reach in its container, and zombies hold a share of `pids_limit`)
as the operator's own risk. The
runner still opens every result `O_NOFOLLOW` and `O_NONBLOCK` and holds and
serves only a regular file within `FETCH_MAX_BYTES`. The isolated gateway mode also means the
host has no route to the internal networks, so the reverse proxy must be a
container on them.

**Checks and their tests.** Every check gets its failing test before its code
(section 12), and the verification panel removes each check and expects a
test to fail.

| ID | Check | Tested by | Shipped |
| --- | --- | --- | --- |
| G1 | Only `CONNECT` to port 443; plain-HTTP proxying, other methods and other ports are refused | unit | PR A |
| G2 | IP literals and malformed, over-long or non-ASCII names are refused | unit | PR A |
| G3 | The name matches the allowlist by whole labels (`youtube.com.example.net` and `notyoutube.com` are refused) | unit | PR A |
| G4 | G1 to G3 and the budgets are decided before any DNS lookup | unit, with a resolver that records its calls | PR A |
| G5 | IPv4 only, the name looked up as an absolute name, and every resolved address is global unicast | unit, with fake answers: private, loopback, link-local, CGNAT, multicast, mixed; a getaddrinfo spy for the trailing dot; compose tests: a loopback fake DNS in the image proves no `search` domain is asked | PR A |
| G6 | The connection goes to an address that passed, without a second lookup | unit | PR A |
| G7 | The request head has a size cap and a read deadline | unit | PR A |
| G8 | Concurrent tunnels and new tunnels per minute are capped | unit | PR A |
| G9 | Upload and download bytes are capped, with an idle timeout and a lifetime | unit; in-image for the download cap against the pinned yt-dlp's range size | PR A |
| G10 | Logs hold the name and the reason only | unit | PR A |
| R1 | The runner accepts only `POST /jobs` with a small JSON object of exactly a valid video ID and a 32-hex token, and one job at a time: a second `POST`, or an audio `GET`, during a run or a stream gets 503 `busy`; `/healthz` still answers | unit | PR B |
| R2 | The runner builds yt-dlp's environment from scratch: fixed values, `HOME` and `DENO_DIR` in the job directory, and exactly the proxy variables its own settings read (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, uppercase, `http://` only, no credentials); the fetcher reads no proxy variable | unit (the fetcher's former tests move) | PR B |
| R3 | The runner deletes its job directory in `finally` on every path; the audio is served once and deleted, deleted when a new job starts, `COLLECT_DEADLINE_SECONDS` (60) after the run if uncollected, and on shutdown, so at most one audio file is on its `/tmp` at any time | unit; in-image | PR B |
| R4 | The runner's log holds method, path with the token masked, status and duration only; never a video ID, a title, a token, an address or yt-dlp's output | unit | PR B |
| S1 | The fetcher sends the runner only the validated ID and a token it minted, never its job ID, and consults no proxy variable; it fails the job (nothing left behind, never retried) on any answer that is not status 200 with exactly one `Content-Type` and `Content-Length`, no `Transfer-Encoding`, JSON of at most 256 KiB of the shape `{status, info}` or `{status, error}`, or audio of 1 to `FETCH_MAX_BYTES` bytes of `audio/mp4` within the per-read and total deadlines; it re-validates the metadata with `parse_info`, follows no redirect, accepts nothing past the declared length, and ends each exchange only at the runner's close, which comes after the runner let go of its one-job lock, so its next request never finds it busy | unit, with a fake runner; the fetcher's flow against a real runner with the stub yt-dlp | PR B |
| S2 | The fetcher (PR A) and the runner (PR B) refuse to start with a default route, or an unreadable or unparsable route table, unless `FETCH_ALLOW_DEFAULT_ROUTE` is set | unit (fake `/proc`), in-image and Compose topology tests | PR A (the fetcher), PR B (the runner) |
| R5 | The runner's kill-and-reap sweep runs only as its container's PID 1, at both run boundaries, never while a `Popen` is in flight; where it can't run, the entry exits 4 before building the server unless `FETCH_ALLOW_NO_SWEEP=true` (parsed like `FETCH_ALLOW_DEFAULT_ROUTE`; a bad value exits 2), which never turns the sweep off as PID 1; the example runs the runner as PID 1 and never sets the override | unit (fake `kill`, `waitpid` and `getpid` that never pass -1 on); in-image as PID 1 (a real sweep of a run's leftovers); Compose topology tests (no init, the exact command, no override, `/proc/1/cmdline` in the running runner) | PR B |
| I1 | The base interpreter's site-packages hold no third-party distribution and no `.pth` file, and its `sys.path` leaves out the venv; no build step after the wheels' install starts the venv's interpreter or compiles the venv with site enabled, and the venv's bytecode is current for the image's interpreter | in-image; unit (static read of `fetcher/Dockerfile`) | PR A |
| I2 | The venv is filled by the base interpreter's `pip install --prefix /opt/yt-dlp`; no build step, the install included, runs pip under the venv's interpreter (`--python`) or starts it; no second install into the venv follows the first; the wheels' scripts land in `/opt/yt-dlp/bin`, none in the base prefix | unit (static read of `fetcher/Dockerfile`); in-image | PR B |
| K1 | Every container of the fetcher image (fetcher, runner, forwarder) has `forwarding` 0 for `default`, `lo` and each attachment's interface, never the global switch, `accept_redirects` 0 for `all`, `default`, `lo` and each interface, no IPv6 sysctl, and a `pids_limit` of at least twice its worst case from the code's caps | Compose topology tests (static and running; a container with the forwarder's settings routes no datagram between two networks, against a control router that does) | PR A (the fetcher and the forwarder), PR B (the runner) |
| T1 | In the compose layout, the fetcher and the runner have no default route (IPv4 or IPv6), each resolves its own peer (the fetcher the runner, the runner the forwarder) and no external name, the fetcher can't resolve the forwarder, and the host has no IPv4 or IPv6 address on `fetcher`, `jobs` or `egress` (also with IPv6 turned on for such a network); the forwarder has the only default route, and the fetcher's CMD and the runner's command each refuse to start on `outside` unless `FETCH_ALLOW_DEFAULT_ROUTE` is set | Compose topology tests | PR A, extended in PR B |
| T2 | The runner reaches the forwarder over `egress`; the fetcher can't reach the forwarder while it reaches the runner over `jobs`; the forwarder can't reach either fetcher address while it reaches the runner over `egress`; nothing on the `fetcher` network reaches the forwarder or the runner, and nothing on `jobs` reaches the forwarder; all three containers run the base interpreter with `-I` | Compose topology tests | PR A, reworded in PR B |
| T3 | The running forwarder refuses an unlisted name, an IP literal, port 80 and other methods before DNS, and logs the name and the reason only (run from the runner, the only container that reaches it) | Compose topology tests | PR A |

## 7. Web app

### 7.1 Trimmer (phone first)

- A full-width canvas waveform with two large draggable handles (44 px touch
  targets), ±0.1 s and ±1 s nudge buttons, and a duration readout.
  - The selection is capped at exactly `MAX_CLIP_SECONDS`. Phase 0 accepted
    30.0 s, so no safety margin is needed.
  - Dragging one edge past the cap pulls the other edge along.
- **Play selection** loops it, which is how a ringtone sounds while ringing.
- Fade-in and fade-out each have a toggle and a slider, and the fade shapes are
  drawn over the waveform.
- The loudness has a toggle (on by default) and a "Softer ↔ Louder" slider with
  a marker at the baseline and a **Reset** button.
- The name field is pre-filled from the file name or the video title.
- Buttons: **Hear final**, **Get on this iPhone**, and **Download .m4r**.

### 7.2 Auto-suggesting the hook

The first rule that applies places the selection:

1. If the shared link has a `t=` timestamp, the selection starts there.
2. If the audio came from the fetcher and YouTube supplied a "Most Replayed"
   heatmap, the selection goes on the window with the highest summed value.
3. Otherwise, it goes on the window with the highest mean short-term loudness,
   since choruses tend to be the loudest part.

The start then moves to the quietest 50 ms (lowest RMS) within ±1 s, to avoid
cutting a word, and the window keeps its full length.

### 7.3 Getting it onto the phone

**Get on this iPhone** calls `navigator.share({files: [<name>.m4a]})` when
`navigator.canShare` allows it. Otherwise it falls back to a download link.
Phase 0 confirmed that path: in Safari's downloads list, tap the file, then
Share offers "Use as Ringtone".

Phase 4 checks whether the Web Share sheet offers it too. If it doesn't, the
download becomes the main button.

As built in milestone 4, the page fetches the `.m4a` right after the render
and enables **Get on this iPhone** only once the bytes are in hand. Its click
handler calls `navigator.share({files: [file]})` with no `await` before it,
because WebKit's user activation does not survive an awaited fetch. The file
is `new File([bytes], <name>.m4a, {type: 'audio/mp4'})`, with the name the
server sanitised (read from `Content-Disposition`), and `navigator.canShare`
checks that very file. Cancelling the sheet is silent; any other failure, or a
browser without the sheet, shows the plain download link. A status line says
"Share sheet: available" or "not available". **Pending the owner's phone
test:** whether the sheet lists "Use as Ringtone", and what it does. The help text covers the Duplicate Ringtone
error, and explains that tones are removed by swiping left in the Ringtone or
Text Tone list. Web Share needs a secure
context, so the origin must use a certificate the phone trusts. With a private
CA, that means its root installed and enabled under Certificate Trust Settings.

### 7.4 iOS Shortcut "Make Ringtone"

The Shortcut has two actions:

1. *Receive URLs from the Share Sheet.*
2. *Open URL* `<PUBLIC_ORIGIN>/#yt=<URL-encoded input>`.

The fragment never reaches the server or proxy logs. The page extracts the
video ID and `t=` on the device, from `watch?v=`, `youtu.be/`, `/shorts/`,
`/live/` and `music.youtube.com` links. It drops tracking parameters such as
`si=` and sends only the ID to the fetcher. The README documents how to build
the Shortcut, with a placeholder origin.

### 7.5 AGPL source link

The footer links **Source** to `SOURCE_URL`, which the release build sets to the
tag's tree on GitHub, so the link matches the running version. It is a plain
link (no prefetch), and `Referrer-Policy: same-origin` keeps the app's URL from
going off-site.

## 8. Security summary

- **Headers on every response:**
  - `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src
    'self'; img-src 'self'; media-src 'self'; connect-src 'self'; base-uri
    'none'; form-action 'self'; frame-ancestors 'none'`. This is stricter than
    `default-src 'self'` and meets the same intent.
  - `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
    `Referrer-Policy: same-origin`.
  - `Cross-Origin-Opener-Policy` and `-Resource-Policy: same-origin`.
  - A `Permissions-Policy` that denies camera, microphone, geolocation, payment
    and USB.
  - `Strict-Transport-Security: max-age=63072000; includeSubDomains` when
    `PUBLIC_ORIGIN` is https, and none for http. Two years is the common
    preload-grade value. The cost of `includeSubDomains`: browsers then insist
    on HTTPS for every subdomain of the `PUBLIC_ORIGIN` host as well, so serve
    the app on a host of its own rather than on a domain whose other hosts
    lack TLS.
- **CSRF:** every state change is a POST, and each requires exactly one `Origin`
  equal to `PUBLIC_ORIGIN` (no `Referer` fallback). Request bodies are
  `audio/wav` or `application/json` only.
- **Client IP:** `X-Forwarded-For` is honoured only from `TRUSTED_PROXIES`, which
  is empty by default. The example sets the fetcher's to empty whatever
  `.env` says, and it must never cover `jobs`, where the runner's requests
  come from (6.1).
- **Escaping:** server-rendered text is escaped, and the client uses
  `textContent` only.
- **Decoder isolation:** see 5.2, 5.3 and 6. Downloads always carry
  `Content-Disposition: attachment` and an explicit `Content-Type`. The filename
  is the sanitised tone name, sent as `filename*` with an ASCII `filename`
  fallback (5.6).
- **Process limits:** `python -I`. A fixed thread pool and global concurrency
  caps. Body reads have socket timeouts, and a whole body must arrive within
  60 s of taking a render slot (408 otherwise), so a trickled upload can't hold
  a slot indefinitely. The proxy handles slow-client defence beyond that.
- **Egress (fetcher):** the fetcher image's own forwarder is the only way
  out: one CONNECT allowlist to YouTube's hosts on port 443, within budgets.
  yt-dlp runs in a container that shares no network with the reverse proxy
  (6.1).

## 9. Supply chain, CI and releases

### 9.1 Pins and dependencies

- Every Action is pinned by full commit SHA with a `# vX.Y.Z` comment. Base
  images are pinned by digest. There is no `curl | sh`.
- Reusable workflows owned by
  `strangebasisdevs` are pinned by full commit SHA with a `# vX.Y.Z` comment and
  never called at `@main`. `security_gate.yml` is the single source of truth for
  the current pin. Dependabot (`github-actions` ecosystem) opens the bump PRs
  from `strange_security` release tags, so a fix in the organisation's security
  repository reaches every repository through a reviewed bump. This applies
  only to organisation-owned reusable workflows. Third-party actions stay
  SHA-pinned.
  The cost: a fix merged to `strange_security` runs here only after the bump PR
  in this repository merges.
- The core has no Python dependencies. The fetcher's yt-dlp lock and ruff use
  hashed pip-tools locks. Semgrep and Trivy versions live in `strange_security`.
- Dependabot covers `pip`, `github-actions` and `docker` for both Dockerfiles,
  with a 7-day cooldown.
- `ffmpeg-watch.yml` runs weekly with `contents: read` and **fails** when a newer
  FFmpeg release exists than the pinned one. GitHub's failure email is the
  alert, so the job needs no issue-write permission. Bumping it means a PR that
  changes the version and SHA-256, with release notes reviewed.

### 9.2 CI on every PR

Local jobs run with `permissions: contents: read`:

- ruff lint.
- Unit tests.
- **Image tests** (from the render milestone on): a core image build with
  buildx and the GitHub Actions cache, then the in-image tests
  (`tests/image`) in the built image with `--network none`, `--read-only`, a
  `/tmp` tmpfs, `--cap-drop ALL`, `no-new-privileges`, `--ipc none` and
  `--ulimit msgqueue=0`: the real ffmpeg, the corpus, the build-configuration
  checks and the container contract. It is a
  required check, because it is the only test that proves the ffmpeg
  allowlist, `--network none` and `read_only`.
- **Fetcher image tests** (from milestone 5 on): the fetcher image built the
  same way (in its own cache scope), then `tests/image_fetcher` in it under
  the same flags with a 96 MB tmpfs (more than either container's `/tmp`,
  because one container runs the fetcher's and the runner's suites): the
  container contract, the venv's yt-dlp and yt-dlp-ejs against the lock and
  each other, the base interpreter holding no distribution (I1), the venv's
  scripts in the venv and none in the base prefix (I2), one yt-dlp range
  plus headroom fitting the forwarder's download cap, Deno under the
  runner's `prlimit` set, the real yt-dlp accepting the exact argument list
  (offline), the CMD answering `/fetch/healthz` with no default route, the
  runner command answering `/healthz`, failing an offline fetch and leaving
  its scratch empty, the forwarder command answering refusals without DNS,
  and the fetcher's, the runner's and the forwarder's unit tests again under
  the image's Python 3.12, with the yt-dlp stub.
- **Compose topology tests** (from the egress follow-up's PR A on): the job
  builds the fetcher image as `strange_ringtone-fetcher:ci`, reading the
  fetcher job's cache scope without writing it so the two jobs never race on
  one scope, brings up `compose.example.yaml` (the fetcher, the runner and
  the forwarder) without any YouTube traffic, and checks T1 to T3 and K1 of
  section 6.1. It runs `tests/compose/`, which is
  skipped unless `STRANGE_RINGTONE_COMPOSE_TESTS=1`, with the image tag
  passed in `STRANGE_RINGTONE_COMPOSE_IMAGE` (default
  `strange_ringtone-fetcher:local`), under a unique project name, a
  temporary env file and no published port. It is the only test that proves
  the layout.
- A **PR-title check** (conventional commits, so release-please works). It is a
  stdlib script that reads the title from an `env:` variable, never from inline
  `${{ }}`.
- A **contract-change check**: if the README's "Deployment contract" section
  changed, the PR title must carry `!` or the body must carry `BREAKING CHANGE:`,
  so the CHANGELOG marks it. It starts enforcing once
  `.release-please-manifest.json` records a released version. Before that, the
  contract is a draft and the check only reports. An unreadable manifest fails
  the check.

The **organisation security gate** (`security_gate.yml`) calls
`strangebasisdevs/strange_security`'s `semgrep.yml`, `trivy.yml` and
`gitleaks.yml`, each pinned as described in 9.1. They are this repository's only security scans:

- Semgrep `p/default` fails on any finding.
- Trivy fails on any secret at any severity, and on HIGH or CRITICAL fixable
  vulnerabilities.
- Gitleaks scans the full history, merge commits included.

The gate's permissions are `actions: read`, `contents: read` and
`security-events: write`; the shared workflows upload SARIF to code scanning,
which needs the last of those. A PR from a fork gets a read-only token, so the
SARIF upload fails there. Until strange_security skips the upload for forks
while still failing on findings, a fork's PR cannot pass the gate; a
maintainer pushes the fork's branch into this repository instead.

**Branch ruleset on `main`** (set 2026-10-25): every change needs a PR, merges
are squash only, and force pushes and deletion are blocked. These checks are
required, all reported by GitHub Actions:

- Ruff
- Unit tests
- Conventional PR title
- Deployment contract change
- Semgrep / Semgrep scan
- Trivy / Trivy filesystem scan
- Gitleaks / Gitleaks history scan
- Image tests (from milestone 3; the owner adds it to the ruleset)
- Fetcher image tests (from milestone 5; the owner adds it to the ruleset,
  named exactly so)
- Compose topology tests (from the egress follow-up's PR A; the owner adds it
  to the ruleset, named exactly so)

The repository squash-merges with the PR title as the commit subject, which is
what release-please reads. A **tag ruleset** on `v*` lets only organisation
admins create, move or delete release tags. Phase 6 must add the release
workflow's identity to that ruleset before release-please can tag.

No workflow uses `pull_request_target` or `workflow_run`.

### 9.3 Releases (release-please)

- `release-please.yml` runs on pushes to `main` and sets top-level
  `permissions: {}`.
  - **Job `release-please`** gets `contents: write` and `pull-requests: write`,
    with `release-type: simple` and `bump-minor-pre-major: true`. The first
    release is forced with `release-as: 0.1.0`.
  - **Job `publish`** runs only when a release was created. It gets
    `packages: write`, `id-token: write`, `attestations: write` and
    `contents: write`. It builds both images with `VERSION` and `SOURCE_URL`,
    scans them with Trivy (failing on HIGH or CRITICAL), pushes them, runs
    `actions/attest-build-provenance`, generates an SBOM and attests it with
    `actions/attest-sbom`, and appends both digests to the release notes.
- **Known gotcha (settled 2026-10-25):** a PR that `GITHUB_TOKEN` opens or
  updates gets its `pull_request` workflow runs in an *approval-required*
  state (GitHub docs, "Triggering a workflow from a workflow"). A maintainer
  clicks **Approve workflows to run** on the release PR, and all three
  workflows report their required checks on its head commit. Every push to
  `main` re-synchronises the release PR and needs the click again. This
  replaces the earlier idea of dispatching `ci.yml`, which could never have
  reported the five required checks that live outside `ci.yml`, and which
  needed `actions: write`. No GitHub App or PAT: both add a long-lived
  secret. The repository setting "Allow GitHub Actions to create and approve
  pull requests" must be on, or release-please cannot open the PR at all.
- A consumer verifies with
  `gh attestation verify oci://ghcr.io/<owner>/strange_ringtone@sha256:… --repo <owner>/strange_ringtone`
  before pinning the digest.
- The repository also has `LICENSE` (AGPL-3.0), `SECURITY.md` (private
  vulnerability reporting through GitHub Security Advisories) and `CHANGELOG.md`
  (maintained by release-please). Branch protection and tag rules are set by
  hand in the GitHub settings.

### 9.4 Security scan gaps are fixed upstream

On 2026-10-25, checking `strange_security` (`main` at `4bca5bb`) against this
repository's rules found four gaps:

1. Semgrep never failed on findings.
2. Trivy's secret scan only reported HIGH and CRITICAL.
3. There was no git-history secret scan.
4. The gate broke for every external caller. It used `github.workflow_ref`,
   which names the *caller*, so it checked out the wrong repository (PR #1,
   Actions run 40170938163).

While they were open, this repository kept an interim local secret-scan job.
All four were fixed in strangebasisdevs/strange_security#12, merged the same
day, and the interim job was removed in PR #1. The rule going forward: a gap
in security scanning is fixed in strange_security, the organisation's one-stop
security repository, and never patched with a local copy here.

## 10. Tests (stdlib `unittest`)

- **Limits:** body size, duration bounds, sample rate, channel count and bit
  depth, the content-type allowlist, and both rate limits and concurrency caps.
- **WAV validation corpus**, each file refused cleanly with no files left
  behind:
  - truncated RIFF
  - a size that lies
  - duplicate `fmt `/`data`
  - float, 24-bit or ADPCM samples
  - MP3 or MP4 bytes named `.wav`
  - empty
  - 31 s or 41 s long
  - 8 channels
  - a 0.1 s clip
  - an oversized body
  - a body cut off mid-stream
- **Decoder invocation:** the argument list is built safely (no shell, no user
  strings except the escaped title), the timeout kills the whole process group (the
  test's stub ffmpeg forks a grandchild, which must be gone afterwards), a
  failure cleans up the job directory, and the build-configuration allowlist
  holds.
  The limiter is always invoked with `level=false`, and the post-chain
  loudness the render pass reports for a fixture lands within 0.5 LU of the
  target.
- **Headers and CSRF:** a missing, duplicated or wrong `Origin` is refused, the
  CSP is exact, and the XFF trust rules hold.
- **Tone-name sanitisation (5.6):** bidi overrides, CR/LF and other controls,
  path separators and reserved characters, accents, non-Latin scripts, emoji
  ZWJ sequences cut at the 40-character cap, input of only dots or spaces, and
  empty input. The `filename*` header must round-trip, and the ASCII fallback
  must stay ASCII. A duplicate, oversized or invalid `X-Tone-Name` header is
  refused.
- **Statelessness:** after a full render and download only the result remains
  under `results/`; after the TTL sweep the scratch path holds nothing. The
  in-image test runs the container with
  `read_only: true` and only `/tmp` writable, and fails if the server tries to
  write anywhere else. Neither the logs nor the query strings ever contain a
  tone name.
- **Fetcher:** the ID regex, URL construction, argument building, metadata
  whitelisting, and heatmap validation. yt-dlp is stubbed, with no network.
- **Egress (6.1):** the forwarder's policy, ordering and budgets with a fake
  resolver and connector, the runner protocol against a fake runner, the
  route check against a fake `/proc`, the image's interpreter isolation, and
  the compose layout in its own CI job. The table in 6.1 maps each check to
  its test.
- **Every security check has a test that fails when the check is removed.**

## 11. Repository layout (proposed)

```
LICENSE  README.md  SECURITY.md  CHANGELOG.md  CLAUDE.md  .env.example
compose.example.yaml        # hardened example, ports only in a commented dev snippet
.dockerignore               # allowlist: only core/ and fetcher/ reach the build context
core/
  Dockerfile  ffmpeg/{VERSION,SHA256SUMS,ffmpeg-devel.asc,configure.txt,components.txt}
  __main__.py               # checkout entry point: python -I core
  strange_ringtone/{__main__.py,config.py,server.py,http_guard.py,static.py,wav.py,render.py,names.py,results.py}
  static/{index.html,style.css,app.js,waveform.js,player.js,share.js,fetch.js}   # fetch.js: link parser, /fetch/ client
  static/audio/{decode,wav,loudness,suggest,fades,request}.js   # pure audio code, unit-tested under Node
  static/package.json       # {"type": "module"} for the Node tests only: not served, not in the image
fetcher/
  Dockerfile  requirements.in  requirements.txt   # yt-dlp and yt-dlp-ejs, hashed lock, installed into the /opt/yt-dlp venv
  __main__.py               # checkout entry point: python -I fetcher; puts ../core on sys.path
  forwarder.py              # the forwarder's entry point: python -I fetcher/forwarder.py; puts only fetcher/ on sys.path
  runner.py                 # the runner's entry point: python -I fetcher/runner.py; puts ../core and fetcher/ on sys.path
  strange_ringtone_fetch/{__init__,__main__,config,server,jobs,routes,metadata,runner_client}.py   # imports only config and http_guard from core
  strange_ringtone_runner/{__init__,__main__,config,server,ytdlp}.py   # yt-dlp's container: imports config and http_guard from core, config, metadata and routes from the fetcher
  strange_ringtone_forwarder/{__init__,__main__,policy,server}.py   # the CONNECT gate: stdlib only, imports nothing from core or the fetcher
tests/  tests/corpus/  tests/image/  tests/image_fetcher/  tests/compose/  tests/stubs/{ffmpeg_stub,ytdlp_stub}.py  tests/js/
scripts/{check_pr_title.py,check_contract_change.py,check_ffmpeg_release.py}   # stdlib checks CI runs
tools/spike/phase0.py       # regenerates the iOS test tones into spike-out/
tools/dev_fetch_stack.py    # loopback dev stack: core, runner with the stub, fetcher, one origin, for the offline product check
.github/{dependabot.yml,workflows/{ci,pr-checks,security_gate,release-please,ffmpeg-watch}.yml}
release-please-config.json  .release-please-manifest.json
docs/PLAN.md
```

## 12. Milestones (one or more PRs each)

0. **Phone spike: done on 2026-10-24** on one iPhone running iOS 26 or later.
   Other phones on the same iOS are assumed to behave the same. The spike
   checked:
   - which files offer "Use as Ringtone", at several lengths and in `.m4a`,
     `.m4r` and `.mp3`
   - from which sources: Files and a Safari download, with the Web Share sheet
     still to test in Phase 4
   - which name Settings shows when the filename and the title tag differ
   - whether custom tones appear in the text-tone picker
   - how −14, −11 and −9 LUFS compare with a built-in tone

   `python -I tools/spike/phase0.py` regenerates the files into the
   git-ignored `spike-out/phase0/` folder, along with `CHECKLIST.txt` and a zip.

   | Check | Result |
   | --- | --- |
   | A1 29.9 s `.m4a` | Offered and accepted |
   | A2 30.0 s `.m4a` | Offered and accepted |
   | A3 30.5 s `.m4a` | Offered, then refused: "File Too Large: Choose a ringtone under 30 seconds" |
   | A4 39.9 s `.m4a` | Offered, then refused with the same error |
   | A5 29.9 s `.m4r` | Offered and accepted |
   | A6 30.5 s `.m4r` | Offered, then refused with the same error |
   | A7 29.9 s `.mp3` | Offered and accepted |
   | B1/B2: the name shown | The **filename** (`B1 name FILENAME`) for both `.m4a` and `.mp3`. The title tag is ignored. |
   | C1–C3 loudness | All accepted. Not yet compared by ear, so −14 LUFS stays the baseline. |
   | Safari download → downloads list → Share | Offers "Use as Ringtone". Re-adding an installed tone fails with "Duplicate Ringtone: This file already exists in the ringtone library". After deleting the first copy, it worked. |
   | AirDrop | Not tested, and not needed: the app delivers through Safari |
   | Text Tone list | Every custom tone appears there too |
   | Loop seam | Not tested. Fades are tunable per tone. |
   | Deleting test tones | Swipe left in the Ringtone or Text Tone list → Delete |
1. **Bootstrap (2026-10-25):** create a public GitHub repo whose only initial
   commit on `main` is LICENSE. Then open a PR adding:
   - README, CLAUDE.md, SECURITY.md and CHANGELOG.md
   - `.gitattributes` (LF) and `.gitignore`
   - Dependabot
   - the CI skeleton: ruff, unit tests, the organisation security gate, and
     the PR-title and contract-change checks with their tests

   On 2026-10-25 the repository moved to the `strangebasisdevs` organisation,
   and its security scanning moved to strange_security (sections 9.2 and 9.4).

   Docker and image jobs arrive with the first Dockerfile (Phase 3). Set the
   branch ruleset (required checks, squash-only), the tag rules and private
   vulnerability reporting in the repository settings.
2. **Core server:** `/healthz`, headers, the Origin check, XFF and rate limits,
   static files and config, with tests.

   From a checkout the server runs as `python -I core [--bind HOST]`, not
   `python -I -m strange_ringtone`: `-I` keeps the current directory off
   `sys.path`, so `-m` finds the package only where it is installed, while a
   directory that Python runs is always on `sys.path`. `POST /api/render` ends
   in 501 `render_unavailable` until milestone 3. The image starts the server
   as `python -I /app/core` (milestone 3).
3. **Render:** the minimal ffmpeg build, WAV validation, measure and render,
   results with TTL, the corpus, and the build-configuration tests.

   The image (`core/Dockerfile`) builds FFmpeg 9.0.2 from the release tarball,
   verified by its committed SHA-256 and by FFmpeg's release signature, with
   exactly `core/ffmpeg/configure.txt`, and runs as UID 10001 on port 8080
   under `python:3.12-slim-bookworm`, both base images pinned by digest. The
   **Image tests** job runs the in-image tests under the contract's
   restrictions; the owner adds it to the ruleset. `ffmpeg-watch.yml` fails
   weekly when a newer FFmpeg is released, and Dependabot tracks the base
   images. `compose.example.yaml` shows the hardened service.
4. **Web app:** decode, waveform, selection, preview (fades and JS loudness),
   render and "Hear final", share or download, and energy-based suggestions.
   Finish the Web Share part of the spike here.

   Implemented: the browser decodes the file (up to 10 minutes) and resamples
   it to 44100 Hz, suggests the loudest window, and previews the selection
   with qsin fades and a BS.1770 meter. The page sends exactly the selection
   as 16-bit WAV, then fetches and plays the result (5.1) and offers the share
   sheet or the downloads (7.3). The pure audio modules have Node unit tests
   (`tests/js/`), run by the stdlib suite when `node` is present. Still open:
   the owner's phone test of the share sheet, recorded in 7.3.
5. **Fetcher** image and UI integration (paste a link, the `#yt=` fragment,
   heatmap suggestions), plus the Shortcut docs.

   Implemented: the opt-in fetcher image (section 6) and, in the page, the
   **From YouTube** box that `FETCH_ENABLED` reveals. `core/static/fetch.js`
   parses a pasted link or the `#yt=` fragment on the device (7.4), posts
   only the video ID to `/fetch/api/jobs`, polls the job every 2 s, fetches
   the audio once into memory and hands it to the same decode path as a
   picked file, with plain words for every fetcher error code. The fragment
   is removed with `history.replaceState` before any request, and with
   `FETCH_ENABLED` off no `/fetch/` request is ever made. `suggest.js` now
   applies rules 1 (`t=`) and 2 (the heatmap) of 7.2 before rule 3, with the
   same snap; an invalid timestamp or heatmap falls through to the next rule.
   The README documents the YouTube flow, the Shortcut with a placeholder
   origin, and the privacy notes of section 13. The CSP is unchanged: every
   URL the page fetches is a same-origin `/fetch/` path.

   The product check runs offline: the UI is driven in a browser against
   `tools/dev_fetch_stack.py`, which serves the core and the fetcher with the
   stubbed yt-dlp on one loopback origin, so no YouTube traffic leaves the
   development machine. The one real fetch from throwaway containers before
   v0.1.0, to confirm that `bestaudio[ext=m4a]` is still served without a PO
   token, was made by 6.1's product checks with the owner's approval: on
   2026-11-05, through the fetcher, the runner and the forwarder, it was.
   Still open: the owner's phone test of the Shortcut.

   **Follow-up: egress confinement (section 6.1, decided on 2026-11-03).** Two
   PRs before the release, one after the other, because their files overlap:

   - **PR A**, branch `feat/fetcher-forwarder`, delivered:
     - yt-dlp and yt-dlp-ejs in a venv at `/opt/yt-dlp`, from the same
       hashed lock, with pip removed from the base interpreter (I1)
     - the default-route start check in the fetcher's entry point, exit 3,
       and `FETCH_ALLOW_DEFAULT_ROUTE` (S2)
     - the forwarder, `python -I /app/fetcher/forwarder.py` on port 3128,
       with its limits named in the README (G1 to G10)
     - `compose.example.yaml` with the fetcher on internal networks only
       (`fetcher`, `egress`), `HTTPS_PROXY` pointing at the forwarder, and
       the forwarder alone on `outside`
     - the internal networks isolated from the Docker host
       (`gateway_mode_ipv4: isolated` and `gateway_mode_ipv6: isolated`,
       Docker Engine 28.0 or newer), log rotation on every service, the
       forwarder's log budget and its absolute-name lookups
     - IP forwarding off on every interface (`forwarding` 0 for `default`,
       `lo` and each network attachment's interface, never the global
       `ip_forward`, which reads 1 inside by design) and a `pids_limit` on
       the fetcher (200) and the forwarder (320), each at least twice its
       worst case from its own caps (owner, 2026-11-04)
     - ICMP redirects refused on both (`accept_redirects` 0 for `all`,
       `default`, `lo` and each network attachment's interface, Docker
       Compose 2.27.1 or newer; owner, 2026-11-04)
     - the **Compose topology tests** job (T1 to T3, in PR A's wording)
     - the README contract, `.env.example`, CLAUDE.md and section 6

     With PR A alone the fetcher still ran yt-dlp itself, through the
     forwarder. PR B's product check made the real fetch through the
     fetcher, the runner and the forwarder on 2026-11-05: six tunnels, one
     at a time, all to allowed names, none refused or cut, and every
     forwarder limit held with headroom (at most 975,753 bytes down in one
     tunnel against 16 MiB). The limits stay provisional until the release
     fixes the contract.
   - **PR B**, branch `feat/fetcher-runner`, delivered: the runner command
     and its protocol (R1 to R4), the fetcher's runner client (S1), the
     runner's start check (S2), the `jobs` network (`egress` moved from the
     fetcher to the runner), `FETCH_RUNNER_URL`, the venv filled with
     `pip install --prefix` (I2), the kernel settings and pids cap on the
     runner (K1), the runner's kill-and-reap sweep as PID 1 and its refusal
     to start where it can't sweep (R5), the fetcher's
     empty `TRUSTED_PROXIES` in the example, T1 to T3 for the final layout,
     the dev stack with the runner in-process, and the same documentation.

   Each PR runs test first. A spec maps 6.1's check IDs to interfaces and test
   files. The tests are written, seen to fail for the right reason, reviewed
   and frozen in a commit. The code then makes them pass without touching
   them. Finally a verification panel tries to refute "done", which includes
   removing each check and expecting a test to fail. The owner adds "Compose
   topology tests" to the ruleset when PR A opens.
6. **Release:** release-please, publish with provenance, SBOM and image scans, a
   complete README contract, then **v0.1.0**. Stop there. Integration into an
   infrastructure repository happens from that repository.

## 13. Privacy notes

- YouTube sees the server's IP fetch the video, the same as watching it. No
  account or cookies are used.
- The Shortcut uses a URL fragment, so proxies never log which video was
  shared, and `si=` share tracking is stripped on the device.
- Rendered files carry no metadata except a title. The server keeps nothing:
  each result is deleted after `RESULT_TTL_SECONDS`, and tone names stay out of
  URLs and logs.
- By default, Safari saves downloads to iCloud Drive → Downloads. To keep files
  off iCloud, change Settings → Apps → Safari → Downloads to *On My iPhone*.
- No telemetry, no CDN, no web fonts, and no third-party requests from the page.
- With a private CA, hostnames stay out of public Certificate Transparency logs.
  Public certificates, including VPN-provided `*.ts.net` HTTPS certificates,
  publish the machine name.

## 14. Later

- **Maybe: a shared tone library.** It was dropped on 2026-10-25 to keep the core
  stateless. If it comes back, it needs a breaking contract change, and it must
  be opt-in through one documented data directory. It also needs a backup entry
  in the deployment, escaped names everywhere, and a quota.
- A TUI client on the same API.
- arm64.
- SponsorBlock `music_offtopic` trimming in the fetcher.
- Cookies for the fetcher (as a `*_FILE` secret, with a throwaway account).
- An ntfy "ready" push for slow fetches.
- A short alert-tone preset (1 to 5 s). Phase 0 showed that custom tones already
  appear in the Text Tone list.
- Beat-snapped edges.

## Sources

- AppleInsider: [Custom ringtones are now slightly easier to add in iOS 26](https://appleinsider.com/articles/25/06/12/custom-ringtones-are-now-slightly-easier-to-add-in-ios-26)
- [Galaxus: personal ringtones could make a comeback with iOS 26](https://www.galaxus.de/en/page/choose-your-own-audio-files-personal-ringtones-could-make-a-comeback-with-ios-26-38379)
- [AppleVis: Setting custom ringtones has become easy in iOS 26](https://applevis.com/comment/195342)
- [iPhone Life: Make a voice memo into a ringtone](https://iphonelife.com/content/make-voice-memo-ringtone)
- [Apple Developer Forums: Use as ringtone](https://developer.apple.com/forums/thread/798223)
- [Apple Community: Create and manage ringtones](https://discussions.apple.com/docs/DOC-12051)
- [TidBITS Talk: custom ringtone stopped working on new iPhone](https://talk.tidbits.com/t/my-wifes-custom-ringtone-stopped-working-on-new-iphone/33164)
- [iMazing: Add ringtones to iPhone](https://imazing.com/guides/how-to-import-or-export-iphone-ringtones-mac-or-pc)
- [GIGAZINE: yt-dlp now requires a JavaScript runtime (Deno)](https://gigazine.net/news/20251113-yt-dlp-required-deno-javascript-runtime/)
- [yt-dlp issue #15012 (EJS / JS runtimes)](https://github.com/yt-dlp/yt-dlp/issues/15012)
- [yt-dlp PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
- [bgutil-ytdlp-pot-provider](https://pypi.org/project/bgutil-ytdlp-pot-provider)
- [yt-dlp `common.py` (`heatmap` field definition)](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/common.py)
- [Tailscale: Enabling HTTPS (Certificate Transparency note)](https://tailscale.com/kb/1153/enabling-https)
- [Tailscale Serve (identity headers)](https://tailscale.com/kb/1312/serve)
- [SSD Nodes: Tailscale free plan limits](https://www.ssdnodes.com/learn/tailscale-pricing-explained)
- [Docker Engine 26.0 release notes (CVE-2024-29018, DNS from internal networks)](https://docs.docker.com/engine/release-notes/26.0/)
- [Docker Engine 28 release notes (28.0.0: the `isolated` gateway mode for internal bridge networks, moby/moby#49262)](https://docs.docker.com/engine/release-notes/28/)
- [OSV MAL-2026-4770: a PyPI package that runs code through a `.pth` file](https://osv.dev/vulnerability/MAL-2026-4770)
- [Smokescreen, Stripe's egress proxy](https://github.com/stripe/smokescreen)
- [Zulip: outgoing proxies with Docker Compose](https://zulip.readthedocs.io/projects/docker/en/latest/how-to/compose-outgoing-proxy.html)
- [piped-proxy's domain allowlist](https://github.com/TeamPiped/piped-proxy/blob/main/src/main.rs)
- [Joshua Rogers: Squid security audit](https://joshua.hu/squid-security-audit-35-0days-45-exploits)
- [Debian tracker: tinyproxy CVE-2023-49606](https://security-tracker.debian.org/tracker/CVE-2023-49606)
- [Envoy: SNI dynamic forward proxy](https://www.envoyproxy.io/docs/envoy/latest/configuration/listeners/network_filters/sni_dynamic_forward_proxy_filter)

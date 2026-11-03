# strange_ringtone: research and v0.1.0 plan

Status: design agreed on 2026-10-24 (decisions in section 3). Milestones 0
to 2 are done, milestone 3 (render) is implemented, and milestone 4 (web app)
is implemented with its Web Share check pending the owner's phone test
(section 12).
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
| Runs with | `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, `ipc: none`, `ulimits: msgqueue: 0`. No egress needed (internal network). |
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
`*_FILE`, with no usable default.

## 6. Fetcher image (opt-in)

- **Input:** JSON `{video_id}` only. The ID must match `^[A-Za-z0-9_-]{11}$`, and
  the server builds `https://www.youtube.com/watch?v=<id>` itself. No URL a user
  typed ever reaches yt-dlp, which removes the SSRF path (yt-dlp's generic
  extractor would otherwise fetch anything).
- **Invocation:** `python -I -m yt_dlp`, run as a subprocess with an argument
  list:
  - `--ignore-config --no-playlist --use-extractors youtube` (verify flag names
    against the pinned version)
  - `-f "bestaudio[ext=m4a]"` (AAC, which Safari decodes, so the fetcher needs
    no ffmpeg)
  - `--match-filter "duration <= FETCH_MAX_SECONDS & !is_live"`
  - `--max-filesize`
  - `--write-info-json`, with `-o`/`-P` pointing into a fresh `/tmp` job directory

  It runs under `prlimit` and a wall-clock timeout, one job at a time
  (`FETCH_CONCURRENCY=1`), with its own per-IP rate limit. The fetcher's
  `prlimit` set is CPU seconds, file size, open files and process count, with
  no address-space limit: Deno reserves a huge virtual address space at
  startup, so `RLIMIT_AS` would kill it. Memory is bounded by the deployment's
  cgroup limit (`mem_limit` in `compose.example.yaml`), which the contract
  documents as the operator's job. `DENO_DIR` and yt-dlp's cache directory
  point under `/tmp`, because the root filesystem is read-only.
- **Logging:** yt-dlp runs with `--quiet --no-warnings --no-progress`, and its
  stdout and stderr are never logged: they carry the destination filename,
  which contains the title that becomes the tone name (CLAUDE.md: tone names
  stay out of logs). Failures map to a fixed error set: `blocked`, `too_long`,
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

  `GET …/audio` streams the file once, then deletes it. A TTL sweep catches any
  file that was never collected.
- **Runtime:** Deno is copied from the official image, pinned by digest so
  Dependabot tracks it. yt-dlp and yt-dlp-ejs come from a hashed pip-tools lock
  (`--require-hashes --no-deps --only-binary :all:`).
- **Contract:** the same UID, read-only and capability rules as the core. It
  needs **internet egress** to YouTube's hosts and no access to any private
  network. Restricting egress (an allowlisting proxy, firewall rules for private
  ranges) is documented as the deployment's job. It honours `HTTPS_PROXY`.
  Settings: `PUBLIC_ORIGIN` (required), `TRUSTED_PROXIES`, `FETCH_MAX_SECONDS`
  (900), `FETCH_MAX_BYTES` (40 MB), `FETCH_TIMEOUT_SECONDS` (120),
  `RATE_LIMIT_FETCHES_PER_MINUTE` (5), `FETCH_RESULT_TTL_SECONDS` (600).
- **Fetcher contract (the README gets a second table):** image
  `ghcr.io/<owner>/strange_ringtone-fetcher:<semver>`, linux/amd64, UID/GID
  10001, port 8080/tcp plain HTTP. The fetcher serves the `/fetch/` prefix
  itself (`GET /fetch/healthz`, `POST /fetch/api/jobs`,
  `GET /fetch/api/jobs/<id>` and `…/audio`), and the proxy forwards those
  paths unchanged, so no rewriting is needed. Writable: `/tmp` (tmpfs) only,
  sized at `2 × FETCH_MAX_BYTES + 16 MB` for the audio, the info JSON and the
  Deno and yt-dlp caches: 96 MB with the defaults.

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
  is empty by default.
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
- **Every security check has a test that fails when the check is removed.**

## 11. Repository layout (proposed)

```
LICENSE  README.md  SECURITY.md  CHANGELOG.md  CLAUDE.md  .env.example
compose.example.yaml        # hardened example, ports only in a commented dev snippet
.dockerignore               # allowlist: only core/ reaches the build context
core/
  Dockerfile  ffmpeg/{VERSION,SHA256SUMS,ffmpeg-devel.asc,configure.txt,components.txt}
  __main__.py               # checkout entry point: python -I core
  strange_ringtone/{__main__.py,config.py,server.py,http_guard.py,static.py,wav.py,render.py,names.py,results.py}
  static/{index.html,style.css,app.js,waveform.js,player.js,share.js}
  static/audio/{decode,wav,loudness,suggest,fades,request}.js   # pure audio code, unit-tested under Node
  static/package.json       # {"type": "module"} for the Node tests only: not served, not in the image
fetcher/
  Dockerfile  requirements.in  requirements.txt
  strange_ringtone_fetch/{server.py,ytdlp.py}
tests/  tests/corpus/  tests/image/  tests/stubs/
scripts/{check_pr_title.py,check_contract_change.py,check_ffmpeg_release.py}   # stdlib checks CI runs
tools/spike/phase0.py       # regenerates the iOS test tones into spike-out/
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

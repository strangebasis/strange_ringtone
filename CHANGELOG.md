# Changelog

## 0.1.0 (2026-11-13)


### ⚠ BREAKING CHANGES

* `compose.example.yaml` and the deployment contract now set `memswap_limit` equal to `mem_limit` on every service, so no container gets swap on top of its memory limit.
* fetch a ten-minute part of a longer video ([#28](https://github.com/strangebasisdevs/strange_ringtone/issues/28))
* clean up after every yt-dlp run and restart the runner when that fails ([#26](https://github.com/strangebasisdevs/strange_ringtone/issues/26))
* contain the core's ffmpeg ([#25](https://github.com/strangebasisdevs/strange_ringtone/issues/25))

### Features

* add a magnified trim view, loop controls and playheads ([#35](https://github.com/strangebasisdevs/strange_ringtone/issues/35)) ([153146a](https://github.com/strangebasisdevs/strange_ringtone/commit/153146a49d56a6f6e6b05d208953d23e1edd57c0))
* add the core server ([#2](https://github.com/strangebasisdevs/strange_ringtone/issues/2)) ([f198094](https://github.com/strangebasisdevs/strange_ringtone/commit/f1980947aa0f006ce76e0c4ba6828853cc0aad8e))
* add the opt-in YouTube fetcher ([#5](https://github.com/strangebasisdevs/strange_ringtone/issues/5)) ([ba5fee0](https://github.com/strangebasisdevs/strange_ringtone/commit/ba5fee0168e5e9b3420c4c8a11a720616e8c2be3))
* add the release flow and settle the deployment contract ([#29](https://github.com/strangebasisdevs/strange_ringtone/issues/29)) ([4d7e2ea](https://github.com/strangebasisdevs/strange_ringtone/commit/4d7e2ea5c4374821f5a0f7abf9a6bf7d70138b2d))
* add the render pipeline and the core image ([#3](https://github.com/strangebasisdevs/strange_ringtone/issues/3)) ([96c929c](https://github.com/strangebasisdevs/strange_ringtone/commit/96c929cb12d8ba7b43c1df49c51af6e9dd8e9d0c))
* add the web app ([#4](https://github.com/strangebasisdevs/strange_ringtone/issues/4)) ([03775cb](https://github.com/strangebasisdevs/strange_ringtone/commit/03775cbfb17d0207c8151065d8fa956ac059ecda))
* confine the fetcher's egress to its own CONNECT forwarder ([#14](https://github.com/strangebasisdevs/strange_ringtone/issues/14)) ([875e7e9](https://github.com/strangebasisdevs/strange_ringtone/commit/875e7e91639d706dd7fce707cda8836a43a89b3b))
* contain the core's ffmpeg ([#25](https://github.com/strangebasisdevs/strange_ringtone/issues/25)) ([006e4d2](https://github.com/strangebasisdevs/strange_ringtone/commit/006e4d2daa0b4114b0c852629fe344943c180698))
* fetch a ten-minute part of a longer video ([#28](https://github.com/strangebasisdevs/strange_ringtone/issues/28)) ([e3d4727](https://github.com/strangebasisdevs/strange_ringtone/commit/e3d4727e8eb93e8cb7cc5b51835399f917c65968))
* run yt-dlp in its own runner container ([#15](https://github.com/strangebasisdevs/strange_ringtone/issues/15)) ([d8b45d3](https://github.com/strangebasisdevs/strange_ringtone/commit/d8b45d337d169b1c61a8b813aa4ab9d3e8f185a0))


### Bug Fixes

* **ci:** inline the nosemgrep suppression and pin strange_security to v1.0.0 ([#6](https://github.com/strangebasisdevs/strange_ringtone/issues/6)) ([d417dd6](https://github.com/strangebasisdevs/strange_ringtone/commit/d417dd6644e3c0318ddc7c654ce92a61d5eddf8f))
* clean up after every yt-dlp run and restart the runner when that fails ([#26](https://github.com/strangebasisdevs/strange_ringtone/issues/26)) ([94d5488](https://github.com/strangebasisdevs/strange_ringtone/commit/94d54883a2e40ef19adf5c9b30eb5ac3401a9bc2))
* count a render once its WAV passes, stop year-long caching of dev assets ([#23](https://github.com/strangebasisdevs/strange_ringtone/issues/23)) ([4019632](https://github.com/strangebasisdevs/strange_ringtone/commit/4019632a849878084fd5ee6b0a2f0181e5521aa4))
* **deps:** bump debian from `7ff6ba3` to `734a72b` in /core ([#32](https://github.com/strangebasisdevs/strange_ringtone/issues/32)) ([75049b1](https://github.com/strangebasisdevs/strange_ringtone/commit/75049b12e976b2a7dcf04ec9fcd7f8ddce726e29))
* **deps:** bump python from `6a0e06f` to `935c3c5` in /core ([#31](https://github.com/strangebasisdevs/strange_ringtone/issues/31)) ([6fba8dc](https://github.com/strangebasisdevs/strange_ringtone/commit/6fba8dc2192ffee9d47c8888b4b6dde7af61d561))
* **deps:** bump python from `6a0e06f` to `935c3c5` in /fetcher ([#30](https://github.com/strangebasisdevs/strange_ringtone/issues/30)) ([d64422e](https://github.com/strangebasisdevs/strange_ringtone/commit/d64422ec9325f14e0911dc145f308d8d6468841c))
* keep only YouTube's extractors in the fetcher image ([#34](https://github.com/strangebasisdevs/strange_ringtone/issues/34)) ([c18c03b](https://github.com/strangebasisdevs/strange_ringtone/commit/c18c03b89a827cb9a10741bc3c91c79dd27dcf99))
* let the iPhone's file picker choose audio files ([#27](https://github.com/strangebasisdevs/strange_ringtone/issues/27)) ([ef29f7e](https://github.com/strangebasisdevs/strange_ringtone/commit/ef29f7e70048f3385f9846affd0fb8c2bfb812f9))
* refuse audio over 64 MiB before the page decodes it ([#22](https://github.com/strangebasisdevs/strange_ringtone/issues/22)) ([81fc766](https://github.com/strangebasisdevs/strange_ringtone/commit/81fc766b0616b74fc80ff47255cf0b23a90514c8))

## Changelog

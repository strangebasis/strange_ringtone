# Security policy

## Supported versions

Only the latest release receives security fixes. Releases are listed on this
repository's Releases page; v0.1.0 is the first.

## Reporting a vulnerability

Please report it privately. On this repository's **Security** tab, choose
**Report a vulnerability**. Please don't open a public issue, pull request or
discussion for it.

Include what you found, how to reproduce it, and the image digest or commit you
tested. This project is maintained in spare time. Expect an acknowledgement
within a week, and a fix or a plan as soon as practical. Reporters are credited
in the advisory unless they prefer not to be.

## Scope

In scope:

- the core image and the opt-in fetcher image published from this repository
- the web app those images serve
- this repository's CI and release workflows

Out of scope:

- how a particular deployment configures its reverse proxy, network or access
  control. The app has no login of its own: the README's
  [Security model](README.md#security-model) says what it leaves to the
  deployment.
- bugs in upstream projects (FFmpeg, yt-dlp, Deno, Python). Please report those
  upstream. A report showing that this project exposes such a bug, or makes it
  worse, is in scope.

## Verifying an image

Release images are published with build provenance and an SBOM. Verify a
digest before you pin it, with the command in the README's
[Images and verification](README.md#images-and-verification): it names the
workflow that must have built the image and the release tag it must have
been built from.

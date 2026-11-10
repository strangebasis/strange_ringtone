# Security policy

## Supported versions

Only the latest release receives security fixes. There is no release yet.

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

There is no release yet, so there is nothing to verify today. Release images
will be published with build provenance and an SBOM (docs/PLAN.md, section
9.3). Once one is, verify its digest before you pin it:

```bash
gh attestation verify oci://ghcr.io/<owner>/strange_ringtone@sha256:<digest> --repo <owner>/strange_ringtone
```

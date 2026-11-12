# Never built. This file only holds the pinned SBOM generator that publish.yml
# hands to BuildKit (`sbom: generator=...`), in the one form Dependabot's
# docker updates can read and bump: the tag stays, the digest moves with it.
# publish.yml reads the reference from the FROM line and refuses anything that
# is not this image pinned by a sha256 digest.
FROM docker/buildkit-syft-scanner:stable-1@sha256:873a7458c3c8ac8e32c7b796c10e1600b07b963d2aa000a72a2d97aaaa2d6315

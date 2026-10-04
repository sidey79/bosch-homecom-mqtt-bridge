# Release Policy

## Versioning

Releases use Semantic Versioning (`MAJOR.MINOR.PATCH`). The canonical version is stored in `VERSION` and must be changed in the release pull request.

Every release pull request must also update all documented full-version container image tags, including the examples in `README.md`.

## Release pull request

1. Create a feature branch from `main`.
2. Update `VERSION` and the documented image tags to the same version.
3. Run the available validation checks: `make verify`, the Docker build, and Compose validation.
4. Open a pull request against `main` and merge it only after the required checks pass.

## Image publication

After a version change reaches `main`, the Docker workflow publishes multi-architecture images to GHCR for `linux/amd64` and `linux/arm64`. Stable releases receive the full version tag plus matching minor and major tags. Every build also receives an immutable `sha-<commit>` tag.

Deployments should use the full version tag for reproducibility. The image tags in the documentation must always refer to the release being prepared.

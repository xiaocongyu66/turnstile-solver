# Bundled Chromium (offline)

Packages from [jizijhj/chromium_1](https://gitee.com/jizijhj/chromium_1/releases).

| Tag | Arch | Files |
|-----|------|-------|
| 22.04_amd64 | x86_64 | chromium-browser.deb + codecs + l10n |
| 22.04_arm64 | aarch64 | same |
| 20.04_* / 22.10_* | | same layout |

Dockerfile installs from `vendor/chromium/<tag>/` by build arch.
Do **not** delete these debs — HF builds should not need Gitee network.

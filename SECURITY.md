# Security notes

- **Loopback only.** `panel_server.py` binds `127.0.0.1` by default. Do not pass `--bind 0.0.0.0`.
  There is no authentication on the loopback surface; anyone who can reach it can drive it.
- **No telemetry, no cloud call.** The panel reads local services; `point-at` uses local OCR. No
  screenshot or unfiltered screen text leaves the machine. The cloud-vision path is not included.
- **A screenshot is content.** `point-at` writes an audit row before capture, deletes the image after
  OCR, and never returns its path. `--privacy strict` fails closed when the classifier cannot run.
- **Grant access is real root.** The shipped catalogue has one no-op example. Read the rendered
  sudoers line before granting. The applier installs root-owned copies into
  `/usr/local/lib/khaoss`; never point a capability at a script the grantee can rewrite.
- **Run as your normal user.** The panel writes only under this directory and `~/.config/boardwalk`.
- **Report a leakage concern** with a reproducible path; do not paste a secret into an issue.

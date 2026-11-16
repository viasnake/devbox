# Working on devbox

Devbox provides `up`, `ssh`, `down`, `status`, and `destroy` from the repository
root through matching `mise run` tasks. The only environment is `main`.
Tasks pass arguments through to `uv run --locked devbox.py`; keep lifecycle
logic in Python and preserve interactive terminal access for SSH.
Keep the implementation small, explicit, and shared across Windows x86-64/WHPX,
Apple Silicon/HVF, and Intel Mac/HVF. Detect the physical CPU, including Rosetta.
Do not add Linux hosts, Windows ARM, TCG fallback, or another VM backend.

Keep the pinned uv in `mise.toml` aligned with CI. The one-time `mise run setup`
uses Homebrew on macOS and WinGet on Windows for QEMU, then uv for the locked
Python environment. Keep package installation out of lifecycle commands and
ordinary tests. Do not upgrade an existing QEMU during setup or automatically
enable host virtualization features. Leave Git, mise, OpenSSH, and the host
package manager as explicit prerequisites.

Keep README focused on one-time prerequisites and daily use. Keep operational
constraints here and all repository prose and comments in plain English.
Do not add design-document trees, progress reports, or copies of implementation
instructions. Use the existing commit-message style and a short-lived branch;
submit through a draft PR. Do not push directly to main or merge without a request.

## Boundaries

The main environment owns one independent, persistent Ubuntu disk. Use JSON for
settings, uv for the Python environment, pycdlib for NoCloud ISO creation, and
OpenSSH for connections. Do not implement ISO, SSH, or QEMU protocols.

Do not add Agent integration, provisioning hooks, language runtimes, Docker,
application templates, shared folders, a host service,
a supervisor, automatic startup/recovery, a GUI, snapshots, generic backend
interfaces, or additional destructive lifecycle commands. Cloud-init is limited
to the initial hostname, the dev user/key, SSH policy, and root filesystem
expansion.

Forward the host SSH agent for interactive SSH connections through OpenSSH.
Keep readiness and shutdown connections independent of the agent with forwarding
disabled. Leave starting agents and loading keys to the user.

Never modify existing VirtualBox registrations/disks, `.vagrant/`, untracked
checkout files, or the user's SSH keys/config. Remove only tracked Vagrant files
that the new implementation replaces. Only `environments/main/config.json` is
tracked by default. Never commit local VM state, keys, seed files, logs, caches,
or `.venv/`.

## Data and lifecycle

Keep runtime data outside the checkout in the documented OS-specific directory
or an absolute `DEVBOX_HOME`. Preserve private Unix modes and Windows user ACLs.
Use argument arrays without a shell. Check path syntax before creating files;
escape QEMU option delimiters and quote OpenSSH configuration paths. Refuse
symlink/junction storage rather than writing through it.

Pin official Ubuntu image URLs to a dated build with SHA-256 from that build's
[checksum manifest](https://cloud-images.ubuntu.com/releases/resolute/release-20261016/SHA256SUMS).
Never combine a moving URL with a fixed checksum. Verify downloads and cached
images before conversion. The machine disk must not depend on a backing cache.
On initialization failure, only unpublished files made by the current attempt
may be cleaned up. Interrupted staging directories from a killed CLI are preserved for manual
inspection; a later attempt uses a fresh staging directory.

Never overwrite an existing disk, key, seed, instance ID, or writable UEFI vars.
Publish first-boot artifacts together. If required artifacts are missing, stop
and explain the incomplete state; do not reconstruct them around a used disk.
A failed guest boot retains the same disk and identity on retry. Diagnose guest
cloud-init failures using SSH and `/var/log/cloud-init*.log`; do not erase data
or reset cloud-init to hide a failure.

Serialize lifecycle commands for main with an OS file lock, released when the CLI
exits. Preserve launch intent before spawning QEMU. Identify a process using its
executable, full argument list (including UUID/disk), and saved PID/start time.
Recover a process from the spawn/record gap only when its full identity matches.
A stale or recycled PID must not authorize any signal. Ambiguous or inaccessible
VM identity must fail closed. Do not create a persistent lock owner or supervisor.

Use WHPX/HVF explicitly and the firmware shipped with the selected QEMU install.
Keep writable ARM UEFI variables per environment and reuse them. Record the QEMU
version, machine, and firmware for diagnosis. A QEMU binary hash change alone is
not a reason to reject an existing VM; missing required features must produce an
error without rebuilding. Review firmware compatibility when upgrading QEMU.

All TCP forwards bind to 127.0.0.1. Report port conflicts without selecting a new
port. Check actual QEMU startup errors as well as the initial port probe. Never
report readiness based on spawn, an open port, or a fixed sleep. Authenticate SSH,
verify the cloud-init instance ID, and require successful cloud-init completion
within a bounded wait. Host keys use a dedicated known_hosts with accept-new;
reject changed keys without deleting the record or disabling verification.

Use saved launch connection settings while a VM is running, including when its
JSON has changed. Apply CPU/RAM/port changes on the next stopped start. Initial
hostname, disk size, image, platform, and architecture cannot change for the
existing disk. Do not resize/rebuild implicitly. SSH/down must still work if the
checkout configuration was edited or removed.

Down requests `sudo -n poweroff` over SSH and confirms the matching QEMU exited.
An SSH disconnect alone is not success. On failure or timeout leave the process
and disk intact. Do not send a force kill, reset host networking, or change
virtualization/security features automatically.

Status reports process state using the saved identity and launch connection
settings without claiming guest readiness. Destroy requires explicit `--yes`,
uses the same clean shutdown, and removes only the recognized `machines/main`
directory after confirming QEMU exited and revalidating storage. Preserve data
on shutdown failure, ambiguous process identity, or incomplete storage. Keep
cached images, the adjacent lock, and interrupted staging directories.

## Validation

Run the non-VM suite and whitespace checks:

```sh
uv run --locked python -m unittest discover -s tests -v
uv run --locked python -m py_compile devbox.py tests/test_devbox.py
git diff --check
```

Keep ordinary tests free of VM launches, package installs, host-setting changes,
and paid services. Stub QEMU and network downloads; use temporary directories for
file protection and process fixtures for PID reuse, duplicate starts, early exit,
readiness failures, and clean-stop timeout. Exercise real ISO creation and
OpenSSH config parsing without connecting. Windows/macOS CI runs these non-VM
checks. CI is not evidence of successful hardware-accelerated VM acceptance.

On Windows x86-64 and Apple Silicon, separately verify a fresh checkout's first
up, SSH login, hostname/architecture/Ubuntu release, cloud-init success and rootfs
size. In the guest, check `hostname`, `uname -m`, `cat /etc/os-release`,
`cloud-init status --long`, `df -h /`, and external DNS/HTTPS access. Create a file,
down, up again, and check its contents. Check repeated up, ports 2222 and 8080,
a port conflict, and down on an already stopped environment.
Close the calling terminal after up and use a new terminal for ssh/down; only QEMU
should remain. Check QEMU or SSH forwarding to a small guest web server and clear
errors for missing QEMU/firmware. Intel Mac acceptance is separate.

In a disposable environment, check status before creation, while running, and
after down. Verify destroy refuses without `--yes`, then cleanly stops and
removes only that environment when confirmed. Check repeated destroy and that
the next up creates a new identity.

Record unavailable hardware/virtualization acceptance as **blocked** in the PR,
with the host and checks that actually ran. Continue independent implementation
and testing. Never present Linux tests, fixture results, or CI as Windows/macOS
VM boot or terminal-detachment evidence.

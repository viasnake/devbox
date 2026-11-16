# devbox

Persistent Ubuntu 26.04 development VMs on Windows x86-64 (WHPX) and macOS (HVF).

Install [mise](https://mise.jdx.dev/installing-mise.html), then prepare the host once:

- **Windows x86-64:** install Git and OpenSSH Client, with WinGet on PATH.
  Enable **Windows Hypervisor Platform** in Windows Features and restart if
  required. Hardware virtualization must be enabled in firmware.
- **macOS:** install Git and OpenSSH through Apple's Command Line Tools
  (`xcode-select --install`), then install [Homebrew](https://brew.sh/).
  Use a macOS version supported by QEMU.

From this repository's root, run:

```sh
mise run setup
```

This installs the pinned uv, installs QEMU through Homebrew or WinGet, and
prepares the locked Python environment. It does not upgrade an existing QEMU.
The Windows installer and one-time virtualization setup may need administrator
approval; daily VM commands run as your normal user. On Windows, add the QEMU
installation directory (normally `C:\Program Files\qemu`) to your user PATH and
reopen the terminal. Existing installations can use `mise install` to install
only uv, keeping QEMU and OpenSSH on PATH.

Use the same commands on both systems:

```sh
mise run up
mise run ssh
mise run down
mise run status
```

The first `up` downloads and verifies a fixed official Ubuntu image, creates an
independent disk and a dedicated SSH key, and waits for SSH and cloud-init.
uv prepares the pinned Python and dependencies automatically. Subsequent starts
reuse the disk, key, and initial identity. After `up` returns, only QEMU stays in
the background; `down` requests a clean shutdown and waits for QEMU to exit.
A failed start preserves the disk. Run `up` again to recheck it, or `ssh` to
inspect a guest that is still running. There is no automatic force stop or reset.

`status` reports `not created`, `stopped`, or `running`, with the saved connection
ports for a running VM. It checks QEMU's identity without connecting to the guest;
`running` does not mean SSH or cloud-init is ready. Use `up` to verify readiness.

To permanently remove `main`, including its disk, dedicated SSH keys, host-key
records, and logs, run:

```sh
mise run destroy --yes
```

`destroy` requires `--yes`, requests a clean shutdown if needed, and deletes data
only after QEMU has exited. Failed shutdowns and unrecognized or incomplete
storage are preserved for inspection. Cached images and interrupted staging
directories are kept. The next `up` creates a new disk and identity.

## Configuration and connections

Commands use `main` by default, configured in `environments/main/config.json`.
Defaults are 4 CPUs, 8192 MiB RAM, an 80 GiB sparse disk, `127.0.0.1:2222`
for SSH, and `127.0.0.1:8080` forwarded to guest port 8080.

To add an environment, copy the configuration:

```sh
mkdir environments/dev
cp environments/main/config.json environments/dev/config.json
```

Edit the copy before the first start. Set a hostname such as `devbox-dev` and
choose unused host ports, for example SSH port 2223 and forwarded host port 8081.
Environments running at the same time must use different host ports, including
forwards. Guest ports can stay the same. Ports are never assigned automatically.

Pass the environment name after any lifecycle command:

```sh
mise run up dev
mise run ssh dev
mise run status dev
mise run down dev
mise run destroy dev --yes
```

Each environment has its own disk, keys, identity, logs, and lifecycle lock.
Names use 1-56 lowercase letters, digits, or hyphens, start and end with a letter
or digit, and cannot be Windows device names such as `con` or `nul`.
Only the `main` configuration is tracked by default; additional configurations
stay local. `ssh`, `down`, `status`, and `destroy` work from saved VM data even
if that environment's configuration was removed.

CPU, RAM, SSH port, and `forwards` changes apply after `down` and the next `up`.
The initial hostname, disk size, image, and host architecture cannot change for
an existing disk. Running VMs keep their saved connection settings.

Add or change TCP forwards in the JSON:

```json
"forwards": [{ "host_port": 8080, "guest_port": 8080 }]
```

All host listeners bind to `127.0.0.1`. For this QEMU forward, the guest application
must listen on its guest network address (for example `0.0.0.0:8080`). For a guest
application listening only on localhost, use OpenSSH forwarding instead:

```sh
mise run ssh -- -L 8081:localhost:8080
mise run ssh dev -- -L 8082:localhost:8080
```

`up` prints the environment's `ssh-config` path. OpenSSH and editors can use that
file directly with host alias `devbox-<name>` (for example `devbox-dev`). Devbox
does not edit your `~/.ssh/config`, share your home, or forward your SSH agent.
The guest user is `dev`, with passwordless sudo and public-key login only.

## Local data

VMs and logs live outside the checkout:

- Windows: `%LOCALAPPDATA%\devbox`
- macOS: `~/Library/Application Support/devbox`

Set `DEVBOX_HOME` to an absolute, private directory to override this location.
Spaces and Unicode are supported. Paths containing percent signs, double quotes,
newlines, symlinks, or junctions are refused. Keep the same location across starts.
The disk, SSH key, host-key records, and logs are under `machines/<name>/`.
Back up that directory only after a successful `down`.

Existing VirtualBox VMs, disks, `.vagrant/`, and host SSH files are left in place.
Their migration, shutdown, and removal are separate tasks. If an old VM owns
port 2222 or 8080, stop it before starting devbox. NAT provides convenient
network access, not complete isolation from the host or other networks.

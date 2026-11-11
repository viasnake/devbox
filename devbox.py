"""Start, connect to, and gracefully stop one persistent QEMU VM per environment."""

import argparse
import ctypes
import hashlib
import io
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

import psutil
import pycdlib

ROOT = Path(__file__).resolve().parent
BOOT_TIMEOUT = 300
HALT_TIMEOUT = 120


class DevboxError(Exception):
    pass


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DevboxError(f"Cannot read {path}: {exc}") from exc


def atomic_json(path, value):
    with temporary_file(path.parent) as tmp:
        tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)


@contextmanager
def temporary_file(parent):
    fd, name = tempfile.mkstemp(prefix=".devbox-", dir=parent)
    os.close(fd)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def run(args, *, timeout=30, check=True, **kwargs):
    try:
        result = subprocess.run(
            [str(arg) for arg in args],
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DevboxError(f"{Path(args[0]).name}: {exc}") from exc
    if check and result.returncode:
        raise DevboxError(
            f"{Path(args[0]).name} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def environment_name(name):
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", name):
        raise DevboxError(
            "Environment name must contain 1-40 lowercase letters, digits, or hyphens; no leading/trailing hyphen."
        )
    if name in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }:
        raise DevboxError("Environment name is reserved on Windows.")
    return name


def validate_config(config):
    required = {"hostname", "cpus", "memory_mib", "disk_gib", "ssh_port", "forwards"}
    if not isinstance(config, dict) or set(config) != required:
        raise DevboxError(
            f"Configuration must contain exactly: {', '.join(sorted(required))}"
        )
    hostname = config["hostname"]
    if (
        not isinstance(hostname, str)
        or not re.fullmatch(r"devbox-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", hostname)
        or len(hostname) > 63
    ):
        raise DevboxError(
            "hostname must be devbox-<name>, at most 63 lowercase letters, digits, and hyphens."
        )
    for key, low, high in [
        ("cpus", 1, 64),
        ("memory_mib", 512, 1048576),
        ("disk_gib", 8, 1048576),
        ("ssh_port", 1024, 65535),
    ]:
        value = config[key]
        if type(value) is not int or not low <= value <= high:
            raise DevboxError(f"{key} must be an integer from {low} to {high}.")
    if not isinstance(config["forwards"], list):
        raise DevboxError("forwards must be a list.")
    ports = {config["ssh_port"]}
    for forward in config["forwards"]:
        if not isinstance(forward, dict) or set(forward) != {"host_port", "guest_port"}:
            raise DevboxError("Each forward needs only host_port and guest_port.")
        for key, low in [("host_port", 1024), ("guest_port", 1)]:
            if type(forward[key]) is not int or not low <= forward[key] <= 65535:
                raise DevboxError(f"{key} must be an integer from {low} to 65535.")
        if forward["host_port"] in ports:
            raise DevboxError(f"Duplicate host port: {forward['host_port']}")
        ports.add(forward["host_port"])
    return config


def windows_native_arch():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.IsWow64Process2.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.POINTER(ctypes.c_ushort),
    ]
    process, native = ctypes.c_ushort(), ctypes.c_ushort()
    if not kernel.IsWow64Process2(
        kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)
    ):
        raise DevboxError("Cannot determine the native Windows CPU (IsWow64Process2).")
    return {0x8664: "amd64", 0xAA64: "arm64"}.get(native.value, "unsupported")


def host():
    system = platform.system()
    if system == "Windows":
        if windows_native_arch() != "amd64":
            raise DevboxError("Only Windows x86-64 with WHPX is supported.")
        return {
            "system": system,
            "arch": "amd64",
            "target": "x86_64",
            "accel": "whpx",
            "machine": "q35",
            "cpu": "max",
        }
    if system == "Darwin":
        # hw.optional.arm64 reports the physical CPU even under Rosetta.
        native = run(["/usr/sbin/sysctl", "-n", "hw.optional.arm64"], check=False)
        arch = "arm64" if native.stdout.strip() == "1" else "amd64"
        if arch == "amd64" and platform.machine() not in ("x86_64", "AMD64"):
            raise DevboxError("Cannot identify this Mac's physical CPU.")
        return {
            "system": system,
            "arch": arch,
            "target": "aarch64" if arch == "arm64" else "x86_64",
            "accel": "hvf",
            "machine": "virt" if arch == "arm64" else "q35",
            "cpu": "host",
        }
    raise DevboxError(
        "Supported hosts: Windows x86-64 (WHPX), macOS arm64/x86-64 (HVF). Linux is for unit tests only."
    )


def data_home(system):
    override = os.environ.get("DEVBOX_HOME")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise DevboxError("DEVBOX_HOME must be an absolute path.")
    elif system == "Windows":
        if not os.environ.get("LOCALAPPDATA"):
            raise DevboxError(
                "LOCALAPPDATA is missing; set DEVBOX_HOME to an absolute private directory."
            )
        path = Path(os.environ["LOCALAPPDATA"]) / "devbox"
    else:
        path = Path.home() / "Library" / "Application Support" / "devbox"
    validate_path(path)
    if path.resolve().is_relative_to(ROOT):
        raise DevboxError("DEVBOX_HOME must be outside this checkout.")
    return path


def validate_path(path):
    # OpenSSH config expands percent tokens and backslashes; reject ambiguous paths before writing.
    if any(char in str(path) for char in '\n\r\x00%"') or (
        os.name != "nt" and "\\" in str(path)
    ):
        raise DevboxError(f"Unsupported character in storage path: {path!s}")
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise DevboxError(f"Refusing a symlink/junction in managed storage: {part}")


def protect(path, system, directory=False):
    if system == "Windows":
        # Use a SID so localized account names do not affect ACL parsing.
        result = run(["whoami", "/user", "/fo", "csv", "/nh"])
        sid = re.search(r"S-1-\d+(?:-\d+)+", result.stdout)
        if not sid:
            raise DevboxError("Cannot resolve the current Windows user SID.")
        grant = f"*{sid.group()}:{'(OI)(CI)' if directory else ''}F"
        run(["icacls", str(path), "/inheritance:r", "/grant:r", grant])
    else:
        path.chmod(0o700 if directory else 0o600)


def private_directory(path, system):
    validate_path(path)
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
        protect(path, system, directory=True)
    elif not path.is_dir():
        raise DevboxError(f"Expected a directory: {path}")


@contextmanager
def machine_lock(home, name, system):
    parent = home / "machines"
    private_directory(parent, system)
    path = parent / f".{name}.lock"
    validate_path(path)
    with path.open("a+b") as lock:
        if path.stat().st_size == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if system == "Windows":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DevboxError(
                f"Another command is changing {name}; retry when it finishes."
            ) from exc
        try:
            yield parent / name
        finally:
            lock.seek(0)
            if system == "Windows":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def executable(name):
    found = shutil.which(name)
    if not found:
        raise DevboxError(
            f"Missing {name}. Install the prerequisites in README and reopen your terminal / check PATH."
        )
    return str(Path(found).resolve())


def firmware(qemu, arch):
    binary = Path(qemu).resolve().parent
    directories = [binary / "share" / "qemu", binary.parent / "share" / "qemu", binary]
    names = (
        ("edk2-aarch64-code.fd", "edk2-aarch64-vars.fd")
        if arch == "arm64"
        else ("bios-256k.bin",)
    )
    for directory in directories:
        paths = [directory / name for name in names]
        if all(path.is_file() for path in paths):
            return [str(path) for path in paths]
    raise DevboxError(
        f"Missing QEMU {arch} firmware ({', '.join(names)}) beside {qemu}. Install a complete QEMU distribution."
    )


def check_acceleration(profile):
    if profile["system"] == "Windows":
        try:
            api = ctypes.WinDLL("WinHvPlatform.dll")
            api.WHvGetCapability.argtypes = [
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            api.WHvGetCapability.restype = ctypes.c_long
            present, written = ctypes.c_uint64(), ctypes.c_uint32()
            status = api.WHvGetCapability(
                0, ctypes.byref(present), ctypes.sizeof(present), ctypes.byref(written)
            )
            if status < 0 or not present.value:
                raise OSError(f"HypervisorPresent={present.value}, HRESULT={status:#x}")
        except OSError as exc:
            raise DevboxError(
                "WHPX is unavailable. Enable hardware virtualization and Windows Hypervisor Platform, then restart Windows. No settings were changed."
            ) from exc
    else:
        available = run(["/usr/sbin/sysctl", "-n", "kern.hv_support"], check=False)
        if available.stdout.strip() != "1":
            raise DevboxError(
                "HVF is unavailable on this Mac. Check macOS/QEMU support and virtualization access; no TCG fallback is used."
            )


def preflight(profile):
    qemu = executable(f"qemu-system-{profile['target']}")
    tools = {
        "qemu": qemu,
        "img": executable("qemu-img"),
        "ssh": executable("ssh"),
        "keygen": executable("ssh-keygen"),
    }
    accel = run([qemu, "-accel", "help"]).stdout.split()
    if profile["accel"] not in accel:
        raise DevboxError(
            f"QEMU lacks {profile['accel']}. Install a native QEMU build; enable Windows Hypervisor Platform for WHPX. TCG is not used."
        )
    check_acceleration(profile)
    tools["firmware"] = firmware(qemu, profile["arch"])
    tools["version"] = run([qemu, "--version"]).stdout.splitlines()[0]
    return tools


def ports_available(config):
    for port in [
        config["ssh_port"],
        *(item["host_port"] for item in config["forwards"]),
    ]:
        with socket.socket() as sock:
            if os.name == "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as exc:
                raise DevboxError(
                    f"Port 127.0.0.1:{port} is unavailable: {exc}. Choose a free port in config.json; no automatic port changes."
                ) from exc


def image_for(arch):
    catalog = read_json(ROOT / "images.json")
    image = catalog["images"][arch]
    if not re.fullmatch(r"[0-9a-f]{64}", image["sha256"]) or not re.fullmatch(
        r"https://cloud-images\.ubuntu\.com/releases/resolute/release-\d{8}/ubuntu-26\.04-server-cloudimg-"
        + arch
        + r"\.img",
        image["url"],
    ):
        raise DevboxError(
            "images.json needs a fixed official Ubuntu image URL and SHA-256."
        )
    return image


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download_image(home, image, system):
    directory = home / "images"
    private_directory(directory, system)
    target = directory / f"{image['sha256']}.qcow2"
    validate_path(target)
    if target.exists():
        if digest(target) != image["sha256"]:
            raise DevboxError(
                f"Cached image hash mismatch: {target}. Preserved for inspection."
            )
        return target
    print(
        "Downloading and verifying the fixed Ubuntu image (first up only)...",
        flush=True,
    )
    with temporary_file(directory) as tmp:
        try:
            with (
                urllib.request.urlopen(image["url"], timeout=60) as response,
                tmp.open("wb") as output,
            ):
                shutil.copyfileobj(response, output)
        except OSError as exc:
            raise DevboxError(
                f"Image download failed: {exc}. Retry up; no machine disk has been created."
            ) from exc
        if digest(tmp) != image["sha256"]:
            raise DevboxError(
                "Downloaded Ubuntu image SHA-256 mismatch; refusing to use it."
            )
        # Publish without replacing a cache entry another environment may have created.
        try:
            os.link(tmp, target)
        except FileExistsError:
            if digest(target) != image["sha256"]:
                raise DevboxError(f"Concurrent image cache hash mismatch: {target}")
    return target


def create_seed(path, metadata, public_key):
    user = {
        "hostname": metadata["config"]["hostname"],
        "manage_etc_hosts": True,
        "users": [
            {
                "name": "dev",
                "groups": ["sudo"],
                "shell": "/bin/bash",
                "lock_passwd": True,
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "ssh_authorized_keys": [public_key],
            }
        ],
        "ssh_pwauth": False,
        "disable_root": True,
        "growpart": {"mode": "auto", "devices": ["/"]},
        "resize_rootfs": True,
        "write_files": [
            {
                "path": "/etc/ssh/sshd_config.d/00-devbox.conf",
                "permissions": "0644",
                "content": "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n",
            }
        ],
    }
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="cidata")
    try:
        for name, content in [
            ("user-data", "#cloud-config\n" + json.dumps(user)),
            (
                "meta-data",
                json.dumps(
                    {
                        "instance-id": metadata["instance_id"],
                        "local-hostname": metadata["config"]["hostname"],
                    }
                ),
            ),
        ]:
            data = content.encode("utf-8")
            iso.add_fp(
                io.BytesIO(data),
                len(data),
                iso_path="/" + name.replace("-", "_").upper() + ";1",
                rr_name=name,
                joliet_path="/" + name,
            )
        iso.write(str(path))
    finally:
        iso.close()


def initialize(directory, home, name, config, profile, tools, image):
    if directory.exists():
        raise DevboxError(
            f"Unrecognized or incomplete environment at {directory}; preserved. Inspect it or use another environment name."
        )
    base = download_image(home, image, profile["system"])
    # Only an unpublished staging directory is disposable. No launched disk is ever removed.
    with tempfile.TemporaryDirectory(
        prefix=f".{name}-creating-", dir=directory.parent
    ) as staging:
        stage = Path(staging)
        protect(stage, profile["system"], directory=True)
        metadata = {
            "schema": 1,
            "name": name,
            "arch": profile["arch"],
            "image": image,
            "config": config,
            "instance_id": str(uuid.uuid4()),
            "uuid": str(uuid.uuid4()),
            "profile": profile,
            "initialized": False,
            "launch": None,
            "process": None,
        }
        print(
            "Creating an independent disk, dedicated SSH key, and NoCloud seed...",
            flush=True,
        )
        run(
            [
                tools["img"],
                "convert",
                "-f",
                "qcow2",
                "-O",
                "qcow2",
                base,
                stage / "disk.qcow2",
            ],
            timeout=600,
        )
        run(
            [
                tools["img"],
                "resize",
                "-f",
                "qcow2",
                stage / "disk.qcow2",
                f"{config['disk_gib']}G",
            ]
        )
        info = json.loads(
            run([tools["img"], "info", "--output=json", stage / "disk.qcow2"]).stdout
        )
        if info.get("backing-filename") or info.get("format") != "qcow2":
            raise DevboxError(
                "Created disk is not an independent qcow2; refusing to publish it."
            )
        run(
            [
                tools["keygen"],
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"devbox-{name}",
                "-f",
                stage / "ssh_key",
            ]
        )
        protect(stage / "ssh_key", profile["system"])
        create_seed(
            stage / "seed.iso",
            metadata,
            (stage / "ssh_key.pub").read_text(encoding="utf-8").strip(),
        )
        if profile["arch"] == "arm64":
            shutil.copyfile(tools["firmware"][1], stage / "uefi-vars.fd")
        (stage / "known_hosts").touch(mode=0o600)
        (stage / "logs").mkdir()
        atomic_json(stage / "machine.json", metadata)
        if directory.exists():
            raise DevboxError(
                f"Environment appeared during initialization: {directory}; refusing to overwrite it."
            )
        stage.rename(directory)
        # TemporaryDirectory cleanup only sees its now absent staging path.
    return metadata


def load_machine(directory, name):
    validate_path(directory)
    if not directory.exists():
        return None
    for child in directory.rglob("*"):
        validate_path(child)
    metadata = read_json(directory / "machine.json")
    if metadata.get("schema") != 1 or metadata.get("name") != name:
        raise DevboxError(f"Unrecognized machine metadata at {directory}; preserved.")
    for key in ("uuid", "instance_id"):
        try:
            uuid.UUID(metadata[key])
        except (KeyError, ValueError, TypeError) as exc:
            raise DevboxError(f"Invalid {key} in machine.json; preserved.") from exc
    for filename in [
        "disk.qcow2",
        "seed.iso",
        "ssh_key",
        "ssh_key.pub",
        "known_hosts",
    ] + (["uefi-vars.fd"] if metadata["arch"] == "arm64" else []):
        if not (directory / filename).is_file():
            raise DevboxError(
                f"Missing {filename} in {directory}; existing disk will not be recreated. Inspect the environment or use a new name."
            )
    return metadata


def check_changes(metadata, config, profile, image):
    if metadata["arch"] != profile["arch"] or metadata["profile"] != profile:
        raise DevboxError(
            "This environment belongs to another host architecture/platform. Use a new environment name."
        )
    immutable = [
        key
        for key in ("hostname", "disk_gib")
        if metadata["config"][key] != config[key]
    ]
    if immutable or metadata["image"] != image:
        raise DevboxError(
            f"Initial hostname, disk capacity, or image changed ({', '.join(immutable) or 'image'}). Use a new environment; existing data is preserved."
        )


def qemu_path(path):
    return str(path).replace(",", ",,")


def qemu_args(directory, metadata, config, tools):
    profile = metadata["profile"]
    args = [
        tools["qemu"],
        "-name",
        f"devbox-{metadata['name']}",
        "-uuid",
        metadata["uuid"],
        "-machine",
        profile["machine"],
        "-accel",
        profile["accel"],
        "-cpu",
        profile["cpu"],
        "-smp",
        str(config["cpus"]),
        "-m",
        str(config["memory_mib"]),
        "-display",
        "none",
        "-monitor",
        "none",
        "-chardev",
        f"file,id=console,path={qemu_path(directory / 'logs' / 'console.log')}",
        "-serial",
        "chardev:console",
    ]
    if profile["arch"] == "arm64":
        args += [
            "-drive",
            f"if=pflash,format=raw,readonly=on,file={qemu_path(tools['firmware'][0])}",
            "-drive",
            f"if=pflash,format=raw,file={qemu_path(directory / 'uefi-vars.fd')}",
        ]
    else:
        args += ["-bios", tools["firmware"][0]]
    args += [
        "-drive",
        f"if=none,id=os,format=qcow2,file={qemu_path(directory / 'disk.qcow2')}",
        "-device",
        "virtio-blk-pci,drive=os,bootindex=0",
        "-drive",
        f"if=none,id=seed,format=raw,readonly=on,file={qemu_path(directory / 'seed.iso')}",
        "-device",
        "virtio-blk-pci,drive=seed",
    ]
    forwards = [
        {"host_port": config["ssh_port"], "guest_port": 22},
        *config["forwards"],
    ]
    network = "user,id=net0" + "".join(
        f",hostfwd=tcp:127.0.0.1:{item['host_port']}-:{item['guest_port']}"
        for item in forwards
    )
    return args + ["-netdev", network, "-device", "virtio-net-pci,netdev=net0"]


def process_matches(process, launch, vm_uuid):
    if process.status() == psutil.STATUS_ZOMBIE:
        return False
    argv = process.cmdline()
    # Compare complete arguments, not substrings that could identify a different disk/VM.
    return (
        os.path.normcase(str(Path(process.exe()).resolve()))
        == os.path.normcase(launch["argv"][0])
        and argv[1:] == launch["argv"][1:]
        and "-uuid" in argv
        and argv[argv.index("-uuid") + 1] == vm_uuid
    )


def find_process(metadata):
    launch, saved = metadata.get("launch"), metadata.get("process")
    if not launch:
        return None
    matches = []
    for process in psutil.process_iter():
        try:
            if (
                saved
                and process.pid == saved["pid"]
                and process.create_time() == saved["created"]
                and not process_matches(process, launch, metadata["uuid"])
            ):
                raise DevboxError(
                    f"PID {process.pid} no longer matches this QEMU; refusing to act."
                )
            if process_matches(process, launch, metadata["uuid"]):
                matches.append(process)
            elif metadata["uuid"] in process.cmdline():
                raise DevboxError(
                    "A process carries this VM UUID but does not match its recorded launch. Inspect it manually; no action taken."
                )
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied as exc:
            if saved and process.pid == saved["pid"]:
                raise DevboxError(
                    "Cannot identify the saved QEMU process; no action taken."
                ) from exc
            # Unrelated protected system processes are not VM candidates.
            continue
    if len(matches) > 1:
        raise DevboxError(
            "Multiple QEMU processes match this environment; inspect manually. No process was stopped."
        )
    return matches[0] if matches else None


def ssh_config(directory, config, name):
    def quoted(path):
        return '"' + str(path).replace("\\", "/") + '"'

    text = f"""Host devbox-{name}
    HostName 127.0.0.1
    Port {config["ssh_port"]}
    User dev
    IdentityFile {quoted(directory / "ssh_key")}
    IdentitiesOnly yes
    IdentityAgent none
    UserKnownHostsFile {quoted(directory / "known_hosts")}
    GlobalKnownHostsFile none
    StrictHostKeyChecking accept-new
    ForwardAgent no
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    ConnectTimeout 5
    ServerAliveInterval 15
    ServerAliveCountMax 3
"""
    with temporary_file(directory) as tmp:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, directory / "ssh-config")


def ssh_args(ssh, directory, name, *, batch=False):
    args = [ssh, "-F", str(directory / "ssh-config")]
    if batch:
        args += ["-o", "BatchMode=yes", "-T"]
    return args


def cloud_status(result, instance_id):
    if result.returncode == 255:
        error = result.stderr.strip()
        if any(
            marker in error
            for marker in (
                "REMOTE HOST IDENTIFICATION HAS CHANGED",
                "Host key verification failed",
                "Offending ",
            )
        ):
            raise DevboxError(
                "SSH host key changed; inspect this environment's known_hosts. It was not removed."
            )
        return False, f"SSH connection/authentication: {error}"
    try:
        identity, payload = result.stdout.split("\n", 1)
        if identity.strip() != instance_id:
            raise DevboxError(
                "SSH reached a different cloud-init instance; refusing to mark it ready."
            )
        status = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise DevboxError(
            f"Cannot read cloud-init status over authenticated SSH (exit {result.returncode})."
        ) from exc
    if status.get("status") == "error" or result.returncode not in (0,):
        raise DevboxError(
            f"cloud-init failed (exit {result.returncode}, status {status.get('status')}); inspect /var/log/cloud-init*.log in the guest. Disk preserved."
        )
    if status.get("status") == "disabled":
        raise DevboxError("cloud-init is disabled; initialization cannot be verified.")
    return status.get(
        "status"
    ) == "done", f"cloud-init: {status.get('status', 'unknown')}"


def wait_ready(directory, metadata, ssh, timeout=BOOT_TIMEOUT):
    deadline = time.monotonic() + timeout
    last = "SSH has not connected"
    print("Waiting for authenticated SSH and cloud-init completion...", flush=True)
    command = "cat /var/lib/cloud/data/instance-id && cloud-init status --format json"
    while time.monotonic() < deadline:
        if not find_process(metadata):
            raise DevboxError(
                f"QEMU exited before readiness. See {directory / 'logs' / 'qemu.log'} (including accelerator/bind errors)."
            )
        try:
            result = run(
                ssh_args(ssh, directory, metadata["name"], batch=True)
                + [f"devbox-{metadata['name']}", command],
                timeout=12,
                check=False,
            )
            ready, last = cloud_status(result, metadata["instance_id"])
            if ready:
                return
        except DevboxError as exc:
            if "timed out" not in str(exc):
                raise
            last = str(exc)
        time.sleep(2)
    raise DevboxError(
        f"Readiness timed out after {timeout}s; {last}. QEMU and disk are preserved. Logs: {directory / 'logs'}. Retry up to recheck; ssh is available for diagnosis."
    )


def detached_options(system):
    if system == "Windows":
        return {
            "creationflags": subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP,
            "close_fds": True,
        }
    return {"start_new_session": True, "close_fds": True}


def launch_machine(directory, metadata, config, tools):
    argv = qemu_args(directory, metadata, config, tools)
    metadata["launch"] = {
        "argv": argv,
        "config": config,
        "qemu_version": tools["version"],
        "firmware": tools["firmware"],
    }
    metadata["process"] = None
    # Save intent before spawn, so a crash in the spawn/record gap can be recovered by identity.
    atomic_json(directory / "machine.json", metadata)
    ssh_config(directory, config, metadata["name"])
    with (directory / "logs" / "qemu.log").open("ab") as log:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            **detached_options(metadata["profile"]["system"]),
        )
    try:
        metadata["process"] = {
            "pid": child.pid,
            "created": psutil.Process(child.pid).create_time(),
        }
    except psutil.NoSuchProcess as exc:
        raise DevboxError(
            f"QEMU exited at startup; see {directory / 'logs' / 'qemu.log'}."
        ) from exc
    atomic_json(directory / "machine.json", metadata)
    # Retain the handle for this CLI's wait; no supervisor process is created.
    return child


def up(directory, home, name, config, profile):
    metadata = load_machine(directory, name)
    image = image_for(profile["arch"])
    if metadata:
        check_changes(metadata, config, profile, image)
        process = find_process(metadata)
        if process:
            if metadata["launch"]["config"] != config:
                print(
                    "Configuration changed. Using the running VM's saved connection settings; halt then up to apply CPU/RAM/port changes."
                )
            ssh_config(directory, metadata["launch"]["config"], name)
            wait_ready(directory, metadata, executable("ssh"))
            metadata["initialized"] = True
            atomic_json(directory / "machine.json", metadata)
            show_connection(directory, name)
            return
    tools = preflight(profile)
    ports_available(config)
    if not metadata:
        metadata = initialize(directory, home, name, config, profile, tools, image)
    elif not metadata["initialized"]:
        print(
            "Reusing the existing disk and seed from an incomplete first boot; no initialization files will be replaced."
        )
    child = launch_machine(directory, metadata, config, tools)
    try:
        wait_ready(directory, metadata, tools["ssh"])
    finally:
        child.poll()
    metadata["initialized"] = True
    atomic_json(directory / "machine.json", metadata)
    show_connection(directory, name)


def show_connection(directory, name):
    print(
        f"Ready. uv run --locked devbox.py ssh {name}\nSSH config: {directory / 'ssh-config'}\nData and logs: {directory}"
    )


def halt(directory, name, metadata, timeout=HALT_TIMEOUT):
    if not metadata or not find_process(metadata):
        print(f"{name} is already stopped.")
        return
    ssh_config(directory, metadata["launch"]["config"], name)
    try:
        result = run(
            ssh_args(executable("ssh"), directory, name, batch=True)
            + [f"devbox-{name}", "sudo -n poweroff"],
            check=False,
            timeout=15,
        )
    except DevboxError as exc:
        raise DevboxError(
            f"Shutdown SSH request failed: {exc}. QEMU was not killed; inspect the guest before retrying halt."
        ) from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not find_process(metadata):
            metadata["process"] = None
            atomic_json(directory / "machine.json", metadata)
            print(f"{name} stopped; its QEMU process has exited.")
            return
        if result.returncode not in (0, 255):
            raise DevboxError(
                f"Shutdown request failed: {result.stderr.strip()}; QEMU was not killed."
            )
        time.sleep(1)
    raise DevboxError(
        f"Shutdown timed out; QEMU was not killed. SSH exit {result.returncode}: {result.stderr.strip()}. Inspect {directory / 'logs'}."
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "ssh", "halt"))
    parser.add_argument("name", nargs="?", default="main", type=environment_name)
    parser.add_argument(
        "ssh_options",
        nargs=argparse.REMAINDER,
        help="ssh only: OpenSSH options after the environment name (e.g. main -- -L 8080:localhost:8080)",
    )
    args = parser.parse_args(argv)
    if args.ssh_options and args.command != "ssh":
        parser.error("Extra arguments are only supported for ssh.")
    return args


def configure_output():
    # Redirected Windows streams can otherwise reject Unicode storage paths.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main(argv=None):
    configure_output()
    try:
        args = parse_args(argv)
        profile = host()
        home = data_home(profile["system"])
        private_directory(home, profile["system"])
        with machine_lock(home, args.name, profile["system"]) as directory:
            if args.command == "up":
                config = validate_config(
                    read_json(ROOT / "environments" / args.name / "config.json")
                )
                up(directory, home, args.name, config, profile)
                return 0
            metadata = load_machine(directory, args.name)
            if args.command == "halt":
                halt(directory, args.name, metadata)
                return 0
            if not metadata or not find_process(metadata):
                raise DevboxError(
                    f"{args.name} is stopped. Run: uv run --locked devbox.py up {args.name}"
                )
            ssh_config(directory, metadata["launch"]["config"], args.name)
            options = args.ssh_options
            if options[:1] == ["--"]:
                options = options[1:]
            # OpenSSH options belong before the generated host alias.
            command = (
                ssh_args(executable("ssh"), directory, args.name)
                + options
                + [f"devbox-{args.name}"]
            )
        return subprocess.call(command)
    except (DevboxError, OSError, KeyError, TypeError) as exc:
        print(f"devbox: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "devbox: Interrupted. Any launched QEMU and existing disk were preserved; retry up or use halt.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())

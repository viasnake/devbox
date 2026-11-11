import copy
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import devbox as d

CONFIG = {
    "hostname": "devbox-main",
    "cpus": 4,
    "memory_mib": 8192,
    "disk_gib": 80,
    "ssh_port": 2222,
    "forwards": [],
}
MAC = {
    "system": "Darwin",
    "arch": "arm64",
    "target": "aarch64",
    "accel": "hvf",
    "machine": "virt",
    "cpu": "host",
}
WINDOWS = {
    "system": "Windows",
    "arch": "amd64",
    "target": "x86_64",
    "accel": "whpx",
    "machine": "q35",
    "cpu": "max",
}
INTEL = dict(WINDOWS, system="Darwin", accel="hvf", cpu="host")


def result(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class Files(unittest.TestCase):
    def setUp(self):
        d.configure_output()
        self.temp = tempfile.TemporaryDirectory(prefix="devbox 日本語 space,")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.directory = self.home / "machines" / "main"
        self.directory.parent.mkdir()
        self.tools = {
            "qemu": str(self.home / "qemu"),
            "img": "qemu-img",
            "ssh": "ssh",
            "keygen": "ssh-keygen",
            "firmware": [str(self.home / "code.fd"), str(self.home / "vars.fd")],
            "version": "QEMU test",
        }
        for name in ("code.fd", "vars.fd"):
            (self.home / name).write_bytes(b"firmware")
        self.image = {
            "url": "https://example.invalid/image",
            "sha256": hashlib.sha256(b"image").hexdigest(),
        }
        self.metadata = {
            "schema": 1,
            "name": "main",
            "arch": "arm64",
            "profile": MAC,
            "uuid": str(uuid.uuid4()),
            "instance_id": str(uuid.uuid4()),
            "config": copy.deepcopy(CONFIG),
            "image": self.image,
            "launch": None,
            "process": None,
            "initialized": False,
        }

    def machine(self):
        self.directory.mkdir()
        (self.directory / "logs").mkdir()
        for filename in (
            "disk.qcow2",
            "seed.iso",
            "ssh_key",
            "ssh_key.pub",
            "known_hosts",
            "uefi-vars.fd",
        ):
            (self.directory / filename).write_bytes(filename.encode())
        d.atomic_json(self.directory / "machine.json", self.metadata)
        return self.metadata

    def prepared_launch(self):
        self.machine()
        self.metadata["launch"] = {
            "argv": d.qemu_args(self.directory, self.metadata, CONFIG, self.tools),
            "config": copy.deepcopy(CONFIG),
        }
        self.metadata["process"] = {"pid": 123, "created": 42.0}
        return self.metadata

    def process(self, pid=123, created=42.0, argv=None, exe=None):
        process = Mock(pid=pid)
        process.create_time.return_value = created
        process.status.return_value = d.psutil.STATUS_RUNNING
        process.cmdline.return_value = argv or self.metadata["launch"]["argv"]
        process.exe.return_value = exe or self.tools["qemu"]
        return process


class Configuration(Files):
    def test_default_and_alternate_commands(self):
        for command in ("up", "ssh", "halt"):
            self.assertEqual(d.parse_args([command]).name, "main")
            self.assertEqual(d.parse_args([command, "sandbox"]).name, "sandbox")
        args = d.parse_args(["ssh", "sandbox", "--", "-L", "8080:localhost:8080"])
        self.assertEqual(args.ssh_options[-2:], ["-L", "8080:localhost:8080"])

    def test_environment_traversal_and_windows_names(self):
        for name in (
            "../main",
            "a/b",
            "a\\b",
            "con",
            "lpt1",
            "Main",
            "-main",
            "main-",
            "",
            "a" * 41,
        ):
            with self.subTest(name=name), self.assertRaises(d.DevboxError):
                d.environment_name(name)

    def test_invalid_values_and_unknown_settings(self):
        for key, values in {
            "cpus": [True, 0, 65, 1.5],
            "memory_mib": [0, "8192"],
            "disk_gib": [7],
            "ssh_port": [22, 65536],
            "hostname": ["main", "devbox-", "devbox-x;echo", "devbox-" + "a" * 60],
            "forwards": [
                None,
                [{"host_port": 2222, "guest_port": 80}],
                [{"host_port": 8080, "guest_port": 0}],
                [{"host_port": 8080, "guest_port": 80, "bind": "0.0.0.0"}],
            ],
        }.items():
            for value in values:
                config = dict(CONFIG, **{key: value})
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaises(d.DevboxError),
                ):
                    d.validate_config(config)
        with self.assertRaises(d.DevboxError):
            d.validate_config(dict(CONFIG, backend="tcg"))

    def test_immutable_changes_require_new_environment(self):
        for key, value in (("hostname", "devbox-other"), ("disk_gib", 100)):
            with self.assertRaisesRegex(d.DevboxError, "new environment"):
                d.check_changes(
                    self.metadata, dict(CONFIG, **{key: value}), MAC, self.image
                )
        with self.assertRaises(d.DevboxError):
            d.check_changes(self.metadata, CONFIG, WINDOWS, self.image)
        with self.assertRaises(d.DevboxError):
            d.check_changes(
                self.metadata, CONFIG, MAC, dict(self.image, sha256="f" * 64)
            )
        d.check_changes(
            self.metadata, dict(CONFIG, cpus=2, ssh_port=4444), MAC, self.image
        )

    def test_running_config_is_used_and_no_second_spawn(self):
        self.prepared_launch()
        with (
            patch.object(d, "load_machine", return_value=self.metadata),
            patch.object(d, "image_for", return_value=self.image),
            patch.object(d, "find_process", return_value=Mock()),
            patch.object(d, "wait_ready") as ready,
            patch.object(d, "executable", return_value="ssh"),
            patch.object(d, "launch_machine") as launch,
            patch.object(d, "preflight") as preflight,
        ):
            d.up(self.directory, self.home, "main", dict(CONFIG, ssh_port=4444), MAC)
        self.assertIn(
            "Port 2222", (self.directory / "ssh-config").read_text(encoding="utf-8")
        )
        launch.assert_not_called()
        preflight.assert_not_called()
        ready.assert_called_once()

    def test_storage_rejects_unsafe_paths_before_mutation(self):
        for value in ("bad%h", 'bad"path', "bad\npath"):
            with (
                patch.dict(os.environ, {"DEVBOX_HOME": str(self.home / value)}),
                self.assertRaises(d.DevboxError),
            ):
                d.data_home("Darwin")
            self.assertFalse((self.home / value).exists())
        with (
            patch.dict(os.environ, {"DEVBOX_HOME": str(d.ROOT / "data")}),
            self.assertRaises(d.DevboxError),
        ):
            d.data_home("Darwin")

    def test_port_conflict_is_specific_and_not_renumbered(self):
        socket = Mock()
        socket.__enter__ = Mock(return_value=socket)
        socket.__exit__ = Mock(return_value=False)
        socket.bind.side_effect = OSError("in use")
        with (
            patch.object(d.socket, "socket", return_value=socket),
            self.assertRaisesRegex(d.DevboxError, "127.0.0.1:2222"),
        ):
            d.ports_available(CONFIG)


class Hosts(Files):
    def test_windows_native_arch_and_arm_rejection(self):
        with (
            patch.object(d.platform, "system", return_value="Windows"),
            patch.object(d, "windows_native_arch", return_value="amd64"),
        ):
            self.assertEqual(d.host(), WINDOWS)
        with (
            patch.object(d.platform, "system", return_value="Windows"),
            patch.object(d, "windows_native_arch", return_value="arm64"),
            self.assertRaises(d.DevboxError),
        ):
            d.host()

    def test_rosetta_and_intel(self):
        with (
            patch.object(d.platform, "system", return_value="Darwin"),
            patch.object(d.platform, "machine", return_value="x86_64"),
            patch.object(d, "run", return_value=result(out="1\n")),
        ):
            self.assertEqual(d.host(), MAC)
        with (
            patch.object(d.platform, "system", return_value="Darwin"),
            patch.object(d.platform, "machine", return_value="x86_64"),
            patch.object(d, "run", return_value=result(1)),
        ):
            self.assertEqual(d.host(), INTEL)
        with (
            patch.object(d.platform, "system", return_value="Linux"),
            self.assertRaisesRegex(d.DevboxError, "Supported hosts"),
        ):
            d.host()

    def test_args_all_platforms_and_loopback_only(self):
        config = dict(CONFIG, forwards=[{"host_port": 8080, "guest_port": 8081}])
        for profile in (MAC, INTEL, WINDOWS):
            metadata = dict(self.metadata, profile=profile)
            args = d.qemu_args(self.directory, metadata, config, self.tools)
            self.assertEqual(args[args.index("-accel") + 1], profile["accel"])
            self.assertEqual(args[args.index("-cpu") + 1], profile["cpu"])
            self.assertIn("hostfwd=tcp:127.0.0.1:2222-:22", args[-3])
            self.assertIn("hostfwd=tcp:127.0.0.1:8080-:8081", args[-3])
            self.assertNotIn("tcg", " ".join(args))
            self.assertIn("space,,", " ".join(args))
            self.assertEqual("uefi-vars.fd" in " ".join(args), profile == MAC)

    def test_firmware_comes_from_selected_qemu(self):
        binary = self.home / "bin" / "qemu-system-aarch64"
        binary.parent.mkdir()
        data = self.home / "share" / "qemu"
        data.mkdir(parents=True)
        with self.assertRaisesRegex(d.DevboxError, "firmware"):
            d.firmware(str(binary), "arm64")
        for filename in ("edk2-aarch64-code.fd", "edk2-aarch64-vars.fd"):
            (data / filename).write_bytes(b"firmware")
        self.assertEqual(len(d.firmware(str(binary), "arm64")), 2)
        with self.assertRaises(d.DevboxError):
            d.firmware(str(binary), "amd64")

    def test_missing_tools_and_acceleration_fail_before_creation(self):
        with (
            patch.object(d.shutil, "which", return_value=None),
            self.assertRaisesRegex(d.DevboxError, "Missing qemu"),
        ):
            d.preflight(MAC)
        with (
            patch.object(d, "executable", side_effect=lambda name: name),
            patch.object(d, "run", return_value=result(out="tcg")),
            self.assertRaisesRegex(d.DevboxError, "lacks hvf"),
        ):
            d.preflight(MAC)
        self.assertFalse(self.directory.exists())

    def test_detachment_flags(self):
        self.assertTrue(d.detached_options("Darwin")["start_new_session"])
        with (
            patch.object(d.subprocess, "DETACHED_PROCESS", 8, create=True),
            patch.object(d.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True),
        ):
            self.assertEqual(d.detached_options("Windows")["creationflags"], 520)

    def test_host_acceleration_check_without_vm_launch(self):
        with patch.object(d, "run", return_value=result(out="1\n")):
            d.check_acceleration(MAC)
        with (
            patch.object(d, "run", return_value=result(out="0\n")),
            self.assertRaisesRegex(d.DevboxError, "HVF is unavailable"),
        ):
            d.check_acceleration(MAC)
        api = Mock()

        def capability(code, buffer, size, written):
            self.assertEqual(code, 0)
            self.assertGreaterEqual(size, 8)
            buffer._obj.value = 1
            return 0

        api.WHvGetCapability.side_effect = capability
        with patch.object(d.ctypes, "WinDLL", return_value=api, create=True):
            d.check_acceleration(WINDOWS)
        with (
            patch.object(
                d.ctypes, "WinDLL", side_effect=OSError("missing"), create=True
            ),
            self.assertRaisesRegex(d.DevboxError, "WHPX is unavailable"),
        ):
            d.check_acceleration(WINDOWS)

    def test_windows_acl_uses_user_sid(self):
        with patch.object(
            d, "run", side_effect=[result(out='"user","S-1-5-21-123"'), result()]
        ) as run:
            d.protect(self.home, "Windows", directory=True)
        self.assertEqual(
            run.call_args.args[0][-3:],
            ["/inheritance:r", "/grant:r", "*S-1-5-21-123:(OI)(CI)F"],
        )


class Initialization(Files):
    def fake_tools(self, argv, **kwargs):
        args = [str(arg) for arg in argv]
        if "convert" in args:
            Path(args[-1]).write_bytes(b"independent disk")
            self.assertNotIn("-b", args)
        elif "info" in args:
            return result(out=json.dumps({"format": "qcow2"}))
        elif args[0] == "ssh-keygen":
            Path(args[-1]).write_bytes(b"PRIVATE KEY")
            Path(args[-1] + ".pub").write_text("ssh-ed25519 PUBLIC test")
        return result()

    def initialize(self):
        base = self.home / "base.qcow2"
        base.write_bytes(b"image")
        with (
            patch.object(d, "download_image", return_value=base),
            patch.object(d, "run", side_effect=self.fake_tools),
        ):
            return d.initialize(
                self.directory, self.home, "main", CONFIG, MAC, self.tools, self.image
            )

    def test_real_iso_contains_minimal_public_data(self):
        metadata = self.initialize()
        iso = d.pycdlib.PyCdlib()
        iso.open(str(self.directory / "seed.iso"))
        try:
            user, meta = io.BytesIO(), io.BytesIO()
            iso.get_file_from_iso_fp(user, rr_path="/user-data")
            iso.get_file_from_iso_fp(meta, rr_path="/meta-data")
        finally:
            iso.close()
        self.assertNotIn(b"PRIVATE KEY", user.getvalue())
        self.assertIn(b"PUBLIC", user.getvalue())
        content = json.loads(user.getvalue().decode().split("\n", 1)[1])
        self.assertEqual(content["users"][0]["name"], "dev")
        self.assertFalse(content["ssh_pwauth"])
        self.assertIn("PermitRootLogin no", content["write_files"][0]["content"])
        self.assertEqual(
            json.loads(meta.getvalue())["instance-id"], metadata["instance_id"]
        )
        self.assertFalse(metadata["initialized"])
        self.assertEqual((self.directory / "uefi-vars.fd").read_bytes(), b"firmware")

    def test_existing_disk_or_partial_directory_is_never_overwritten(self):
        self.machine()
        before = {
            p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file()
        }
        with (
            patch.object(d, "download_image") as download,
            self.assertRaises(d.DevboxError),
        ):
            d.initialize(
                self.directory, self.home, "main", CONFIG, MAC, self.tools, self.image
            )
        download.assert_not_called()
        self.assertEqual(
            before,
            {p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file()},
        )
        (self.directory / "machine.json").unlink()
        with self.assertRaises(d.DevboxError):
            d.load_machine(self.directory, "main")
        self.assertEqual((self.directory / "disk.qcow2").read_bytes(), b"disk.qcow2")

    def test_interrupted_creation_does_not_publish_and_retry_succeeds(self):
        base = self.home / "base.qcow2"
        base.write_bytes(b"base")
        with (
            patch.object(d, "download_image", return_value=base),
            patch.object(d, "run", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            d.initialize(
                self.directory, self.home, "main", CONFIG, MAC, self.tools, self.image
            )
        self.assertFalse(self.directory.exists())
        self.assertEqual(list(self.directory.parent.glob(".main-creating-*")), [])
        self.initialize()
        self.assertTrue((self.directory / "disk.qcow2").is_file())

    def test_missing_managed_file_does_not_recreate_disk(self):
        self.machine()
        (self.directory / "seed.iso").unlink()
        with self.assertRaisesRegex(d.DevboxError, "Missing seed.iso"):
            d.load_machine(self.directory, "main")
        self.assertEqual((self.directory / "disk.qcow2").read_bytes(), b"disk.qcow2")

    def test_valid_download_and_cache_verification(self):
        with patch.object(
            d.urllib.request, "urlopen", return_value=io.BytesIO(b"image")
        ) as download:
            path = d.download_image(self.home, self.image, "Darwin")
            self.assertEqual(path.read_bytes(), b"image")
            self.assertEqual(d.download_image(self.home, self.image, "Darwin"), path)
            download.assert_called_once()
        path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(d.DevboxError, "Cached image hash mismatch"):
            d.download_image(self.home, self.image, "Darwin")
        self.assertEqual(path.read_bytes(), b"corrupt")

    def test_bad_hash_and_download_failure_leave_no_image_or_machine(self):
        for response in (io.BytesIO(b"wrong"), OSError("interrupted")):
            with (
                patch.object(
                    d.urllib.request,
                    "urlopen",
                    side_effect=response if isinstance(response, Exception) else None,
                    return_value=response,
                ),
                self.assertRaises(d.DevboxError),
            ):
                d.download_image(self.home, self.image, "Darwin")
            self.assertEqual(list((self.home / "images").iterdir()), [])
            self.assertFalse(self.directory.exists())

    def test_reboot_preserves_identity_disk_key_seed_and_vars(self):
        metadata = self.initialize()
        before = {
            name: (self.directory / name).read_bytes()
            for name in ("disk.qcow2", "ssh_key", "seed.iso", "uefi-vars.fd")
        }
        for attempt in range(2):
            with (
                patch.object(d, "image_for", return_value=self.image),
                patch.object(d, "find_process", return_value=None),
                patch.object(d, "preflight", return_value=self.tools),
                patch.object(d, "ports_available"),
                patch.object(d, "launch_machine", return_value=Mock()) as launch,
                patch.object(d, "wait_ready"),
                patch.object(d, "initialize") as initialize,
            ):
                d.up(self.directory, self.home, "main", CONFIG, MAC)
                initialize.assert_not_called()
                self.assertEqual(
                    launch.call_args.args[1]["instance_id"], metadata["instance_id"]
                )
        self.assertEqual(
            before, {name: (self.directory / name).read_bytes() for name in before}
        )

    @unittest.skipIf(
        os.name == "nt", "Symlink creation can require a Windows privilege"
    )
    def test_symlink_in_managed_storage_is_refused(self):
        self.machine()
        (self.directory / "known_hosts").unlink()
        target = self.home / "unrelated"
        target.write_bytes(b"untouched")
        (self.directory / "known_hosts").symlink_to(target)
        with self.assertRaises(d.DevboxError):
            d.load_machine(self.directory, "main")
        self.assertEqual(target.read_bytes(), b"untouched")


class Processes(Files):
    def test_stale_and_recycled_pid_are_not_authority(self):
        self.prepared_launch()
        with patch.object(d.psutil, "process_iter", return_value=[]):
            self.assertIsNone(d.find_process(self.metadata))
        unrelated = self.process(created=999, argv=["other"], exe="/other")
        with patch.object(d.psutil, "process_iter", return_value=[unrelated]):
            self.assertIsNone(d.find_process(self.metadata))
        unrelated.terminate.assert_not_called()
        unrelated.kill.assert_not_called()

    def test_matching_pid_wrong_executable_or_arguments_is_refused(self):
        self.prepared_launch()
        for process in (
            self.process(exe="/other"),
            self.process(argv=[self.tools["qemu"], "-uuid", "other-vm"]),
        ):
            with (
                patch.object(d.psutil, "process_iter", return_value=[process]),
                self.assertRaises(d.DevboxError),
            ):
                d.find_process(self.metadata)

    def test_crash_after_spawn_recovers_by_full_identity(self):
        self.prepared_launch()
        self.metadata["process"] = None
        process = self.process()
        with patch.object(d.psutil, "process_iter", return_value=[process]):
            self.assertIs(d.find_process(self.metadata), process)
        process.cmdline.return_value = [
            self.tools["qemu"],
            "-uuid",
            self.metadata["uuid"],
        ]
        with (
            patch.object(d.psutil, "process_iter", return_value=[process]),
            self.assertRaisesRegex(d.DevboxError, "UUID"),
        ):
            d.find_process(self.metadata)

    def test_ambiguous_and_inaccessible_saved_process_fail_closed(self):
        self.prepared_launch()
        process = self.process()
        with (
            patch.object(
                d.psutil, "process_iter", return_value=[process, self.process(pid=456)]
            ),
            self.assertRaisesRegex(d.DevboxError, "Multiple"),
        ):
            d.find_process(self.metadata)
        process.create_time.side_effect = d.psutil.AccessDenied(123)
        with (
            patch.object(d.psutil, "process_iter", return_value=[process]),
            self.assertRaisesRegex(d.DevboxError, "Cannot identify"),
        ):
            d.find_process(self.metadata)

    def test_lock_contention_and_release(self):  # noqa: SIM117
        system = "Windows" if os.name == "nt" else "Darwin"
        with patch.object(d, "protect"):
            with d.machine_lock(self.home, "main", system):
                with self.assertRaisesRegex(d.DevboxError, "Another command"):
                    with d.machine_lock(self.home, "main", system):
                        self.fail("Second command acquired the lock")
            with d.machine_lock(self.home, "main", system):
                pass

    def test_launch_intent_is_saved_before_spawn_and_parent_uses_no_pipe(self):
        self.machine()

        def spawn(argv, **kwargs):
            saved = d.read_json(self.directory / "machine.json")
            self.assertEqual(saved["launch"]["argv"], argv)
            self.assertIsNone(saved["process"])
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(kwargs["start_new_session"])
            self.assertNotEqual(kwargs["stdout"], subprocess.PIPE)
            return Mock(pid=123)

        with (
            patch.object(d.subprocess, "Popen", side_effect=spawn),
            patch.object(
                d.psutil, "Process", return_value=Mock(create_time=lambda: 42.0)
            ),
        ):
            d.launch_machine(self.directory, self.metadata, CONFIG, self.tools)
        self.assertEqual(
            d.read_json(self.directory / "machine.json")["process"],
            {"pid": 123, "created": 42.0},
        )

    def test_halt_confirms_process_exit_not_ssh_disconnect(self):
        self.prepared_launch()
        with (
            patch.object(d, "find_process", side_effect=[Mock(), Mock(), None]),
            patch.object(d, "executable", return_value="ssh"),
            patch.object(
                d, "run", return_value=result(255, err="connection closed")
            ) as run,
            patch.object(d.time, "sleep"),
        ):
            d.halt(self.directory, "main", self.metadata)
        self.assertIn("sudo -n poweroff", run.call_args.args[0])
        self.assertIsNone(d.read_json(self.directory / "machine.json")["process"])

    def test_halt_timeout_never_kills_and_keeps_disk(self):
        self.prepared_launch()
        process = Mock()
        with (
            patch.object(d, "find_process", return_value=process),
            patch.object(d, "executable", return_value="ssh"),
            patch.object(d, "run", return_value=result()),
            self.assertRaisesRegex(d.DevboxError, "not killed"),
        ):
            d.halt(self.directory, "main", self.metadata, timeout=0)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertEqual((self.directory / "disk.qcow2").read_bytes(), b"disk.qcow2")

    def test_stopped_halt_is_success_without_ssh(self):
        with patch.object(d, "run") as run:
            d.halt(self.directory, "main", None)
        run.assert_not_called()


class SSHReadiness(Files):
    def status(self, state="done", code=0, instance=None):
        return result(
            code,
            (instance or self.metadata["instance_id"])
            + "\n"
            + json.dumps({"status": state}),
        )

    def test_only_authenticated_expected_instance_and_finished_init_are_ready(self):
        self.assertEqual(
            d.cloud_status(self.status(), self.metadata["instance_id"])[0], True
        )
        self.assertEqual(
            d.cloud_status(self.status("running"), self.metadata["instance_id"])[0],
            False,
        )
        self.assertFalse(
            d.cloud_status(result(255, err="Permission denied (publickey)"), "id")[0]
        )
        for status in (
            self.status("error", 1),
            self.status("done", 2),
            self.status("disabled"),
            self.status(instance="other"),
            result(0, "not json"),
        ):
            with self.assertRaises(d.DevboxError):
                d.cloud_status(status, self.metadata["instance_id"])

    def test_changed_host_key_is_refused(self):
        with self.assertRaisesRegex(d.DevboxError, "host key changed"):
            d.cloud_status(
                result(255, err="REMOTE HOST IDENTIFICATION HAS CHANGED"), "id"
            )

    def test_ready_wait_checks_init_after_ssh_and_qemu_exit(self):
        self.prepared_launch()
        with (
            patch.object(d, "find_process", return_value=Mock()),
            patch.object(
                d,
                "run",
                side_effect=[
                    result(255, err="Connection refused"),
                    self.status("running"),
                    self.status(),
                ],
            ) as run,
            patch.object(d.time, "sleep"),
        ):
            d.wait_ready(self.directory, self.metadata, "ssh")
        self.assertEqual(run.call_count, 3)
        with (
            patch.object(d, "find_process", return_value=None),
            self.assertRaisesRegex(d.DevboxError, "QEMU exited"),
        ):
            d.wait_ready(self.directory, self.metadata, "ssh")

    def test_ready_timeout_reports_authentication_and_preserves_data(self):
        self.prepared_launch()
        with (
            patch.object(d, "find_process", return_value=Mock()),
            patch.object(
                d, "run", return_value=result(255, err="Permission denied (publickey)")
            ),
            patch.object(d.time, "monotonic", side_effect=[0, 0, 2]),
            patch.object(d.time, "sleep"),
            self.assertRaisesRegex(d.DevboxError, "authentication.*Permission denied"),
        ):
            d.wait_ready(self.directory, self.metadata, "ssh", timeout=1)
        self.assertEqual((self.directory / "disk.qcow2").read_bytes(), b"disk.qcow2")

    def test_config_uses_only_private_key_hosts_and_quoted_paths(self):
        self.machine()
        d.ssh_config(self.directory, CONFIG, "main")
        text = (self.directory / "ssh-config").read_text(encoding="utf-8")
        for setting in (
            "IdentityAgent none",
            "ForwardAgent no",
            "StrictHostKeyChecking accept-new",
            "GlobalKnownHostsFile none",
            "IdentitiesOnly yes",
        ):
            self.assertIn(setting, text)
        self.assertIn(f'IdentityFile "{(self.directory / "ssh_key").as_posix()}"', text)
        self.assertNotIn("StrictHostKeyChecking no", text)
        self.assertFalse((self.home / ".ssh").exists())

    def test_unicode_connection_output_survives_a_legacy_windows_stream(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252")
        with patch.object(d.sys, "stdout", stream):
            d.configure_output()
            d.show_connection(self.directory, "main")
            stream.flush()
            self.assertIn("日本語", raw.getvalue().decode("utf-8"))
        stream.detach()

    def test_real_openssh_parses_generated_config_without_connecting(self):
        ssh = d.shutil.which("ssh")
        if not ssh:
            self.skipTest("OpenSSH client is unavailable")
        self.machine()
        d.ssh_config(self.directory, CONFIG, "main")
        output = d.run([ssh, "-G", "-F", self.directory / "ssh-config", "devbox-main"])
        self.assertIn("port 2222", output.stdout)
        self.assertIn("user dev", output.stdout)
        self.assertIn("日本語", output.stdout)


if __name__ == "__main__":
    unittest.main()

# devbox

Ubuntu 26.04 VMs managed with Vagrant and VirtualBox. One VM per environment, with no provisioning.

Install Vagrant and VirtualBox, then start your development environment:

```sh
cd environments/main
vagrant up
vagrant ssh
```

Use `vagrant halt` to stop the VM. `vagrant destroy` deletes it and its data.

If `vagrant status` reports `aborted-saved` and `vagrant halt` does not clear it, discard the saved VM state, then start it again:

```powershell
& 'C:\Program Files\Oracle\VirtualBox\VBoxManage.exe' discardstate default
vagrant up
```

Run these commands from the environment directory. Discarding the saved state loses the VM's suspended session, but keeps files on its virtual disk. If `discardstate` fails, open VirtualBox Manager and use **Discard Saved State** for that VM; do not remove the VM.

Environments inherit the root `Vagrantfile` and set their own hostname. See [AGENTS.md](AGENTS.md) for setup and operations.

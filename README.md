# devbox

Ubuntu 26.04 VMs managed with Vagrant and VirtualBox. One VM per environment, with no provisioning.

Install Vagrant and VirtualBox, then start your development environment:

```sh
cd environments/main
vagrant up
vagrant ssh
```

Use `vagrant halt` to stop the VM. `vagrant destroy` deletes it and its data.

Environments inherit the root `Vagrantfile` and set their own hostname. See [AGENTS.md](AGENTS.md) for setup and operations.

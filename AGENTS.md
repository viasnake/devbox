# Working on devbox

Keep shared VM settings in the root `Vagrantfile` and environment overrides in `environments/<name>/Vagrantfile`. Use VirtualBox only. The box is `bento/ubuntu-26.04`; defaults are 8 GB RAM and 4 CPUs per VM.

Keep this repository limited to VM lifecycle settings. Do not add provisioning, configuration management, wrappers, or compatibility code. Keep operational notes here, README brief, and all text in plain English. Do not add separate documentation.

## Environments

`environments/main/` is the personal development environment, with hostname `devbox-main`. To add another, copy only its `Vagrantfile` into a new environment directory and change the hostname:

```ruby
load File.expand_path("../../Vagrantfile", __dir__)

Vagrant.configure("2") do |config|
  config.vm.hostname = "devbox-sandbox"
end
```

Use a unique `devbox-<name>` hostname: lowercase letters, digits, and hyphens; at most 63 characters; start and end with a letter or digit. Override resources here as needed. Shared changes affect every environment.

Only `environments/main/Vagrantfile` is tracked under `environments/`. Other environments and local files are ignored. Never copy, commit, or expose `.vagrant/`; it contains VM state and private keys.

## Operations

Run Vagrant inside the target environment, where a local `Vagrantfile` must exist. Do not start VMs from the repository root. Each environment manages one `default` machine; VirtualBox assigns its VM name.

- Inspect: `vagrant status`, `vagrant ssh-config`, `vagrant port`.
- Run guest commands: `vagrant ssh -c '<command>'`. Account for host shell quoting; do not assume extra users or tools exist.
- Apply settings: `vagrant reload`. Changing the box requires recreating existing VMs to change their OS.
- Delete: `vagrant destroy`, only when deletion of that VM is authorized. It erases guest data. Do not delete `.vagrant/` as a substitute.

SSH maps localhost port 2222 to guest port 22, choosing another host port on conflict. Shared folders and SSH agent forwarding are disabled. Keep work in the guest. No guest firewall rules are added.

## Checks

Run `vagrant validate` in `environments/main/` and `git diff --check` at the root. When boot testing is in scope, run `vagrant up`, then `vagrant ssh -c 'hostname; cat /etc/os-release'` and `vagrant port`. Report checks that could not run.

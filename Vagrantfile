ENV["VAGRANT_DEFAULT_PROVIDER"] = "virtualbox"

Vagrant.configure("2") do |config|
  config.vm.box = "bento/ubuntu-26.04"

  config.vm.network "forwarded_port",
    guest: 22, host: 2222, host_ip: "127.0.0.1", id: "ssh", auto_correct: true
  config.ssh.forward_agent = false
  config.vm.synced_folder ".", "/vagrant", disabled: true

  config.vm.provider "virtualbox" do |vb|
    vb.memory = 8192
    vb.cpus = 4
    vb.gui = false
  end
end

# Networking

This is a living document describing how our networking and clustering is set up

## How to srt up the K8 cluster

*As of right now is untested, this is in the planning phase

### Disable swap

`sudo swapoff -a`
and take out the swap line in `swap line in /etc/fstab`

### Set unique hostnames

`sudo hostnamectl set-hostname db01` on the control/db node and `sudo hostnamectl set-hostname <hostname>` on the rest of the nodes

Our indexing nodes are named `inxx`, where `xx` increments with every new node added. The databse node is named `db01`

There should be a sticker on each machine corresponding to its hostname

### Add hostnames to hosts file

In order for all of the machines to correspond hostnames to ip addresses, add all of the machines' hostname and ip address (should be static by now) into all of the `/etc/hosts` files accross all machines

To make things easier, on a single node add all of the hostnames to the host file and run `pdcp -w host1,host2,host3 /etc/hosts /etc/hosts` to do all in one go

Before you do this ^ make sure to add ssh key access for the main node to ssh into all other nodes without having to type in the sudo password every time

### Disable Firewall?

You might need to disable the firewall on all of the nodes for the rest of the setup, try without diabling first and if you have issues come back to this. Try to keep the firewall enabled with the cluster is fully up and running though

Disable on Rocky with `sudo systemctl disable firewalld --now`, you can parralelize ti run the same command on multiple nodes with `pdsh -w in[01-10] "sudo systemctl disable firewalld --now"`

### Install K3 on all nodes

K3 is the runtime we use to cluster our nodes, install it on all nodes with `pdsh -w in[01-10] "curl -sfL https://get.k3s.io | sh -"`
`


 
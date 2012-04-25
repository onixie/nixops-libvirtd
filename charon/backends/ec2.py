# -*- coding: utf-8 -*-

import os
import sys
import re
import time
import subprocess
import shutil
import boto.ec2
from charon.backends import MachineDefinition, MachineState
import charon.known_hosts
import charon.util


class EC2Definition(MachineDefinition):
    """Definition of an EC2 machine."""

    @classmethod
    def get_type(cls):
        return "ec2"

    def __init__(self, xml):
        MachineDefinition.__init__(self, xml)
        x = xml.find("attrs/attr[@name='ec2']/attrs")
        assert x is not None
        self.access_key_id = x.find("attr[@name='accessKeyId']/string").get("value")
        self.type = x.find("attr[@name='type']/string").get("value")
        self.region = x.find("attr[@name='region']/string").get("value")
        self.controller = x.find("attr[@name='controller']/string").get("value")
        self.ami = x.find("attr[@name='ami']/string").get("value")
        if self.ami == "": raise Exception("no AMI defined for EC2 machine ‘{0}’".format(self.name))
        self.instance_type = x.find("attr[@name='instanceType']/string").get("value")
        self.key_pair = x.find("attr[@name='keyPair']/string").get("value")
        self.private_key = x.find("attr[@name='privateKey']/string").get("value")
        self.security_groups = [e.get("value") for e in x.findall("attr[@name='securityGroups']/list/string")]
        self.tags = {k.get("name"): k.find("string").get("value") for k in x.findall("attr[@name='tags']/attrs/attr")}
        def f(xml):
            return {'disk': xml.find("attrs/attr[@name='disk']/string").get("value"),
                    'size': int(xml.find("attrs/attr[@name='size']/int").get("value")),
                    'fsType': xml.find("attrs/attr[@name='fsType']/string").get("value"),
                    'deleteOnTermination': xml.find("attrs/attr[@name='deleteOnTermination']/bool").get("value") == "true"}
        self.block_device_mapping = {_xvd_to_sd(k.get("name")): f(k) for k in x.findall("attr[@name='blockDeviceMapping']/attrs/attr")}
        self.elastic_ipv4 = x.find("attr[@name='elasticIPv4']/string").get("value")

    def make_state():
        return MachineState()


class EC2State(MachineState):
    """State of an EC2 machine."""

    @classmethod
    def get_type(cls):
        return "ec2"
    
    def __init__(self, depl, name):
        MachineState.__init__(self, depl, name)
        self._conn = None
        self._access_key_id = None
        self._reset_state()


    def _reset_state(self):
        self._region = None
        self._zone = None
        self._controller = None
        self._ami = None
        self._instance_type = None
        self._key_pair = None
        self._private_key = None
        self._security_groups = None
        
        self._instance_id = None
        self._public_ipv4 = None
        self._private_ipv4 = None
        self._elastic_ipv4 = None
        self._tags = {}
        self._block_device_mapping = {}
        self._public_host_key = False
        self._public_vpn_key = False
        self._root_device_type = None
        
        
    def serialise(self):
        x = MachineState.serialise(self)
        
        if self._instance_id: x['vmId'] = self._instance_id
        if self._public_ipv4: x['ipv4'] = self._public_ipv4
        if self._private_ipv4: x['privateIpv4'] = self._private_ipv4

        y = {}
        if self._access_key_id: y['accessKeyId'] = self._access_key_id
        if self._region: y['region'] = self._region
        if self._zone: y['zone'] = self._zone
        if self._controller: y['controller'] = self._controller
        if self._ami: y['ami'] = self._ami
        if self._instance_type: y['instanceType'] = self._instance_type
        if self._key_pair: y['keyPair'] = self._key_pair
        if self._private_key: y['privateKey'] = self._private_key
        if self._security_groups: y['securityGroups'] = self._security_groups
        if self._tags: y['tags'] = self._tags
        if self._block_device_mapping: y['blockDeviceMapping'] = self._block_device_mapping
        if self._public_host_key: y['publicHostKey'] = self._public_host_key
        if self._public_vpn_key: y['publicVpnKey'] = self._public_vpn_key
        if self._root_device_type: y['rootDeviceType'] = self._root_device_type
        if self._elastic_ipv4: y['elasticIPv4'] = self._elastic_ipv4
        x['ec2'] = y
        
        return x

    
    def deserialise(self, x):
        MachineState.deserialise(self, x)

        self._instance_id = x.get('vmId', None)
        self._public_ipv4 = x.get('ipv4', None)
        self._private_ipv4 = x.get('privateIpv4', None)
        
        y = x.get('ec2')
        self._access_key_id = y.get('accessKeyId', None)
        self._region = y.get('region', None)
        self._zone = y.get('zone', None)
        self._controller = y.get('controller', None)
        self._ami = y.get('ami', None)
        self._instance_type = y.get('instanceType', None)
        self._key_pair = y.get('keyPair', None)
        self._private_key = y.get('privateKey', None)
        self._security_groups = y.get('securityGroups', None)
        self._tags = y.get('tags', {})
        self._block_device_mapping = y.get('blockDeviceMapping', {})
        self._public_host_key = y.get('publicHostKey', None)
        self._vpn_key_set = y.get('vpnKeySet', False)
        self._public_vpn_key = y.get('publicVpnKey', None)
        self._root_device_type = y.get('rootDeviceType', None)
        self._elastic_ipv4 = y.get('elasticIPv4', None)

        
    def get_ssh_name(self):
        if not self._public_ipv4:
            raise Exception("EC2 machine ‘{0}’ does not have a public IPv4 address (yet)".format(self.name))
        return self._public_ipv4

    
    def get_ssh_flags(self):
        return ["-i", self._private_key] if self._private_key else []

    
    def get_physical_spec(self, machines):
        lines = ['    require = [ <nixos/modules/virtualisation/amazon-config.nix> ];',
                 '    services.openssh.extraConfig = "PermitTunnel yes\\n";']
        authorized_keys = []
        tun = 0
        for m in machines.itervalues():
            tun = tun + 1
            if self != m and isinstance(m, EC2State) and self._region != m._region:
                # The two machines are in different regions, so they
                # can't talk directly to each other over their private
                # IP.  So create a VPN connection over their public
                # IPs to forward the private IPs.
                if self.name > m.name:
                    # Since it's a two-way tunnel, we only need to
                    # start it on one machine (for each pair of
                    # machines).  Pick the one that has the higher
                    # name (lexicographically).
                    lines.append('    jobs."vpn-to-{0}" = {{'.format(m.name))
                    lines.append('      startOn = "started network-interfaces";')
                    lines.append('      path = [ pkgs.nettools pkgs.openssh ];')
                    lines.append('      daemonType = "fork";')
                    lines.append('      exec = "ssh -i /root/.ssh/id_vpn -o StrictHostKeyChecking=no -f -x -w {0}:{0} {4} \'ifconfig tun{0} {2} {3} netmask 255.255.255.255; route add {2}/32 dev tun{0}\'";'
                                 .format(tun, m.name, self._private_ipv4, m._private_ipv4, m._public_ipv4))
                    lines.append('      postStart = "ifconfig tun{0} {2} {1} netmask 255.255.255.255; route add {2}/32 dev tun{0}";'
                                 .format(tun, self._private_ipv4, m._private_ipv4))
                    lines.append('    };')
                else:
                    # The other side just needs an authorized_keys entry.
                    authorized_keys.append('"' + m._public_vpn_key + '"')
        lines.append('    users.extraUsers.root.openssh.authorizedKeys.keys = [ {0} ];'.format(" ".join(authorized_keys)))
        return lines

    
    def show_type(self):
        s = MachineState.show_type(self)
        if self._zone or self._region: s = "{0} [{1}; {2}]".format(s, self._zone or self._region, self._instance_type)
        return s

    
    @property
    def vm_id(self):
        return self._instance_id

    
    @property
    def public_ipv4(self):
        return self._public_ipv4

    
    @property
    def private_ipv4(self):
        return self._private_ipv4

    
    def address_to(self, m):
        if isinstance(m, EC2State):
            return m._private_ipv4
        return MachineState.address_to(self, m)

    
    def connect(self):
        if self._conn: return
        assert self._region

        # Get the secret access key from the environment or from ~/.ec2-keys.
        secret_access_key = os.environ.get('EC2_SECRET_KEY') or os.environ.get('AWS_SECRET_ACCESS_KEY')
        path = os.path.expanduser("~/.ec2-keys")
        if os.path.isfile(path):
            f = open(path, 'r')
            contents = f.read()
            f.close()
            for l in contents.splitlines():
                l = l.split("#")[0] # drop comments
                w = l.split()
                if len(w) < 2: continue
                if w[0] == self._access_key_id:
                    secret_access_key = w[1]
                    break
            
        if not secret_access_key:
            raise Exception("please set $EC2_SECRET_KEY or $AWS_SECRET_ACCESS_KEY, or add the key for ‘{0}’ to ~/ec2-keys"
                            .format(self._access_key_id))

        self._conn = boto.ec2.connect_to_region(
            region_name=self._region, aws_access_key_id=self._access_key_id, aws_secret_access_key=secret_access_key)

        
    def _get_instance_by_id(self, instance_id, allow_missing=False):
        """Get instance object by instance id."""
        self.connect()
        reservations = self._conn.get_all_instances([instance_id])
        if len(reservations) == 0:
            if allow_missing: return None
            raise Exception("EC2 instance ‘{0}’ disappeared!".format(instance_id))
        return reservations[0].instances[0]


    def _get_volume_by_id(self, volume_id):
        """Get instance object by instance id."""
        self.connect()
        volumes = self._conn.get_all_volumes([volume_id])
        if len(volumes) != 1:
            raise Exception("unable to find volume ‘{0}’".format(volume_id))
        return volumes[0]


    def create(self, defn, check):
        assert isinstance(defn, EC2Definition)
        assert defn.type == "ec2"

        # Figure out the access key.
        if not self._access_key_id:
            self._access_key_id = defn.access_key_id or os.environ.get('EC2_ACCESS_KEY') or os.environ.get('AWS_ACCESS_KEY_ID')
            if not self._access_key_id:
                raise Exception("please set ‘deployment.ec2.accessKeyId’, $EC2_ACCESS_KEY or $AWS_ACCESS_KEY_ID")

        self._private_key = defn.private_key or None
        
        # Check whether the instance hasn't been killed behind our
        # backs.  Restart stopped instances.
        if self._instance_id and check:
            self.connect()
            instance = self._get_instance_by_id(self._instance_id, allow_missing=True)
            if instance is None or instance.state in {"shutting-down", "terminated"}:
                self.log("EC2 instance for ‘{0}’ went away (state ‘{1}’), will recreate".format(self.name, instance.state if instance else "gone"))
                self._reset_state()
                self.write()
            elif instance.state == "stopped":
                self.log("EC2 instance for ‘{0}’ was stopped, restarting...".format(self.name))

                # When we restart, we'll probably get a new IP.  So forget the current one.
                self._public_ipv4 = None
                self._private_ipv4 = None
                self.write()

                instance.start()

        # Start the instance.
        if not self._instance_id:
            self.log("creating EC2 instance ‘{0}’ (AMI ‘{1}’, type ‘{2}’, region ‘{3}’)...".format(
                self.name, defn.ami, defn.instance_type, defn.region))

            self._region = defn.region
            self.connect()

            # Figure out whether this AMI is EBS-backed.
            ami = self._conn.get_all_images([defn.ami])[0]

            (private, public) = self._create_key_pair()

            user_data = "SSH_HOST_DSA_KEY_PUB:{0}\nSSH_HOST_DSA_KEY:{1}\n".format(public, private.replace("\n", "|"))

            zone = None

            devmap = boto.ec2.blockdevicemapping.BlockDeviceMapping()
            devs_mapped = {}
            for k, v in defn.block_device_mapping.iteritems():
                if re.match("/dev/sd[a-e]", k) and not v['disk'].startswith("ephemeral"):
                    raise Exception("non-ephemeral disk not allowed on device ‘{0}’; use /dev/xvdf or higher".format(_sd_to_xvd(k)))
                if v['disk'] == '':
                    if ami.root_device_type == "ebs":
                        devmap[k] = boto.ec2.blockdevicemapping.BlockDeviceType(
                            size=v['size'], delete_on_termination=v['deleteOnTermination'])
                        v['needsInit'] = True
                        self._block_device_mapping[k] = v
                    # Otherwise, it's instance store backed, and we'll create the volume later.
                elif v['disk'].startswith("ephemeral"):
                    devmap[k] = boto.ec2.blockdevicemapping.BlockDeviceType(ephemeral_name=v['disk'])
                    self._block_device_mapping[k] = v
                elif v['disk'].startswith("snap-"):
                    devmap[k] = boto.ec2.blockdevicemapping.BlockDeviceType(snapshot_id=v['disk'], delete_on_termination=True)
                    self._block_device_mapping[k] = v
                elif v['disk'].startswith("vol-"):
                    # Volumes cannot be attached at boot time, so
                    # attach it later.  But make note of the placement
                    # zone of the volume.
                    volume = self._get_volume_by_id(v['disk'])
                    if not zone:
                        self.log("starting EC2 instance ‘{0}’ in zone ‘{1}’ due to volume ‘{2}’".format(
                            self.name, volume.zone, v['disk']))
                        zone = volume.zone
                    elif zone != volume.zone:
                        raise Exception("unable to start EC2 instance ‘{0}’ in zone ‘{1}’ because volume ‘{2}’ is in zone ‘{3}’"
                                        .format(self.name, zone, v['disk'], volume.zone))
                else:
                    raise Exception("device mapping ‘{0}’ not (yet) supported".format(v['disk']))

            # FIXME: Should use client_token to ensure idempotency.
            reservation = ami.run(
                instance_type=defn.instance_type,
                placement=zone,
                key_name=defn.key_pair,
                security_groups=defn.security_groups,
                block_device_map=devmap,
                user_data=user_data)

            assert len(reservation.instances) == 1

            instance = reservation.instances[0]

            self._instance_id = instance.id
            self._controller = defn.controller
            self._ami = defn.ami
            self._instance_type = defn.instance_type
            self._key_pair = defn.key_pair
            self._security_groups = defn.security_groups
            self._zone = instance.placement
            self._public_host_key = public
            self._root_device_type = ami.root_device_type
            
            self.write()

        # Reapply tags if they have changed.
        common_tags = {'CharonNetworkUUID': str(self.depl.uuid), 'CharonMachineName': self.name}
        tags = {'Name': "{0} [{1}]".format(self.depl.description, self.name)}
        tags.update(defn.tags)
        tags.update(common_tags)
        if check or self._tags != tags:
            self.connect()
            self._conn.create_tags([self._instance_id], tags)
            # TODO: remove obsolete tags?
            self._tags = tags
            self.write()

        # Assign or release an elastic IP address, if given.
        if (self._elastic_ipv4 or "") != defn.elastic_ipv4:
            self.connect()
            if defn.elastic_ipv4 != "":
                self.log("associating IP address ‘{0}’ with EC2 machine ‘{1}’...".format(
                    defn.elastic_ipv4, self.name))
                self._conn.associate_address(instance_id=self._instance_id, public_ip=defn.elastic_ipv4)
                self._elastic_ipv4 = defn.elastic_ipv4
                self._public_ipv4 = defn.elastic_ipv4
                self._ssh_pinged = False
                charon.known_hosts.add(defn.elastic_ipv4, self._public_host_key)
            else:
                self.log("disassociating IP address ‘{0}’ from EC2 machine ‘{1}’...".format(
                    self._elastic_ipv4, self.name))
                self._conn.disassociate_address(public_ip=self._elastic_ipv4)
                self._elastic_ipv4 = None
                self._public_ipv4 = None
            self.write()

        # Wait for the IP address.
        if not self._public_ipv4 or check:
            instance = self._get_instance_by_id(self._instance_id)
            sys.stderr.write("waiting for IP address of ‘{0}’... ".format(self.name))
            while True:
                sys.stderr.write("[{0}] ".format(instance.state))
                if instance.state not in {"pending", "running", "scheduling", "launching"}:
                    raise Exception("EC2 instance ‘{0}’ failed to start (state is ‘{1}’)".format(self._instance_id, instance.state))
                if instance.ip_address: break
                time.sleep(3)
                instance.update()
            sys.stderr.write("{0} / {1}\n".format(instance.ip_address, instance.private_ip_address))

            charon.known_hosts.add(instance.ip_address, self._public_host_key)
            
            self._private_ipv4 = instance.private_ip_address
            self._public_ipv4 = instance.ip_address
            self._ssh_pinged = False
            self.write()

        # Wait until the instance is reachable via SSH.
        self.wait_for_ssh(check=check)

        # Create missing volumes.
        # FIXME: support snapshots.
        for k, v in defn.block_device_mapping.iteritems():
            if k not in self._block_device_mapping and v['disk'] == '':
                self.log("creating {0} GiB volume for EC2 machine ‘{1}’...".format(v['size'], self.name))
                self.connect()
                volume = self._conn.create_volume(size=v['size'], zone=self._zone)
                v['needsInit'] = True
                # The flag charonDeleteOnTermination denotes that on
                # instance termination, we have to delete the volume
                # ourselves.  For volumes created at instance creation
                # time, EC2 will do it for us.
                v['charonDeleteOnTermination'] = v['deleteOnTermination']
                v['needsAttach'] = True
                v['volumeId'] = volume.id
                self._block_device_mapping[k] = v
                self.write()

        # Attach missing volumes.
        for k, v in defn.block_device_mapping.iteritems():
            if k not in self._block_device_mapping:
                self.log("attaching volume ‘{0}’ to EC2 machine ‘{1}’ as ‘{2}’...".format(v['disk'], self.name, _sd_to_xvd(k)))
                self.connect()
                if v['disk'].startswith("vol-"):
                    self._conn.attach_volume(v['disk'], self._instance_id, k)
                    self._block_device_mapping[k] = v
                    self.write()
                else:
                    raise Exception("adding device mapping ‘{0}’ to a running instance is not (yet) supported".format(v['disk']))

        for k, v in self._block_device_mapping.items():
            if v.get('needsAttach', False):
                self.log("attaching volume ‘{0}’ to EC2 machine ‘{1}’ as ‘{2}’...".format(v['volumeId'], self.name, _sd_to_xvd(k)))
                self.connect()

                volume_tags = {'Name': "{0} [{1} - {2}]".format(self.depl.description, self.name, _sd_to_xvd(k))}
                volume_tags.update(common_tags)
                self._conn.create_tags([v['volumeId']], volume_tags)
                
                volume = self._get_volume_by_id(v['volumeId'])
                if volume.volume_state() == "available":
                    self._conn.attach_volume(v['volumeId'], self._instance_id, k)
                # Wait until the device is visible in the instance.
                def check_dev():
                    res = self.run_command("test -e {0}".format(_sd_to_xvd(k)), check=False)
                    return res == 0
                charon.util.check_wait(check_dev)
                del v['needsAttach']
                self.write()

        # FIXME: process changes to the deleteOnTermination flag.
                
        # Detach volumes that are no longer in the deployment spec.
        for k, v in self._block_device_mapping.items():
            if k not in defn.block_device_mapping:
                self.log("detaching device ‘{0}’ from EC2 machine ‘{1}’...".format(_sd_to_xvd(k), self.name))
                self.connect()
                volumes = self._conn.get_all_volumes([], filters={'attachment.instance-id': self._instance_id, 'attachment.device': k})
                assert len(volumes) <= 1
                if len(volumes) == 1:
                    self.run_command("umount -l {0}".format(_sd_to_xvd(k)), check=False)
                    if not self._conn.detach_volume(volumes[0].id, instance_id=self._instance_id, device=k):
                        raise Exception("unable to detach device ‘{0}’ from EC2 machine ‘{1}’".format(v['disk'], self.name))
                    # FIXME: Wait until the volume is actually detached.
                    
                if v.get('charonDeleteOnTermination', False):
                    self._delete_volume(v['volumeId'])
                
                del self._block_device_mapping[k]
                self.write()

        # Format volumes that need it.
        for k, v in self._block_device_mapping.items():
            if v.get('needsInit', None) == 1:
                self.log("formatting device ‘{0}’ on EC2 machine ‘{1}’...".format(_sd_to_xvd(k), self.name))
                self.run_command("mkfs.{0} {1}".format(v['fsType'], _sd_to_xvd(k)))
                del v['needsInit']
                self.write()

        # Generate an SSH key for ad hoc VPN links between EC2
        # machines, and upload the private half.
        if not self._public_vpn_key:
            (private, public) = self._create_key_pair()
            f = open(self.depl.tempdir + "/id_vpn-" + self.name, "w+")
            f.write(private)
            f.seek(0)
            # FIXME: use run_command
            res = subprocess.call(
                ["ssh", "-x", "root@" + self.get_ssh_name()]
                + self.get_ssh_flags() +
                ["umask 077 && mkdir -p /root/.ssh && cat > /root/.ssh/id_vpn"],
                stdin=f)
            f.close()
            if res != 0: raise Exception("unable to upload VPN key to ‘{0}’".format(self.name))
            self._public_vpn_key = public
            self.write()


    def _delete_volume(self, volume_id):
        self.log("destroying EC2 volume ‘{0}’...".format(volume_id))
        try:
            volume = self._get_volume_by_id(volume_id)
            charon.util.check_wait(lambda: volume.update() == 'available')
            volume.delete()
        except boto.exception.EC2ResponseError as e:
            # Ignore volumes that have disappeared already.
            if e.error_code != "InvalidVolume.NotFound": raise


    def destroy(self):
        sys.stderr.write("destroying EC2 machine ‘{0}’... ".format(self.name))

        instance = self._get_instance_by_id(self._instance_id)
        instance.terminate()        

        # Wait until it's really terminated.
        while True:
            sys.stderr.write("[{0}] ".format(instance.state))
            if instance.state == "terminated": break
            time.sleep(3)
            instance.update()
        sys.stderr.write("\n")

        # Destroy volumes created for this instance.
        for k, v in self._block_device_mapping.items():
            if v.get('charonDeleteOnTermination', False):
                self._delete_volume(v['volumeId'])


def _xvd_to_sd(dev):
    return dev.replace("/dev/xvd", "/dev/sd")

def _sd_to_xvd(dev):
    return dev.replace("/dev/sd", "/dev/xvd")
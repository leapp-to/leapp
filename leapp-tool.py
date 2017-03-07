#!/usr/bin/python

from argparse import ArgumentParser
from leappto.providers.libvirt_provider import LibvirtMachineProvider
from json import dumps
from os import getuid
from subprocess import Popen, PIPE, check_output, CalledProcessError


def _get_ssh_config():
    # use vagrant ssh-config to obtain SSH configuration for the desired box
    ssh_kludge = {'target': 'ansible/centos7-target', 'source': 'ansible/centos6-guest-lamp'}
    out = {}
    for typ, path in ssh_kludge.iteritems():
        try:
            # bleak ugly code to convert SSH configuration file into bunch of `-o` flags for ssh
            out[typ] = ['-o {}={}'.format(*x.strip().split(' '))
                        for x in check_output(['vagrant', 'ssh-config'], cwd=path).decode('utf-8').splitlines()[1:-1]]
        except CalledProcessError:
            # domain probably not running
            pass
    return out


if getuid() != 0:
    print("Please run me as root")
    exit(-1)


ap = ArgumentParser()
ap.add_argument('-v', '--version', action='store_true', help='display version information')
parser = ap.add_subparsers(help='sub-command', dest='action')

list_cmd = parser.add_parser('list-machines', help='list running virtual machines and some information')
migrate_cmd = parser.add_parser('migrate-machine', help='migrate source VM to a target container host')

list_cmd.add_argument('pattern', nargs='*', default=['*'], help='list machines matching pattern')

migrate_cmd.add_argument('machine', help='source machine to migrate')
migrate_cmd.add_argument('-t', '--target', default=None, help='target VM name ')


def _find_machine(ms, name):
    for machine in ms:
        if machine.hostname == name:
            return machine
    return None


class MigrationContext:
    def __init__(self, target, target_cfg, disk):
        self.target = target
        self.target_cfg = target_cfg
        self.disk = disk

    @property
    def _ssh_base(self):
        return ['ssh'] + self.target_cfg + ['-4', self.target]

    def _ssh(self, cmd, **kwargs):
        arg = self._ssh_base + [cmd]
        return Popen(arg, **kwargs).wait()

    def _ssh_sudo(self, cmd, **kwargs):
        return self._ssh("sudo bash -c '{}'".format(cmd), **kwargs)

    def copy(self):
        proc = Popen(['virt-tar-out', '-a', self.disk, '/', '-'], stdout=PIPE)
        return self._ssh('cat > /opt/leapp-to/container.tar.gz', stdin=proc.stdout)

    def start_container(self, img, init):
        command = 'docker rm -f container 2>/dev/null 1>/dev/null ; rm -rf /opt/leapp-to/container ; mkdir -p /opt/leapp-to/container && ' + \
                  'tar xf /opt/leapp-to/container.tar.gz -C /opt/leapp-to/container && ' + \
                  'docker run -tid' + \
                  ' -v /sys/fs/cgroup:/sys/fs/cgroup:ro'
        good_mounts = ['bin', 'etc', 'home', 'lib', 'lib64', 'media', 'opt', 'root', 'sbin', 'srv', 'usr', 'var']
        for mount in good_mounts:
            command += ' -v /opt/leapp-to/container/{m}:/{m}:Z'.format(m=mount)
        command += ' -p 9000:9000 -p 9022:22 --name container ' + img + ' ' + init
        return self._ssh_sudo(command)

    def _fix_container(self, fix_str):
        return self._ssh_sudo('docker exec -t container {}'.format(fix_str))

    def fix_upstart(self):
        fixer = 'bash -c "echo ! waiting ; ' + \
                'sleep 2 ; ' + \
                'mkdir -p /var/log/httpd && ' + \
                'service mysqld start && ' + \
                'service httpd start"'
        return self._fix_container(fixer)

    def fix_systemd(self):
        # systemd cleans /var/log/ and mariadb & httpd can't handle that, might be a systemd bug
        fixer = 'bash -c "echo ! waiting ; ' + \
                'sleep 2 ; ' + \
                'mkdir -p /var/log/{httpd,mariadb} && ' + \
                'chown mysql:mysql /var/log/mariadb && ' + \
                'systemctl enable httpd mariadb ; ' + \
                'systemctl start httpd mariadb"'
        return self._fix_container(fixer)


parsed = ap.parse_args()
if parsed.action == 'list-machines':
    lmp = LibvirtMachineProvider()
    print(dumps({'machines': [m._to_dict() for m in lmp.get_machines()]}, indent=3))

elif parsed.action == 'migrate-machine':
    if not parsed.target:
        print('! no target specified, creating leappto container package in current directory')
        # TODO: not really for now
    else:
        source = parsed.machine
        target = parsed.target

        print('! looking up "{}" as source and "{}" as target'.format(source, target))

        lmp = LibvirtMachineProvider()
        machines = lmp.get_machines()

        machine_src = _find_machine(machines, source)
        machine_dst = _find_machine(machines, target)

        if not machine_dst or not machine_src:
            print("Machines are not ready:")
            print("Source: " + repr(machine_src))
            print("Target: " + repr(machine_dst))
            exit(-1)

        print('! obtaining SSH keys')
        ssh = _get_ssh_config()
        ip, config = machine_dst.ip[0], ssh['target']

        mc = MigrationContext(ip, ssh['target'], machine_src.disks[0].host_path)
        print('! copying over')
        mc.copy()
        print('! provisioning ...')
        # if el7 then use systemd
        if machine_src.installation.os.version.startswith('7'):
            mc.start_container('centos:7', '/usr/lib/systemd/systemd --system')
            print('! starting services')
            mc.fix_systemd()
            print('! done')
        else:
            mc.start_container('centos:6', '/sbin/init')
            print('! starting services')
            mc.fix_upstart()
            print('! done')
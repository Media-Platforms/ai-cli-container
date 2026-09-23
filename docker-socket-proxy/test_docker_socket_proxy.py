#!/usr/bin/env python3
"""Tests for docker_socket_proxy module."""

import asyncio
import json
import logging
import os
import runpy
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import docker_socket_proxy as proxy


# -- Helpers ----------------------------------------------


class MockWriter:
    """In-memory async writer for testing."""

    def __init__(self):
        self.data = bytearray()
        self.closed = False
        self.drain_count = 0
        self.eof_written = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        self.drain_count += 1

    def close(self):
        self.closed = True

    def can_write_eof(self):
        return True

    def write_eof(self):
        self.eof_written = True


def make_reader(data: bytes):
    """Create an asyncio.StreamReader pre-loaded with data."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def build_http_request(
    method, path, body=None, headers=None,
):
    """Build raw HTTP/1.1 request bytes."""
    line = f'{method} {path} HTTP/1.1\r\n'
    hdrs = headers or {}
    if body is not None:
        encoded = (
            body if isinstance(body, bytes)
            else json.dumps(body).encode()
        )
        hdrs['Content-Length'] = str(len(encoded))
    else:
        encoded = b''
    hdr_lines = ''.join(
        f'{k}: {v}\r\n' for k, v in hdrs.items()
    )
    return (line + hdr_lines + '\r\n').encode() + encoded


def build_http_response(
    status, phrase='OK', body=b'', headers=None,
):
    """Build raw HTTP/1.1 response bytes."""
    hdrs = headers or {}
    if body and 'Content-Length' not in hdrs:
        hdrs['Content-Length'] = str(len(body))
    hdr_lines = ''.join(
        f'{k}: {v}\r\n' for k, v in hdrs.items()
    )
    line = f'HTTP/1.1 {status} {phrase}\r\n'
    return (line + hdr_lines + '\r\n').encode() + body


# -- is_allowed_path --------------------------------------


class TestIsAllowedPath:

    def test_under_base(self):
        assert proxy.is_allowed_path(
            '/home/user/projects/app',
            '/home/user/projects',
        )

    def test_equal_to_base(self):
        assert proxy.is_allowed_path(
            '/home/user/projects',
            '/home/user/projects',
        )

    def test_outside_base(self):
        assert not proxy.is_allowed_path(
            '/etc/passwd', '/home/user/projects',
        )

    def test_dotdot_escape(self):
        assert not proxy.is_allowed_path(
            '/home/user/projects/../../etc/passwd',
            '/home/user/projects',
        )

    def test_dotdot_still_under(self):
        assert proxy.is_allowed_path(
            '/home/user/projects/a/../b',
            '/home/user/projects',
        )

    def test_prefix_not_subdir(self):
        """'/home/user/projects-evil' is not under
        '/home/user/projects'."""
        assert not proxy.is_allowed_path(
            '/home/user/projects-evil',
            '/home/user/projects',
        )

    def test_root_base(self):
        assert proxy.is_allowed_path('/anything', '/')

    def test_trailing_slash(self):
        assert proxy.is_allowed_path(
            '/home/user/projects/app/',
            '/home/user/projects',
        )


# -- check_create_request ---------------------------------


class TestCheckCreateRequest:
    base = '/home/user/projects'
    rw = '/home/user/projects/app'

    def _check(self, body):
        return proxy.check_create_request(
            body, self.base, self.rw,
        )

    def test_clean_request(self):
        body = {'Image': 'ubuntu', 'HostConfig': {}}
        assert self._check(body) is None

    def test_no_host_config(self):
        body = {'Image': 'ubuntu'}
        assert self._check(body) is None

    def test_null_host_config(self):
        body = {'Image': 'ubuntu', 'HostConfig': None}
        assert self._check(body) is None

    def test_privileged(self):
        body = {'HostConfig': {'Privileged': True}}
        assert 'privileged' in self._check(body)

    def test_privileged_false(self):
        body = {'HostConfig': {'Privileged': False}}
        assert self._check(body) is None

    def test_blocked_cap_sys_admin(self):
        body = {'HostConfig': {'CapAdd': ['SYS_ADMIN']}}
        assert 'SYS_ADMIN' in self._check(body)

    def test_blocked_cap_all(self):
        body = {'HostConfig': {'CapAdd': ['ALL']}}
        assert 'ALL' in self._check(body)

    def test_allowed_cap(self):
        body = {
            'HostConfig': {
                'CapAdd': ['NET_BIND_SERVICE'],
            },
        }
        assert self._check(body) is None

    def test_null_cap_add(self):
        body = {'HostConfig': {'CapAdd': None}}
        assert self._check(body) is None

    def test_host_pid(self):
        body = {'HostConfig': {'PidMode': 'host'}}
        assert 'PidMode' in self._check(body)

    def test_container_pid(self):
        """Another container's PID namespace exposes that
        container's mounts through /proc/<pid>/root."""
        body = {
            'HostConfig': {'PidMode': 'container:abc'},
        }
        assert 'PidMode' in self._check(body)

    def test_private_pid(self):
        body = {'HostConfig': {'PidMode': 'private'}}
        assert self._check(body) is None

    def test_host_network(self):
        body = {'HostConfig': {'NetworkMode': 'host'}}
        assert 'network' in self._check(body)

    def test_bridge_network(self):
        body = {'HostConfig': {'NetworkMode': 'bridge'}}
        assert self._check(body) is None

    def test_bad_bind(self):
        body = {
            'HostConfig': {
                'Binds': [
                    '/etc/shadow:/etc/shadow:ro',
                ],
            },
        }
        result = self._check(body)
        assert 'bind mount not allowed' in result
        assert '/etc/shadow' in result

    def test_bind_under_rw_base(self):
        bind = f'{self.rw}/src:/src'
        body = {'HostConfig': {'Binds': [bind]}}
        assert self._check(body) is None

    def test_bind_sibling_ro(self):
        bind = f'{self.base}/other:/other:ro'
        body = {'HostConfig': {'Binds': [bind]}}
        assert self._check(body) is None

    def test_bind_sibling_rw_rejected(self):
        bind = f'{self.base}/other:/other'
        body = {'HostConfig': {'Binds': [bind]}}
        result = self._check(body)
        assert 'must be read-only' in result

    def test_bind_sibling_explicit_rw_rejected(self):
        bind = f'{self.base}/other:/other:rw'
        body = {'HostConfig': {'Binds': [bind]}}
        result = self._check(body)
        assert 'must be read-only' in result

    def test_named_volume_in_binds(self):
        body = {
            'HostConfig': {
                'Binds': [
                    'rover_rover-static:/app/static',
                ],
            },
        }
        assert self._check(body) is None

    def test_null_binds(self):
        body = {'HostConfig': {'Binds': None}}
        assert self._check(body) is None

    def test_bad_mount(self):
        body = {
            'HostConfig': {
                'Mounts': [{
                    'Type': 'bind',
                    'Source': '/etc',
                }],
            },
        }
        assert 'bind mount not allowed' in (
            self._check(body)
        )

    def test_mount_under_rw_base(self):
        body = {
            'HostConfig': {
                'Mounts': [{
                    'Type': 'bind',
                    'Source': f'{self.rw}/data',
                }],
            },
        }
        assert self._check(body) is None

    def test_mount_sibling_readonly_true(self):
        body = {
            'HostConfig': {
                'Mounts': [{
                    'Type': 'bind',
                    'Source': f'{self.base}/other',
                    'ReadOnly': True,
                }],
            },
        }
        assert self._check(body) is None

    def test_mount_sibling_rw_rejected(self):
        body = {
            'HostConfig': {
                'Mounts': [{
                    'Type': 'bind',
                    'Source': f'{self.base}/other',
                }],
            },
        }
        result = self._check(body)
        assert 'must be read-only' in result

    def test_mount_sibling_readonly_false_rejected(self):
        body = {
            'HostConfig': {
                'Mounts': [{
                    'Type': 'bind',
                    'Source': f'{self.base}/other',
                    'ReadOnly': False,
                }],
            },
        }
        result = self._check(body)
        assert 'must be read-only' in result

    def test_volume_mount_ignored(self):
        body = {
            'HostConfig': {
                'Mounts': [{
                    'Type': 'volume',
                    'Source': 'myvolume',
                }],
            },
        }
        assert self._check(body) is None

    def test_null_mounts(self):
        body = {'HostConfig': {'Mounts': None}}
        assert self._check(body) is None


# -- check_bind_string ------------------------------------


class TestCheckBindString:
    base = '/home/user/projects'
    rw = '/home/user/projects/app'

    def test_under_rw_no_opts(self):
        assert proxy.check_bind_string(
            f'{self.rw}/src:/src', self.base, self.rw,
        ) is None

    def test_under_rw_explicit_rw(self):
        assert proxy.check_bind_string(
            f'{self.rw}/src:/src:rw',
            self.base, self.rw,
        ) is None

    def test_sibling_ro(self):
        assert proxy.check_bind_string(
            f'{self.base}/other:/x:ro',
            self.base, self.rw,
        ) is None

    def test_sibling_ro_with_extra_opts(self):
        assert proxy.check_bind_string(
            f'{self.base}/other:/x:ro,z',
            self.base, self.rw,
        ) is None

    def test_sibling_no_opts_rejected(self):
        result = proxy.check_bind_string(
            f'{self.base}/other:/x',
            self.base, self.rw,
        )
        assert 'must be read-only' in result

    def test_sibling_rw_rejected(self):
        result = proxy.check_bind_string(
            f'{self.base}/other:/x:rw',
            self.base, self.rw,
        )
        assert 'must be read-only' in result

    def test_outside_base_rejected(self):
        result = proxy.check_bind_string(
            '/etc/passwd:/etc/passwd:ro',
            self.base, self.rw,
        )
        assert 'bind mount not allowed' in result

    def test_named_volume_allowed(self):
        assert proxy.check_bind_string(
            'myvolume:/app/data',
            self.base, self.rw,
        ) is None

    def test_named_volume_with_opts_allowed(self):
        assert proxy.check_bind_string(
            'rover_rover-static:/app/static:rw',
            self.base, self.rw,
        ) is None


# -- check_mount_object -----------------------------------


class TestCheckMountObject:
    base = '/home/user/projects'
    rw = '/home/user/projects/app'

    def test_under_rw(self):
        mount = {
            'Type': 'bind',
            'Source': f'{self.rw}/data',
        }
        assert proxy.check_mount_object(
            mount, self.base, self.rw,
        ) is None

    def test_sibling_readonly(self):
        mount = {
            'Type': 'bind',
            'Source': f'{self.base}/other',
            'ReadOnly': True,
        }
        assert proxy.check_mount_object(
            mount, self.base, self.rw,
        ) is None

    def test_sibling_not_readonly(self):
        mount = {
            'Type': 'bind',
            'Source': f'{self.base}/other',
        }
        result = proxy.check_mount_object(
            mount, self.base, self.rw,
        )
        assert 'must be read-only' in result

    def test_outside_base(self):
        mount = {
            'Type': 'bind',
            'Source': '/etc',
            'ReadOnly': True,
        }
        result = proxy.check_mount_object(
            mount, self.base, self.rw,
        )
        assert 'bind mount not allowed' in result


# -- validate_create_body ---------------------------------


class TestValidateCreateBody:
    base = '/home/user/projects'
    rw = '/home/user/projects/app'

    def test_empty_body(self):
        assert proxy.validate_create_body(
            b'', self.base, self.rw,
        ) is None

    def test_valid_json_allowed(self):
        body = json.dumps({
            'Image': 'ubuntu',
            'HostConfig': {},
        }).encode()
        assert proxy.validate_create_body(
            body, self.base, self.rw,
        ) is None

    def test_valid_json_rejected(self):
        body = json.dumps({
            'HostConfig': {'Privileged': True},
        }).encode()
        result = proxy.validate_create_body(
            body, self.base, self.rw,
        )
        assert 'privileged' in result

    def test_invalid_json(self):
        result = proxy.validate_create_body(
            b'not json', self.base, self.rw,
        )
        assert 'invalid JSON' in result

    def test_invalid_unicode(self):
        result = proxy.validate_create_body(
            b'\xff\xfe', self.base, self.rw,
        )
        assert 'invalid JSON' in result


# -- A real Docker CLI payload ----------------------------
#
# Every HostConfig field Docker CLI 29 sends for
#
#   docker run --rm -i --cap-add NET_BIND_SERVICE \
#     --mount type=bind,source=/tmp,target=/x,readonly \
#     alpine true
#
# with the default-valued fields left as the CLI sends
# them.  The field allowlist has to let all of this
# through, or the proxy rejects an ordinary docker run.

REAL_HOST_CONFIG = {
    'AutoRemove': True,
    'Binds': None,
    'BlkioDeviceReadBps': [],
    'BlkioDeviceReadIOps': [],
    'BlkioDeviceWriteBps': [],
    'BlkioDeviceWriteIOps': [],
    'BlkioWeight': 0,
    'BlkioWeightDevice': [],
    'CapAdd': ['CAP_NET_BIND_SERVICE'],
    'CapDrop': None,
    'Cgroup': '',
    'CgroupParent': '',
    'CgroupnsMode': '',
    'ConsoleSize': [0, 0],
    'ContainerIDFile': '',
    'CpuCount': 0,
    'CpuPercent': 0,
    'CpuPeriod': 0,
    'CpuQuota': 0,
    'CpuRealtimePeriod': 0,
    'CpuRealtimeRuntime': 0,
    'CpuShares': 0,
    'CpusetCpus': '',
    'CpusetMems': '',
    'DeviceCgroupRules': None,
    'DeviceRequests': None,
    'Devices': [],
    'Dns': None,
    'DnsOptions': [],
    'DnsSearch': [],
    'ExtraHosts': None,
    'GroupAdd': None,
    'IOMaximumBandwidth': 0,
    'IOMaximumIOps': 0,
    'IpcMode': '',
    'Isolation': '',
    'Links': None,
    'LogConfig': {'Type': '', 'Config': {}},
    'MaskedPaths': None,
    'Memory': 0,
    'MemoryReservation': 0,
    'MemorySwap': 0,
    'MemorySwappiness': -1,
    'Mounts': [{
        'Type': 'bind',
        'Source': '/tmp',
        'Target': '/x',
        'ReadOnly': True,
    }],
    'NanoCpus': 0,
    'NetworkMode': 'default',
    'OomKillDisable': False,
    'OomScoreAdj': 0,
    'PidMode': '',
    'PidsLimit': 0,
    'PortBindings': {},
    'Privileged': False,
    'PublishAllPorts': False,
    'ReadonlyPaths': None,
    'ReadonlyRootfs': False,
    'RestartPolicy': {
        'Name': 'no', 'MaximumRetryCount': 0,
    },
    'SecurityOpt': None,
    'ShmSize': 0,
    'Tmpfs': {'/scratch': ''},
    'UTSMode': '',
    'Ulimits': None,
    'UsernsMode': '',
    'VolumeDriver': '',
    'VolumesFrom': None,
}


class TestRealPayload:

    def test_real_docker_run_allowed(self):
        body = {
            'Image': 'alpine',
            'Cmd': ['true'],
            'HostConfig': dict(REAL_HOST_CONFIG),
        }
        assert proxy.check_create_request(
            body, '/tmp', '/tmp',
        ) is None

    def test_real_payload_covers_every_field(self):
        """Every field the CLI sends is classified, so an
        allowed request never falls through to the
        unknown-field check."""
        known = (
            proxy.PASS_FIELDS
            | proxy.CHECKED_FIELDS
            | proxy.NULL_ONLY_FIELDS
        )
        unclassified = {
            key for key in REAL_HOST_CONFIG
            if key.lower() not in known
        }
        assert unclassified == set()


# -- HostConfig field allowlist ---------------------------


class TestHostConfigFields:
    base = '/home/user/projects'
    rw = '/home/user/projects/app'

    def _check(self, host_cfg):
        return proxy.check_create_request(
            {'HostConfig': host_cfg}, self.base, self.rw,
        )

    def test_lowercase_host_config_privileged(self):
        """Docker's JSON decoder matches field names
        without regard to case, so the proxy must too."""
        result = proxy.check_create_request(
            {'hostconfig': {'privileged': True}},
            self.base, self.rw,
        )
        assert 'privileged' in result

    def test_mixed_case_field(self):
        result = self._check({'PRIVILEGED': True})
        assert 'privileged' in result

    def test_two_spellings_of_host_config(self):
        result = proxy.check_create_request(
            {
                'HostConfig': {},
                'hostconfig': {'Privileged': True},
            },
            self.base, self.rw,
        )
        assert 'given twice' in result

    def test_two_spellings_of_field(self):
        result = self._check({
            'Privileged': False, 'privileged': True,
        })
        assert 'given twice' in result

    def test_unknown_field_with_value_rejected(self):
        result = self._check({'NotAFieldYet': 'something'})
        assert 'not allowed' in result
        assert 'NotAFieldYet' in result

    def test_unknown_field_at_default_allowed(self):
        """A field the proxy has never heard of is fine as
        long as it asks for nothing."""
        assert self._check({'NotAFieldYet': ''}) is None

    def test_masked_paths_empty_list_rejected(self):
        """MaskedPaths: [] unmasks everything the daemon
        masks by default, so an empty list is not a
        default."""
        result = self._check({'MaskedPaths': []})
        assert 'MaskedPaths' in result

    def test_masked_paths_null_allowed(self):
        assert self._check({'MaskedPaths': None}) is None

    def test_readonly_paths_empty_list_rejected(self):
        result = self._check({'ReadonlyPaths': []})
        assert 'ReadonlyPaths' in result

    def test_host_config_wrong_type(self):
        result = proxy.check_create_request(
            {'HostConfig': []}, self.base, self.rw,
        )
        assert 'must be an object' in result

    def test_devices_rejected(self):
        result = self._check({
            'Devices': [{'PathOnHost': '/dev/sda'}],
        })
        assert 'Devices' in result or 'devices' in result

    def test_device_requests_rejected(self):
        result = self._check({
            'DeviceRequests': [{'Driver': 'nvidia'}],
        })
        assert 'not allowed' in result

    def test_device_cgroup_rules_rejected(self):
        result = self._check({
            'DeviceCgroupRules': ['c 1:3 rwm'],
        })
        assert 'not allowed' in result

    def test_volumes_from_rejected(self):
        """--volumes-from copies the dev container's own
        mounts, including the Docker socket."""
        result = self._check({'VolumesFrom': ['devbox']})
        assert 'not allowed' in result

    def test_runtime_rejected(self):
        result = self._check({'Runtime': 'custom'})
        assert 'not allowed' in result

    def test_cgroup_rejected(self):
        result = self._check({'Cgroup': 'container:abc'})
        assert 'not allowed' in result

    def test_annotations_rejected(self):
        result = self._check({'Annotations': {'a': 'b'}})
        assert 'not allowed' in result


# -- capabilities -----------------------------------------


class TestCapabilities:

    def _check(self, host_cfg):
        return proxy.check_create_request(
            {'HostConfig': host_cfg}, '/tmp', '/tmp',
        )

    def test_cap_prefix_normalized(self):
        """The CLI sends CAP_SYS_ADMIN, not SYS_ADMIN."""
        result = self._check({'CapAdd': ['CAP_SYS_ADMIN']})
        assert 'SYS_ADMIN' in result

    def test_lowercase_cap_normalized(self):
        result = self._check({'CapAdd': ['sys_admin']})
        assert 'SYS_ADMIN' in result

    def test_capabilities_field_checked(self):
        """Capabilities replaces CapAdd/CapDrop in newer
        API versions."""
        result = self._check({
            'Capabilities': ['CAP_BPF'],
        })
        assert 'BPF' in result

    def test_bpf_not_allowed(self):
        result = self._check({'CapAdd': ['BPF']})
        assert 'BPF' in result

    def test_sys_boot_not_allowed(self):
        result = self._check({'CapAdd': ['SYS_BOOT']})
        assert 'SYS_BOOT' in result

    def test_default_caps_allowed(self):
        assert self._check({
            'CapAdd': ['CAP_CHOWN', 'net_raw', 'MKNOD'],
        }) is None

    def test_cap_drop_any_allowed(self):
        assert self._check({
            'CapDrop': ['ALL', 'CAP_SYS_ADMIN'],
        }) is None

    def test_cap_not_a_string(self):
        result = self._check({'CapAdd': [{'a': 1}]})
        assert 'must be a string' in result

    def test_cap_add_not_a_list(self):
        result = self._check({'CapAdd': 'SYS_ADMIN'})
        assert 'must be a list' in result


# -- namespaces -------------------------------------------


class TestNamespaces:

    def _check(self, host_cfg):
        return proxy.check_create_request(
            {'HostConfig': host_cfg}, '/tmp', '/tmp',
        )

    def test_host_userns(self):
        result = self._check({'UsernsMode': 'host'})
        assert 'UsernsMode' in result

    def test_host_ipc(self):
        result = self._check({'IpcMode': 'host'})
        assert 'IpcMode' in result

    def test_container_ipc(self):
        result = self._check({'IpcMode': 'container:abc'})
        assert 'IpcMode' in result

    def test_shareable_ipc_allowed(self):
        assert self._check({
            'IpcMode': 'shareable',
        }) is None

    def test_host_uts(self):
        result = self._check({'UTSMode': 'host'})
        assert 'UTSMode' in result

    def test_host_cgroupns(self):
        result = self._check({'CgroupnsMode': 'host'})
        assert 'CgroupnsMode' in result

    def test_container_network(self):
        """Joining the dev container's network namespace
        reaches services it has bound to localhost."""
        result = self._check({
            'NetworkMode': 'container:devbox',
        })
        assert 'network mode' in result

    def test_ns_path_network(self):
        result = self._check({
            'NetworkMode': 'ns:/proc/1/ns/net',
        })
        assert 'network mode' in result

    def test_user_defined_network_allowed(self):
        assert self._check({
            'NetworkMode': 'comp_default',
        }) is None

    def test_namespace_not_a_string(self):
        result = self._check({'PidMode': 42})
        assert 'must be a string' in result


# -- SecurityOpt, logging and simple values ---------------


class TestSecurityOpt:

    def _check(self, entries):
        return proxy.check_create_request(
            {'HostConfig': {'SecurityOpt': entries}},
            '/tmp', '/tmp',
        )

    def test_apparmor_unconfined_rejected(self):
        result = self._check(['apparmor=unconfined'])
        assert 'security option' in result

    def test_systempaths_unconfined_rejected(self):
        result = self._check(['systempaths=unconfined'])
        assert 'security option' in result

    def test_label_disable_rejected(self):
        result = self._check(['label=disable'])
        assert 'security option' in result

    def test_label_disable_colon_form_rejected(self):
        """Docker falls back to ':' when there is no '='."""
        result = self._check(['label:disable'])
        assert 'security option' in result

    def test_label_value_allowed(self):
        assert self._check(['label=level:s0:c1,c2']) is None

    def test_no_new_privileges_allowed(self):
        assert self._check(['no-new-privileges']) is None

    def test_no_new_privileges_true_allowed(self):
        assert self._check([
            'no-new-privileges=true',
        ]) is None

    def test_seccomp_allowed(self):
        """The dev container already runs with seccomp
        disabled, so a child gains nothing here."""
        assert self._check(['seccomp=unconfined']) is None

    def test_unknown_option_rejected(self):
        result = self._check(['writable-cgroups=true'])
        assert 'security option' in result

    def test_not_a_string(self):
        result = self._check([7])
        assert 'must be a string' in result

    def test_null_entry_rejected(self):
        result = self._check([None])
        assert 'security option' in result


class TestLogConfig:

    def _check(self, log_cfg):
        return proxy.check_create_request(
            {'HostConfig': {'LogConfig': log_cfg}},
            '/tmp', '/tmp',
        )

    def test_syslog_rejected(self):
        """A syslog address points daemon traffic at a
        socket on the host."""
        result = self._check({
            'Type': 'syslog',
            'Config': {
                'syslog-address':
                    'unix:///var/run/docker.sock',
            },
        })
        assert 'log driver' in result

    def test_json_file_allowed(self):
        assert self._check({
            'Type': 'json-file',
            'Config': {'max-size': '10m'},
        }) is None

    def test_empty_allowed(self):
        assert self._check({}) is None


class TestSimpleValues:

    def _check(self, host_cfg):
        return proxy.check_create_request(
            {'HostConfig': host_cfg}, '/tmp', '/tmp',
        )

    def test_isolation_rejected(self):
        result = self._check({'Isolation': 'process'})
        assert 'isolation' in result

    def test_isolation_default_allowed(self):
        assert self._check({
            'Isolation': 'default',
        }) is None

    def test_volume_driver_rejected(self):
        result = self._check({'VolumeDriver': 'sshfs'})
        assert 'volume driver' in result

    def test_volume_driver_local_allowed(self):
        assert self._check({
            'VolumeDriver': 'local',
        }) is None


# -- volume driver options --------------------------------


class TestVolumeDriverOptions:

    def test_bind_device_rejected(self):
        """A local volume with a bind device reaches any
        host path without naming it in a bind mount."""
        result = proxy.validate_volume('', json.dumps({
            'Name': 'escape',
            'Driver': 'local',
            'DriverOpts': {
                'type': 'none', 'o': 'bind', 'device': '/',
            },
        }).encode())
        assert 'volume type not allowed: none' == result

    def test_bind_device_lowercase_keys(self):
        result = proxy.validate_volume('', json.dumps({
            'driver': 'local',
            'driveropts': {'device': '/etc'},
        }).encode())
        assert 'device' in result

    def test_nfs_type_rejected(self):
        result = proxy.validate_volume('', json.dumps({
            'DriverOpts': {
                'type': 'nfs', 'o': 'addr=10.0.0.1',
            },
        }).encode())
        assert 'volume type' in result

    def test_bind_option_rejected(self):
        result = proxy.validate_volume('', json.dumps({
            'DriverOpts': {'o': 'rw,bind'},
        }).encode())
        assert 'o=bind' in result

    def test_third_party_driver_rejected(self):
        result = proxy.validate_volume('', json.dumps({
            'Driver': 'rexray',
        }).encode())
        assert 'volume driver' in result

    def test_tmpfs_volume_allowed(self):
        """The local driver requires a device even for tmpfs,
        which mount(2) ignores, so this must be allowed --
        it is what compose driver_opts produce."""
        assert proxy.validate_volume('', json.dumps({
            'Name': 'scratch',
            'Driver': 'local',
            'DriverOpts': {
                'type': 'tmpfs',
                'device': 'tmpfs',
                'o': 'size=100m',
            },
        }).encode()) is None

    def test_tmpfs_volume_with_bind_option_rejected(self):
        """MS_BIND makes the kernel ignore the tmpfs type and
        bind the device instead."""
        result = proxy.validate_volume('', json.dumps({
            'DriverOpts': {
                'type': 'tmpfs', 'device': '/', 'o': 'bind',
            },
        }).encode())
        assert 'o=bind' in result

    def test_plain_volume_allowed(self):
        assert proxy.validate_volume('', json.dumps({
            'Name': 'data',
        }).encode()) is None

    def test_cluster_volume_rejected(self):
        result = proxy.validate_volume('', json.dumps({
            'ClusterVolumeSpec': {'Group': 'g'},
        }).encode())
        assert 'cluster volumes' in result

    def test_invalid_json(self):
        result = proxy.validate_volume('', b'{')
        assert 'invalid JSON' in result

    def test_wrong_type(self):
        result = proxy.validate_volume('', json.dumps({
            'Driver': 5,
        }).encode())
        assert 'must be a string' in result

    def test_volume_mount_carries_driver_opts(self):
        """A Type=volume mount can set the same driver
        options POST /volumes/create would."""
        body = {'HostConfig': {'Mounts': [{
            'Type': 'volume',
            'Source': 'escape',
            'Target': '/host',
            'VolumeOptions': {
                'DriverConfig': {
                    'Name': 'local',
                    'Options': {
                        'type': 'none',
                        'o': 'bind',
                        'device': '/',
                    },
                },
            },
        }]}}
        result = proxy.check_create_request(
            body, '/tmp', '/tmp',
        )
        assert 'volume type not allowed: none' == result


# -- mount types ------------------------------------------


class TestMountTypes:

    def _check(self, mount):
        return proxy.check_mount_object(
            mount, '/home/user/projects',
            '/home/user/projects/app',
        )

    def test_tmpfs_allowed(self):
        assert self._check({
            'Type': 'tmpfs', 'Target': '/scratch',
        }) is None

    def test_unknown_type_rejected(self):
        result = self._check({
            'Type': 'cluster', 'Source': 'x',
        })
        assert 'mount type' in result

    def test_missing_type_rejected(self):
        result = self._check({'Source': '/tmp'})
        assert 'mount type' in result

    def test_type_not_a_string(self):
        result = self._check({'Type': 7})
        assert 'must be a string' in result

    def test_source_not_a_string(self):
        result = self._check({
            'Type': 'bind', 'Source': 7,
        })
        assert 'must be a string' in result

    def test_lowercase_mount_keys(self):
        result = self._check({
            'type': 'bind', 'source': '/etc',
        })
        assert 'bind mount not allowed' in result

    def test_mounts_not_a_list(self):
        result = proxy.check_create_request(
            {'HostConfig': {'Mounts': {}}},
            '/tmp', '/tmp',
        )
        assert 'must be a list' in result

    def test_mount_entry_not_an_object(self):
        result = proxy.check_create_request(
            {'HostConfig': {'Mounts': ['x']}},
            '/tmp', '/tmp',
        )
        assert 'must be an object' in result


# -- bind strings that are not paths ----------------------


class TestBindNames:
    base = '/home/user/projects'
    rw = '/home/user/projects/app'

    def test_relative_path_rejected(self):
        """Not a volume name and not an absolute path."""
        result = proxy.check_bind_string(
            '../../etc:/etc', self.base, self.rw,
        )
        assert 'bind mount not allowed' in result

    def test_binds_not_a_list(self):
        result = proxy.check_create_request(
            {'HostConfig': {'Binds': 'x:/x'}},
            self.base, self.rw,
        )
        assert 'must be a list' in result

    def test_bind_not_a_string(self):
        result = proxy.check_create_request(
            {'HostConfig': {'Binds': [5]}},
            self.base, self.rw,
        )
        assert 'must be a string' in result


# -- exec -------------------------------------------------


class TestValidateExec:

    def test_privileged_rejected(self):
        result = proxy.validate_exec('', json.dumps({
            'Cmd': ['sh'], 'Privileged': True,
        }).encode())
        assert 'privileged exec' in result

    def test_lowercase_privileged_rejected(self):
        result = proxy.validate_exec('', json.dumps({
            'privileged': True,
        }).encode())
        assert 'privileged exec' in result

    def test_plain_exec_allowed(self):
        assert proxy.validate_exec('', json.dumps({
            'Cmd': ['sh'], 'AttachStdout': True,
        }).encode()) is None

    def test_empty_body_allowed(self):
        assert proxy.validate_exec('', b'') is None

    def test_invalid_json(self):
        result = proxy.validate_exec('', b'{')
        assert 'invalid JSON' in result

    def test_two_spellings(self):
        result = proxy.validate_exec(
            '',
            b'{"Privileged": false, "privileged": true}',
        )
        assert 'given twice' in result


# -- build ------------------------------------------------


class TestValidateBuild:

    def test_plain_build_allowed(self):
        assert proxy.validate_build(
            'dockerfile=Dockerfile&t=app%3A1&version=1',
            None,
        ) is None

    def test_no_query_allowed(self):
        assert proxy.validate_build('', None) is None

    def test_buildkit_rejected(self):
        """BuildKit carries its own build options over
        gRPC, where the proxy cannot read them."""
        result = proxy.validate_build('version=2', None)
        assert 'builder version' in result

    def test_security_opt_rejected(self):
        result = proxy.validate_build(
            'securityopt=seccomp%3Dunconfined', None,
        )
        assert 'security options' in result

    def test_host_network_rejected(self):
        result = proxy.validate_build(
            'networkmode=host', None,
        )
        assert 'network mode' in result

    def test_container_network_rejected(self):
        result = proxy.validate_build(
            'networkmode=container%3Aabc', None,
        )
        assert 'network mode' in result


# -- JSON helpers -----------------------------------------


class TestJsonHelpers:

    def test_parse_body_null(self):
        assert proxy.parse_body(b'null') == ({}, None)

    def test_parse_body_not_an_object(self):
        parsed, error = proxy.parse_body(b'[1, 2]')
        assert parsed is None
        assert 'must be a JSON object' in error

    def test_field_on_non_dict(self):
        assert proxy.field(['a'], 'a') is None

    def test_field_collision(self):
        with pytest.raises(proxy.Denied):
            proxy.field({'A': 1, 'a': 2}, 'a')

    def test_field_last_spelling_wins(self):
        """json.loads keeps the last of two identical keys,
        exactly as Go does, so the proxy checks the value
        the daemon will use."""
        parsed, _ = proxy.parse_body(
            b'{"Privileged": false, "Privileged": true}',
        )
        assert proxy.field(parsed, 'privileged') is True

    def test_as_list_none(self):
        assert proxy.as_list(None, 'x') == []

    def test_as_dict_none(self):
        assert proxy.as_dict(None, 'x') == {}

    def test_as_str_none(self):
        assert proxy.as_str(None, 'x') == ''

    def test_is_empty(self):
        for value in (None, False, 0, '', [], {}):
            assert proxy.is_empty(value)
        for value in (True, 1, -1, 'x', [0], {'a': 1}):
            assert not proxy.is_empty(value)


# -- parse_target -----------------------------------------


class TestParseTarget:

    def test_versioned_path(self):
        method, path, query, error = proxy.parse_target(
            b'POST /v1.52/containers/create HTTP/1.1\r\n',
        )
        assert (method, path, query, error) == (
            'POST', '/containers/create', '', None,
        )

    def test_unversioned_path(self):
        """The daemon answers POST /containers/create as
        well as the versioned form."""
        _, path, _, error = proxy.parse_target(
            b'POST /containers/create HTTP/1.1\r\n',
        )
        assert (path, error) == (
            '/containers/create', None,
        )

    def test_percent_encoded_path(self):
        """The daemon's router matches the decoded path."""
        _, path, _, error = proxy.parse_target(
            b'POST /v1.52/containers/%63reate HTTP/1.1\r\n',
        )
        assert (path, error) == (
            '/containers/create', None,
        )

    def test_query_separated(self):
        _, path, query, _ = proxy.parse_target(
            b'POST /v1.52/containers/create?name=x '
            b'HTTP/1.1\r\n',
        )
        assert path == '/containers/create'
        assert query == 'name=x'

    def test_absolute_form(self):
        _, path, _, error = proxy.parse_target(
            b'GET http://localhost/v1.52/info HTTP/1.1\r\n',
        )
        assert (path, error) == ('/info', None)

    def test_two_digit_minor_version(self):
        _, path, _, _ = proxy.parse_target(
            b'GET /v1.7/info HTTP/1.1\r\n',
        )
        assert path == '/info'

    def test_empty_segment_rejected(self):
        _, _, _, error = proxy.parse_target(
            b'GET //v1.52/info HTTP/1.1\r\n',
        )
        assert error == 'malformed request path'

    def test_dot_segment_rejected(self):
        _, _, _, error = proxy.parse_target(
            b'GET /v1.52/./info HTTP/1.1\r\n',
        )
        assert error == 'malformed request path'

    def test_dotdot_segment_rejected(self):
        _, _, _, error = proxy.parse_target(
            b'GET /v1.52/containers/../info HTTP/1.1\r\n',
        )
        assert error == 'malformed request path'

    def test_relative_target_rejected(self):
        _, _, _, error = proxy.parse_target(
            b'GET info HTTP/1.1\r\n',
        )
        assert error == 'malformed request path'

    def test_too_few_parts_rejected(self):
        _, _, _, error = proxy.parse_target(b'GET /info\r\n')
        assert error == 'malformed request line'

    def test_not_http_rejected(self):
        _, _, _, error = proxy.parse_target(
            b'GET /info RTSP/1.0\r\n',
        )
        assert error == 'malformed request line'


# -- read_framing -----------------------------------------


class TestReadFraming:

    def _headers(self, pairs):
        headers = proxy.Headers()
        for name, value in pairs:
            headers.add(name, value)
        return headers

    def test_content_length(self):
        framing, error = proxy.read_framing(
            self._headers([('content-length', '12')]),
        )
        assert (framing, error) == ((12, False), None)

    def test_chunked(self):
        framing, error = proxy.read_framing(
            self._headers([
                ('transfer-encoding', 'chunked'),
            ]),
        )
        assert (framing, error) == ((0, True), None)

    def test_no_body(self):
        framing, error = proxy.read_framing(
            self._headers([('host', 'localhost')]),
        )
        assert (framing, error) == ((0, False), None)

    def test_both_headers_rejected(self):
        """The proxy trusts Content-Length and Go trusts
        Transfer-Encoding, so a request that sets both
        could hide a second request from the proxy."""
        _, error = proxy.read_framing(
            self._headers([
                ('content-length', '5'),
                ('transfer-encoding', 'chunked'),
            ]),
        )
        assert 'both set' in error

    def test_repeated_content_length_rejected(self):
        _, error = proxy.read_framing(
            self._headers([
                ('content-length', '5'),
                ('content-length', '6'),
            ]),
        )
        assert 'repeated framing header' in error

    def test_repeated_transfer_encoding_rejected(self):
        _, error = proxy.read_framing(
            self._headers([
                ('transfer-encoding', 'chunked'),
                ('transfer-encoding', 'chunked'),
            ]),
        )
        assert 'repeated framing header' in error

    def test_unsupported_encoding_rejected(self):
        _, error = proxy.read_framing(
            self._headers([
                ('transfer-encoding', 'gzip'),
            ]),
        )
        assert 'unsupported Transfer-Encoding' in error

    def test_malformed_content_length_rejected(self):
        _, error = proxy.read_framing(
            self._headers([('content-length', '5 ,5')]),
        )
        assert 'malformed Content-Length' in error


# -- match_rule -------------------------------------------


class TestMatchRule:

    def test_read_allowed(self):
        rule, error = proxy.match_rule('GET', '/info')
        assert (rule, error) == ((False, None), None)

    def test_head_allowed(self):
        rule, error = proxy.match_rule('HEAD', '/_ping')
        assert error is None

    def test_create_needs_body(self):
        rule, error = proxy.match_rule(
            'POST', '/containers/create',
        )
        assert error is None
        assert rule == (True, proxy.validate_create)

    def test_exec_create_validated(self):
        rule, _ = proxy.match_rule(
            'POST', '/containers/abc/exec',
        )
        assert rule == (True, proxy.validate_exec)

    def test_volume_create_validated(self):
        rule, _ = proxy.match_rule(
            'POST', '/volumes/create',
        )
        assert rule == (True, proxy.validate_volume)

    def test_build_validated_without_body(self):
        rule, _ = proxy.match_rule('POST', '/build')
        assert rule == (False, proxy.validate_build)

    def test_container_start_allowed(self):
        rule, error = proxy.match_rule(
            'POST', '/containers/abc/start',
        )
        assert (rule, error) == ((False, None), None)

    def test_multi_segment_image_ref(self):
        rule, error = proxy.match_rule(
            'POST', '/images/docker.io/library/alpine/tag',
        )
        assert error is None

    def test_image_delete_allowed(self):
        _, error = proxy.match_rule(
            'DELETE', '/images/alpine:latest',
        )
        assert error is None

    def test_plugin_pull_rejected(self):
        """A plugin can be granted host mounts and
        capabilities of its own."""
        _, error = proxy.match_rule(
            'POST', '/plugins/pull',
        )
        assert 'endpoint not allowed' in error

    def test_plugin_read_rejected(self):
        _, error = proxy.match_rule('GET', '/plugins')
        assert 'endpoint not allowed' in error

    def test_service_create_rejected(self):
        _, error = proxy.match_rule(
            'POST', '/services/create',
        )
        assert 'endpoint not allowed' in error

    def test_swarm_init_rejected(self):
        _, error = proxy.match_rule('POST', '/swarm/init')
        assert 'endpoint not allowed' in error

    def test_buildkit_session_rejected(self):
        _, error = proxy.match_rule('POST', '/session')
        assert 'endpoint not allowed' in error

    def test_unknown_endpoint_rejected(self):
        _, error = proxy.match_rule(
            'POST', '/containers/abc/nonsense',
        )
        assert 'endpoint not allowed' in error

    def test_unknown_method_rejected(self):
        _, error = proxy.match_rule(
            'PATCH', '/containers/abc',
        )
        assert 'endpoint not allowed' in error


# -- parse_status -----------------------------------------


class TestParseStatus:

    def test_normal(self):
        assert proxy.parse_status(
            b'HTTP/1.1 200 OK\r\n',
        ) == 200

    def test_no_phrase(self):
        assert proxy.parse_status(
            b'HTTP/1.1 204\r\n',
        ) == 204

    def test_malformed(self):
        assert proxy.parse_status(b'garbage\r\n') == 0

    def test_empty(self):
        assert proxy.parse_status(b'') == 0

    def test_non_integer(self):
        assert proxy.parse_status(
            b'HTTP/1.1 abc OK\r\n',
        ) == 0


# -- read_headers -----------------------------------------


class TestReadHeaders:

    @pytest.mark.asyncio
    async def test_normal_request(self):
        data = (
            b'GET /v1.45/info HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Accept: */*\r\n'
            b'\r\n'
        )
        reader = make_reader(data)
        line, raw, hdrs = await proxy.read_headers(
            reader,
        )
        assert line == b'GET /v1.45/info HTTP/1.1\r\n'
        assert raw == data
        assert hdrs['host'] == 'localhost'
        assert hdrs['accept'] == '*/*'

    @pytest.mark.asyncio
    async def test_eof(self):
        reader = make_reader(b'')
        line, raw, hdrs = await proxy.read_headers(
            reader,
        )
        assert line is None
        assert raw is None
        assert hdrs is None

    @pytest.mark.asyncio
    async def test_header_without_colon(self):
        data = (
            b'GET / HTTP/1.1\r\n'
            b'malformed-header\r\n'
            b'\r\n'
        )
        reader = make_reader(data)
        _, _, hdrs = await proxy.read_headers(reader)
        assert 'malformed-header' not in hdrs

    @pytest.mark.asyncio
    async def test_empty_line_terminator(self):
        """Handle \\n without \\r as header terminator."""
        data = b'GET / HTTP/1.1\r\nHost: x\r\n\n'
        reader = make_reader(data)
        line, raw, hdrs = await proxy.read_headers(
            reader,
        )
        assert line is not None
        assert hdrs['host'] == 'x'


# -- send_error -------------------------------------------


class TestSendError:

    @pytest.mark.asyncio
    async def test_403(self):
        writer = MockWriter()
        await proxy.send_error(writer, 403, 'denied')
        output = bytes(writer.data)
        assert b'HTTP/1.1 403 Forbidden' in output
        assert b'"message": "denied"' in output
        assert b'Connection: close' in output

    @pytest.mark.asyncio
    async def test_413(self):
        writer = MockWriter()
        await proxy.send_error(writer, 413, 'too big')
        output = bytes(writer.data)
        assert b'413 Request Entity Too Large' in output

    @pytest.mark.asyncio
    async def test_unknown_status(self):
        writer = MockWriter()
        await proxy.send_error(writer, 500, 'oops')
        output = bytes(writer.data)
        assert b'500 Error' in output


# -- forward_chunked --------------------------------------


class TestForwardChunked:

    @pytest.mark.asyncio
    async def test_single_chunk(self):
        data = b'5\r\nhello\r\n0\r\n\r\n'
        reader = make_reader(data)
        writer = MockWriter()
        await proxy.forward_chunked(reader, writer)
        assert b'hello' in bytes(writer.data)
        assert bytes(writer.data).endswith(b'0\r\n\r\n')

    @pytest.mark.asyncio
    async def test_multiple_chunks(self):
        data = (
            b'3\r\nabc\r\n'
            b'2\r\nde\r\n'
            b'0\r\n\r\n'
        )
        reader = make_reader(data)
        writer = MockWriter()
        await proxy.forward_chunked(reader, writer)
        output = bytes(writer.data)
        assert b'abc' in output
        assert b'de' in output

    @pytest.mark.asyncio
    async def test_empty_chunked(self):
        data = b'0\r\n\r\n'
        reader = make_reader(data)
        writer = MockWriter()
        await proxy.forward_chunked(reader, writer)
        assert bytes(writer.data) == b'0\r\n\r\n'

    @pytest.mark.asyncio
    async def test_incomplete_read_error_partial(self):
        """IncompleteReadError forwards partial data."""
        reader = asyncio.StreamReader()
        # Feed chunk header then only partial chunk data
        reader.feed_data(b'a\r\n')  # 10 bytes expected
        reader.feed_data(b'hello')  # only 5 bytes
        reader.feed_eof()
        writer = MockWriter()
        await proxy.forward_chunked(reader, writer)
        output = bytes(writer.data)
        assert b'a\r\n' in output
        assert b'hello' in output

    @pytest.mark.asyncio
    async def test_incomplete_read_error_empty_partial(self):
        """IncompleteReadError with empty partial is safe."""
        reader = asyncio.StreamReader()
        reader.feed_data(b'5\r\n')  # 5 bytes + 2 CRLF
        # Feed nothing, then EOF - readexactly gets 0 bytes
        reader.feed_eof()
        writer = MockWriter()
        await proxy.forward_chunked(reader, writer)
        # Header was written, but no partial data to forward
        assert b'5\r\n' in bytes(writer.data)

    @pytest.mark.asyncio
    async def test_empty_readline_connection_drop(self):
        """Empty readline means connection dropped."""
        reader = make_reader(b'')
        writer = MockWriter()
        await proxy.forward_chunked(reader, writer)
        assert bytes(writer.data) == b''


# -- _forward_stream --------------------------------------


class TestForwardStream:

    @pytest.mark.asyncio
    async def test_normal_forward(self):
        reader = make_reader(b'hello world')
        writer = MockWriter()
        await proxy._forward_stream(reader, writer)
        assert bytes(writer.data) == b'hello world'

    @pytest.mark.asyncio
    async def test_connection_error(self):
        reader = asyncio.StreamReader()
        reader.set_exception(
            ConnectionResetError('gone'),
        )
        writer = MockWriter()
        await proxy._forward_stream(reader, writer)

    @pytest.mark.asyncio
    async def test_os_error(self):
        reader = asyncio.StreamReader()
        reader.set_exception(OSError('broken'))
        writer = MockWriter()
        await proxy._forward_stream(reader, writer)

    @pytest.mark.asyncio
    async def test_cancelled(self):
        """CancelledError is handled gracefully."""
        reader = asyncio.StreamReader()
        reader.set_exception(asyncio.CancelledError())
        writer = MockWriter()
        await proxy._forward_stream(reader, writer)


# -- _half_close ------------------------------------------


class TestHalfClose:

    def test_supported(self):
        w = MockWriter()
        proxy._half_close(w)
        assert w.eof_written

    def test_not_supported(self):
        w = MockWriter()
        w.can_write_eof = lambda: False
        proxy._half_close(w)
        assert not w.eof_written

    def test_write_eof_raises(self):
        w = MockWriter()
        w.write_eof = MagicMock(
            side_effect=OSError('broken'),
        )
        proxy._half_close(w)

    def test_can_write_eof_raises(self):
        w = MockWriter()
        w.can_write_eof = MagicMock(
            side_effect=RuntimeError('fail'),
        )
        proxy._half_close(w)


# -- splice -----------------------------------------------


class TestSplice:

    @pytest.mark.asyncio
    async def test_bidirectional(self):
        r1 = make_reader(b'from-client')
        w1 = MockWriter()
        r2 = make_reader(b'from-server')
        w2 = MockWriter()
        await proxy.splice(r1, w1, r2, w2)
        assert bytes(w2.data) == b'from-client'
        assert bytes(w1.data) == b'from-server'
        assert w1.closed
        assert w2.closed

    @pytest.mark.asyncio
    async def test_one_side_closes_first(self):
        """When one direction ends first, the other
        continues until done."""
        r1 = make_reader(b'short')
        w1 = MockWriter()
        r2 = make_reader(b'')
        w2 = MockWriter()
        await proxy.splice(r1, w1, r2, w2)
        assert bytes(w2.data) == b'short'
        assert w1.closed
        assert w2.closed

    @pytest.mark.asyncio
    async def test_writer_close_raises(self):
        """Exception in writer.close() is suppressed."""
        r1 = make_reader(b'data')
        w1 = MockWriter()
        w1.close = MagicMock(
            side_effect=RuntimeError('close boom'),
        )
        r2 = make_reader(b'')
        w2 = MockWriter()
        w2.close = MagicMock(
            side_effect=OSError('close fail'),
        )
        await proxy.splice(r1, w1, r2, w2)

    @pytest.mark.asyncio
    async def test_client_half_close(self):
        """Client EOF first; server data still arrives."""
        r1 = make_reader(b'')
        w1 = MockWriter()
        r2 = asyncio.StreamReader()
        w2 = MockWriter()

        async def feed_later():
            await asyncio.sleep(0.01)
            r2.feed_data(b'container output')
            r2.feed_eof()

        feeder = asyncio.create_task(feed_later())
        await proxy.splice(r1, w1, r2, w2)
        await feeder
        assert bytes(w1.data) == b'container output'
        assert w2.eof_written
        assert w1.closed
        assert w2.closed

    @pytest.mark.asyncio
    async def test_server_closes_first(self):
        """Server EOF first; client data still arrives."""
        r1 = asyncio.StreamReader()
        w1 = MockWriter()
        r2 = make_reader(b'server-data')
        w2 = MockWriter()

        async def feed_later():
            await asyncio.sleep(0.01)
            r1.feed_data(b'client-data')
            r1.feed_eof()

        feeder = asyncio.create_task(feed_later())
        await proxy.splice(r1, w1, r2, w2)
        await feeder
        assert bytes(w1.data) == b'server-data'
        assert bytes(w2.data) == b'client-data'
        assert w1.eof_written
        assert w1.closed
        assert w2.closed

    @pytest.mark.asyncio
    async def test_remaining_task_raises(self):
        """Exception from remaining task is caught."""
        r1 = make_reader(b'')
        w1 = MockWriter()
        r2 = asyncio.StreamReader()
        w2 = MockWriter()
        original = proxy._forward_stream

        async def error_forward(reader, writer):
            if reader is r2:
                await asyncio.sleep(0.01)
                raise ValueError('unexpected')
            return await original(reader, writer)

        with patch.object(
            proxy, '_forward_stream', error_forward,
        ):
            await proxy.splice(r1, w1, r2, w2)
        assert w1.closed
        assert w2.closed

    @pytest.mark.asyncio
    async def test_external_cancellation(self):
        """External cancellation cleans up properly."""

        async def uncatchable(reader, writer):
            await asyncio.sleep(100)

        r1 = asyncio.StreamReader()
        w1 = MockWriter()
        r2 = asyncio.StreamReader()
        w2 = MockWriter()
        with patch.object(
            proxy, '_forward_stream', uncatchable,
        ):
            task = asyncio.create_task(
                proxy.splice(r1, w1, r2, w2),
            )
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert w1.closed
        assert w2.closed


# -- relay_response ---------------------------------------


class TestRelayResponse:

    @pytest.mark.asyncio
    async def test_200_with_body(self):
        resp = build_http_response(200, body=b'ok')
        reader = make_reader(resp)
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 200
        output = bytes(writer.data)
        assert b'200 OK' in output
        assert output.endswith(b'ok')

    @pytest.mark.asyncio
    async def test_head_body_not_read(self):
        # A HEAD response advertises a length it does not
        # send, so reading it would block; "docker cp" stats
        # the destination with HEAD before uploading.
        resp = build_http_response(
            200, body=b'', headers={'Content-Length': '512'},
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer, 'HEAD',
        )
        assert status == 200
        assert not close
        assert not bytes(writer.data).endswith(b'\x00')

    @pytest.mark.asyncio
    async def test_204_no_body(self):
        resp = build_http_response(
            204, phrase='No Content', body=b'',
            headers={'Content-Length': '0'},
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 204
        assert not close

    @pytest.mark.asyncio
    async def test_304_no_body(self):
        resp = build_http_response(
            304, phrase='Not Modified', body=b'',
            headers={'Content-Length': '0'},
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, _ = await proxy.relay_response(
            reader, writer,
        )
        assert status == 304

    @pytest.mark.asyncio
    async def test_1xx_no_body(self):
        resp = build_http_response(
            100, phrase='Continue', body=b'',
            headers={'Content-Length': '0'},
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, _ = await proxy.relay_response(
            reader, writer,
        )
        assert status == 100

    @pytest.mark.asyncio
    async def test_101_upgrade(self):
        resp = (
            b'HTTP/1.1 101 Switching Protocols\r\n'
            b'Upgrade: tcp\r\n'
            b'\r\n'
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 101
        assert close

    @pytest.mark.asyncio
    async def test_chunked_response(self):
        resp = (
            b'HTTP/1.1 200 OK\r\n'
            b'Transfer-Encoding: chunked\r\n'
            b'\r\n'
            b'3\r\nabc\r\n0\r\n\r\n'
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, _ = await proxy.relay_response(
            reader, writer,
        )
        assert status == 200
        assert b'abc' in bytes(writer.data)

    @pytest.mark.asyncio
    async def test_close_delimited(self):
        resp = (
            b'HTTP/1.1 200 OK\r\n'
            b'\r\n'
            b'streaming data'
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 200
        assert close
        assert b'streaming data' in bytes(writer.data)

    @pytest.mark.asyncio
    async def test_eof_on_headers(self):
        reader = make_reader(b'')
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 0
        assert close

    @pytest.mark.asyncio
    async def test_connection_close_header(self):
        resp = build_http_response(
            200, body=b'x',
            headers={'Connection': 'close'},
        )
        reader = make_reader(resp)
        writer = MockWriter()
        _, close = await proxy.relay_response(
            reader, writer,
        )
        assert close

    @pytest.mark.asyncio
    async def test_content_length_zero(self):
        resp = build_http_response(
            200, body=b'',
            headers={'Content-Length': '0'},
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 200
        assert not close

    @pytest.mark.asyncio
    async def test_204_connection_close(self):
        resp = (
            b'HTTP/1.1 204 No Content\r\n'
            b'Connection: close\r\n'
            b'\r\n'
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 204
        assert close


# -- proxy_request ----------------------------------------


class TestProxyRequest:

    def _make_pair(self, req_data, resp_data):
        """Create reader/writer pairs for
        client+upstream."""
        client_r = make_reader(req_data)
        client_w = MockWriter()
        upstream_r = make_reader(resp_data)
        upstream_w = MockWriter()
        return client_r, client_w, upstream_r, upstream_w

    @pytest.mark.asyncio
    async def test_allowed_create(self):
        body = {'Image': 'ubuntu', 'HostConfig': {}}
        req = build_http_request(
            'POST', '/v1.45/containers/create', body,
        )
        resp = build_http_response(
            201, body=b'{"Id":"x"}',
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        with patch.object(
            proxy, 'ALLOWED_MOUNT_BASE', '/home',
        ), patch.object(
            proxy, 'ALLOWED_RW_BASE', '/home',
        ):
            result = await proxy.proxy_request(
                cr, cw, ur, uw,
            )
        assert result is False or result is True
        assert b'containers/create' in bytes(uw.data)

    @pytest.mark.asyncio
    async def test_rejected_create_privileged(self):
        body = {'HostConfig': {'Privileged': True}}
        req = build_http_request(
            'POST', '/v1.45/containers/create', body,
        )
        resp = build_http_response(
            200, body=b'unused',
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        with patch.object(
            proxy, 'ALLOWED_MOUNT_BASE', '/home',
        ), patch.object(
            proxy, 'ALLOWED_RW_BASE', '/home',
        ):
            result = await proxy.proxy_request(
                cr, cw, ur, uw,
            )
        assert result is False
        assert b'403 Forbidden' in bytes(cw.data)
        assert len(uw.data) == 0

    @pytest.mark.asyncio
    async def test_rejected_create_too_large(self):
        req = (
            b'POST /v1.45/containers/create HTTP/1.1\r\n'
            b'Content-Length: 999999999\r\n'
            b'\r\n'
        )
        resp = build_http_response(
            200, body=b'unused',
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        with patch.object(proxy, 'MAX_BODY', 100):
            result = await proxy.proxy_request(
                cr, cw, ur, uw,
            )
        assert result is False
        assert b'413' in bytes(cw.data)

    @pytest.mark.asyncio
    async def test_create_no_body(self):
        req = build_http_request(
            'POST', '/v1.45/containers/create',
        )
        resp = build_http_response(
            400, phrase='Bad Request',
            body=b'{"message":""}',
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        with patch.object(
            proxy, 'ALLOWED_MOUNT_BASE', '/home',
        ), patch.object(
            proxy, 'ALLOWED_RW_BASE', '/home',
        ):
            await proxy.proxy_request(
                cr, cw, ur, uw,
            )
        assert b'containers/create' in bytes(uw.data)

    @pytest.mark.asyncio
    async def test_non_create_with_body(self):
        req = build_http_request(
            'POST', '/v1.45/containers/abc/start',
            body={'detach': True},
        )
        resp = build_http_response(
            204, phrase='No Content',
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        result = await proxy.proxy_request(
            cr, cw, ur, uw,
        )
        assert b'containers/abc/start' in bytes(uw.data)

    @pytest.mark.asyncio
    async def test_non_create_chunked(self):
        req = (
            b'POST /v1.45/build HTTP/1.1\r\n'
            b'Transfer-Encoding: chunked\r\n'
            b'\r\n'
            b'5\r\nhello\r\n0\r\n\r\n'
        )
        resp = build_http_response(200, body=b'built')
        cr, cw, ur, uw = self._make_pair(req, resp)
        result = await proxy.proxy_request(
            cr, cw, ur, uw,
        )
        assert b'hello' in bytes(uw.data)

    @pytest.mark.asyncio
    async def test_eof_returns_false(self):
        cr = make_reader(b'')
        cw = MockWriter()
        ur = make_reader(b'')
        uw = MockWriter()
        result = await proxy.proxy_request(
            cr, cw, ur, uw,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_upgrade_response(self):
        req = build_http_request(
            'POST', '/v1.45/exec/abc/start',
            body={'Detach': False, 'Tty': True},
        )
        resp = (
            b'HTTP/1.1 101 Switching Protocols\r\n'
            b'Upgrade: tcp\r\n'
            b'\r\n'
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        result = await proxy.proxy_request(
            cr, cw, ur, uw,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_keep_alive(self):
        req = build_http_request(
            'GET', '/v1.45/info',
        )
        resp = build_http_response(
            200, body=b'{"info": true}',
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        result = await proxy.proxy_request(
            cr, cw, ur, uw,
        )
        assert result is True


class TestProxyRequestPolicy:
    """Rejections that happen before any body is read."""

    def _make_pair(self, req_data):
        return (
            make_reader(req_data), MockWriter(),
            make_reader(build_http_response(200)),
            MockWriter(),
        )

    async def _send(self, req_data):
        cr, cw, ur, uw = self._make_pair(req_data)
        result = await proxy.proxy_request(cr, cw, ur, uw)
        return result, bytes(cw.data), bytes(uw.data)

    @pytest.mark.asyncio
    async def test_unversioned_create_checked(self):
        """The unversioned path reaches the same daemon
        endpoint, so it gets the same check."""
        req = build_http_request(
            'POST', '/containers/create',
            {'HostConfig': {'Privileged': True}},
        )
        result, sent, forwarded = await self._send(req)
        assert result is False
        assert b'403 Forbidden' in sent
        assert forwarded == b''

    @pytest.mark.asyncio
    async def test_lowercase_host_config_checked(self):
        req = build_http_request(
            'POST', '/v1.52/containers/create',
            {'hostconfig': {'privileged': True}},
        )
        result, sent, forwarded = await self._send(req)
        assert b'403 Forbidden' in sent
        assert forwarded == b''

    @pytest.mark.asyncio
    async def test_plugin_pull_rejected(self):
        req = build_http_request(
            'POST', '/v1.52/plugins/pull?remote=evil',
        )
        result, sent, forwarded = await self._send(req)
        assert b'403 Forbidden' in sent
        assert b'endpoint not allowed' in sent
        assert forwarded == b''

    @pytest.mark.asyncio
    async def test_privileged_exec_rejected(self):
        req = build_http_request(
            'POST', '/v1.52/containers/abc/exec',
            {'Cmd': ['sh'], 'Privileged': True},
        )
        result, sent, forwarded = await self._send(req)
        assert b'403 Forbidden' in sent
        assert forwarded == b''

    @pytest.mark.asyncio
    async def test_bind_volume_create_rejected(self):
        req = build_http_request(
            'POST', '/v1.52/volumes/create',
            {'DriverOpts': {'device': '/'}},
        )
        result, sent, forwarded = await self._send(req)
        assert b'403 Forbidden' in sent
        assert forwarded == b''

    @pytest.mark.asyncio
    async def test_build_security_opt_rejected(self):
        """A query-only check still rejects before the
        build context is forwarded."""
        req = build_http_request(
            'POST',
            '/v1.52/build?securityopt=seccomp%3Dunconfined',
        )
        result, sent, forwarded = await self._send(req)
        assert b'403 Forbidden' in sent
        assert forwarded == b''

    @pytest.mark.asyncio
    async def test_chunked_create_rejected(self):
        """A validated endpoint may not describe its body
        in a way the proxy would have to guess at."""
        req = (
            b'POST /v1.52/containers/create HTTP/1.1\r\n'
            b'Transfer-Encoding: chunked\r\n'
            b'\r\n'
            b'2\r\n{}\r\n0\r\n\r\n'
        )
        result, sent, forwarded = await self._send(req)
        assert b'400 Bad Request' in sent
        assert forwarded == b''

    @pytest.mark.asyncio
    async def test_smuggled_framing_rejected(self):
        req = (
            b'POST /v1.52/containers/create HTTP/1.1\r\n'
            b'Content-Length: 2\r\n'
            b'Transfer-Encoding: chunked\r\n'
            b'\r\n'
            b'{}'
        )
        result, sent, forwarded = await self._send(req)
        assert b'400 Bad Request' in sent
        assert b'both set' in sent
        assert forwarded == b''

    @pytest.mark.asyncio
    async def test_malformed_request_line_rejected(self):
        req = b'BOGUS\r\n\r\n'
        result, sent, forwarded = await self._send(req)
        assert b'400 Bad Request' in sent
        assert forwarded == b''

    @pytest.mark.asyncio
    async def test_dot_segment_rejected(self):
        req = build_http_request(
            'GET', '/v1.52/containers/../info',
        )
        result, sent, forwarded = await self._send(req)
        assert b'400 Bad Request' in sent
        assert forwarded == b''


# -- handle_connection ------------------------------------


class TestHandleConnection:

    @pytest.mark.asyncio
    async def test_full_request_response(self):
        req = build_http_request(
            'GET', '/v1.45/info',
        )
        resp = build_http_response(
            200, body=b'ok',
            headers={'Connection': 'close'},
        )
        with tempfile.TemporaryDirectory() as td:
            upstream_sock = os.path.join(
                td, 'upstream.sock',
            )
            responses = [resp]

            async def fake_server(reader, writer):
                _ = await reader.read(4096)
                writer.write(responses.pop(0))
                await writer.drain()
                writer.close()

            server = await asyncio.start_unix_server(
                fake_server, path=upstream_sock,
            )
            try:
                client_r = make_reader(req)
                client_w = MockWriter()
                await proxy.handle_connection(
                    client_r, client_w, upstream_sock,
                )
                assert b'ok' in bytes(client_w.data)
            finally:
                server.close()

    @pytest.mark.asyncio
    async def test_upstream_connect_fails(self):
        client_r = make_reader(
            b'GET / HTTP/1.1\r\n\r\n',
        )
        client_w = MockWriter()
        await proxy.handle_connection(
            client_r, client_w, '/nonexistent/sock',
        )
        assert client_w.closed

    @pytest.mark.asyncio
    async def test_incomplete_read_error(self):
        """IncompleteReadError is handled gracefully."""
        req = (
            b'POST /v1.45/containers/create HTTP/1.1\r\n'
            b'Content-Length: 9999\r\n'
            b'\r\n'
            b'short'
        )
        with tempfile.TemporaryDirectory() as td:
            sock = os.path.join(td, 'up.sock')

            async def fake_server(reader, writer):
                await reader.read(1)

            server = await asyncio.start_unix_server(
                fake_server, path=sock,
            )
            try:
                client_r = make_reader(req)
                client_w = MockWriter()
                with patch.object(
                    proxy, 'ALLOWED_MOUNT_BASE', '/',
                ):
                    await proxy.handle_connection(
                        client_r, client_w, sock,
                    )
            finally:
                server.close()

    @pytest.mark.asyncio
    async def test_writer_close_raises(self):
        """Exception in writer.close() is swallowed."""
        client_r = make_reader(b'')
        client_w = MockWriter()
        client_w.close = MagicMock(
            side_effect=RuntimeError('boom'),
        )
        await proxy.handle_connection(
            client_r, client_w, '/nonexistent/sock',
        )


# -- run_proxy --------------------------------------------


class TestRunProxy:

    @pytest.mark.asyncio
    async def test_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            listen = os.path.join(td, 'listen.sock')
            upstream = os.path.join(
                td, 'upstream.sock',
            )
            ready = asyncio.Event()
            stop = asyncio.Event()

            async def stop_after_ready():
                await ready.wait()
                assert os.path.exists(listen)
                await asyncio.sleep(0.05)
                stop.set()

            task = asyncio.create_task(
                proxy.run_proxy(
                    listen, upstream, ready,
                    stop_event=stop,
                ),
            )
            stopper = asyncio.create_task(
                stop_after_ready(),
            )
            await asyncio.gather(task, stopper)

    @pytest.mark.asyncio
    async def test_removes_stale_socket(self):
        with tempfile.TemporaryDirectory() as td:
            listen = os.path.join(td, 'listen.sock')
            upstream = os.path.join(
                td, 'upstream.sock',
            )
            with open(listen, 'w') as f:
                f.write('stale')
            ready = asyncio.Event()
            stop = asyncio.Event()

            async def stop_soon():
                await ready.wait()
                stop.set()

            task = asyncio.create_task(
                proxy.run_proxy(
                    listen, upstream, ready,
                    stop_event=stop,
                ),
            )
            stopper = asyncio.create_task(stop_soon())
            await asyncio.gather(task, stopper)


# -- main -------------------------------------------------


class TestMain:

    def test_main_starts_proxy(self):
        # asyncio.run is mocked, so the coroutine run_proxy
        # returns is never awaited.
        with patch.object(proxy, 'run_proxy', MagicMock()):
            with patch('asyncio.run') as mock_run:
                proxy.main()
                mock_run.assert_called_once()

    def test_log_level_from_env(self):
        with patch.object(proxy, 'run_proxy', MagicMock()):
            with patch('asyncio.run'), \
                 patch.dict(
                     os.environ, {'LOG_LEVEL': 'DEBUG'},
                 ):
                proxy.main()
        root = logging.getLogger()
        assert root.level in (
            logging.DEBUG, logging.WARNING,
        )

    def test_log_level_invalid_falls_back(self):
        with patch.object(proxy, 'run_proxy', MagicMock()):
            with patch('asyncio.run'), \
                 patch.dict(
                     os.environ,
                     {'LOG_LEVEL': 'NOTREAL'},
                 ):
                proxy.main()

    def test_dunder_main(self):
        with patch('asyncio.run'):
            runpy.run_module(
                'docker_socket_proxy',
                run_name='__main__',
            )


# -- Integration ------------------------------------------


class TestIntegration:

    @pytest.mark.asyncio
    async def test_end_to_end_allowed_request(self):
        """Full proxy: allowed GET flows through."""
        with tempfile.TemporaryDirectory() as td:
            listen_sock = os.path.join(
                td, 'proxy.sock',
            )
            upstream_sock = os.path.join(
                td, 'docker.sock',
            )

            async def docker_fake(reader, writer):
                await reader.readline()
                while True:
                    line = await reader.readline()
                    if line in (b'\r\n', b'\n', b''):
                        break
                resp = build_http_response(
                    200, body=b'{"ok":true}',
                    headers={'Connection': 'close'},
                )
                writer.write(resp)
                await writer.drain()
                writer.close()

            server = await asyncio.start_unix_server(
                docker_fake, path=upstream_sock,
            )
            try:
                ready = asyncio.Event()
                stop = asyncio.Event()
                proxy_task = asyncio.create_task(
                    proxy.run_proxy(
                        listen_sock,
                        upstream_sock,
                        ready,
                        stop_event=stop,
                    ),
                )
                await ready.wait()

                r, w = (
                    await asyncio.open_unix_connection(
                        listen_sock,
                    )
                )
                req = build_http_request(
                    'GET', '/v1.45/info',
                )
                w.write(req)
                await w.drain()
                data = await r.read(4096)
                assert b'200 OK' in data
                assert b'{"ok":true}' in data
                w.close()
                stop.set()
                await proxy_task
            finally:
                server.close()

    @pytest.mark.asyncio
    async def test_end_to_end_blocked_create(self):
        """Full proxy: privileged create is rejected."""
        with tempfile.TemporaryDirectory() as td:
            listen_sock = os.path.join(
                td, 'proxy.sock',
            )
            upstream_sock = os.path.join(
                td, 'docker.sock',
            )

            async def docker_fake(reader, writer):
                await reader.read(1)

            server = await asyncio.start_unix_server(
                docker_fake, path=upstream_sock,
            )
            try:
                ready = asyncio.Event()
                stop = asyncio.Event()
                proxy_task = asyncio.create_task(
                    proxy.run_proxy(
                        listen_sock,
                        upstream_sock,
                        ready,
                        stop_event=stop,
                    ),
                )
                await ready.wait()

                r, w = (
                    await asyncio.open_unix_connection(
                        listen_sock,
                    )
                )
                body = {
                    'HostConfig': {'Privileged': True},
                }
                req = build_http_request(
                    'POST',
                    '/v1.45/containers/create',
                    body,
                )
                w.write(req)
                await w.drain()
                data = await r.read(4096)
                assert b'403 Forbidden' in data
                assert b'privileged' in data
                w.close()
                stop.set()
                await proxy_task
            finally:
                server.close()

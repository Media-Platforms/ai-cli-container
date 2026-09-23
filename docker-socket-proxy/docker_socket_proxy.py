#!/usr/bin/env python3
"""Docker socket proxy enforcing a default-deny policy on
the daemon API.

Sits between the Docker CLI and the real daemon socket.
Every request is matched against an endpoint allowlist and
anything the proxy does not recognize is rejected.  The
endpoints that can create a new execution context --
container create, exec create, volume create and image
build -- are inspected field by field, and a field the
proxy does not know to be harmless is a rejection rather
than a pass.

The checks mirror the daemon's own parsing wherever being
more lenient than the daemon would open a hole: paths are
compared after percent-decoding and with the API version
prefix removed, because the daemon serves both
/v1.52/containers/create and /containers/create; and JSON
field names are matched without regard to case, because
Go's JSON decoder does, so {"hostconfig": {"privileged":
true}} asks for exactly what {"HostConfig": {"Privileged":
true}} asks for.

What the proxy still does not protect against is recorded
under "What the Proxy Does Not Protect Against" in the
README.  It depends on the "dev" user having no sudo
rights: with them, the real socket can be read around the
proxy and none of this applies.
"""

import asyncio
import json
import logging
import os
import re
import signal
from urllib.parse import parse_qs, unquote

UPSTREAM_SOCK = os.environ.get(
    'DOCKER_PROXY_UPSTREAM', '/var/run/docker-real.sock'
)
LISTEN_SOCK = os.environ.get(
    'DOCKER_PROXY_LISTEN', '/var/run/docker.sock'
)
ALLOWED_MOUNT_BASE = os.environ.get('ALLOWED_MOUNT_BASE', '/')
ALLOWED_RW_BASE = os.environ.get('ALLOWED_RW_BASE', '/')

# Docker's default capability set.  Adding anything beyond
# it is rejected; dropping capabilities is always allowed.
# The daemon normalizes names, so "SYS_ADMIN", "sys_admin"
# and "CAP_SYS_ADMIN" all name the same capability and are
# compared here in one form.
ALLOWED_CAPS = frozenset({
    'AUDIT_WRITE', 'CHOWN', 'DAC_OVERRIDE', 'FOWNER',
    'FSETID', 'KILL', 'MKNOD', 'NET_BIND_SERVICE',
    'NET_RAW', 'SETFCAP', 'SETGID', 'SETPCAP', 'SETUID',
    'SYS_CHROOT',
})

# HostConfig fields forwarded with whatever value they
# carry: resource limits, logging, naming and networking
# details that cannot reach the host.  Field names are
# lowercased for case-insensitive comparison.
PASS_FIELDS = frozenset({
    'autoremove', 'blkiodevicereadbps',
    'blkiodevicereadiops', 'blkiodevicewritebps',
    'blkiodevicewriteiops', 'blkioweight',
    'blkioweightdevice', 'capdrop', 'cgroupparent',
    'consolesize', 'containeridfile', 'cpucount',
    'cpupercent', 'cpuperiod', 'cpuquota',
    'cpurealtimeperiod', 'cpurealtimeruntime', 'cpushares',
    'cpusetcpus', 'cpusetmems', 'dns', 'dnsoptions',
    'dnssearch', 'extrahosts', 'groupadd', 'init',
    'iomaximumbandwidth', 'iomaximumiops', 'kernelmemory',
    'kernelmemorytcp', 'links', 'memory',
    'memoryreservation', 'memoryswap', 'memoryswappiness',
    'nanocpus', 'oomkilldisable', 'oomscoreadj',
    'pidslimit', 'portbindings', 'publishallports',
    'readonlyrootfs', 'restartpolicy', 'shmsize',
    'storageopt', 'sysctls', 'tmpfs', 'ulimits',
})

# HostConfig fields with a dedicated check in
# check_host_config.
CHECKED_FIELDS = frozenset({
    'annotations', 'binds', 'capabilities', 'capadd',
    'cgroup', 'cgroupnsmode', 'devicecgrouprules',
    'devicerequests', 'devices', 'ipcmode', 'isolation',
    'logconfig', 'maskedpaths', 'mounts', 'networkmode',
    'pidmode', 'privileged', 'readonlypaths', 'runtime',
    'securityopt', 'usernsmode', 'utsmode', 'volumedriver',
    'volumesfrom',
})

# Fields that must be absent or null.  An empty list is not
# good enough here: "MaskedPaths": [] tells the daemon to
# unmask every path it masks by default, which is what
# --security-opt systempaths=unconfined sends.
NULL_ONLY_FIELDS = frozenset({
    'maskedpaths', 'readonlypaths',
})

# Fields that must be absent or empty.  Each of these can
# hand the container a host device, another container's
# mounts, or a runtime the proxy knows nothing about.
EMPTY_ONLY_FIELDS = frozenset({
    'annotations', 'cgroup', 'devicecgrouprules',
    'devicerequests', 'devices', 'runtime', 'volumesfrom',
})

# Namespace fields, mapped to their API spelling and the
# values they may take.  "host" joins the host's namespace
# and anything with a colon joins another container's, so
# neither is allowed: sharing a PID namespace with a
# container that has host mounts exposes those mounts
# through /proc/<pid>/root.
NAMESPACE_FIELDS = {
    'cgroupnsmode': (
        'CgroupnsMode', frozenset({'', 'private'}),
    ),
    'ipcmode': (
        'IpcMode',
        frozenset({'', 'private', 'shareable', 'none'}),
    ),
    'pidmode': ('PidMode', frozenset({'', 'private'})),
    'usernsmode': (
        'UsernsMode', frozenset({'', 'private'}),
    ),
    'utsmode': ('UTSMode', frozenset({'', 'private'})),
}

# Log drivers that write where the daemon already writes.
# Drivers such as syslog take an address, which lets a
# container aim daemon-generated traffic at a socket on the
# host.
ALLOWED_LOG_DRIVERS = frozenset({
    '', 'none', 'json-file', 'local', 'journald',
})

# Volume drivers the proxy understands well enough to
# check.  A third-party driver can mount anything it likes.
ALLOWED_VOLUME_DRIVERS = frozenset({'', 'local'})

# Filesystem types the local volume driver may mount.  For
# anything but tmpfs the "device" option names the host path
# the volume points at, which is a way to reach a path
# without naming it in a bind mount.
ALLOWED_VOLUME_TYPES = frozenset({'', 'tmpfs'})

# SecurityOpt keys the proxy forwards.  seccomp is allowed
# because the dev container itself runs with seccomp
# disabled for Codex's bubblewrap sandbox, so a child
# container gains nothing its parent does not already have.
# apparmor and systempaths are not allowed, because
# unconfining either of those does grant something new.
ALLOWED_SECURITY_OPTS = frozenset({
    'no-new-privileges', 'seccomp', 'label',
})

# Resources the proxy never forwards, whatever the method.
# Swarm services and daemon plugins can both ask for host
# mounts and capabilities without going through
# POST /containers/create, and the BuildKit session
# endpoints carry their requests over gRPC where the proxy
# cannot read them.
DENIED_RESOURCES = frozenset({
    'configs', 'grpc', 'nodes', 'plugins', 'secrets',
    'services', 'session', 'swarm', 'tasks',
})

READ_METHODS = frozenset({'GET', 'HEAD'})

# Container, volume and network names and IDs.
ID = r'[a-zA-Z0-9][a-zA-Z0-9_.-]*'
# Image references hold slashes and colons, so they are
# matched loosely and the daemon does the parsing.
REF = r'[^?]+'
VERSION_RE = re.compile(r'^/v\d+(?:\.\d+)?(?=/)')
MAX_BODY = 10 * 1024 * 1024
BUF_SIZE = 65536
LOG = logging.getLogger('docker-socket-proxy')


class Denied(Exception):
    """A policy rejection carrying its client-facing
    reason."""


def main():
    """Configure logging and run the async proxy."""
    log_level = os.environ.get('LOG_LEVEL', 'WARNING').upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.WARNING),
        format='%(name)s: %(message)s',
    )
    LOG.info(
        'starting: %s -> %s (mounts under %s, rw under %s)',
        LISTEN_SOCK, UPSTREAM_SOCK,
        ALLOWED_MOUNT_BASE, ALLOWED_RW_BASE,
    )
    asyncio.run(run_proxy())


async def run_proxy(
    listen=None, upstream=None, ready_event=None,
    stop_event=None,
):
    """Start the Unix socket server and run until signalled."""
    listen = listen or LISTEN_SOCK
    upstream = upstream or UPSTREAM_SOCK
    if os.path.exists(listen):
        os.unlink(listen)

    async def on_connect(r, w):
        await handle_connection(r, w, upstream)

    server = await asyncio.start_unix_server(
        on_connect, path=listen,
    )
    os.chmod(listen, 0o666)
    LOG.info('listening on %s', listen)
    if ready_event is not None:
        ready_event.set()
    stop = stop_event or asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    server.close()


async def handle_connection(client_r, client_w, upstream):
    """Handle a single client connection to the proxy."""
    upstream_r = upstream_w = None
    try:
        upstream_r, upstream_w = (
            await asyncio.open_unix_connection(upstream)
        )
        while True:
            keep_going = await proxy_request(
                client_r, client_w,
                upstream_r, upstream_w,
            )
            if not keep_going:
                break
    except (
        asyncio.IncompleteReadError,
        ConnectionError,
        OSError,
    ):
        pass
    finally:
        for writer in (client_w, upstream_w):
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass


async def proxy_request(
    client_r, client_w, upstream_r, upstream_w,
):
    """Proxy one HTTP request-response cycle.

    Returns True to continue (keep-alive), False to stop.
    A rejected request always stops: the proxy has not read
    the body it refused, so it cannot find the start of the
    next request on the connection.
    """
    req_line, req_raw, req_hdrs = (
        await read_headers(client_r)
    )
    if req_line is None:
        return False
    LOG.debug(
        'request: %s', req_line.decode('latin-1').strip(),
    )
    framing, error = read_framing(req_hdrs)
    if error:
        LOG.warning('rejected: %s', error)
        await send_error(client_w, 400, error)
        return False
    content_length, is_chunked = framing
    method, path, query, error = parse_target(req_line)
    if error:
        LOG.warning('rejected: %s', error)
        await send_error(client_w, 400, error)
        return False
    rule, error = match_rule(method, path)
    if error:
        LOG.warning('rejected: %s', error)
        await send_error(client_w, 403, error)
        return False
    needs_body, validator = rule

    if needs_body:
        if is_chunked:
            error = (
                'request body must use Content-Length: '
                f'{method} {path}'
            )
            LOG.warning('rejected: %s', error)
            await send_error(client_w, 400, error)
            return False
        if content_length > MAX_BODY:
            await send_error(
                client_w, 413, 'request body too large',
            )
            return False
        body = b''
        if content_length > 0:
            body = await client_r.readexactly(
                content_length,
            )
        error = validator(query, body)
        if error:
            LOG.warning('rejected: %s', error)
            await send_error(client_w, 403, error)
            return False
        upstream_w.write(req_raw + body)
        await upstream_w.drain()
    else:
        if validator is not None:
            error = validator(query, None)
            if error:
                LOG.warning('rejected: %s', error)
                await send_error(client_w, 403, error)
                return False
        upstream_w.write(req_raw)
        await upstream_w.drain()
        if content_length > 0:
            body = await client_r.readexactly(
                content_length,
            )
            upstream_w.write(body)
            await upstream_w.drain()
        elif is_chunked:
            await forward_chunked(client_r, upstream_w)
    status, should_close = await relay_response(
        upstream_r, client_w, method,
    )
    LOG.debug(
        'response: %d, close=%s', status, should_close,
    )
    if status == 101:
        LOG.debug('entering splice mode')
        await splice(
            client_r, client_w, upstream_r, upstream_w,
        )
        LOG.debug('splice finished')
        return False
    return not should_close


def read_framing(headers):
    """Work out where the request body ends.

    Returns ((content_length, is_chunked), error).  Hiding
    a second request from the proxy needs the proxy and the
    daemon to disagree about that boundary, so a request
    that describes its body two ways, or repeats either
    framing header, is rejected instead of guessed at.
    """
    if headers.duplicated & {
        'content-length', 'transfer-encoding',
    }:
        return None, 'repeated framing header'
    encoding = headers.get('transfer-encoding', '').lower()
    raw_length = headers.get('content-length')
    if encoding and raw_length is not None:
        return None, (
            'Content-Length and Transfer-Encoding are '
            'both set'
        )
    if encoding and encoding != 'chunked':
        return None, (
            f'unsupported Transfer-Encoding: {encoding}'
        )
    content_length = 0
    if raw_length is not None:
        if not raw_length.isdigit():
            return None, 'malformed Content-Length'
        content_length = int(raw_length)
    return (content_length, encoding == 'chunked'), None


def parse_target(req_line):
    """Split a request line into method, path and query.

    Returns (method, path, query, error).  The path is
    percent-decoded and stripped of its API version prefix
    so that it can be compared against one set of rules,
    because the daemon answers both /v1.52/containers/create
    and /containers/create.
    """
    parts = req_line.decode('latin-1').split()
    if len(parts) != 3 or not parts[2].startswith('HTTP/'):
        return None, None, None, 'malformed request line'
    method, target = parts[0], parts[1]
    if '://' in target:
        target = '/' + target.split('://', 1)[1].partition(
            '/',
        )[2]
    path, _, query = target.partition('?')
    path = unquote(path)
    if not path.startswith('/'):
        return None, None, None, 'malformed request path'
    # Go's mux redirects rather than serves these, so the
    # proxy never has to reason about what they mean.
    if any(
        segment in ('', '.', '..')
        for segment in path.split('/')[1:]
    ):
        return None, None, None, 'malformed request path'
    path = VERSION_RE.sub('', path, count=1)
    return method, path, query, None


def match_rule(method, path):
    """Find the policy rule for a request.

    Returns ((needs_body, validator), error).
    """
    resource = path.split('/')[1]
    if resource in DENIED_RESOURCES:
        return None, f'endpoint not allowed: {method} {path}'
    if method in READ_METHODS:
        return (False, None), None
    for rule_method, pattern, needs_body, validator in (
        WRITE_RULES
    ):
        if method == rule_method and pattern.fullmatch(path):
            return (needs_body, validator), None
    return None, f'endpoint not allowed: {method} {path}'


async def relay_response(upstream_r, client_w, method=None):
    """Read one HTTP response and forward to the client.

    Returns (status_code, should_close).  A response to HEAD
    describes the body the same GET would return but does not
    send it, so its Content-Length must not be read as one --
    "docker cp" starts with a HEAD and would otherwise hang.
    """
    resp_line, resp_raw, resp_hdrs = (
        await read_headers(upstream_r)
    )
    if resp_line is None:
        return 0, True
    status = parse_status(resp_line)
    client_w.write(resp_raw)
    await client_w.drain()
    if status == 101:
        return 101, True
    if (
        method == 'HEAD'
        or (100 <= status < 200)
        or status in (204, 304)
    ):
        conn = resp_hdrs.get('connection', '').lower()
        return status, conn == 'close'
    resp_cl = int(
        resp_hdrs.get('content-length', '-1')
    )
    is_chunked = 'chunked' in resp_hdrs.get(
        'transfer-encoding', '',
    )
    if resp_cl >= 0:
        if resp_cl > 0:
            data = await upstream_r.readexactly(resp_cl)
            client_w.write(data)
            await client_w.drain()
    elif is_chunked:
        await forward_chunked(upstream_r, client_w)
    else:
        while True:
            data = await upstream_r.read(BUF_SIZE)
            if not data:
                break
            client_w.write(data)
            await client_w.drain()
        return status, True
    conn = resp_hdrs.get('connection', '').lower()
    return status, conn == 'close'


async def forward_chunked(src, dst):
    """Forward chunked transfer-encoded body.

    Handles IncompleteReadError (Docker closing mid-chunk)
    by forwarding any partial data before returning.
    Also handles empty readline (connection drop before
    chunk header).
    """
    try:
        while True:
            chunk_hdr = await src.readline()
            if not chunk_hdr:
                break
            dst.write(chunk_hdr)
            size = int(chunk_hdr.strip(), 16)
            if size == 0:
                trailer = await src.readline()
                dst.write(trailer)
                await dst.drain()
                break
            data = await src.readexactly(size + 2)
            dst.write(data)
            await dst.drain()
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            dst.write(exc.partial)
            await dst.drain()


async def splice(r1, w1, r2, w2):
    """Bidirectional byte forwarding until both directions
    complete.

    When one direction hits EOF, propagate the half-close
    and wait for the other direction to finish.
    """
    task1 = asyncio.create_task(
        _forward_stream(r1, w2),
    )
    task2 = asyncio.create_task(
        _forward_stream(r2, w1),
    )
    try:
        done, pending = await asyncio.wait(
            [task1, task2],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if pending:
            completed = done.pop()
            remaining = pending.pop()
            if completed == task1:
                _half_close(w2)
            else:
                _half_close(w1)
            try:
                await remaining
            except Exception:
                pass
    finally:
        for task in (task1, task2):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (
                    asyncio.CancelledError,
                    Exception,
                ):
                    pass
        for writer in (w1, w2):
            try:
                writer.close()
            except Exception:
                pass


def _half_close(writer):
    """Send write-EOF without tearing down the full
    connection.
    """
    try:
        if writer.can_write_eof():
            writer.write_eof()
    except Exception:
        pass


async def _forward_stream(reader, writer):
    """Forward bytes from reader to writer until EOF."""
    try:
        while True:
            data = await reader.read(BUF_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (
        ConnectionError, OSError, asyncio.CancelledError,
    ):
        pass


class Headers(dict):
    """Header map that also records repeated names."""

    def __init__(self):
        super().__init__()
        self.duplicated = set()

    def add(self, name, value):
        """Record a header, noting a repeated name."""
        if name in self:
            self.duplicated.add(name)
        self[name] = value


async def read_headers(reader):
    """Read HTTP start line and headers.

    Returns (first_line, all_raw_bytes, headers).
    Returns (None, None, None) on EOF.
    """
    first_line = await reader.readline()
    if not first_line:
        return None, None, None
    raw = bytearray(first_line)
    headers = Headers()
    while True:
        line = await reader.readline()
        raw.extend(line)
        if line in (b'\r\n', b'\n', b''):
            break
        decoded = line.decode('latin-1').strip()
        if ':' in decoded:
            key, _, value = decoded.partition(':')
            headers.add(key.strip().lower(), value.strip())
    return bytes(first_line), bytes(raw), headers


async def send_error(writer, status, message):
    """Send an HTTP error response with a JSON body."""
    body = json.dumps({'message': message}).encode()
    phrases = {
        400: 'Bad Request',
        403: 'Forbidden',
        413: 'Request Entity Too Large',
    }
    phrase = phrases.get(status, 'Error')
    header = (
        f'HTTP/1.1 {status} {phrase}\r\n'
        f'Content-Type: application/json\r\n'
        f'Content-Length: {len(body)}\r\n'
        f'Connection: close\r\n'
        f'\r\n'
    )
    writer.write(header.encode() + body)
    await writer.drain()


# -- JSON body helpers ------------------------------------


def parse_body(body):
    """Parse a JSON request body into a dict.

    Returns (parsed, error).  A body that is valid JSON but
    not an object is an error, because every endpoint the
    proxy inspects takes an object.
    """
    if not body:
        return {}, None
    try:
        parsed = json.loads(body)
    except (
        json.JSONDecodeError, UnicodeDecodeError,
        RecursionError,
    ):
        return None, 'invalid JSON in request body'
    if parsed is None:
        return {}, None
    if not isinstance(parsed, dict):
        return None, 'request body must be a JSON object'
    return parsed, None


def field(obj, name):
    """Read one struct field from a parsed JSON object.

    Docker's JSON decoder matches field names without
    regard to case, so match the same way.  Two spellings
    of one field are rejected rather than guessed at: the
    daemon would take the last of them, and a proxy that
    checked the first would be checking a field the daemon
    ignores.  Repeats of a single spelling need no such
    care, because json.loads keeps the last value just as
    Go does.
    """
    if not isinstance(obj, dict):
        return None
    matches = [
        key for key in obj if key.lower() == name
    ]
    if len(matches) > 1:
        raise Denied(f'field given twice: {name}')
    if not matches:
        return None
    return obj[matches[0]]


def as_list(value, name):
    """Read a field that must hold a list."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise Denied(f'{name} must be a list')
    return value


def as_str(value, name):
    """Read a field that must hold a string."""
    if value is None:
        return ''
    if not isinstance(value, str):
        raise Denied(f'{name} must be a string')
    return value


def as_dict(value, name):
    """Read a field that must hold an object."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Denied(f'{name} must be an object')
    return value


def is_empty(value):
    """Whether a value leaves the daemon's default alone."""
    return value in (None, False, 0, '', [], {})


# -- POST /containers/create ------------------------------


def validate_create(query, body):
    """Validate a container create request.

    Returns an error message or None.
    """
    return validate_create_body(
        body, ALLOWED_MOUNT_BASE, ALLOWED_RW_BASE,
    )


def validate_create_body(body, allowed_base, rw_base):
    """Parse and validate a container create request body.

    Returns an error message or None.
    """
    parsed, error = parse_body(body)
    if error:
        return error
    return check_create_request(
        parsed, allowed_base, rw_base,
    )


def check_create_request(body, allowed_base, rw_base):
    """Validate a parsed container create request.

    Returns an error message if the request violates policy,
    or None if allowed.
    """
    try:
        host_cfg = as_dict(
            field(body, 'hostconfig'), 'HostConfig',
        )
        check_host_config(host_cfg, allowed_base, rw_base)
    except Denied as denied:
        return str(denied)
    return None


def check_host_config(host_cfg, allowed_base, rw_base):
    """Check every field a HostConfig sets.

    Raises Denied on the first field that is neither known
    to be harmless nor left at its default.
    """
    seen = set()
    for key, value in host_cfg.items():
        name = key.lower()
        if name in seen:
            raise Denied(f'field given twice: {name}')
        seen.add(name)
        if name in NULL_ONLY_FIELDS:
            if value is not None:
                raise Denied(
                    f'HostConfig field not allowed: {key}'
                )
        elif name in CHECKED_FIELDS or name in PASS_FIELDS:
            continue
        elif not is_empty(value):
            raise Denied(
                f'HostConfig field not allowed: {key}'
            )

    if field(host_cfg, 'privileged'):
        raise Denied('privileged containers are not allowed')
    for name in sorted(EMPTY_ONLY_FIELDS):
        if not is_empty(field(host_cfg, name)):
            raise Denied(
                f'HostConfig field not allowed: {name}'
            )
    check_caps(host_cfg)
    check_namespaces(host_cfg)
    check_security_opt(host_cfg)
    check_log_config(host_cfg)
    check_simple_values(host_cfg)
    check_binds(host_cfg, allowed_base, rw_base)
    check_mounts(host_cfg, allowed_base, rw_base)


def check_caps(host_cfg):
    """Check CapAdd and Capabilities against the allowlist.

    The daemon accepts "SYS_ADMIN", "sys_admin" and
    "CAP_SYS_ADMIN" for one capability, so compare them in
    a single normalized form.
    """
    for name in ('capadd', 'capabilities'):
        value = field(host_cfg, name)
        if value is None:
            continue
        requested = set()
        for entry in as_list(value, name):
            requested.add(
                as_str(entry, name).upper().removeprefix(
                    'CAP_',
                )
            )
        blocked = requested - ALLOWED_CAPS
        if blocked:
            raise Denied(
                f'capabilities not allowed: '
                f'{sorted(blocked)}'
            )


def check_namespaces(host_cfg):
    """Check the namespace and network mode fields."""
    for name, (label, allowed) in sorted(
        NAMESPACE_FIELDS.items(),
    ):
        value = as_str(field(host_cfg, name), label)
        if value.lower() not in allowed:
            raise Denied(f'{label} not allowed: {value}')
    network = as_str(
        field(host_cfg, 'networkmode'), 'networkmode',
    )
    if network.lower() == 'host' or ':' in network:
        raise Denied(f'network mode not allowed: {network}')


def check_security_opt(host_cfg):
    """Check SecurityOpt entries against the allowlist.

    Mirrors the daemon's own splitting, which cuts at the
    first '=' and falls back to ':' for the older syntax.
    """
    entries = as_list(
        field(host_cfg, 'securityopt'), 'SecurityOpt',
    )
    for entry in entries:
        opt = as_str(entry, 'SecurityOpt')
        key, sep, value = opt.partition('=')
        if not sep and key != 'no-new-privileges':
            key, _, value = opt.partition(':')
        key = key.strip().lower()
        if key not in ALLOWED_SECURITY_OPTS:
            raise Denied(f'security option not allowed: {opt}')
        if key == 'label' and value.strip() == 'disable':
            raise Denied(f'security option not allowed: {opt}')


def check_log_config(host_cfg):
    """Check the logging driver.

    A driver that takes an address lets a container aim
    daemon-generated traffic at a socket on the host.
    """
    log_cfg = as_dict(
        field(host_cfg, 'logconfig'), 'LogConfig',
    )
    driver = as_str(field(log_cfg, 'type'), 'LogConfig.Type')
    if driver.lower() not in ALLOWED_LOG_DRIVERS:
        raise Denied(f'log driver not allowed: {driver}')


def check_simple_values(host_cfg):
    """Check the remaining single-value fields."""
    isolation = as_str(
        field(host_cfg, 'isolation'), 'Isolation',
    )
    if isolation.lower() not in ('', 'default'):
        raise Denied(f'isolation not allowed: {isolation}')
    driver = as_str(
        field(host_cfg, 'volumedriver'), 'VolumeDriver',
    )
    if driver.lower() not in ALLOWED_VOLUME_DRIVERS:
        raise Denied(f'volume driver not allowed: {driver}')


def check_binds(host_cfg, allowed_base, rw_base):
    """Check every Binds entry."""
    binds = as_list(field(host_cfg, 'binds'), 'Binds')
    for bind in binds:
        error = check_bind_string(
            as_str(bind, 'Binds'), allowed_base, rw_base,
        )
        if error:
            raise Denied(error)


def check_mounts(host_cfg, allowed_base, rw_base):
    """Check every Mounts entry."""
    mounts = as_list(field(host_cfg, 'mounts'), 'Mounts')
    for mount in mounts:
        error = check_mount_object(
            as_dict(mount, 'Mounts'),
            allowed_base, rw_base,
        )
        if error:
            raise Denied(error)


def check_bind_string(bind, allowed_base, rw_base):
    """Validate a Binds entry ('host:container[:opts]').

    Returns an error message or None.  An entry whose first
    field is not a path names a volume, which the daemon
    creates under its own directory; POST /volumes/create
    is what keeps such a volume from pointing back at the
    host.
    """
    parts = bind.split(':')
    host_path = parts[0]
    if not host_path.startswith('/'):
        if not re.fullmatch(ID, host_path):
            return f'bind mount not allowed: {host_path}'
        return None
    if not is_allowed_path(host_path, allowed_base):
        return f'bind mount not allowed: {host_path}'
    if not is_allowed_path(host_path, rw_base):
        opts = parts[2] if len(parts) > 2 else ''
        if 'ro' not in opts.split(','):
            return (
                f'bind mount must be read-only: '
                f'{host_path}'
            )
    return None


def check_mount_object(mount, allowed_base, rw_base):
    """Validate a Mounts entry.

    Returns an error message or None.
    """
    try:
        mount_type = as_str(
            field(mount, 'type'), 'Mounts.Type',
        ).lower()
        if mount_type == 'tmpfs':
            return None
        if mount_type == 'volume':
            return check_volume_mount(mount)
        if mount_type != 'bind':
            return f'mount type not allowed: {mount_type}'
        source = as_str(
            field(mount, 'source'), 'Mounts.Source',
        )
        if not is_allowed_path(source, allowed_base):
            return f'bind mount not allowed: {source}'
        if not is_allowed_path(source, rw_base):
            if not field(mount, 'readonly'):
                return (
                    f'bind mount must be read-only: '
                    f'{source}'
                )
    except Denied as denied:
        return str(denied)
    return None


def check_volume_mount(mount):
    """Check a Type=volume mount.

    A volume mount can carry the driver options that would
    otherwise be set by POST /volumes/create, so it gets
    the same check.
    """
    options = as_dict(
        field(mount, 'volumeoptions'),
        'Mounts.VolumeOptions',
    )
    driver_cfg = as_dict(
        field(options, 'driverconfig'),
        'Mounts.VolumeOptions.DriverConfig',
    )
    return check_volume_driver(
        as_str(field(driver_cfg, 'name'), 'DriverConfig.Name'),
        as_dict(
            field(driver_cfg, 'options'),
            'DriverConfig.Options',
        ),
    )


def is_allowed_path(path, allowed_base):
    """Check whether path is under the allowed base."""
    normalized = os.path.normpath(path)
    base = os.path.normpath(allowed_base)
    if base == '/':
        return True
    return (
        normalized == base
        or normalized.startswith(base + '/')
    )


# -- POST /volumes/create ---------------------------------


def validate_volume(query, body):
    """Validate a volume create request.

    Returns an error message or None.
    """
    parsed, error = parse_body(body)
    if error:
        return error
    try:
        if not is_empty(field(parsed, 'clustervolumespec')):
            return 'cluster volumes are not allowed'
        return check_volume_driver(
            as_str(field(parsed, 'driver'), 'Driver'),
            as_dict(field(parsed, 'driveropts'),
                    'DriverOpts'),
        )
    except Denied as denied:
        return str(denied)


def check_volume_driver(driver, options):
    """Check a volume driver and its options.

    Returns an error message or None.  The local driver
    mounts whatever "device" names when "type" and "o"
    describe a bind, which is a way to reach any host path
    without ever naming it in a bind mount.
    """
    if driver.lower() not in ALLOWED_VOLUME_DRIVERS:
        return f'volume driver not allowed: {driver}'
    lowered = {
        str(key).lower(): value
        for key, value in options.items()
    }
    vol_type = as_str(lowered.get('type'), 'type').lower()
    if vol_type not in ALLOWED_VOLUME_TYPES:
        return f'volume type not allowed: {vol_type}'
    mount_opts = as_str(lowered.get('o'), 'o').lower()
    if 'bind' in mount_opts.split(','):
        return 'volume driver option not allowed: o=bind'
    # mount(2) ignores the source argument when the type is
    # tmpfs, and the local driver rejects a tmpfs volume that
    # does not name a device, so tmpfs volumes may set one.
    # Any other type mounts the device, so it must stay empty.
    if vol_type != 'tmpfs' and not is_empty(
        lowered.get('device'),
    ):
        return 'volume driver option not allowed: device'
    return None


# -- POST /containers/{id}/exec ---------------------------


def validate_exec(query, body):
    """Validate an exec create request.

    Returns an error message or None.  A privileged exec
    runs with every capability even in a container that was
    created without them.
    """
    parsed, error = parse_body(body)
    if error:
        return error
    try:
        if field(parsed, 'privileged'):
            return 'privileged exec is not allowed'
    except Denied as denied:
        return str(denied)
    return None


# -- POST /build ------------------------------------------


def validate_build(query, body):
    """Validate an image build request.

    Returns an error message or None.  The classic builder
    runs each step in a container the daemon creates
    itself, so these query parameters are the only chance
    to check it; BuildKit is refused outright because it
    carries its own build options over gRPC.
    """
    params = parse_qs(query, keep_blank_values=True)

    def first(name):
        return (params.get(name) or [''])[0]

    version = first('version')
    if version not in ('', '1'):
        return f'builder version not allowed: {version}'
    if first('securityopt'):
        return 'build security options are not allowed'
    network = first('networkmode')
    if network.lower() == 'host' or ':' in network:
        return f'network mode not allowed: {network}'
    return None


# -- Endpoint rules ---------------------------------------
#
# Every write request must match one of these.  A rule that
# needs its body sees it as bytes; the others are forwarded
# with the body untouched, which is what lets POST /build
# stream a build context the proxy never buffers.

WRITE_RULES = tuple(
    (method, re.compile(pattern), needs_body, validator)
    for method, pattern, needs_body, validator in (
        ('POST', r'/containers/create', True,
         validate_create),
        ('POST', rf'/containers/{ID}/exec', True,
         validate_exec),
        ('POST', rf'/containers/{ID}/'
                 r'(start|stop|restart|kill|pause|unpause'
                 r'|wait|resize|rename|update|attach'
                 r'|attach/ws)', False, None),
        ('POST', r'/containers/prune', False, None),
        ('PUT', rf'/containers/{ID}/archive', False, None),
        ('DELETE', rf'/containers/{ID}', False, None),
        ('POST', rf'/exec/{ID}/(start|resize)', False, None),
        ('POST', r'/images/(create|load|prune)', False,
         None),
        ('POST', rf'/images/{REF}/(push|tag)', False, None),
        ('DELETE', rf'/images/{REF}', False, None),
        ('POST', r'/build', False, validate_build),
        ('POST', r'/commit', False, None),
        ('POST', r'/networks/(create|prune)', False, None),
        ('POST', rf'/networks/{ID}/(connect|disconnect)',
         False, None),
        ('DELETE', rf'/networks/{ID}', False, None),
        ('POST', r'/volumes/create', True, validate_volume),
        ('POST', r'/volumes/prune', False, None),
        ('DELETE', rf'/volumes/{ID}', False, None),
        ('POST', r'/auth', False, None),
    )
)


def parse_status(resp_line):
    """Extract HTTP status code from a response line."""
    parts = resp_line.split(None, 2)
    if len(parts) >= 2:
        try:
            return int(parts[1])
        except (ValueError, TypeError):
            pass
    return 0


if __name__ == '__main__':
    main()

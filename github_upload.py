#!/usr/bin/env python3
"""Upload files and completed ROS bags as resumable GitHub release assets."""

import argparse
import datetime
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from fetch_bag import TransferError as PublishError, rename_without_replacing


REPOSITORY = 'marcusnguyen2411/ROS-Bags'
API_ROOT = 'https://api.github.com'
PART_BYTES = 512 * 1024 * 1024  # Below GitHub's 2 GiB release-asset limit.
MAX_PARTS = 998  # Leave room for the manifest and any future metadata.
BUFFER_BYTES = 1024 * 1024


class UploadError(Exception):
    pass


def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.job-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def token_from_login(checkout=None, allow_environment=True):
    if allow_environment:
        for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
            if os.environ.get(name):
                return os.environ[name]
    environment = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never')
    if not allow_environment:
        # A one-shot shell token may not be available to the background service.
        environment.pop('GH_TOKEN', None)
        environment.pop('GITHUB_TOKEN', None)
    if shutil.which('gh'):
        try:
            result = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com'],
                                    text=True, capture_output=True, env=environment, timeout=15)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    if shutil.which('git'):
        try:
            result = subprocess.run(['git', 'credential', 'fill'], cwd=checkout,
                                    input='protocol=https\nhost=github.com\n\n', text=True,
                                    capture_output=True, env=environment, timeout=15)
            credentials = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            if result.returncode == 0 and credentials.get('password'):
                return credentials['password']
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise UploadError('GitHub login unavailable. Install GitHub CLI and run gh auth login on Karbon.')


class GitHub:
    def __init__(self, token=None):
        self.token = token

    def headers(self):
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'rosbag-uploader',
                   'X-GitHub-Api-Version': '2022-11-28'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        return headers

    def request(self, method, path, value=None):
        body = None if value is None else json.dumps(value).encode('utf-8')
        request = urllib.request.Request(API_ROOT + path, data=body, headers=self.headers(), method=method)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                content = response.read()
                return json.loads(content) if content else None
        except urllib.error.HTTPError as error:
            raise UploadError(f'GitHub API returned HTTP {error.code} for {method} {path}.') from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise UploadError(f'GitHub connection failed: {type(error).__name__}.') from error

    def repo_path(self, suffix=''):
        return '/repos/' + REPOSITORY + suffix

    def create_release(self, job, source_name):
        tag = 'data-' + job['id']
        notes = ('Public data upload. Download all upload.tar.part files and run:\n\n'
                 '`cat upload.tar.part* | tar -xf -`\n\n'
                 'The manifest contains SHA-256 checksums. The repository uploader can also download and verify them.')
        return self.request('POST', self.repo_path('/releases'),
                            {'tag_name': tag, 'name': source_name, 'body': notes, 'draft': True})

    def get_release(self, release_id):
        return self.request('GET', self.repo_path('/releases/' + str(release_id)))

    def get_release_by_tag(self, tag):
        return self.request('GET', self.repo_path('/releases/tags/' + urllib.parse.quote(tag, safe='')))

    def list_assets(self, release_id):
        assets, page = [], 1
        while True:
            batch = self.request('GET', self.repo_path(f'/releases/{release_id}/assets?per_page=100&page={page}'))
            assets.extend(batch)
            if len(batch) < 100:
                return assets
            page += 1

    def delete_starter(self, asset_id):
        self.request('DELETE', self.repo_path('/releases/assets/' + str(asset_id)))

    def upload_asset(self, upload_url, path, name):
        # Never send credentials to a URL supplied by an unexpected host.
        parsed = urllib.parse.urlsplit(upload_url.split('{', 1)[0])
        if parsed.scheme != 'https' or parsed.hostname != 'uploads.github.com' or parsed.username or parsed.password:
            raise UploadError('GitHub returned an unexpected upload address.')
        query = urllib.parse.urlencode({'name': name})
        target = parsed.path + '?' + query
        headers = dict(self.headers(), **{'Content-Type': 'application/octet-stream',
                                         'Content-Length': str(Path(path).stat().st_size)})
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=120, blocksize=BUFFER_BYTES)
        try:
            with Path(path).open('rb') as stream:
                connection.request('POST', target, body=stream, headers=headers)
                response = connection.getresponse()
                data = response.read()
            if response.status != 201:
                raise UploadError(f'GitHub rejected asset {name} (HTTP {response.status}).')
            return json.loads(data)
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise UploadError(f'Upload of {name} was interrupted: {type(error).__name__}.') from error
        finally:
            connection.close()

    def publish(self, release_id):
        return self.request('PATCH', self.repo_path('/releases/' + str(release_id)),
                            {'draft': False, 'make_latest': 'false'})


def valid_source(source):
    if not str(source):
        raise UploadError('Choose a file or folder.')
    source = Path(source).expanduser().resolve(strict=True)
    if not source.name or source == source.parent:
        raise UploadError('Choose a file or folder, not the filesystem root.')
    if '\\' in source.name or any(ord(character) < 32 or ord(character) == 127 for character in source.name):
        raise UploadError('The top-level name cannot contain backslashes or control characters.')
    if not source.is_file() and not source.is_dir():
        raise UploadError('Choose a regular file or directory.')
    return source


def source_fingerprint(source):
    """Detect changes across retries without reading every large bag twice."""
    digest = hashlib.sha256()

    def add(path, relative):
        stat = path.lstat()
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise UploadError(f'Symbolic links and special files are not supported: {relative}')
        fields = [relative, str(stat.st_mode), str(stat.st_size), str(stat.st_mtime_ns), str(stat.st_ino)]
        digest.update(json.dumps(fields, ensure_ascii=True).encode('utf-8') + b'\n')

    add(source, '.')
    if source.is_dir():
        for parent, directories, files in os.walk(source, followlinks=False):
            directories.sort()
            files.sort()
            for name in directories + files:
                path = Path(parent) / name
                add(path, path.relative_to(source).as_posix())
    return digest.hexdigest()


def source_from_job(job, checkout):
    if job.get('version') != 1 or not re.fullmatch(r'[0-9a-f]{32}', job.get('id', '')):
        raise UploadError('Invalid upload job.')
    if job.get('source_type') == 'checkout':
        if checkout is None:
            raise UploadError('A checkout is required for this recording.')
        root = Path(checkout).resolve()
        relative = PurePosixPath(job.get('source', ''))
        if not relative.parts or relative.is_absolute() or any(part in ('.', '..') for part in relative.parts):
            raise UploadError('Invalid relative recording path.')
        source = valid_source(root.joinpath(*relative.parts))
        try:
            source.relative_to(root)
        except ValueError as error:
            raise UploadError('The recording is outside the checkout.') from error
        if not source.is_dir() or not (source / 'metadata.yaml').is_file():
            raise UploadError('The queued recording or metadata.yaml is missing.')
        return source
    if job.get('source_type') == 'absolute':
        return valid_source(job.get('source', ''))
    raise UploadError('Invalid upload source type.')


def sanitize_tar_info(info):
    # Do not publish the host username or retain ownership on extraction.
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    return info


def asset_matches(asset, size, checksum):
    return (asset.get('state') == 'uploaded' and asset.get('size') == size and
            asset.get('digest') == 'sha256:' + checksum)


class PartWriter:
    """Write a tar stream in bounded pieces and upload each completed piece."""
    def __init__(self, client, release, assets, temp_path):
        self.client, self.release, self.assets = client, release, assets
        self.temp_path = Path(temp_path)
        self.stream = None
        self.size = 0
        self.index = 0
        self.hash = None
        self.parts = []

    def write(self, data):
        total = len(data)
        position = 0
        while position < total:
            if self.stream is None:
                self.index += 1
                if self.index > MAX_PARTS:
                    raise UploadError('The upload exceeds GitHub\'s release asset count limit.')
                self.stream = self.temp_path.open('wb')
                self.hash = hashlib.sha256()
                self.size = 0
            block = data[position:position + min(PART_BYTES - self.size, total - position)]
            self.stream.write(block)
            self.hash.update(block)
            self.size += len(block)
            position += len(block)
            if self.size == PART_BYTES:
                self.finish_part()
        return total

    def finish_part(self):
        if self.stream is None:
            return
        self.stream.close()
        self.stream = None
        name = f'upload.tar.part{self.index:06d}'
        checksum = self.hash.hexdigest()
        existing = self.assets.get(name)
        if existing and existing.get('state') == 'starter' and existing.get('size') == 0:
            self.client.delete_starter(existing['id'])
            existing = None
        if existing:
            if not asset_matches(existing, self.size, checksum):
                raise UploadError(f'Existing asset {name} differs; the source may have changed.')
            print(f'Already uploaded: {name}', flush=True)
        else:
            print(f'Uploading {name} ({self.size} bytes)', flush=True)
            uploaded = self.client.upload_asset(self.release['upload_url'], self.temp_path, name)
            if not asset_matches(uploaded, self.size, checksum):
                raise UploadError(f'GitHub did not confirm the checksum for {name}.')
            self.assets[name] = uploaded
        self.parts.append({'name': name, 'size': self.size, 'sha256': checksum})
        self.temp_path.unlink()

    def finish(self):
        self.finish_part()

    def abort(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def process_job(job_path, checkout=None, client=None):
    job_path = Path(job_path)
    job = read_json(job_path)
    source = source_from_job(job, checkout)
    fingerprint = source_fingerprint(source)
    if job.get('fingerprint') and job['fingerprint'] != fingerprint:
        raise UploadError('The source changed since this upload began; queue it again as a new upload.')
    job['fingerprint'] = fingerprint
    atomic_json(job_path, job)
    client = client or GitHub(token_from_login(checkout))
    if job.get('release_id'):
        release = client.get_release(job['release_id'])
    else:
        release = client.create_release(job, source.name)
        job['release_id'] = release['id']
        atomic_json(job_path, job)
    assets = {asset['name']: asset for asset in client.list_assets(release['id'])}
    if not release['draft']:
        manifest = assets.get('manifest.json')
        if not job.get('manifest_sha256') or not manifest or manifest.get('digest') != 'sha256:' + job['manifest_sha256']:
            raise UploadError('The published release does not match this upload job.')
        return release['html_url']

    # Only one bounded temporary part is needed. Uploaded parts are compared
    # by SHA-256 on retries and are never replaced or uploaded a second time.
    temporary = job_path.parent / ('.' + job['id'] + '.part')
    writer = PartWriter(client, release, assets, temporary)
    try:
        with tarfile.open(fileobj=writer, mode='w|', format=tarfile.PAX_FORMAT, dereference=True) as archive:
            archive.add(source, arcname=source.name, recursive=True, filter=sanitize_tar_info)
        writer.finish()
    except BaseException:
        writer.abort()
        raise
    if source_fingerprint(source) != fingerprint:
        raise UploadError('The source changed during upload; the draft was not published.')
    expected = {part['name'] for part in writer.parts}
    unexpected = [name for name in assets if name.startswith('upload.tar.part') and name not in expected]
    if unexpected:
        raise UploadError('The draft contains unexpected parts; the source may have changed.')
    manifest = {'version': 1, 'name': source.name, 'parts': writer.parts}
    manifest_data = (json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode('utf-8')
    manifest_hash = hashlib.sha256(manifest_data).hexdigest()
    manifest_path = job_path.parent / ('.' + job['id'] + '.manifest')
    manifest_path.write_bytes(manifest_data)
    existing = assets.get('manifest.json')
    if existing and existing.get('state') == 'starter' and existing.get('size') == 0:
        client.delete_starter(existing['id'])
        existing = None
    if existing:
        if not asset_matches(existing, len(manifest_data), manifest_hash):
            raise UploadError('The draft manifest differs; the source may have changed.')
    else:
        uploaded = client.upload_asset(release['upload_url'], manifest_path, 'manifest.json')
        if not asset_matches(uploaded, len(manifest_data), manifest_hash):
            raise UploadError('GitHub did not confirm the manifest checksum.')
    manifest_path.unlink()
    job['manifest_sha256'] = manifest_hash
    atomic_json(job_path, job)
    result = client.publish(release['id'])
    return result['html_url']


def make_job(source, queue):
    source = valid_source(source)
    source_fingerprint(source)  # Reject unsupported files before creating a job.
    queue = Path(queue)
    queue.mkdir(parents=True, exist_ok=True, mode=0o700)
    job_id = uuid.uuid4().hex
    job = {'version': 1, 'id': job_id, 'source_type': 'absolute', 'source': str(source),
           'created': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    job_path = queue / (job_id + '.json')
    atomic_json(job_path, job)
    return job_path


def complete_job(job_path, url):
    job_path = Path(job_path)
    job = read_json(job_path)
    job.pop('last_error', None)
    job['completed_url'] = url
    job['completed'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    atomic_json(job_path, job)
    done = job_path.parent.parent / 'done'
    done.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = done / job_path.name
    if os.path.lexists(target):
        raise UploadError(f'Completed job already exists: {target}')
    os.rename(job_path, target)
    return target


def remember_error(job_path, error):
    if Path(job_path).is_file():
        job = read_json(job_path)
        job['last_error'] = str(error)[:500]
        atomic_json(job_path, job)


def run_job(job_path, checkout=None, client=None):
    job_path = Path(job_path)
    if not job_path.is_file():
        raise UploadError(f'Upload job not found: {job_path}')
    lock_path = job_path.with_suffix('.lock')
    lock = lock_path.open('a')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UploadError('This upload job is already running.') from error
        try:
            url = process_job(job_path, checkout=checkout, client=client)
            complete_job(job_path, url)
            try:
                lock_path.unlink()
            except OSError:
                pass
            print('Published:', url, flush=True)
            return url
        except (UploadError, OSError, ValueError, json.JSONDecodeError, tarfile.TarError) as error:
            remember_error(job_path, error)
            raise UploadError(str(error)) from error
    finally:
        lock.close()


def state_directory():
    return Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'rosbag-upload'


def watch(checkout, interval=60):
    checkout = Path(checkout).expanduser().resolve(strict=True)
    root = checkout / '.rosbag-upload'
    pending = root / 'pending'
    pending.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (root / '.watch.lock').open('a')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UploadError('Another uploader is already watching this checkout.') from error
        print(f'Watching {pending} for completed recordings.', flush=True)
        while True:
            for job_path in sorted(pending.glob('*.json')):
                try:
                    run_job(job_path, checkout=checkout)
                except UploadError as error:
                    print(f'{job_path.name}: {error}', file=sys.stderr, flush=True)
            time.sleep(interval)
    finally:
        lock.close()


def systemd_quote(value):
    value = str(value)
    if '\n' in value or '\r' in value:
        raise UploadError('Paths containing newlines are not supported by the service.')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def install_recorder_integration(checkout):
    checkout = Path(checkout)
    source = checkout / 'ros2_ws/src/vehicle_ui_hub/src/bag_recorder.py'
    module = source.with_name('github_upload_queue.py')
    cmake = checkout / 'ros2_ws/src/vehicle_ui_hub/CMakeLists.txt'

    def installed():
        return (module.is_file() and cmake.is_file() and
                'from github_upload_queue import queue_recording' in source.read_text(encoding='utf-8') and
                'queue_recording(self._bag_path)' in source.read_text(encoding='utf-8') and
                'src/github_upload_queue.py' in cmake.read_text(encoding='utf-8'))

    if installed():
        return
    patch = Path(__file__).resolve().parent / 'integration/1811-recording-upload.patch'
    if not patch.is_file() or not shutil.which('git'):
        raise UploadError('The recorder integration patch or Git is missing from this installation.')
    for arguments in (['git', '-C', str(checkout), 'apply', '--check', str(patch)],
                      ['git', '-C', str(checkout), 'apply', str(patch)]):
        result = subprocess.run(arguments, text=True, capture_output=True, check=False)
        if result.returncode:
            raise UploadError('Could not safely apply the 1811 recorder integration; existing files were not '
                              'overwritten. Check the checkout version and local changes. ' + result.stderr.strip()[-500:])
    if not installed():
        raise UploadError('The recorder integration could not be verified after applying the patch.')
    print('Added the recorder integration to the 1811 checkout. Restart ./g to rebuild it.')


def setup_automatic(checkout):
    checkout = Path(checkout).expanduser().resolve(strict=True)
    if not (checkout / 'ros2_ws/src/vehicle_ui_hub/src/bag_recorder.py').is_file():
        raise UploadError('That path is not the 1811 checkout on Karbon.')
    if not shutil.which('systemctl'):
        raise UploadError('systemctl is required for automatic background uploads.')
    client = GitHub(token_from_login(checkout, allow_environment=False))
    info = client.request('GET', client.repo_path())
    if not info.get('permissions', {}).get('push'):
        raise UploadError('The GitHub login does not have write access to ' + REPOSITORY + '.')

    install_recorder_integration(checkout)

    root = checkout / '.rosbag-upload'
    (root / 'pending').mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / 'done').mkdir(parents=True, exist_ok=True, mode=0o700)
    service_suffix = hashlib.sha256(os.fsencode(checkout)).hexdigest()[:12]
    service = f'rosbag-upload-{service_suffix}.service'
    config_home = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
    service_path = config_home / 'systemd/user' / service
    service_path.parent.mkdir(parents=True, exist_ok=True)
    system_python = Path('/usr/bin/python3') if Path('/usr/bin/python3').is_file() else Path(sys.executable)
    command = ' '.join(map(systemd_quote, [system_python, Path(__file__).resolve(), 'watch', '--checkout', checkout]))
    unit = ('[Unit]\nDescription=Upload completed ROS bags to GitHub\nAfter=network-online.target\n\n'
            '[Service]\nType=simple\nExecStart=' + command + '\nNice=10\nIOSchedulingClass=idle\n'
            'Restart=on-failure\nRestartSec=10\n\n'
            '[Install]\nWantedBy=default.target\n')
    if service_path.exists() and service_path.read_text(encoding='utf-8') != unit:
        raise UploadError(f'Existing service differs; inspect it before rerunning setup: {service_path}')
    if not service_path.exists():
        service_path.write_text(unit, encoding='utf-8')
    for arguments in (['systemctl', '--user', 'daemon-reload'],
                      ['systemctl', '--user', 'enable', '--now', service]):
        result = subprocess.run(arguments, text=True, capture_output=True, check=False)
        if result.returncode:
            raise UploadError('Could not start the user service: ' + result.stderr.strip()[-500:])
    atomic_json(root / 'enabled', {'version': 1, 'repository': REPOSITORY, 'service': service})
    print('Automatic public uploads enabled for:', checkout)
    print('Service:', service)
    print('Upload log: journalctl --user -u ' + service + ' -f')


def show_status(checkout=None):
    roots = [('Manual uploads', state_directory())]
    if checkout:
        roots.insert(0, ('Automatic uploads', Path(checkout).expanduser().resolve() / '.rosbag-upload'))
    for label, root in roots:
        print(label + ':')
        found = False
        for status in ('pending', 'done'):
            for path in sorted((root / status).glob('*.json')):
                found = True
                try:
                    job = read_json(path)
                    detail = job.get('completed_url') or job.get('last_error') or 'waiting'
                    print(f'  {status}: {job.get("id", path.stem)}  {job.get("source", "")}  {detail}')
                except (OSError, ValueError):
                    print(f'  {status}: unreadable job {path}')
        if not found:
            print('  No uploads yet.')


def public_asset_bytes(url, limit):
    request = urllib.request.Request(url, headers={'User-Agent': 'rosbag-uploader'})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = response.read(limit + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise UploadError(f'Download failed: {type(error).__name__}.') from error
    if len(data) > limit:
        raise UploadError('The manifest is unexpectedly large.')
    return data


def download_part(asset, part, path):
    path = Path(path)
    if path.is_file() and path.stat().st_size == part['size']:
        with path.open('rb') as stream:
            existing_hash = hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hash_stream(stream)
        if existing_hash == part['sha256']:
            print('Already downloaded:', part['name'], flush=True)
            return
    temporary = path.with_name(path.name + '.tmp')
    request = urllib.request.Request(asset['browser_download_url'], headers={'User-Agent': 'rosbag-uploader'})
    digest, size = hashlib.sha256(), 0
    print('Downloading:', part['name'], flush=True)
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open('wb') as stream:
            while True:
                data = response.read(BUFFER_BYTES)
                if not data:
                    break
                size += len(data)
                if size > part['size']:
                    raise UploadError(f'The downloaded part is too large: {part["name"]}')
                stream.write(data)
                digest.update(data)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise UploadError(f'Download interrupted for {part["name"]}: {type(error).__name__}.') from error
    if size != part['size'] or digest.hexdigest() != part['sha256']:
        raise UploadError(f'Checksum mismatch for {part["name"]}.')
    os.replace(temporary, path)


def hash_stream(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(BUFFER_BYTES), b''):
        digest.update(block)
    return digest.hexdigest()


class PartReader:
    def __init__(self, paths):
        self.paths = iter(paths)
        self.current = None

    def read(self, size):
        chunks = []
        while size > 0:
            if self.current is None:
                try:
                    self.current = next(self.paths).open('rb')
                except StopIteration:
                    break
            chunk = self.current.read(size)
            if not chunk:
                self.current.close()
                self.current = None
                continue
            chunks.append(chunk)
            size -= len(chunk)
        return b''.join(chunks)

    def close(self):
        if self.current is not None:
            self.current.close()
            self.current = None


def extract_archive(parts, output, expected_name):
    reader = PartReader(parts)
    directories = []
    try:
        with tarfile.open(fileobj=reader, mode='r|') as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if (path.is_absolute() or not path.parts or path.parts[0] != expected_name or
                        any(part in ('.', '..') for part in path.parts) or
                        not (member.isfile() or member.isdir())):
                    raise UploadError(f'Unsafe archive entry: {member.name}')
                target = output.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    directories.append((target, member.mode & 0o777, member.mtime))
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, target.open('xb') as destination:
                        shutil.copyfileobj(source, destination, length=BUFFER_BYTES)
                    os.chmod(target, member.mode & 0o777)
                    os.utime(target, (member.mtime, member.mtime))
        for target, mode, mtime in reversed(directories):
            os.chmod(target, mode)
            os.utime(target, (mtime, mtime))
    finally:
        reader.close()


def download_release(tag, destination):
    if not re.fullmatch(r'data-[0-9a-f]{32}', tag):
        raise UploadError('Use the data-... tag shown on the GitHub release.')
    client = GitHub()
    release = client.get_release_by_tag(tag)
    assets = {asset['name']: asset for asset in client.list_assets(release['id'])}
    metadata = assets.get('manifest.json')
    if not metadata:
        raise UploadError('This release has no manifest.json.')
    data = public_asset_bytes(metadata['browser_download_url'], 1024 * 1024)
    if metadata.get('digest') != 'sha256:' + hashlib.sha256(data).hexdigest():
        raise UploadError('The manifest checksum does not match GitHub.')
    manifest = json.loads(data)
    name = manifest.get('name')
    if not isinstance(name, str) or not name or name in ('.', '..') or '/' in name or '\\' in name:
        raise UploadError('The manifest has an invalid file or directory name.')
    parts = manifest.get('parts')
    if manifest.get('version') != 1 or not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        raise UploadError('Invalid upload manifest.')
    for index, part in enumerate(parts, 1):
        if (not isinstance(part, dict) or part.get('name') != f'upload.tar.part{index:06d}' or
                not isinstance(part.get('size'), int) or not 0 < part['size'] <= PART_BYTES or
                not re.fullmatch(r'[0-9a-f]{64}', str(part.get('sha256', ''))) or part['name'] not in assets):
            raise UploadError('Invalid or missing upload part in the manifest.')
    destination = Path(destination).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    final = destination / name
    if os.path.lexists(final):
        raise UploadError(f'Destination already exists: {final}')
    stage = destination / ('.' + tag + '.partial')
    stage.mkdir(mode=0o700, exist_ok=True)
    if stage.is_symlink() or not stage.is_dir():
        raise UploadError('Invalid download staging directory.')
    with (stage / '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UploadError('This release is already being downloaded here.') from error
        for part in parts:
            download_part(assets[part['name']], part, stage / part['name'])
        extraction = stage / 'extracted'
        if extraction.exists():
            raise UploadError(f'Previous extraction remains at {extraction}; move it before retrying.')
        extraction.mkdir()
        extract_archive([stage / part['name'] for part in parts], extraction, name)
        extracted = extraction / name
        if not extracted.exists():
            raise UploadError('The archive did not contain the expected file or directory.')
        try:
            rename_without_replacing(extracted, final)
        except PublishError as error:
            raise UploadError(str(error)) from error
        extraction.rmdir()
        for part in parts:
            (stage / part['name']).unlink()
        (stage / '.lock').unlink()
        try:
            stage.rmdir()
        except OSError:
            pass
    print('Downloaded and verified:', final)
    return final


def list_releases():
    client = GitHub()
    page = 1
    while True:
        releases = client.request('GET', client.repo_path(f'/releases?per_page=100&page={page}'))
        for release in releases:
            if release.get('tag_name', '').startswith('data-'):
                print(f'{release["tag_name"]}  {release.get("name", "")}  {release["html_url"]}')
        if len(releases) < 100:
            break
        page += 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='Upload and download public files in the ROS-Bags GitHub repository.')
    commands = parser.add_subparsers(dest='command', required=True)
    upload = commands.add_parser('upload', help='upload an existing file or folder')
    upload.add_argument('source')
    retry = commands.add_parser('retry', help='retry a manual upload by job ID')
    retry.add_argument('job_id')
    setup = commands.add_parser('setup-auto', help='enable the Karbon background uploader')
    setup.add_argument('--checkout', required=True, help='absolute path to the 1811 checkout on Karbon')
    service = commands.add_parser('watch', help='run the background recording uploader')
    service.add_argument('--checkout', required=True)
    service.add_argument('--interval', type=int, default=60)
    status = commands.add_parser('status', help='show queued and finished uploads')
    status.add_argument('--checkout')
    commands.add_parser('list', help='list public uploads')
    download = commands.add_parser('download', help='download and verify a published upload')
    download.add_argument('tag')
    download.add_argument('--destination', '-d', default='~/rosbags')
    args = parser.parse_args(argv)
    try:
        if args.command == 'upload':
            job = make_job(args.source, state_directory() / 'pending')
            print('Upload job:', job.stem)
            run_job(job)
        elif args.command == 'retry':
            if not re.fullmatch(r'[0-9a-f]{32}', args.job_id):
                raise UploadError('Invalid upload job ID.')
            run_job(state_directory() / 'pending' / (args.job_id + '.json'))
        elif args.command == 'setup-auto':
            setup_automatic(args.checkout)
        elif args.command == 'watch':
            if args.interval < 1:
                raise UploadError('The watch interval must be at least one second.')
            watch(args.checkout, args.interval)
        elif args.command == 'status':
            show_status(args.checkout)
        elif args.command == 'list':
            list_releases()
        elif args.command == 'download':
            download_release(args.tag, args.destination)
    except (UploadError, OSError, ValueError, json.JSONDecodeError, tarfile.TarError) as error:
        print('Error:', error, file=sys.stderr)
        if args.command in ('upload', 'retry'):
            print('Retry with: python3 github_upload.py retry JOB_ID', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nInterrupted; queued uploads and downloaded parts are retained.', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())

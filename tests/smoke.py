#!/usr/bin/env python3
"""Exercise a fresh disposable Grimmory instance over HTTP; never use production data."""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
import zlib


def fixture(title):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))

    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 32, 48, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress((b'\0' + b'\x20\x60\xa0' * 32) * 48)) + chunk(b'IEND', b''))
    fb2 = f'''<?xml version="1.0" encoding="UTF-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
<description><title-info><genre>sf</genre><author><first-name>Smoke</first-name><last-name>Author</last-name></author>
<book-title>{title}</book-title><lang>en</lang><coverpage><image l:href="#cover"/></coverpage></title-info>
<document-info><author><nickname>Fixture</nickname></author><date>2026-09-29</date><id>{title}</id><version>1.0</version></document-info></description>
<body><section><title><p>{title}</p></title><p>FB2 ZIP smoke reader text.</p></section></body>
<binary id="cover" content-type="image/png">{base64.b64encode(png).decode()}</binary></FictionBook>'''.encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        output.writestr('nested/book.fb2', fb2)
    return fb2, archive.getvalue()


class Client:
    def __init__(self, base):
        self.base = base.rstrip('/')
        self.token = None

    def request(self, path, method='GET', payload=None, raw=False, content_type='application/json'):
        if payload is not None and not isinstance(payload, bytes):
            payload = json.dumps(payload).encode()
        headers = {'Content-Type': content_type}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        request = urllib.request.Request(self.base + '/api/v1' + path, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                data = response.read()
                return data if raw or not data else json.loads(data)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'{method} {path}: HTTP {error.code}: {error.read().decode()}') from error

    def upload(self, path, name, data):
        boundary = uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
        return self.request(path, 'POST', body, content_type='multipart/form-data; boundary=' + boundary)


def wait_for(callback, description):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = callback()
        if result:
            return result
        time.sleep(2)
    raise AssertionError('Timed out: ' + description)


def application_logs():
    if not os.environ.get('COMPOSE_PROJECT_NAME'):
        raise ValueError('Set COMPOSE_PROJECT_NAME to the disposable smoke project')
    compose = Path(__file__).with_name('smoke-compose.yml')
    return subprocess.run(['docker', 'compose', '-f', str(compose), 'logs', '--no-color',
                           '--no-log-prefix', 'grimmory'], check=True, capture_output=True,
                          text=True, timeout=20).stdout


def wait_for_scan(previous_logs):
    def completed():
        current = application_logs()
        assert current.startswith(previous_logs), 'Application logs changed during scan'
        new = current[len(previous_logs):]
        assert not any(message in new for message in (' ERROR ', 'InvalidDataAccessApiUsageException',
                                                       'skipping duplicate')), new
        return 'Parsing task completed!' in new
    wait_for(completed, 'library scan completion in Compose logs')


def run(base):
    client = Client(base)
    assert client.request('/setup/status')['data'] is False, 'Smoke requires an unconfigured disposable instance'
    credentials = {'username': 'smoke', 'password': 'Smoke-only-password-123'}
    client.request('/setup', 'POST', {**credentials, 'email': 'smoke@example.invalid', 'name': 'Smoke'})
    client.token = client.request('/auth/login', 'POST', credentials)['accessToken']
    before_scan = application_logs()
    library = client.request('/libraries', 'POST', {
        'name': 'Smoke', 'paths': [{'path': '/books'}], 'watch': True,
        'metadataSource': 'EMBEDDED', 'allowedFormats': ['FB2'],
    })
    wait_for_scan(before_scan)
    library_id = library['id']
    path_id = library['paths'][0]['id']
    books_path = f'/libraries/{library_id}/book'
    fb2, archive = fixture('Archive Smoke')
    client.upload(f'/files/upload?libraryId={library_id}&pathId={path_id}', 'Книга.FB2.ZIP', archive)
    book = wait_for(lambda: next(iter(client.request(books_path)), None), 'uploaded book')
    book_id = book['id']
    book = client.request(f'/books/{book_id}')
    assert book['metadata']['title'] == 'Archive Smoke', book
    assert book['primaryFile']['fileName'].endswith('.fb2'), book
    assert client.request(f'/books/{book_id}/download', raw=True) == fb2
    assert client.request(f'/media/book/{book_id}/cover', raw=True), 'Missing embedded cover'
    print(f'Library upload, metadata, cover and byte-exact download passed: book {book_id}', flush=True)

    client.request(f'/libraries/{library_id}/file-naming-pattern', 'PATCH', {'fileNamingPattern': 'Renamed/{title}'})
    client.request('/files/move', 'POST', {'moves': [
        {'bookId': book_id, 'targetLibraryId': library_id, 'targetLibraryPathId': path_id}]})
    renamed = client.request(f'/books/{book_id}')['primaryFile']
    assert renamed['fileName'] == 'Archive Smoke.fb2' and renamed['fileSubPath'] == 'Renamed', renamed
    before_scan = application_logs()
    client.request(f'/libraries/{library_id}/refresh', 'PUT')
    wait_for_scan(before_scan)
    assert [item['id'] for item in client.request(books_path)] == [book_id], 'Rescan duplicated the book'
    assert client.request(f'/books/{book_id}/download', raw=True) == fb2
    print('Rename and rescan without duplicates passed', flush=True)

    drop_fb2, drop_zip = fixture('BookDrop Smoke')
    client.upload('/files/upload/bookdrop', 'BookDrop.fb2.zip', drop_zip)
    dropped = wait_for(lambda: next((item for item in client.request('/bookdrop/files')['content']
                                   if (item.get('originalMetadata') or {}).get('title') == 'BookDrop Smoke'), None),
                       'BookDrop metadata')
    assert dropped['fileName'].endswith('.fb2'), dropped
    result = client.request('/bookdrop/imports/finalize', 'POST', {
        'selectAll': False, 'files': [{'fileId': dropped['id'], 'libraryId': library_id,
                                     'pathId': path_id, 'metadata': dropped['originalMetadata']}],
        'defaultLibraryId': library_id, 'defaultPathId': path_id,
    })
    assert result['successfullyImported'] == 1 and result['failed'] == 0, result
    drop_book = wait_for(lambda: next((item for item in client.request(books_path)
                                      if item.get('metadata', {}).get('title') == 'BookDrop Smoke'), None), 'BookDrop import')
    assert client.request(f'/books/{drop_book["id"]}/download', raw=True) == drop_fb2, result
    print('BookDrop upload, finalize and byte-exact download passed', flush=True)

    client.request(f'/libraries/{library_id}/file-naming-pattern', 'PATCH', {'fileNamingPattern': '{currentFilename}'})
    physical = client.request('/books/physical', 'POST', {'libraryId': library_id, 'title': 'Physical Smoke'})
    physical_fb2, physical_zip = fixture('Physical Smoke')
    added = client.upload(f'/books/{physical["id"]}/files?isBook=false', 'Physical.fb2.zip', physical_zip)
    assert added['bookType'] == 'FB2' and added['fileName'].endswith('.fb2'), added
    assert client.request(f'/books/{physical["id"]}/download', raw=True) == physical_fb2
    alt_fb2, alt_zip = fixture('Alternative Smoke')
    alternative = client.upload(f'/books/{book_id}/files?isBook=true', 'Alternative.fb2.zip', alt_zip)
    assert alternative['bookType'] == 'FB2' and alternative['fileName'].endswith('.fb2'), alternative
    assert client.request(f'/books/{book_id}/files/{alternative["id"]}/download', raw=True) == alt_fb2
    attached = client.upload(f'/books/{book_id}/files?isBook=false', 'Attachment.fb2.zip', archive)
    assert client.request(f'/books/{book_id}/files/{attached["id"]}/download', raw=True) == archive
    print('Physical first file, alternative format and unchanged ZIP attachment passed', flush=True)
    print(f'Smoke passed. Reader fixture: {base}/ebook-reader/book/{book_id}; disposable login: smoke / Smoke-only-password-123', flush=True)


if __name__ == '__main__':
    run(sys.argv[1])

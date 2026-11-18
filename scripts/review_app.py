#!/usr/bin/env python3
"""Loopback-only, model-blind reference review with private durable revisions."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlsplit

APP_ID = 'reference-review/v1'
DB_ID = 0x53545452
ASSETS = Path(__file__).resolve().parents[1] / 'review_ui'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


class ReviewError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Dataset:
    def __init__(self, manifest, expected_sha, audio_root, expected_count=300):
        self.fingerprint = sha256(manifest)
        if self.fingerprint != expected_sha:
            raise ValueError('Manifest fingerprint mismatch')
        data = json.loads(Path(manifest).read_text())
        if data.get('schema') != 'corpus-manifest/v2' or not data['summary']['run_ready_audio_hashes']:
            raise ValueError('Sealed source manifest required')
        root = Path(audio_root).resolve()
        self.items = {}
        selected = [r for r in data['samples'] if r.get('reference_review_substudy') is True]
        if len(selected) != expected_count or not selected:
            raise ValueError('Frozen review subset count mismatch')
        seen = set()
        for number, row in enumerate(selected, 1):
            sample = row['sample_id']
            if not re.fullmatch(r'[A-Za-z0-9_-]+', sample) or sample in seen:
                raise ValueError('Invalid or duplicate source identity')
            seen.add(sample)
            path = root / (sample + '.wav')
            if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
                raise ValueError('Selected audio unavailable')
            if sha256(path) != row['audio_sha256']:
                raise ValueError('Selected audio fingerprint mismatch')
            reference = row['reference']
            if not isinstance(reference, str) or hashlib.sha256(reference.encode()).hexdigest() != row['reference_utf8_sha256']:
                raise ValueError('Reference fingerprint mismatch')
            key = f'R{number:03}'
            # Explicit projection: no source code, model names or hypothesis fields.
            self.items[key] = {'id': key, 'ordinal': number, 'reference': reference,
                               'duration': row['audio_seconds'], 'audio_sha256': row['audio_sha256'],
                               '_audio': path}

    def get(self, key):
        if key not in self.items:
            raise ReviewError(404, '검토 항목을 찾을 수 없습니다.')
        return self.items[key]


class Store:
    def __init__(self, path, dataset, reviewer):
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,39}', reviewer):
            raise ValueError('Use an opaque reviewer identifier')
        self.path, self.dataset, self.reviewer = Path(path), dataset, reviewer
        if self.path.is_symlink():
            raise ValueError('Symlink database refused')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size:
            with closing(sqlite3.connect(f'{self.path.resolve().as_uri()}?mode=ro', uri=True)) as db:
                if db.execute('PRAGMA application_id').fetchone()[0] != DB_ID or db.execute('PRAGMA user_version').fetchone()[0] != 1:
                    raise ValueError('Unrecognized database; no changes made')
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute(f'PRAGMA application_id={DB_ID}')
            db.execute('PRAGMA user_version=1')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS reviews (
                reviewer TEXT NOT NULL, item TEXT NOT NULL, version INTEGER NOT NULL,
                status TEXT NOT NULL, transcript TEXT NOT NULL, notes TEXT NOT NULL,
                heard INTEGER NOT NULL, played_seconds REAL NOT NULL, saved_at TEXT NOT NULL,
                PRIMARY KEY(reviewer,item))''')
            db.execute('''CREATE TABLE IF NOT EXISTS revisions (
                reviewer TEXT NOT NULL, item TEXT NOT NULL, version INTEGER NOT NULL,
                status TEXT NOT NULL, transcript TEXT NOT NULL, notes TEXT NOT NULL,
                heard INTEGER NOT NULL, played_seconds REAL NOT NULL, saved_at TEXT NOT NULL,
                PRIMARY KEY(reviewer,item,version))''')
            existing = db.execute("SELECT value FROM metadata WHERE key='manifest_sha256'").fetchone()
            if existing and existing[0] != dataset.fingerprint:
                raise ValueError('Database belongs to a different frozen dataset')
            db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)', ('manifest_sha256', dataset.fingerprint))
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, key):
        self.dataset.get(key)
        with self.connect() as db:
            row = db.execute('SELECT * FROM reviews WHERE reviewer=? AND item=?', (self.reviewer, key)).fetchone()
        if not row:
            return None
        result = dict(row)
        result.pop('reviewer'); result.pop('item')
        result['heard'] = bool(result['heard'])
        return result

    def summary(self):
        with self.connect() as db:
            records = {r['item']: dict(r) for r in db.execute('SELECT item,status,version FROM reviews WHERE reviewer=?', (self.reviewer,))}
        queue = [{'id': key, 'status': records.get(key, {}).get('status', 'unreviewed'),
                  'version': records.get(key, {}).get('version', 0)} for key in self.dataset.items]
        return {'reviewer': self.reviewer, 'total': len(queue),
                'reviewed': sum(r['status'] == 'reviewed' for r in queue), 'items': queue}

    def save(self, key, value):
        self.dataset.get(key)
        fields = {'version', 'status', 'transcript', 'notes', 'heard', 'played_seconds'}
        if not isinstance(value, dict) or set(value) != fields:
            raise ReviewError(400, '저장 요청 형식이 올바르지 않습니다.')
        if type(value['version']) is not int or value['version'] < 0:
            raise ReviewError(400, '저장 버전이 올바르지 않습니다.')
        status, text, notes = value['status'], value['transcript'], value['notes']
        seconds = value['played_seconds']
        if status not in ('draft', 'reviewed', 'flagged') or not isinstance(text, str) or not isinstance(notes, str):
            raise ReviewError(400, '검토 내용 형식이 올바르지 않습니다.')
        if len(text) > 16000 or len(notes) > 2000 or '\0' in text + notes:
            raise ReviewError(400, '입력 길이를 확인해 주세요.')
        if type(value['heard']) is not bool or type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 86400:
            raise ReviewError(400, '청취 기록이 올바르지 않습니다.')
        if status == 'reviewed' and (not text.strip() or not value['heard'] or seconds < .05):
            raise ReviewError(400, '음성을 확인하고 정답을 입력한 뒤 완료해 주세요.')
        if status == 'flagged' and not notes.strip():
            raise ReviewError(400, '보류 사유를 메모에 남겨 주세요.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT version FROM reviews WHERE reviewer=? AND item=?', (self.reviewer, key)).fetchone()
            version = current[0] if current else 0
            if value['version'] != version:
                raise ReviewError(409, '다른 창에서 변경됐습니다. 입력 내용을 보존한 뒤 저장된 내용을 다시 불러오세요.')
            payload = (self.reviewer, key, version + 1, status, text, notes, int(value['heard']), seconds, datetime.now(timezone.utc).isoformat())
            db.execute('INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?)', payload)
            db.execute('INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?,?,?,?,?)', payload)
        saved = {'version': payload[2], 'status': status, 'transcript': text, 'notes': notes,
                 'heard': value['heard'], 'played_seconds': seconds, 'saved_at': payload[-1]}
        return {'review': saved, 'summary': self.summary()}

    def export(self):
        with self.connect() as db:
            db.execute('BEGIN')
            history = [dict(r) for r in db.execute('SELECT * FROM revisions WHERE reviewer=? ORDER BY item,version', (self.reviewer,))]
            records = {r['item']: dict(r) for r in db.execute('SELECT * FROM reviews WHERE reviewer=?', (self.reviewer,))}
        rows = []
        for key, item in self.dataset.items.items():
            record = records.get(key)
            if record:
                record.pop('item'); record.pop('reviewer'); record['heard'] = bool(record['heard'])
            rows.append({'id': key, 'audio_sha256': item['audio_sha256'], 'provided_reference': item['reference'], 'review': record})
        return {'schema': 'reference-review-export/v1', 'manifest_sha256': self.dataset.fingerprint,
                'reviewer': self.reviewer, 'items': rows, 'revisions': history}


def byte_range(header, size):
    if not header:
        return 0, size - 1, 200
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or not any(match.groups()):
        raise ReviewError(416, '지원하지 않는 음성 범위입니다.')
    first, last = match.groups()
    if not first:
        count = int(last)
        if count < 1:
            raise ReviewError(416, '음성 범위를 확인해 주세요.')
        start, end = max(0, size - count), size - 1
    else:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise ReviewError(416, '음성 범위를 확인해 주세요.')
    return start, end, 206


def make_server(dataset, store, port, allowed_host):
    token = secrets.token_urlsafe(32)
    hosts = {allowed_host, '127.0.0.1', 'localhost'}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass  # Never log source text, request bodies or filesystem paths.

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def headers_common(self):
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

        def boundary(self, mutation=False):
            host = self.headers.get('Host', '')
            if urlsplit('http://' + host).hostname not in hosts or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise ReviewError(403, '로컬 검토 화면에서만 접근할 수 있습니다.')
            if mutation:
                origin = self.headers.get('Origin')
                if origin and (urlsplit(origin).netloc != host or urlsplit(origin).scheme not in ('http', 'https')):
                    raise ReviewError(403, '검토 화면의 출처를 확인해 주세요.')
                if not secrets.compare_digest(self.headers.get('X-Review-Token', ''), token):
                    raise ReviewError(403, '검토 화면을 새로 열어 주세요.')

        def send_bytes(self, data, mime='application/json; charset=utf-8', status=200, head=False, extra=None):
            self.send_response(status); self.headers_common()
            self.send_header('Content-Type', mime); self.send_header('Content-Length', str(len(data)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def json(self, value, status=200, head=False):
            self.send_bytes(json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), status=status, head=head)

        def do_HEAD(self):
            self.do_GET(head=True)

        def do_GET(self, head=False):
            try:
                self.boundary()
                route = urlsplit(self.path).path
                if route == '/api/session':
                    self.json({'app': APP_ID, 'manifest_sha256': dataset.fingerprint, 'csrf_token': token, **store.summary()}, head=head)
                elif route == '/api/export':
                    self.send_bytes(json.dumps(store.export(), ensure_ascii=False, indent=2).encode(), head=head,
                        extra={'Content-Disposition': f'attachment; filename="reference-review-{store.reviewer}.json"'})
                elif (match := re.fullmatch(r'/api/items/(R\d{3})', route)):
                    item = dataset.get(match[1])
                    self.json({**{k: item[k] for k in ('id', 'ordinal', 'reference', 'duration')},
                               'audio_url': '/audio/' + item['id'], 'review': store.record(item['id'])}, head=head)
                elif (match := re.fullmatch(r'/audio/(R\d{3})', route)):
                    item = dataset.get(match[1]); path = item['_audio']; size = path.stat().st_size
                    if sha256(path) != item['audio_sha256']:
                        raise ReviewError(409, '원음이 변경되었습니다. 검토를 중단하고 자료를 확인해 주세요.')
                    try:
                        start, end, status = byte_range(self.headers.get('Range'), size)
                    except ReviewError:
                        self.send_bytes(b'', status=416, head=head, extra={'Content-Range': f'bytes */{size}'})
                        return
                    self.send_response(status); self.headers_common(); self.send_header('Content-Type', 'audio/wav')
                    self.send_header('Content-Length', str(end - start + 1)); self.send_header('Accept-Ranges', 'bytes')
                    if status == 206:
                        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                    self.end_headers()
                    if not head:
                        with path.open('rb') as stream:
                            stream.seek(start); remaining = end - start + 1
                            while remaining:
                                chunk = stream.read(min(65536, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk); remaining -= len(chunk)
                else:
                    assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/assets/review.css': ('review.css', 'text/css; charset=utf-8'), '/assets/review.js': ('review.js', 'text/javascript; charset=utf-8')}
                    if route not in assets:
                        raise ReviewError(404, '페이지를 찾을 수 없습니다.')
                    name, mime = assets[route]; self.send_bytes((ASSETS / name).read_bytes(), mime, head=head)
            except ReviewError as error:
                self.json({'error': error.message}, error.status, head)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.json({'error': '자료를 불러오지 못했습니다. 로컬 서버 상태를 확인해 주세요.'}, 500, head)

        def do_POST(self):
            try:
                self.boundary(mutation=True)
                match = re.fullmatch(r'/api/items/(R\d{3})', urlsplit(self.path).path)
                if not match:
                    raise ReviewError(404, '저장 경로를 찾을 수 없습니다.')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ReviewError(400, '저장 요청 형식을 확인해 주세요.')
                try:
                    value = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
                    raise ReviewError(400, '저장 내용을 읽지 못했습니다.') from None
                self.json(store.save(match[1], value))
            except ReviewError as error:
                self.close_connection = True
                self.json({'error': error.message}, error.status)
            except Exception:
                self.json({'error': '저장하지 못했습니다. 입력 내용은 유지됩니다.'}, 500)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        def get_request(self):
            sock, address = super().get_request(); sock.settimeout(30)
            return sock, address
    return Server(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'expected-manifest-sha256', 'audio-root', 'db'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--reviewer', default='reviewer-1')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--allowed-host', default='stt-review.localhost')
    parser.add_argument('--expected-count', type=int, default=300)
    args = parser.parse_args(); os.umask(0o077)
    if not re.fullmatch(r'[a-z0-9-]+\.localhost', args.allowed_host):
        parser.error('Use an isolated *.localhost hostname')
    dataset = Dataset(args.manifest, args.expected_manifest_sha256, args.audio_root, args.expected_count)
    store = Store(args.db, dataset, args.reviewer)
    with make_server(dataset, store, args.port, args.allowed_host) as server:
        print(f'검토 도구: http://{args.allowed_host}:{server.server_port}/ · {len(dataset.items)}개 · {args.reviewer}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('검토 서버를 종료했습니다. 저장 내용은 유지됩니다.', flush=True)


if __name__ == '__main__':
    main()

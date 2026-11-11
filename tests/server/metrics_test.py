"""Private native metrics must observe the actual engines and durable operations."""
import http.client
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid

SERVER = sys.argv[1]
del sys.argv[1]


class ServerMetrics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('GLAMDB_TEST_TMPDIR', '/tmp'))
        self.addCleanup(self.tmp.cleanup)
        self.directory = str(Path(self.tmp.name).resolve() / 'service')
        self.proc = None
        self.addCleanup(self.stop)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.metrics_port = listener.getsockname()[1]

    def start(self, metrics=True):
        options = ['--metrics-listen-address', f'127.0.0.1:{self.metrics_port}'] if metrics else []
        self.proc = subprocess.Popen([SERVER, '--data-dir', self.directory, '--port', '0', *options],
            env=dict(os.environ, GLAMDB_TOKEN='metrics-test'), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.assertTrue(select.select([self.proc.stdout], [], [], 15)[0])
        line = self.proc.stdout.readline()
        self.assertTrue(line, self.proc.stderr.read() if self.proc.poll() is not None else 'no server output')
        self.port = json.loads(line)['port']

    def stop(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
            out, err = self.proc.communicate(timeout=30)
            code = self.proc.returncode
            self.proc = None
            self.assertEqual(code, 0, out + err)

    def api(self, path, body=None, status=200):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        connection.request('GET' if body is None else 'POST', path,
            None if body is None else json.dumps(body),
            {'Authorization': 'Bearer metrics-test', 'Content-Type': 'application/json'})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        self.assertEqual(response.status, status, data)
        return json.loads(data)

    def scrape(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.metrics_port, timeout=5)
        connection.request('GET', '/metrics')
        response = connection.getresponse()
        data = response.read().decode()
        connection.close()
        self.assertEqual(response.status, 200, data)
        return data

    def value(self, name, **labels):
        matches = [line.rsplit(' ', 1)[1] for line in self.scrape().splitlines()
            if line.startswith(name + '{') and all(f'{key}="{value}"' in line for key, value in labels.items())]
        self.assertEqual(len(matches), 1, matches)
        return float(matches[0])

    def ready(self):
        return self.value('glamdb_server_ready', service_instance='core')

    def collection(self, base, name):
        return self.api(base + '/collections', {'collection_id': str(uuid.uuid4()), 'name': name,
            'schema': {'fields': [{'name': 'key', 'type': 'string'}]}})

    def test_default_does_not_publish_metrics_on_http(self):
        self.start(metrics=False)
        self.api('/metrics', status=404)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', self.metrics_port))

    def test_private_listener_tracks_real_operations_health_restart_and_shutdown(self):
        self.start()
        self.api('/metrics', status=404)
        self.assertEqual(self.ready(), 0)
        database = self.api('/databases', {'name': 'metrics-private-name'})
        base = '/databases/' + database['database_id']
        self.assertEqual(self.ready(), 1)
        collection = self.collection(base, 'metrics-private-collection')
        append = {'operation_id': str(uuid.uuid4()), 'rows': [{'key': 'first'}, {'key': 'second'}]}
        self.api(base + '/collections/' + collection['collection_id'] + '/append', append)
        self.assertEqual(self.value('glamdb_operations_total', service_instance='core',
            operation='create_collection', outcome='committed'), 1)
        self.assertEqual(self.value('glamdb_operations_total', service_instance='core',
            operation='append', outcome='committed'), 1)
        for private in ('metrics-private-name', 'metrics-private-collection', database['database_id'],
                        collection['collection_id'], append['operation_id'], self.directory):
            self.assertNotIn(private, self.scrape())
        self.api(base + '/close', {})
        self.assertEqual(self.ready(), 0)
        self.api(base + '/open', {})
        self.assertEqual(self.ready(), 1)
        self.stop()
        with self.assertRaises(OSError):
            self.scrape()
        self.start()
        self.assertEqual(self.ready(), 1)
        self.assertEqual(len(self.api(base + '/collections')['items']), 1)

    def test_one_failed_database_closes_service_readiness(self):
        self.start()
        self.api('/databases', {'name': 'healthy'})
        database = self.api('/databases', {'name': 'will-fail-open'})
        base = '/databases/' + database['database_id']
        self.collection(base, 'one')
        self.collection(base, 'two')
        self.assertEqual(self.ready(), 1)
        self.api(base + '/close', {})
        self.assertEqual(self.ready(), 1)
        self.api(base + '/open', {'limits': {'max_collections': 1}}, status=400)
        self.assertEqual(self.api(base)['state'], 'error')
        self.assertEqual(self.ready(), 0)
        self.api(base + '/close', {})
        self.api(base + '/open', {'limits': {'max_collections': 2}})
        self.assertEqual(self.ready(), 1)


if __name__ == '__main__':
    unittest.main()

"""Real migration metadata and independent worker-process metric regressions."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.db import engine


@pytest.mark.parametrize('recorded', [None, '0001', 'future_unknown_revision'])
def test_ready_rejects_missing_or_mismatched_revision(client, recorded):
    with engine.begin() as connection:
        original = connection.execute(text('SELECT version_num FROM alembic_version')).scalars().all()
        connection.execute(text('DELETE FROM alembic_version'))
        if recorded:
            connection.execute(text('INSERT INTO alembic_version (version_num) VALUES (:version)'), {'version': recorded})
    try:
        response = client.get('/readyz')
        assert response.status_code == 503, response.text
        assert response.json()['db'] and response.json()['redis']
        assert response.json()['migrations_current'] is False
        assert client.get('/livez').status_code == 200
    finally:
        with engine.begin() as connection:
            connection.execute(text('DELETE FROM alembic_version'))
            for value in original:
                connection.execute(text('INSERT INTO alembic_version (version_num) VALUES (:version)'), {'version': value})
    assert client.get('/readyz').status_code == 200


def test_metric_scrape_aggregates_exited_and_restarted_worker_processes(tmp_path):
    env = {**os.environ, 'TL_SECRETS_FILE': '/dev/null', 'PROMETHEUS_MULTIPROC_DIR': str(tmp_path),
           'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    script = '''from app.core.observability import HTTP_REQ, HTTP_LAT
import sys
n = int(sys.argv[1])
HTTP_REQ.labels("TEST", "/synthetic-multiprocess", "200").inc(n)
for _ in range(n): HTTP_LAT.labels("TEST", "/synthetic-multiprocess").observe(0.2)
'''
    for count in (2, 3):
        subprocess.run([sys.executable, '-c', script, str(count)], env=env, check=True, capture_output=True, text=True, timeout=30)
    scrape = '''from app.core.observability import metrics_payload
from prometheus_client.parser import text_string_to_metric_families
import json
body = metrics_payload()[0].decode()
print(json.dumps({s.name: s.value for m in text_string_to_metric_families(body) for s in m.samples if s.labels.get("route") == "/synthetic-multiprocess" and s.name.endswith(("_total", "_count", "_sum"))}))
'''
    def values():
        result = subprocess.run([sys.executable, '-c', scrape], env=env, check=True, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout)
    v = values()
    assert v.get('tl_http_requests_total') == 5
    assert v.get('tl_http_request_seconds_count') == 5
    assert v.get('tl_http_request_seconds_sum') == pytest.approx(1.0)
    subprocess.run([sys.executable, '-c', script, '2'], env=env, check=True, capture_output=True, text=True, timeout=30)
    v = values()
    assert v.get('tl_http_requests_total') == 7
    assert v.get('tl_http_request_seconds_count') == 7


def test_production_launcher_starts_two_workers_and_cleans_metrics_directory(tmp_path):
    import re
    import socket
    import time

    import httpx

    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'TL_SECRETS_FILE': '/dev/null', 'TL_HTTP_HOST': '127.0.0.1', 'TL_HTTP_PORT': str(port),
           'TL_HTTP_WORKERS': '2', 'TMPDIR': str(tmp_path), 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    log = tmp_path / 'server.log'
    with log.open('w') as output:
        process = subprocess.Popen([sys.executable, '-m', 'app.server'], env=env, stdout=output, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert process.poll() is None, log.read_text()
            if len(set(re.findall(r'Started server process \[(\d+)\]', log.read_text()))) >= 2:
                try:
                    if httpx.get(f'http://127.0.0.1:{port}/readyz', timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
            time.sleep(.1)
        else:
            pytest.fail('Two healthy HTTP workers did not start: ' + log.read_text()[-3000:])
        assert len(list(tmp_path.glob('tuoyu-http-metrics-*'))) == 1
        for _ in range(20):
            assert httpx.get(f'http://127.0.0.1:{port}/livez', timeout=2).status_code == 200
        from prometheus_client.parser import text_string_to_metric_families
        payload = httpx.get(f'http://127.0.0.1:{port}/metrics', timeout=2).text
        counts = [s.value for m in text_string_to_metric_families(payload) for s in m.samples
                  if s.name == 'tl_http_requests_total' and s.labels.get('route') == '/livez']
        assert sum(counts) == 20
        assert payload.count('\ntl_db_up 1.0\n') == 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            pytest.fail('HTTP master did not shut down cleanly')
    assert not list(tmp_path.glob('tuoyu-http-metrics-*'))

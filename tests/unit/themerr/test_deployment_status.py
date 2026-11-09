"""Verify published deployment timestamps, persisted caching, and failure behavior."""

# standard imports
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock

# lib imports
import pytest
import requests

# local imports
from themerr import deployment_status, storage

PUBLICATION = '2026-10-02T12:00:00Z'
DEPLOYMENT_URL = 'https://themerr.lizardbyte.dev/ThemerrDB/deployment.json'
REPOSITORY_URL = 'https://github.com/Themerr/ThemerrDB'


def response(payload=None, status=200):
    value = Mock(status_code=status)
    value.json.return_value = payload if payload is not None else {
        'deployed_at': PUBLICATION,
        'schemaVersion': 1,
        'label': 'last deployment',
        'message': '2026-10-02',
        'color': 'brightgreen',
    }
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=False)
    return value


def test_hourly_limit_survives_restart_and_server_scopes(configured, monkeypatch):
    clock = Mock(return_value=10000)
    get = Mock(return_value=response())
    monkeypatch.setattr(deployment_status.time, 'time', clock)
    monkeypatch.setattr(deployment_status.requests, 'get', get)
    first = deployment_status.publication_status()
    assert first == {
        'updated_at': '2026-10-02T12:00:00+00:00',
        'url': REPOSITORY_URL,
        'checked_at': 10000,
        'next_check': 13600,
        'stale': False,
    }
    get.assert_called_once_with(
        DEPLOYMENT_URL,
        headers={
            'Accept': 'application/json',
            'Cache-Control': 'no-cache',
        },
        timeout=10, allow_redirects=False,
    )
    storage.close()
    clock.return_value = 13599
    with storage.server_scope('another-server'):
        assert deployment_status.publication_status() == first
    get.assert_called_once()
    clock.return_value = 13600
    assert deployment_status.publication_status()['next_check'] == 17200
    assert get.call_count == 2


@pytest.mark.parametrize('failure', [
    requests.ConnectionError(),
    requests.Timeout(),
    requests.HTTPError(),
    ValueError('Invalid JSON'),
    TypeError('Invalid payload'),
    KeyError('deployed_at'),
])
def test_failed_check_preserves_publication_and_does_not_retry_early(configured, monkeypatch, failure):
    clock = Mock(return_value=10000)
    get = Mock(return_value=response())
    monkeypatch.setattr(deployment_status.time, 'time', clock)
    monkeypatch.setattr(deployment_status.requests, 'get', get)
    first = deployment_status.publication_status()
    clock.return_value = 13600
    get.side_effect = failure
    failed = deployment_status.publication_status()
    assert failed['stale'] is True
    assert failed['updated_at'] == first['updated_at']
    assert failed['url'] == first['url']
    assert failed['next_check'] == 17200
    storage.close()
    clock.return_value = 17199
    assert deployment_status.publication_status() == failed
    assert get.call_count == 2
    get.side_effect = None
    clock.return_value = 17200
    assert deployment_status.publication_status()['stale'] is False
    assert get.call_count == 3


@pytest.mark.parametrize('payload', [
    {'deployed_at': '2026-10-02T12:00:00'},
    {'deployed_at': '2026-10-02'},
    {'deployed_at': 'invalid'},
    {'deployed_at': ''},
    {'deployed_at': None},
    {'deployed_at': 123456},
    {'deployed_at': {}},
    {'deployed_at': []},
    {},
    [],
    'invalid',
    123456,
])
def test_invalid_response_is_not_published_or_retried(configured, monkeypatch, payload):
    get = Mock(return_value=response(payload))
    monkeypatch.setattr(deployment_status.requests, 'get', get)
    failed = deployment_status.publication_status()
    assert failed['updated_at'] is None
    assert failed['stale'] is True
    assert failed['url'] == REPOSITORY_URL
    assert deployment_status.publication_status() == failed
    get.assert_called_once()


@pytest.mark.parametrize('status', [
    204,
    302,
    404,
    500,
])
def test_unsuccessful_response_is_not_published_or_retried(configured, monkeypatch, status):
    get = Mock(return_value=response(status=status))
    monkeypatch.setattr(deployment_status.requests, 'get', get)
    failed = deployment_status.publication_status()
    assert failed['updated_at'] is None
    assert failed['stale'] is True
    assert failed['url'] == REPOSITORY_URL
    assert deployment_status.publication_status() == failed
    get.assert_called_once()


@pytest.mark.parametrize('deployed_at', [
    PUBLICATION,
    '2026-10-02T12:00:00+00:00',
    '2026-10-02T17:30:00+05:30',
    '2026-10-02T08:00:00-04:00',
])
def test_deployment_time_is_normalized_to_utc(configured, monkeypatch, deployed_at):
    get = Mock(return_value=response({'deployed_at': deployed_at}))
    monkeypatch.setattr(deployment_status.requests, 'get', get)
    result = deployment_status.publication_status()
    assert result['updated_at'] == '2026-10-02T12:00:00+00:00'
    assert result['stale'] is False


def test_concurrent_page_requests_share_one_check(configured, monkeypatch):
    started, release = Event(), Event()

    def request(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return response()

    get = Mock(side_effect=request)
    monkeypatch.setattr(deployment_status.requests, 'get', get)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(deployment_status.publication_status)
        assert started.wait(10)
        second = executor.submit(deployment_status.publication_status)
        release.set()
        assert first.result(timeout=10) == second.result(timeout=10)
    get.assert_called_once()


def test_interrupted_attempt_still_reserves_the_hour(configured, monkeypatch):
    get = Mock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(deployment_status.requests, 'get', get)
    with pytest.raises(KeyboardInterrupt):
        deployment_status.publication_status()
    storage.close()
    assert deployment_status.publication_status()['stale'] is True
    get.assert_called_once()


@pytest.mark.parametrize('version', [
    None,
    2,
])
@pytest.mark.parametrize('failure', [
    False,
    True,
])
def test_legacy_cache_refreshes_once_without_losing_the_last_result(configured, monkeypatch, version, failure):
    old_date = '2026-08-13T16:24:23+00:00'
    deployment_status._save({
        'version': version,
        'updated_at': old_date,
        'run_id': 999,
        'next_check': 999999,
        'stale': False,
    })
    clock = Mock(return_value=10000)
    get = Mock(side_effect=requests.Timeout()) if failure else Mock(return_value=response())
    monkeypatch.setattr(deployment_status.time, 'time', clock)
    monkeypatch.setattr(deployment_status.requests, 'get', get)
    result = deployment_status.publication_status()
    assert result['updated_at'] == (old_date if failure else '2026-10-02T12:00:00+00:00')
    assert result['stale'] is failure
    assert result['next_check'] == 13600
    assert result['url'] == REPOSITORY_URL
    assert deployment_status._load()['version'] == deployment_status._CACHE_VERSION
    if not failure:
        assert 'run_id' not in deployment_status._load()
    storage.close()
    clock.return_value = 13599
    assert deployment_status.publication_status() == result
    get.assert_called_once()


def test_deployment_cache_uses_the_test_installation_database(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(deployment_status.requests, 'get', Mock(return_value=response()))
    assert Path(storage.database_path()).parent == tmp_path
    deployment_status.publication_status()
    assert deployment_status._load()['updated_at'] == '2026-10-02T12:00:00+00:00'

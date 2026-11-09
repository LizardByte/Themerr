"""Notification decisions use fake desktop services and GitHub responses."""

import json
from unittest.mock import Mock

import pytest
import requests
from sqlalchemy.orm import Session

from common import definitions, notifications, version
from plex import servers
from themerr import cache, storage
from media_servers import get_backend
from plex import dashboard


@pytest.fixture
def desktop(configured, monkeypatch):
    monkeypatch.setattr(notifications, '_desktop_available', lambda: True)
    sent = Mock(return_value=True)
    monkeypatch.setattr(notifications, '_send', sent)
    monkeypatch.setattr(version, 'VERSION', 'v2026.1001.120000')
    monkeypatch.setattr(notifications.time, 'time', lambda: 1000)
    return sent


def release(tag='v2026.1002.120000', prerelease=False, draft=False):
    return {'tag_name': tag, 'prerelease': prerelease, 'draft': draft}


def response(monkeypatch, payload, status=200, error=None):
    result = Mock(status_code=status)
    result.json.return_value = payload
    result.raise_for_status.side_effect = error
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    request = Mock(return_value=result)
    monkeypatch.setattr(notifications.requests, 'get', request)
    return request


def snapshot(total, installed):
    return {'1': {
        'key': 1, 'title': 'Movies', 'agent': 'tv.plex.agents.movie', 'type': 'movie',
        'media_count': total, 'media_percent_complete': 0, 'collection_count': 0,
        'collection_percent_complete': 0, 'collections_enabled': False, 'total_count': total,
        'items': [{'rating_key': str(index), 'title': f'Item {index}', 'type': 'movie',
                   'theme': index < installed, 'theme_status': 'complete' if index < installed else 'missing'}
                  for index in range(total)],
    }}


def server(identifier, total, installed, enabled=True):
    with Session(storage.engine()) as database:
        database.merge(servers.ServerRecord(id=identifier, name=identifier, url='http://plex.example', enabled=enabled))
        database.commit()
    with storage.server_scope(identifier):
        storage.replace_dashboard(snapshot(total, installed))


def test_stable_release_alert_survives_restart_and_is_rate_limited(desktop, monkeypatch):
    fetch = response(monkeypatch, release())
    notifications.check_for_releases()
    assert fetch.call_args.args == ('https://api.github.com/repos/Themerr/Themerr/releases/latest',)
    assert fetch.call_args.kwargs['timeout'] == 10
    assert fetch.call_args.kwargs['allow_redirects'] is False
    assert '2026.1002.120000' in desktop.call_args.args[1]
    assert 'https://github.com/Themerr/Themerr/releases/tag/v2026.1002.120000' in desktop.call_args.args[1]
    storage.close()
    notifications.check_for_releases()
    fetch.assert_called_once()
    monkeypatch.setattr(notifications.time, 'time', lambda: 4600)
    notifications.check_for_releases()
    assert fetch.call_count == 2
    desktop.assert_called_once()


def test_prereleases_are_opt_in_and_use_version_order(desktop, configured, monkeypatch):
    fetch = response(monkeypatch, release('v2026.1003.120000', prerelease=True))
    notifications.check_for_releases()
    desktop.assert_not_called()
    configured['Notifications']['FOLLOW_PRERELEASES'] = True
    fetch = response(monkeypatch, [release('v2026.1002.120000'), release('v2026.1003.120000', prerelease=True),
                                   release('v2026.1004.120000', draft=True), release('invalid')])
    notifications.check_for_releases()
    assert fetch.call_args.args == (notifications._RELEASE_API,)
    assert fetch.call_args.kwargs['params'] == {'per_page': 100}
    assert '2026.1003.120000' in desktop.call_args.args[1]


@pytest.mark.parametrize('payload,status', [
    (release('v2026.1001.120000'), 200), (release('v2026.999.120000'), 200),
    (release('v2026.1002.120000rc1'), 200), (release(draft=True), 200),
    (release('unparseable'), 200), (None, 404), ({}, 200), ([], 200), (release(), 302),
])
def test_no_alert_for_old_unknown_draft_or_invalid_stable_release(desktop, monkeypatch, payload, status):
    response(monkeypatch, payload, status=status)
    notifications.check_for_releases()
    desktop.assert_not_called()


@pytest.mark.parametrize('failure', [requests.Timeout(), requests.HTTPError(), ValueError()])
def test_failed_release_check_reserves_hour_and_preserves_last_alert(desktop, monkeypatch, failure):
    notifications._save(notifications._RELEASE_KEY, {'notified_version': '2026.1000.120000'})
    fetch = response(monkeypatch, None, error=failure)
    notifications.check_for_releases()
    storage.close()
    notifications.check_for_releases()
    fetch.assert_called_once()
    desktop.assert_not_called()
    assert notifications._load(notifications._RELEASE_KEY)['notified_version'] == '2026.1000.120000'


def test_failed_desktop_delivery_can_be_retried_next_hour(desktop, monkeypatch):
    response(monkeypatch, release())
    desktop.return_value = False
    notifications.check_for_releases()
    assert 'notified_version' not in notifications._load(notifications._RELEASE_KEY)
    desktop.return_value = True
    monkeypatch.setattr(notifications.time, 'time', lambda: 4600)
    notifications.check_for_releases()
    assert desktop.call_count == 2


def test_source_version_checks_at_startup_and_respects_the_saved_deadline(desktop, monkeypatch):
    from themerr import scheduled_tasks
    monkeypatch.setattr(version, 'VERSION', '0.0.0')
    fetch = response(monkeypatch, release())
    monkeypatch.setattr(scheduled_tasks, 'run_threaded', lambda target, **kwargs: target())
    monkeypatch.setattr(scheduled_tasks, 'cache_data', Mock())
    monkeypatch.setattr(scheduled_tasks, 'scheduled_update', Mock())

    def stop_loop(seconds):
        if seconds:
            raise RuntimeError('stop scheduler')

    monkeypatch.setattr(scheduled_tasks.time, 'sleep', stop_loop)
    scheduled_tasks.schedule.clear()
    try:
        scheduled_tasks.configure_jobs()
        with pytest.raises(RuntimeError, match='stop scheduler'):
            scheduled_tasks.schedule_loop()
    finally:
        scheduled_tasks.schedule.clear()
    fetch.assert_called_once()
    desktop.assert_called_once()
    storage.close()
    notifications.check_for_releases()
    fetch.assert_called_once()
    desktop.assert_called_once()


def test_invalid_installed_version_skips_release_check(desktop, monkeypatch):
    monkeypatch.setattr(version, 'VERSION', 'unknown')
    fetch = response(monkeypatch, release())
    notifications.check_for_releases()
    fetch.assert_not_called()


def test_disabled_or_headless_release_check_has_no_network(configured, monkeypatch):
    fetch = response(monkeypatch, release())
    notifications.check_for_releases()
    monkeypatch.setattr(notifications, '_desktop_available', lambda: True)
    configured['Notifications']['NEW_RELEASE'] = False
    notifications.check_for_releases()
    fetch.assert_not_called()


def test_coverage_uses_weighted_total_and_includes_uploads_and_collections(desktop):
    server('a', 2, 0)
    server('b', 8, 4, enabled=False)
    notifications.refresh_completed()
    desktop.assert_not_called()
    storage.close()
    with storage.server_scope('a'):
        storage.mark_dashboard_theme_uploaded('0', 'themerr')
    with storage.server_scope('b'):
        data = snapshot(8, 5)
        data['1']['items'][-1]['type'] = 'collection'
        data['1']['collection_count'] = 1
        data['1']['media_count'] = 7
        storage.replace_dashboard(data)
    notifications.refresh_completed()
    desktop.assert_called_once()
    assert '20 percentage points: 40.00% → 60.00%' in desktop.call_args.args[1]
    assert '6 of 10 items' in desktop.call_args.args[1]
    assert storage.current_server_id() == 'default'
    notifications.refresh_completed()
    desktop.assert_called_once()


@pytest.mark.parametrize('total,installed', [(10, 5), (10, 4), (20, 10), (0, 0)])
def test_coverage_no_increase_or_empty_library_stays_quiet(desktop, total, installed):
    server('a', 10, 5)
    notifications.refresh_completed()
    server('a', total, installed)
    notifications.refresh_completed()
    desktop.assert_not_called()


def test_small_coverage_increase_is_not_rounded_to_zero(desktop, monkeypatch):
    monkeypatch.setattr(get_backend(), 'list_servers', lambda: [{'id': 'a', 'enabled': True}])
    notifications._save(notifications._COVERAGE_KEY, {
        'servers': [['a', True]], 'total': 100000, 'installed': 50000,
    })
    monkeypatch.setattr(storage, 'get_dashboard', lambda: {'1': {
        'items': [{'theme': True}] * 50001 + [{'theme': False}] * 49999,
    }})
    notifications.refresh_completed()
    assert '0.001 percentage points' in desktop.call_args.args[1]


def test_disabled_coverage_alert_updates_baseline_and_server_changes_reset_it(desktop, configured):
    server('a', 10, 0)
    notifications.refresh_completed()
    configured['Notifications']['COVERAGE_INCREASE'] = False
    server('a', 10, 5)
    notifications.refresh_completed()
    configured['Notifications']['COVERAGE_INCREASE'] = True
    notifications.refresh_completed()
    server('b', 10, 10)
    notifications.refresh_completed()
    server('b', 10, 10, enabled=False)
    notifications.refresh_completed()
    desktop.assert_not_called()
    server('a', 10, 6)
    notifications.refresh_completed()
    desktop.assert_called_once()


@pytest.mark.parametrize('failure', [False, OSError('offline')])
def test_partial_refresh_preserves_baseline_and_recovery_notifies(desktop, monkeypatch, failure):
    server('a', 2, 0)
    server('b', 8, 4)
    notifications.refresh_completed()

    def refresh():
        if storage.current_server_id() == 'a':
            if isinstance(failure, Exception):
                raise failure
            return failure
        storage.replace_dashboard(snapshot(8, 6))
        return True

    monkeypatch.setattr(dashboard, '_cache_server', refresh)
    cache.cache_data()
    desktop.assert_not_called()
    assert notifications._load(notifications._COVERAGE_KEY)['installed'] == 4
    assert servers.get_server('a')['last_error']
    monkeypatch.setattr(dashboard, '_cache_server', lambda: True)
    cache.cache_data()
    desktop.assert_called_once()
    assert '20 percentage points' in desktop.call_args.args[1]


def test_corrupt_notification_state_is_replaced(desktop, monkeypatch):
    with Session(storage.engine()) as database:
        database.add_all([storage.AppSetting(key=notifications._RELEASE_KEY, value='broken'),
                          storage.AppSetting(key=notifications._COVERAGE_KEY, value='[]')])
        database.commit()
    response(monkeypatch, release())
    notifications.check_for_releases()
    server('a', 10, 5)
    notifications.refresh_completed()
    assert json.loads(json.dumps(notifications._load(notifications._COVERAGE_KEY)))['installed'] == 5
    desktop.assert_called_once()


def test_desktop_backend_is_lazy_serialized_and_failure_does_not_escape(monkeypatch):
    from desktop_notifier.sync import DesktopNotifierSync
    import desktop_notifier.sync
    notifier = Mock(spec=DesktopNotifierSync)
    factory = Mock(return_value=notifier)
    monkeypatch.setattr(desktop_notifier.sync, 'DesktopNotifierSync', factory)
    monkeypatch.setattr(notifications, '_notifier', None)
    monkeypatch.setattr(notifications, '_desktop_available', lambda: True)
    assert notifications._send('Title', 'Message')
    assert notifications._send('Second', 'Message')
    factory.assert_called_once()
    assert factory.call_args.kwargs['app_name'] == 'Themerr'
    notifier.send.side_effect = RuntimeError('backend failed')
    assert notifications._send('Failure', 'Message') is False


def test_docker_and_headless_linux_have_no_desktop(monkeypatch):
    monkeypatch.setattr(definitions.Modes, 'DOCKER', True)
    assert not notifications._desktop_available()
    monkeypatch.setattr(definitions.Modes, 'DOCKER', False)
    monkeypatch.setattr(definitions.Platform, 'os_platform', 'linux')
    monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
    assert not notifications._desktop_available()
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/run/user/1000/bus')
    assert notifications._desktop_available()

"""Mocked Jellyfin API pagination, scoped theme processing, and credentialed playback."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common import helpers
from jellyfin import backend, connector, metadata, servers
from media_servers import get_backend
from media_servers.base import MediaServerError
from themerr import storage

ITEM = '1' * 32
LIBRARY = '2' * 32
SERVER = 'jellyfin:' + '3' * 32


@pytest.fixture
def connected(configured, connector_bundle, monkeypatch):
    connection = Mock(server_version='12.1.0')
    monkeypatch.setattr(servers, 'client', lambda _: connection)
    monkeypatch.setattr(servers, 'get_server', lambda _: {
        'ignored_libraries': '', 'enabled': True, 'url': 'http://jellyfin.example:8096'})
    monkeypatch.setattr(connector, 'verify', lambda _: connector_bundle)
    return connection


def test_complete_pagination():
    connection = Mock()
    connection.json.side_effect = [{'Items': [{'Id': ITEM}], 'TotalRecordCount': 2},
                                   {'Items': [{'Id': LIBRARY}], 'TotalRecordCount': 2}]
    assert [row['Id'] for row in backend._items(connection)] == [ITEM, LIBRARY]
    assert [call.kwargs['params']['StartIndex'] for call in connection.json.call_args_list] == [0, 1]


def test_registry_dispatch_keeps_plex_and_jellyfin_separate():
    registry = get_backend()
    assert isinstance(registry.server(SERVER), backend.JellyfinMediaServer)
    assert registry.server('plex-machine').__class__.__name__ == 'PlexMediaServer'
    assert registry.display_name(SERVER) == 'Jellyfin'
    assert registry.display_name('plex-machine') == 'Plex'


def test_scan_honors_paused_categories_ignored_libraries_and_scope(connected, configured, monkeypatch):
    monkeypatch.setattr(servers, 'get_server', lambda _: {'ignored_libraries': LIBRARY})
    connected.json.side_effect = [[{'ItemId': LIBRARY}, {'ItemId': '4' * 32}], {
        'Items': [{'Id': ITEM, 'Type': 'Movie'}, {'Id': '5' * 32, 'Type': 'Series'},
                  {'Id': '6' * 32, 'Type': 'Movie', 'IsLocked': True}], 'TotalRecordCount': 3,
    }]
    configured['Jellyfin']['BOOL_SERIES_SUPPORT'] = False
    configured['Themerr']['BOOL_IGNORE_LOCKED_FIELDS'] = False
    seen = []
    backend.JellyfinMediaServer(SERVER).scan(lambda key: seen.append((storage.current_server_id(), key)))
    assert seen == [(SERVER, ITEM)]
    assert storage.current_server_id() == 'default'


@pytest.mark.parametrize('owned, present, overwrite', [
    (False, True, False), (True, True, False), (False, False, False), (False, True, True)])
def test_verified_upload_and_user_theme_protection(
        connected, configured, monkeypatch, tmp_path, owned, present, overwrite):
    configured['Jellyfin']['BOOL_OVERWRITE_USER_THEMES'] = overwrite
    configured['Jellyfin']['BOOL_BACKUP_USER_THEMES'] = not overwrite
    item = {'Id': ITEM, 'Name': 'Example', 'Type': 'Movie'}
    state = {'present': present, 'owned': owned, 'sha256': 'old'}
    connected.json.side_effect = [
        {'Items': [item], 'TotalRecordCount': 1}, state,
        *([state] if present and not owned else []), {'owned': True, 'sha256': 'digest'},
    ]
    monkeypatch.setattr(metadata, 'resolve', lambda _: {
        'exists': True, 'database_type': 'movies', 'database': 'themoviedb', 'database_id': '42'})
    database_request = Mock(return_value={'youtube_theme_url': 'https://youtube.example'})
    monkeypatch.setattr(helpers, 'json_get', database_request)
    path = tmp_path / 'audio.m4a'
    path.write_bytes(b'complete audio')
    audio = SimpleNamespace(path=str(path), codec='mp4a', sha256='digest', mp4a_available=True)
    download = Mock(return_value=nullcontext(audio))
    monkeypatch.setattr(backend, 'download_youtube', download)
    assert backend.JellyfinMediaServer(SERVER).update_item(ITEM) is True
    lookup = connected.json.call_args_list[0]
    assert lookup.args == ('GET', '/Items')
    assert lookup.kwargs['params']['Ids'] == ITEM
    assert lookup.kwargs['params']['Limit'] == 1
    assert 'UserId' not in lookup.kwargs['params']
    with storage.server_scope(SERVER):
        tracked = storage.get_tracking(ITEM)
    if present and not owned and not overwrite:
        download.assert_not_called()
        assert tracked == {}
    else:
        assert database_request.call_args.kwargs['url'] == (
            'https://themerr.lizardbyte.dev/ThemerrDB/movies/themoviedb/42.json'
        )
        assert tracked['audio_sha256'] == 'digest'
        assert connected.json.call_args.kwargs['headers']['X-Themerr-Connector'] == 'a' * 64
        assert connected.json.call_args.kwargs['headers']['Content-Type'] == 'audio/mp4'
        assert connected.json.call_args.kwargs['headers']['X-Themerr-Overwrite-User'] == str(overwrite).lower()
        assert connected.json.call_args.kwargs['headers']['X-Themerr-Backup-User'] == str(not overwrite).lower()
    assert storage.current_server_id() == 'default'
    assert storage.get_tracking(ITEM) == {}


def test_deleted_item_records_a_fixed_error_without_attempting_an_upload(connected, caplog):
    connected.json.return_value = {'Items': [], 'TotalRecordCount': 0}
    assert backend.JellyfinMediaServer(SERVER).update_item(ITEM) is False
    connected.json.assert_called_once()
    connected.request.assert_not_called()
    assert ITEM in caplog.text
    assert SERVER in caplog.text
    assert 'This Jellyfin item is unavailable.' in caplog.text
    with storage.server_scope(SERVER):
        assert storage.get_errors()[ITEM] == 'This Jellyfin item is unavailable.'
        assert storage.get_tracking(ITEM) == {}


def test_failed_upload_never_records_success(connected, monkeypatch, tmp_path):
    connected.json.return_value = {'owned': True, 'sha256': 'wrong'}
    path = tmp_path / 'audio.m4a'
    path.write_bytes(b'complete audio')
    audio = SimpleNamespace(path=str(path), codec='mp4a', sha256='digest', mp4a_available=True)
    with storage.server_scope(SERVER), pytest.raises(MediaServerError):
        backend.JellyfinMediaServer._send_audio(connected, ITEM, {'Type': 'Movie'}, 'source', {'build': 'a'}, audio)
    with storage.server_scope(SERVER):
        assert storage.get_tracking(ITEM) == {}


def test_playback_uses_fixed_native_paths_and_header_credentials(connected):
    connected.json.return_value = {'Items': [{'Id': '7' * 32}]}
    adapter = backend.JellyfinMediaServer(SERVER)
    result = adapter.open_theme(ITEM, {'Range': 'bytes=1-4'})
    assert result is connected.request.return_value
    assert connected.request.call_args.args == ('GET', '/Audio/' + '7' * 32 + '/stream')
    assert connected.request.call_args.kwargs['headers'] == {'Range': 'bytes=1-4'}
    assert connected.request.call_args.kwargs['stream'] is True
    connected.json.return_value = {'Items': []}
    with pytest.raises(MediaServerError) as error:
        adapter.open_theme(ITEM, {})
    assert error.value.status_code == 404

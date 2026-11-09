"""FastAPI routes exercised with temporary configuration and cache data."""

# standard imports
import re
from types import SimpleNamespace
from unittest.mock import Mock

# lib imports
from fastapi.testclient import TestClient
import pytest
import requests
from plexapi.exceptions import NotFound
from sqlalchemy.orm import Session

# local imports
from tests.http_helpers import get_session, set_session
from common import admin, config, credentials, webapp
from plex import servers
from plex import auth, plexapi, web as plex_web
from themerr import storage
from themerr import theme_errors


@pytest.fixture
def client(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(admin, 'HASH_METHOD', 'scrypt:16384:8:1')
    current = admin._save('admin', 'a unique test password')
    with Session(storage.engine()) as database:
        database.add(servers.ServerRecord(id='default', name='Plex', url='http://plex.example'))
        database.commit()
    with TestClient(
        webapp.create_app(https_only=False),
        base_url='http://localhost',
        follow_redirects=False,
    ) as test_client:
        test_client.app.state.csrf_enabled = False
        set_session(test_client, {'admin_revision': current['revision']})
        yield test_client


def _dashboard(items):
    """Build a small persisted library snapshot for route tests."""
    return {'1': {
        'key': 1, 'title': 'Movies', 'agent': 'tv.plex.agents.movie', 'type': 'movie',
        'media_count': len(items), 'media_percent_complete': 0,
        'collection_count': 0, 'collection_percent_complete': 0,
        'collections_enabled': False, 'total_count': len(items), 'items': items,
    }}


def test_home(client, configured, monkeypatch):
    from common import version
    monkeypatch.setattr(version, 'VERSION', '2026.1003.120000')
    response = client.get('/')
    assert response.status_code == 200
    assert b'Getting to know your library.' in response.content
    assert b'Version 2026.1003.120000' in response.content
    storage.replace_dashboard(_dashboard([]))
    assert client.get('/home').status_code == 200


def test_log_viewer_and_api_require_authentication(client, monkeypatch, tmp_path):
    from common import log_viewer
    monkeypatch.setattr(log_viewer.Paths, 'LOG_DIR', str(tmp_path))
    (tmp_path / 'themerr.log').write_text(
        '2026-10-03 10:00:00 - WARNING :: Worker : example warning\n', encoding='utf-8',
    )
    page = client.get('/logs')
    assert page.status_code == 200
    assert 'id="log-viewer"' in page.text
    assert 'value="startup"' in page.text
    assert 'aria-current="page"' in page.text
    assert 'href="/logs"' in client.get('/').text
    result = client.get('/api/logs?source=themerr&limit=1')
    assert result.status_code == 200
    assert result.headers['Cache-Control'] == 'no-store'
    assert result.json()['entries'][0]['message'] == 'example warning'
    for query in ('source=../config.ini', 'source=common', 'source=themerr.log',
                  'source=themerr%2f..%2fconfig.ini', 'source=%252e%252e%252fconfig.ini',
                  'source=C%3a%5cconfig.ini', 'source=themerr.log%3asecret', 'source=themerr%00',
                  'limit=0', 'limit=2001', 'limit=nope',
                  'scope=invalid', 'scope=startup&cursor=-1'):
        assert client.get('/api/logs?' + query).status_code == 422
    client.cookies.clear()
    assert client.get('/logs').status_code == 302
    assert client.get('/api/logs').status_code == 401


def test_startup_log_api_batches_and_appends_current_session_records(client, monkeypatch):
    import logging
    from common import logger, log_viewer
    capture = log_viewer._SessionLogHandler()
    monkeypatch.setattr(logger, '_session_handler', capture)
    try:
        for message, name in [('first', 'themerr'), ('backend', 'uvicorn.error'), ('last', 'yt-dlp')]:
            capture.handle(logging.LogRecord(name, logging.INFO, __file__, 1, message, (), None))
        first = client.get('/api/logs?scope=startup&limit=2').json()
        assert len(first['entries']) == 2
        assert first['has_more']
        last = client.get(f'/api/logs?scope=startup&limit=2&cursor={first["cursor"]}').json()
        assert len(last['entries']) == 1
        assert last['entries'][0]['source'] == 'yt-dlp'
        assert not last['has_more']
        capture.handle(logging.LogRecord('themerr', logging.ERROR, __file__, 1, 'new error', (), None))
        appended = client.get(f'/api/logs?scope=startup&cursor={last["cursor"]}').json()
        assert len(appended['entries']) == 1
        assert appended['entries'][0]['level'] == 'ERROR'
        filtered = client.get('/api/logs?scope=startup&source=backend').json()
        assert len(filtered['entries']) == 1
        assert filtered['entries'][0]['source'] == 'backend'
        client.cookies.clear()
        assert client.get('/api/logs?scope=startup').status_code == 401
    finally:
        capture.close()


def test_asset_urls_change_when_compiled_content_changes(client, monkeypatch, tmp_path):
    from common.http import asset_url
    from fastapi import Request
    monkeypatch.setattr(client.app.state, 'static_directory', str(tmp_path))
    css = tmp_path / 'app.css'
    css.write_text('body {color: red}', encoding='utf-8')
    request = Request({'type': 'http', 'app': client.app})
    first = asset_url(request, 'app.css')
    assert first == asset_url(request, 'app.css')
    css.write_text('body {color: green}', encoding='utf-8')
    second = asset_url(request, 'app.css')
    assert first != second
    assert asset_url(request, 'missing.css') == '/web/assets/missing.css'
    assert b'href="' + second.encode() + b'"' in client.get('/').content


def test_branding_uses_the_project_asset_and_links(client):
    page = client.get('/').content
    assert b'class="brand-logo"' in page
    assert b'src="/images/icon-default.png"' in page
    assert b'href="https://app.lizardbyte.dev/" target="_blank" rel="noopener noreferrer"' in page
    assert b'href="https://github.com/Themerr/Themerr" target="_blank" rel="noopener noreferrer"' in page
    assert b'href="https://docs.lizardbyte.dev/projects/themerr/latest/" target="_blank"' in page
    for body in re.findall(rb'<a\b[^>]*target="_blank"[^>]*>(.*?)</a>', page, re.DOTALL):
        assert b'data-lucide="arrow-up-right"' in body


def test_cookie_help_links_to_the_hosted_usage_page(client):
    page = client.get('/settings/').text
    assert 'https://docs.lizardbyte.dev/projects/themerr/latest/docs/about/usage.html#youtube-cookies' in page


@pytest.mark.parametrize('path', [
    '/docs/',
    '/docs/index.html',
    '/docs/%2e%2e/private.txt',
])
def test_project_documentation_is_not_served_from_the_installation(client, path):
    assert client.get(path).status_code == 404
    assert client.head(path).status_code == 404


def test_theme_controls_only_for_installed_themes(client):
    providers = ['plex', 'user', 'themerr', 'uploaded', None]
    items = [{
        'rating_key': str(index), 'title': f'Theme {index}', 'type': 'movie', 'year': 2020,
        'issue_url': None, 'theme_provider': provider, 'theme_status': 'complete', 'theme': True,
    } for index, provider in enumerate(providers, start=1)]
    items.append({
        'rating_key': '99', 'title': 'No theme', 'type': 'show', 'year': 2020,
        'issue_url': None, 'theme_provider': None, 'theme_status': 'pending', 'theme': False,
    })
    storage.replace_dashboard(_dashboard(items))

    page = client.get('/home').content
    for index in range(1, 6):
        assert f'data-theme-url="/api/themes/{index}"'.encode() in page
    assert b'data-theme-url="/api/themes/99"' not in page
    assert page.count(b'data-lucide="play"') == len(providers) + 1
    assert page.count(b'id="theme-player"') == 1
    assert b'preload="none"' in page
    assert b'data-theme-title="Theme 1"' in page
    assert b'data-theme-year="2020"' in page
    assert b'data-theme-poster="/api/themes/1/poster"' in page
    assert page.index(b'id="theme-widget"') > page.index(b'</main>')
    for path in ('/servers', '/settings/', '/activity', '/logs', '/api/docs'):
        assert client.get(path).content.count(b'id="theme-player"') == 1


@pytest.fixture
def theme_server(monkeypatch):
    server = Mock()
    server.fetchItem.return_value = SimpleNamespace(theme='/library/metadata/42/theme/123')
    server.url.return_value = 'http://plex.example/library/metadata/42/theme/123'
    server._headers.side_effect = lambda **headers: {'X-Plex-Token': 'private-token', **headers}
    upstream = Mock(status_code=200, headers={
        'Content-Type': 'audio/mpeg', 'Content-Length': '6', 'Accept-Ranges': 'bytes',
        'X-Plex-Token': 'private-token', 'Location': 'http://plex.example/?X-Plex-Token=private-token',
    })
    upstream.iter_content.return_value = iter([b'abc', b'def'])
    server._session.get.return_value = upstream
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: server)
    return server, upstream


@pytest.mark.parametrize('partial', [False, True])
def test_theme_audio_stream_and_ranges(client, theme_server, partial):
    server, upstream = theme_server
    headers = {}
    if partial:
        headers = {'Range': 'bytes=0-5', 'If-Range': 'theme-version'}
        upstream.status_code = 206
        upstream.headers['Content-Range'] = 'bytes 0-5/10'
    response = client.get('/api/themes/42', headers=headers)

    assert response.status_code == upstream.status_code
    assert response.content == b'abcdef'
    assert response.headers['content-type'] == 'audio/mpeg'
    assert response.headers['Content-Length'] == '6'
    assert response.headers['Accept-Ranges'] == 'bytes'
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'X-Plex-Token' not in response.headers
    assert 'Location' not in response.headers
    if partial:
        assert response.headers['Content-Range'] == 'bytes 0-5/10'
    server.fetchItem.assert_called_once_with(42)
    server.url.assert_called_once_with('/library/metadata/42/theme/123', includeToken=False)
    forwarded = server._session.get.call_args.kwargs
    assert forwarded['stream'] is True
    assert forwarded['allow_redirects'] is False
    assert forwarded['headers'] == {'X-Plex-Token': 'private-token', 'Accept-Encoding': 'identity', **headers}
    response.close()
    upstream.close.assert_called()


def test_theme_head_closes_without_reading_audio(client, theme_server):
    _, upstream = theme_server
    response = client.head('/api/themes/42')
    assert response.status_code == 200
    assert response.content == b''
    assert response.headers['Content-Length'] == '6'
    upstream.iter_content.assert_not_called()
    upstream.close.assert_called_once()


def test_theme_proxy_does_not_serve_upstream_html_on_the_application_origin(client, theme_server):
    _, upstream = theme_server
    upstream.headers['Content-Type'] = 'text/html'
    response = client.get('/api/themes/42')
    assert response.status_code == 502
    assert response.json()['message'] == 'Plex returned an unsupported audio format.'
    upstream.iter_content.assert_not_called()
    upstream.close.assert_called_once()


@pytest.mark.parametrize('theme, status', [
    (None, 404),
    ('https://other.example/theme/123', 502),
    ('//other.example/library/metadata/42/theme/123', 502),
])
def test_theme_missing_or_invalid_path(client, theme_server, theme, status):
    server, _ = theme_server
    server.fetchItem.return_value.theme = theme
    response = client.get('/api/themes/42')
    assert response.status_code == status
    server._session.get.assert_not_called()


def test_theme_requires_plex_connection(client, monkeypatch):
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: None)
    assert client.get('/api/themes/42').status_code == 503
    assert client.get('/api/themes/not-an-id').status_code == 404


@pytest.mark.parametrize('error, status', [
    (NotFound('private-token'), 404),
    (requests.Timeout('private-token'), 502),
])
def test_theme_lookup_errors_are_sanitized(client, theme_server, caplog, error, status):
    server, _ = theme_server
    server.fetchItem.side_effect = error
    response = client.get('/api/themes/42')
    assert response.status_code == status
    assert b'private-token' not in response.content
    assert 'private-token' not in caplog.text


@pytest.mark.parametrize('status, expected', [(302, 502), (404, 404), (500, 502), (416, 416)])
def test_theme_upstream_errors_close_connection(client, theme_server, status, expected):
    _, upstream = theme_server
    upstream.status_code = status
    upstream.headers['Content-Range'] = 'bytes */6'
    response = client.get('/api/themes/42')
    assert response.status_code == expected
    assert b'private-token' not in response.content
    if status == 416:
        assert response.headers['Content-Range'] == 'bytes */6'
        assert response.headers['Content-Length'] == '0'
    upstream.iter_content.assert_not_called()
    upstream.close.assert_called_once()


def test_theme_interrupted_stream_is_closed(client, theme_server, caplog):
    _, upstream = theme_server

    def interrupted_audio(**kwargs):
        yield b'abc'
        raise requests.ConnectionError('private-token')

    upstream.iter_content.side_effect = interrupted_audio
    response = client.get('/api/themes/42')
    assert response.content == b'abc'
    upstream.close.assert_called()
    assert 'Theme playback interrupted for rating_key=42' in caplog.text
    assert 'private-token' not in caplog.text


def test_theme_cancelled_playback_closes_connection(client, theme_server):
    import asyncio
    from fastapi import Request
    _, upstream = theme_server

    async def cancelled():
        disconnected = asyncio.Event()
        scope = {'type': 'http', 'method': 'GET', 'asgi': {'spec_version': '2.0'}}
        response = webapp._theme_audio_response(Request(scope), upstream, 42)

        async def receive():
            await disconnected.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            if message['type'] == 'http.response.body' and message.get('body'):
                disconnected.set()

        await response(scope, receive, send)

    asyncio.run(cancelled())
    upstream.close.assert_called()


def test_home_shows_item_failure(client, configured):
    dashboard = _dashboard([{
            'rating_key': '42', 'title': 'Example', 'type': 'movie', 'year': 2020,
            'issue_action': 'add', 'issue_url': None, 'theme_provider': None,
            'theme_status': 'pending', 'theme': False,
        }])
    storage.replace_dashboard(dashboard)
    assert b'Theme not installed yet' in client.get('/home').content
    theme_errors.set_error(42, 'Video unavailable')

    response = client.get('/home')
    assert response.status_code == 200
    assert b'Video unavailable' in response.content
    assert b'Failed to add theme' in response.content
    assert b'Example' in response.content
    theme_errors.set_error(42, '<script>alert(1)</script>')
    response = client.get('/home')
    assert b'&lt;script&gt;' in response.content
    assert b'<script>alert(1)</script>' not in response.content


def test_home_reflects_successful_upload_on_reload(client):
    storage.replace_dashboard(_dashboard([{
        'rating_key': '42', 'title': 'Example', 'type': 'movie', 'year': 2020,
        'issue_action': 'add', 'issue_url': None, 'theme_provider': None,
        'theme_status': 'pending', 'theme': False,
    }]))
    assert b'Theme not installed yet' in client.get('/home').content

    storage.mark_dashboard_theme_uploaded(42, 'themerr')

    response = client.get('/home')
    assert response.status_code == 200
    assert b'Themerr provided' in response.content
    assert b'Theme not installed yet' not in response.content


def test_home_distinguishes_external_id_and_unknown_provider(client, configured):
    storage.replace_dashboard(_dashboard([
        {
            'rating_key': '42', 'title': 'TV example', 'type': 'show', 'year': 2020,
            'source_database': 'thetvdb', 'source_id': '123', 'issue_url': None,
            'theme_provider': None, 'theme_status': 'unresolved', 'theme': False,
        },
        {
            'rating_key': '43', 'title': 'Movie example', 'type': 'movie', 'year': 2021,
            'issue_url': None, 'theme_provider': 'uploaded', 'theme_status': 'complete', 'theme': True,
        },
        {
            'rating_key': '44', 'title': 'Unattributed theme', 'type': 'movie', 'year': 2022,
            'issue_url': None, 'theme_provider': None, 'theme_status': 'complete', 'theme': True,
        },
    ]))

    response = client.get('/home')
    assert response.status_code == 200
    page = re.sub(rb'\s+', b' ', response.content)
    assert b'TVDB 123' in page
    assert b'TMDB ID unavailable' in page
    assert page.count(b'Unknown provider') == 2
    assert b'Plex ID: 43' in page


def test_home_shows_failed_replacement_of_unknown_provider(client):
    storage.replace_dashboard(_dashboard([
        {
            'rating_key': '43', 'title': 'Uploaded theme', 'type': 'movie', 'year': 2021,
            'issue_url': None, 'theme_provider': 'uploaded', 'theme_status': 'complete', 'theme': True,
        },
        {
            'rating_key': '44', 'title': 'Unattributed theme', 'type': 'movie', 'year': 2022,
            'issue_url': None, 'theme_provider': None, 'theme_status': 'complete', 'theme': True,
        },
    ]))
    theme_errors.set_error(43, 'Plex rejected the replacement')
    theme_errors.set_error(44, 'Video unavailable')

    page = re.sub(rb'\s+', b' ', client.get('/home').content)
    assert page.count(b'Unknown provider') == 2
    assert page.count(b'Outdated from ThemerrDB') == 2
    assert b'Plex rejected the replacement' in page
    assert b'Video unavailable' in page
    assert b'Failed to add theme' not in page
    assert page.count(b'class="failure-reason"') == 2

    theme_errors.set_error(43, None)
    page = client.get('/home').content
    assert page.count(b'Outdated from ThemerrDB') == 1
    assert b'Plex rejected the replacement' not in page


def test_images_and_status(client):
    assert client.get('/favicon.ico').headers['content-type'] == 'image/vnd.microsoft.icon'
    assert client.get('/images/missing.png').status_code == 404
    assert client.get('/status').json() == {'result': 'success', 'message': 'Ok'}


def test_settings(client):
    assert client.get('/settings/').status_code == 200
    assert client.get('/settings/plugin/example').status_code == 404
    response = client.get('/api/settings')
    assert response.status_code == 200
    assert 'Themerr' in response.json()
    assert 'PLEX_TOKEN' not in response.json()['Plex']
    assert b'Your Plex account' in client.get('/servers').content
    assert b'id="plex-auth-status"' in client.get('/servers').content
    assert b'data-directory-target="Logging-LOG_DIR"' in client.get('/settings/').content
    assert b'Plex data directory' in client.get('/servers').content
    assert b'PLEX_TOKEN' not in client.get('/settings/').content


def test_settings_separate_integrations_and_use_unique_field_identifiers(client, configured):
    page = client.get('/settings/').text
    ids = re.findall(r'\bid="([^"]+)"', page)
    assert len(ids) == len(set(ids))
    assert 'id="plex"' in page
    assert 'href="#plex"' in page
    assert 'id="jellyfin"' in page
    assert 'href="#jellyfin"' in page
    assert 'id="Themerr-BOOL_IGNORE_LOCKED_FIELDS"' in page
    assert 'id="Jellyfin-BOOL_IGNORE_LOCKED_FIELDS"' in page
    assert 'id="Jellyfin-BOOL_BACKUP_USER_THEMES"' in page
    assert 'data-lucide="bell"' in page
    assert configured['Jellyfin']['BOOL_OVERWRITE_USER_THEMES'] is False
    assert configured['Jellyfin']['BOOL_BACKUP_USER_THEMES'] is True
    response = client.post('/api/settings', data={
        'Jellyfin|BOOL_OVERWRITE_USER_THEMES': 'true', 'Jellyfin|BOOL_BACKUP_USER_THEMES': 'false',
        'Jellyfin|BOOL_IGNORE_LOCKED_FIELDS': 'true', 'Themerr|BOOL_IGNORE_LOCKED_FIELDS': 'false',
    })
    assert response.status_code == 200
    assert configured['Jellyfin']['BOOL_OVERWRITE_USER_THEMES'] is True
    assert configured['Jellyfin']['BOOL_BACKUP_USER_THEMES'] is False
    assert configured['Jellyfin']['BOOL_IGNORE_LOCKED_FIELDS'] is True
    assert configured['Themerr']['BOOL_IGNORE_LOCKED_FIELDS'] is False


def test_notification_preferences_render_validate_and_persist(client, configured):
    from configobj import ConfigObj
    page = client.get('/settings/').text
    assert 'id="notifications"' in page
    assert 'href="#notifications"' in page
    assert 'id="Notifications-NEW_RELEASE"' in page
    assert 'id="Notifications-COVERAGE_INCREASE"' in page
    assert configured['Notifications']['FOLLOW_PRERELEASES'] is False
    assert configured['Notifications']['NEW_RELEASE'] is True
    assert configured['Notifications']['COVERAGE_INCREASE'] is True
    assert client.post('/api/settings', data={'Notifications|FOLLOW_PRERELEASES': 'invalid'}).status_code == 400
    assert configured['Notifications']['FOLLOW_PRERELEASES'] is False
    response = client.post('/api/settings', data={
        'Notifications|NEW_RELEASE': 'false', 'Notifications|FOLLOW_PRERELEASES': 'true',
        'Notifications|COVERAGE_INCREASE': 'false',
    })
    assert response.status_code == 200
    assert configured['Notifications']['FOLLOW_PRERELEASES'] is True
    assert configured['Notifications']['NEW_RELEASE'] is False
    saved = ConfigObj(configured.filename)
    assert saved['Notifications']['FOLLOW_PRERELEASES'] == 'True'
    assert saved['Notifications']['COVERAGE_INCREASE'] == 'False'


def test_directory_browser_lists_server_folders(client, tmp_path):
    child = tmp_path / 'Plex Media Server'
    child.mkdir()
    (tmp_path / 'private.txt').write_text('not listed', encoding='utf-8')
    assert re.fullmatch(webapp.config.regex_directory, str(child))

    response = client.post('/api/directories', json={'path': str(tmp_path)})

    assert response.status_code == 200
    assert response.json()['path'] == str(tmp_path)
    assert response.json()['directories'] == [{'name': child.name, 'path': str(child)}]
    assert response.json()['parent'] == str(tmp_path.parent)
    assert client.post('/api/directories', json={'path': str(tmp_path / 'missing')}).status_code == 400
    assert client.post('/api/directories', json={'path': 'relative/path'}).status_code == 400


def test_directory_browser_requires_csrf(client, tmp_path):
    client.app.state.csrf_enabled = True
    page = client.get('/settings/')
    csrf_token = re.search(rb'data-csrf-token="([^"]+)"', page.content).group(1).decode()

    assert client.post('/api/directories', json={'path': str(tmp_path)}).status_code == 400
    response = client.post('/api/directories', json={'path': str(tmp_path)},
                           headers={'X-CSRFToken': csrf_token})
    assert response.status_code == 200


def test_plex_sign_in_requires_csrf(client, configured, monkeypatch):
    client.app.state.csrf_enabled = True
    page = client.get('/settings/')
    csrf_token = re.search(rb'data-csrf-token="([^"]+)"', page.content).group(1).decode()
    monkeypatch.setattr(auth, 'start_login', lambda: {
        'pin_id': 123, 'code': 'strong-code', 'auth_url': 'https://app.plex.tv/auth#?code=strong-code',
    })
    assert client.post('/api/plex/auth/start').status_code == 400
    assert client.post('/api/plex/auth/start', headers={'X-CSRFToken': csrf_token}).status_code == 200
    assert client.post('/api/plex/auth/check').status_code == 400
    assert client.post('/api/plex/auth/disconnect').status_code == 400


def test_plex_sign_in_flow(client, configured, monkeypatch):
    monkeypatch.setattr(auth, 'start_login', lambda: {
        'pin_id': 123, 'code': 'strong-code', 'auth_url': 'https://app.plex.tv/auth#?code=strong-code',
    })
    check = iter(['', 'issued-token'])
    monkeypatch.setattr(auth, 'check_login', lambda **_: next(check))
    server = object()
    monkeypatch.setattr(plexapi, 'connect_plex_server', Mock(return_value=server))
    monkeypatch.setattr(plexapi, 'plex_server', None)
    listener = Mock()
    stop_listener = Mock()
    monkeypatch.setattr(plexapi, 'plex_listener', listener)
    monkeypatch.setattr(plexapi, 'stop_plex_listener', stop_listener)

    assert client.get('/api/plex/auth').json() == {'connected': False}
    assert client.post('/api/plex/auth/check').status_code == 400
    assert client.post('/api/plex/auth/start').json()['auth_url'].startswith('https://app.plex.tv/auth#?')
    assert client.post('/api/plex/auth/check').status_code == 202
    assert client.get('/api/plex/auth').json() == {'connected': False}
    assert client.post('/api/plex/auth/check').json() == {'connected': True}
    assert auth.get_token() == 'issued-token'
    assert plexapi.plex_server is None
    listener.assert_not_called()
    assert client.get('/api/plex/auth').json() == {'connected': True}
    assert client.post('/api/plex/auth/check').status_code == 400

    assert client.post('/api/plex/auth/disconnect').json() == {'connected': False}
    assert auth.get_token() == ''
    assert plexapi.plex_server is None
    stop_listener.assert_called()


def test_account_sign_in_does_not_require_a_preconfigured_server(client, configured, monkeypatch):
    auth.set_token('previous-token')
    monkeypatch.setattr(auth, 'start_login', lambda: {
        'pin_id': 123, 'code': 'strong-code', 'auth_url': 'https://app.plex.tv/auth#?code=strong-code',
    })
    monkeypatch.setattr(auth, 'check_login', lambda **_: 'new-token')
    monkeypatch.setattr(plexapi, 'connect_plex_server', Mock(side_effect=RuntimeError('server unavailable')))
    assert client.post('/api/plex/auth/start').status_code == 200
    response = client.post('/api/plex/auth/check')
    assert response.status_code == 200
    assert auth.get_token() == 'new-token'
    plexapi.connect_plex_server.assert_not_called()


def test_plex_sign_in_requires_secure_store(client, configured, monkeypatch):
    monkeypatch.setenv('THEMERR_DOCKER', 'True')
    monkeypatch.setattr(auth, 'start_login', lambda: {
        'pin_id': 123, 'code': 'strong-code', 'auth_url': 'https://app.plex.tv/auth#?code=strong-code',
    })
    monkeypatch.setattr(auth, 'check_login', lambda **_: 'issued-token')
    monkeypatch.setattr(plexapi, 'connect_plex_server', Mock(return_value=object()))

    assert client.post('/api/plex/auth/start').status_code == 200
    response = client.post('/api/plex/auth/check')
    assert response.status_code == 500
    assert response.json() == {'message': 'Unable to save Plex sign-in.'}
    assert auth.get_token() == ''
    assert storage.get_encrypted_token() == ''


def test_plex_sign_in_expired_and_upstream_failure(client, configured, monkeypatch):
    monkeypatch.setattr(auth, 'start_login', lambda: {
        'pin_id': 123, 'code': 'strong-code', 'auth_url': 'https://app.plex.tv/auth#?code=strong-code',
    })
    monkeypatch.setattr(auth, 'check_login', Mock(side_effect=RuntimeError('not called')))
    client.post('/api/plex/auth/start')
    browser_session = get_session(client)
    browser_session['plex_login']['started'] -= plex_web.PLEX_LOGIN_LIFETIME + 1
    set_session(client, browser_session)
    assert client.post('/api/plex/auth/check').status_code == 410
    assert client.post('/api/plex/auth/check').status_code == 400

    monkeypatch.setattr(auth, 'start_login', Mock(side_effect=requests.Timeout('upstream')))
    assert client.post('/api/plex/auth/start').status_code == 502


def test_plex_pin_expired_upstream(client, configured, monkeypatch):
    monkeypatch.setattr(auth, 'start_login', lambda: {
        'pin_id': 123, 'code': 'strong-code', 'auth_url': 'https://app.plex.tv/auth#?code=strong-code',
    })
    response = requests.Response()
    response.status_code = 404
    monkeypatch.setattr(auth, 'check_login', Mock(side_effect=requests.HTTPError(response=response)))
    assert client.post('/api/plex/auth/start').status_code == 200
    assert client.post('/api/plex/auth/check').status_code == 410
    assert client.post('/api/plex/auth/check').status_code == 400


def test_save_settings_with_csrf(client, configured):
    client.app.state.csrf_enabled = True
    page = client.get('/settings/')
    token = re.search(rb'data-csrf-token="([^"]+)"', page.content).group(1).decode()

    denied = client.post('/api/settings', data={'General|LAUNCH_BROWSER': 'false'})
    assert denied.status_code == 400

    response = client.post(
        '/api/settings',
        data={'General|LAUNCH_BROWSER': 'false'},
        headers={'X-CSRFToken': token},
    )
    assert response.status_code == 200
    assert response.json()['status'] == 'OK'
    assert configured['General']['LAUNCH_BROWSER'] is False


def test_reject_invalid_settings_without_mutating_config(client, configured):
    original_port = configured['Network']['HTTP_PORT']
    for data in (
        {'Network|HTTP_PORT': '99999'},
        {'Network|HTTP_PORT': 'not a number'},
        {'Info|CONFIG_VERSION': '2'},
        {'Plex|PLEX_TOKEN': 'manual-token'},
        {'unknown': 'value'},
    ):
        response = client.post('/api/settings', data=data)
        assert response.status_code == 400
        assert response.json()['status'] == 'ERROR'
        assert configured['Network']['HTTP_PORT'] == original_port


def test_save_failure_restores_config(client, configured, monkeypatch):
    original = configured['General']['LAUNCH_BROWSER']
    monkeypatch.setattr(webapp.config, 'save_config', lambda **_: False)
    response = client.post('/api/settings', data={'General|LAUNCH_BROWSER': 'false'})
    assert response.status_code == 500
    assert configured['General']['LAUNCH_BROWSER'] == original


def test_saved_cookies_are_hidden_preserved_replaced_and_cleared(client, configured):
    raw = '[{"value":"private-cookie"}]'
    response = client.post('/api/settings', data={'Themerr|STR_YOUTUBE_COOKIES': raw})
    assert response.status_code == 200
    assert config.youtube_cookies() == raw
    assert 'private-cookie' not in str(response.json())
    assert client.get('/api/settings').json()['Themerr']['STR_YOUTUBE_COOKIES'] == ''
    page = client.get('/settings/').text
    assert raw not in page
    assert configured['Themerr']['STR_YOUTUBE_COOKIES'] not in page
    assert 'data-secret-toggle="Themerr-STR_YOUTUBE_COOKIES"' in page
    assert 'data-has-secret="true"' in page
    assert re.search(r'id="Themerr-STR_YOUTUBE_COOKIES"[^>]*type="password"', page)
    assert client.post('/api/settings', data={
        'General|LAUNCH_BROWSER': 'false',
        'Themerr|STR_YOUTUBE_COOKIES': '',
    }).status_code == 200
    assert config.youtube_cookies() == raw
    assert client.post('/api/settings', data={'Themerr|STR_YOUTUBE_COOKIES': 'replacement'}).status_code == 200
    assert config.youtube_cookies() == 'replacement'
    assert client.post('/api/settings', data={'Themerr|STR_YOUTUBE_COOKIES|clear': 'true'}).status_code == 200
    assert configured['Themerr']['STR_YOUTUBE_COOKIES'] == ''
    assert config.youtube_cookies() == ''
    assert 'data-has-secret="false"' in client.get('/settings/').text


def test_cookie_reveal_requires_authentication_and_csrf_and_disables_caching(client, configured):
    raw = '[{"value":"private-cookie"}]'
    assert client.post('/api/settings', data={'Themerr|STR_YOUTUBE_COOKIES': raw}).status_code == 200
    client.app.state.csrf_enabled = True
    assert client.post('/api/settings/youtube-cookies').status_code == 400
    page = client.get('/settings/')
    token = re.search(rb'data-csrf-token="([^"]+)"', page.content).group(1).decode()
    response = client.post('/api/settings/youtube-cookies', headers={'X-CSRFToken': token})
    assert response.status_code == 200
    assert response.json() == {'value': raw}
    assert response.headers['Cache-Control'] == 'no-store'
    assert client.get('/api/settings/youtube-cookies').status_code == 405
    client.cookies.clear()
    assert client.post('/api/settings/youtube-cookies', headers={'X-CSRFToken': token}).status_code == 401


@pytest.mark.parametrize('save_failure', (
    True,
    False,
))
def test_failed_cookie_update_preserves_existing_settings(client, configured, monkeypatch, save_failure):
    assert client.post('/api/settings', data={'Themerr|STR_YOUTUBE_COOKIES': 'old-cookie'}).status_code == 200
    previous = configured['Themerr']['STR_YOUTUBE_COOKIES']
    if save_failure:
        monkeypatch.setattr(config, 'save_config', lambda **_: False)
    else:
        monkeypatch.setattr(credentials.keyring, 'get_password', Mock(side_effect=RuntimeError('private-cookie')))
    response = client.post('/api/settings', data={
        'General|LAUNCH_BROWSER': 'false',
        'Themerr|STR_YOUTUBE_COOKIES': 'new-cookie',
    })
    assert response.status_code == 500
    assert 'private-cookie' not in response.text
    assert configured['Themerr']['STR_YOUTUBE_COOKIES'] == previous
    assert configured['General']['LAUNCH_BROWSER'] is True


def test_translations_and_logging(client):
    assert client.get('/translations').status_code == 200
    assert client.post('/test_logger').status_code == 200


@pytest.mark.parametrize('method', ['get', 'head'])
def test_theme_poster_is_bounded_and_hides_credentials(client, theme_server, method):
    server, upstream = theme_server
    server.fetchItem.return_value.thumb = '/library/metadata/42/thumb/123'
    upstream.headers['Content-Type'] = 'image/jpeg'
    response = getattr(client, method)('/api/themes/42/poster')
    assert response.status_code == 200
    assert response.headers['Content-Type'] == 'image/jpeg'
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.content == (b'abcdef' if method == 'get' else b'')
    assert 'X-Plex-Token' not in response.headers
    assert 'Location' not in response.headers
    assert 'private-token' not in str(response.headers)
    server.url.assert_called_once_with('/library/metadata/42/thumb/123', includeToken=False)
    assert server._session.get.call_args.kwargs['allow_redirects'] is False
    upstream.close.assert_called_once()
    if method == 'head':
        upstream.iter_content.assert_not_called()


@pytest.mark.parametrize('thumbnail', [
    None, 'https://other.example/image', '//other.example/image', '/library/metadata/42/theme/123',
    '/library/metadata/42/thumb/../config', '/library/metadata/42/thumb/%2e%2e',
    '/library/metadata/42/thumb%2f123', '/library/metadata/42/thumb?X-Plex-Token=private-token',
    r'C:\config.ini', r'\\server\share', '/library/metadata/42/thumb\\123',
])
def test_theme_poster_rejects_unexpected_upstream_paths(client, theme_server, thumbnail):
    server, upstream = theme_server
    server.fetchItem.return_value.thumb = thumbnail
    assert client.get('/api/themes/42/poster').status_code == 404
    server._session.get.assert_not_called()
    upstream.close.assert_not_called()


@pytest.mark.parametrize('status, media_type', [
    (302, 'image/png'), (404, 'image/png'), (500, 'image/png'),
    (200, 'text/html'), (200, 'image/svg+xml'), (200, 'application/octet-stream'),
])
def test_theme_poster_rejects_redirects_and_active_content(client, theme_server, status, media_type):
    server, upstream = theme_server
    server.fetchItem.return_value.thumb = '/library/metadata/42/thumb'
    upstream.status_code = status
    upstream.headers['Content-Type'] = media_type
    assert client.get('/api/themes/42/poster').status_code == 404
    upstream.iter_content.assert_not_called()
    upstream.close.assert_called_once()


def test_theme_poster_stops_oversized_stream(client, theme_server):
    server, upstream = theme_server
    server.fetchItem.return_value.thumb = '/library/metadata/42/thumb'
    upstream.headers['Content-Type'] = 'image/png'
    upstream.iter_content.return_value = iter([b'x' * (5 * 1024 * 1024), b'!', b'must not be read'])
    assert client.get('/api/themes/42/poster').status_code == 502
    assert next(upstream.iter_content.return_value) == b'must not be read'
    upstream.close.assert_called_once()


def test_theme_poster_read_errors_are_sanitized_and_closed(client, theme_server, caplog):
    server, upstream = theme_server
    server.fetchItem.return_value.thumb = '/library/metadata/42/thumb'
    upstream.headers['Content-Type'] = 'image/png'
    upstream.iter_content.side_effect = requests.exceptions.ConnectionError('private-token')
    response = client.get('/api/themes/42/poster')
    assert response.status_code == 502
    assert 'private-token' not in response.text
    assert 'private-token' not in caplog.text
    upstream.close.assert_called_once()


def test_theme_poster_server_scope_and_authentication(client, theme_server, monkeypatch):
    server, upstream = theme_server
    server.fetchItem.return_value.thumb = '/library/metadata/42/thumb'
    upstream.headers['Content-Type'] = 'image/png'
    observed = []
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: (observed.append(storage.current_server_id()), server)[1])
    assert client.get('/api/servers/other/themes/42/poster').status_code == 200
    assert observed == ['other']
    assert client.get('/api/themes/not-a-key/poster').status_code == 404
    client.cookies.clear()
    assert client.get('/api/themes/42/poster').status_code == 401
    assert client.get('/api/servers/other/themes/42/poster').status_code == 401


def test_theme_poster_without_plex_uses_placeholder(client, monkeypatch):
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: None)
    assert client.get('/api/themes/42/poster').status_code == 404


def test_theme_poster_deleted_item_is_unavailable(client, theme_server):
    server, upstream = theme_server
    server.fetchItem.side_effect = NotFound('private-token')
    assert client.get('/api/themes/42/poster').status_code == 404
    server._session.get.assert_not_called()
    upstream.close.assert_not_called()

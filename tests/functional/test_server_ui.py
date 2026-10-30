"""Admin-only server routes and cross-server dashboard behavior."""

# standard imports
import re
from types import SimpleNamespace
from unittest.mock import Mock

# lib imports
from fastapi.testclient import TestClient
import pytest
from plexapi.exceptions import Unauthorized
from requests.exceptions import (ConnectionError, ConnectTimeout, JSONDecodeError, ReadTimeout, RequestException,
                                 SSLError)
from sqlalchemy.orm import Session

# local imports
from tests.http_helpers import set_session
from common import admin, server_ui, webapp
from common.validation import ValidationError, ValidationMessage
from plex import auth, plexapi, servers, token_store
from media_servers import processing
from themerr import storage


@pytest.fixture
def client(configured, monkeypatch):
    monkeypatch.setattr(admin, 'HASH_METHOD', 'scrypt:16384:8:1')
    current = admin._save('admin', 'a long unique passphrase')
    monkeypatch.setattr(plexapi, 'plex_listener', Mock())
    monkeypatch.setattr(server_ui, '_refresh', Mock())
    with TestClient(
        webapp.create_app(https_only=False),
        base_url='http://localhost',
        follow_redirects=False,
    ) as browser:
        set_session(browser, {'admin_revision': current['revision']})
        yield browser


def headers(client):
    page = client.get('/servers')
    token = re.search(rb'name="csrf-token" content="([^"]+)"', page.content).group(1).decode()
    return {'X-CSRFToken': token}


def save_server(identifier):
    with Session(storage.engine()) as session:
        session.add(servers.ServerRecord(id=identifier, name='Server ' + identifier,
                                         url='http://' + identifier + ':32400'))
        session.commit()


def snapshot(title, provider=None):
    item = {'rating_key': '42', 'title': title, 'type': 'movie',
            'theme': bool(provider), 'theme_provider': provider,
            'theme_status': 'complete' if provider else 'pending'}
    return {'1': {'key': 1, 'title': 'Movies', 'agent': 'tv.plex.agents.movie', 'type': 'movie',
                  'media_count': 1, 'media_percent_complete': 100 if provider else 0,
                  'collection_count': 0, 'collection_percent_complete': 0, 'collections_enabled': False,
                  'total_count': 1, 'items': [item]}}


def test_library_choices_require_login_and_a_saved_server(client, monkeypatch):
    connect = Mock()
    monkeypatch.setattr(servers, 'connect', connect)
    with TestClient(client.app, follow_redirects=False) as anonymous:
        assert anonymous.get('/api/servers/a/libraries').status_code == 401
    assert client.get('/api/servers/missing/libraries').status_code == 404
    connect.assert_not_called()


def test_library_choices_load_only_the_requested_server_and_return_public_metadata(client, monkeypatch):
    save_server('a')
    save_server('b')
    connection = Mock()
    connection.library.sections.return_value = [
        SimpleNamespace(key=1, title='Movies', accessToken='private-token'),
        SimpleNamespace(key=2, title='<b>Shows</b>'),
    ]
    connect = Mock(return_value=connection)
    monkeypatch.setattr(servers, 'connect', connect)
    response = client.get('/api/servers/b/libraries')
    assert response.status_code == 200
    assert response.json() == {'libraries': [{'id': '1', 'title': 'Movies'}, {'id': '2', 'title': '<b>Shows</b>'}],
                               'cached': False}
    assert response.headers['Cache-Control'] == 'no-store'
    connect.assert_called_once_with('b')
    assert storage.current_server_id() == 'default'


@pytest.mark.parametrize('paused', [True, False])
def test_library_choices_fall_back_to_only_this_servers_cache(client, monkeypatch, paused):
    for identifier in ('a', 'b'):
        save_server(identifier)
        data = snapshot('Title ' + identifier)
        data['1']['title'] = 'Movies ' + identifier
        with storage.server_scope(identifier):
            storage.replace_dashboard(data)
    servers.update_server('b', {'enabled': not paused})
    connect = Mock(side_effect=ReadTimeout('private-token'))
    monkeypatch.setattr(servers, 'connect', connect)
    log = Mock()
    monkeypatch.setattr(server_ui, 'log', log)
    response = client.get('/api/servers/b/libraries')
    assert response.json() == {'libraries': [{'id': '1', 'title': 'Movies b'}], 'cached': True}
    assert 'private-token' not in str(log.warning.call_args)
    if paused:
        connect.assert_not_called()
    else:
        connect.assert_called_once_with('b')


def test_library_choices_allow_an_empty_server_and_report_no_cache(client, monkeypatch):
    save_server('a')
    connection = Mock()
    connection.library.sections.return_value = []
    monkeypatch.setattr(servers, 'connect', Mock(return_value=connection))
    assert client.get('/api/servers/a/libraries').json() == {'libraries': [], 'cached': False}
    monkeypatch.setattr(servers, 'connect', Mock(return_value=None))
    assert client.get('/api/servers/a/libraries').json() == {'libraries': [], 'cached': True}


def test_server_page_preselects_saved_ids_in_separate_dropdowns(client):
    for identifier, ignored in [('a', '1, 2'), ('b', '1,9')]:
        save_server(identifier)
        servers.update_server(identifier, {'ignored_libraries': ignored})
    page = client.get('/servers').text
    forms = re.findall(r'<form data-server-form.*?</form>', page, re.DOTALL)
    assert len(forms) == 2
    for form, (identifier, ids) in zip(forms, [('a', ['1', '2']), ('b', ['1', '9'])]):
        assert f'data-server-id="{identifier}"' in form
        assert 'data-library-picker' in form
        assert re.findall(r'name="ignored_libraries" value="([^"]+)" checked', form) == ids
    assert 'Comma-separated library IDs' not in page


def test_collection_library_header_uses_collection_coverage(client):
    save_server('one')
    data = snapshot('Top Gun', 'themerr')
    data['1'].update(type='collection', media_count=0, media_percent_complete=0,
                     collection_count=1, collection_percent_complete=100, collections_enabled=True)
    data['1']['items'][0]['type'] = 'collection'
    with storage.server_scope('one'):
        storage.replace_dashboard(data)
    page = client.get('/').text
    assert '<strong>100%</strong><span class="mini-progress"><span style="width: 100%">' in page
    assert '<strong>0%</strong><span class="mini-progress">' not in page


def test_scoped_dashboard_and_playback_url_do_not_mix_identical_rating_keys(client):
    for identifier, provider in [('a', 'themerr'), ('b', 'plex')]:
        save_server(identifier)
        with storage.server_scope(identifier):
            storage.replace_dashboard(snapshot('Title ' + identifier, provider))
    with storage.server_scope('b'):
        storage.set_error(42, 'B cannot update its theme')
    libraries, _, stats = server_ui.dashboard()
    assert len(libraries) == 2
    assert stats['installed'] == 2
    assert stats['attention'] == 1
    assert libraries['a:1']['items'][0]['error'] is None
    assert libraries['b:1']['items'][0]['error'] == 'B cannot update its theme'
    page = client.get('/').content
    for identifier in ('a', 'b'):
        assert ('/api/servers/' + identifier + '/themes/42').encode() in page
        assert ('Title ' + identifier).encode() in page
        server_url = 'https://app.plex.tv/desktop/#!/media/' + identifier + '/com.plexapp.plugins.library'
        assert f'href="{server_url}" target="_blank" rel="noopener noreferrer"'.encode() in page
        assert f'href="{server_url}?source=1" target="_blank" rel="noopener noreferrer"'.encode() in page
    assert page.count(b'B cannot update its theme') == 1
    assert b'Outdated from ThemerrDB' in page
    assert b'B cannot update its theme' in client.get('/activity').content


def test_publication_api_requires_login_and_returns_cached_status(client, monkeypatch):
    from themerr import deployment_status
    status = {'updated_at': '2026-10-02T12:00:00+00:00', 'next_check': 12345, 'stale': False}
    lookup = Mock(return_value=status)
    monkeypatch.setattr(deployment_status, 'publication_status', lookup)
    with TestClient(client.app, follow_redirects=False) as anonymous:
        assert anonymous.get('/api/themerrdb').status_code == 401
    lookup.assert_not_called()
    result = client.get('/api/themerrdb')
    assert result.status_code == 200
    assert result.json() == status
    assert result.headers['Cache-Control'] == 'no-store'
    lookup.assert_called_once()


def test_playback_selects_the_requested_server(client, monkeypatch):
    seen = []
    upstream = Mock(status_code=200, headers={'Content-Type': 'audio/mpeg'})
    upstream.iter_content.return_value = iter([b'audio'])
    server = Mock()
    server.fetchItem.return_value = SimpleNamespace(theme='/library/metadata/42/theme/123')
    server._session.get.return_value = upstream
    server.url.return_value = 'http://selected.example/theme'

    def setup():
        seen.append(storage.current_server_id())
        return server

    monkeypatch.setattr(plexapi, 'setup_plexapi', setup)
    response = client.get('/api/servers/b/themes/42')
    assert response.content == b'audio'
    assert response.status_code == 200
    assert seen == ['b']
    assert storage.current_server_id() == 'default'
    response.close()


def test_discovery_and_server_settings_require_csrf(client, monkeypatch):
    discover = Mock(return_value=[{'id': 'one', 'name': 'Living room', 'connections': []}])
    monkeypatch.setattr(servers, 'discover_account', discover)
    assert client.post('/api/servers/discover', json={'source': 'account'}).status_code == 400
    response = client.post('/api/servers/discover', json={'source': 'account'}, headers=headers(client))
    assert response.status_code == 200
    assert response.json()['servers'][0]['name'] == 'Living room'
    discover.assert_called_once()
    monkeypatch.setattr(servers, 'discover_local', Mock(side_effect=OSError('private token')))
    response = client.post('/api/servers/discover', json={'source': 'local'}, headers=headers(client))
    assert response.status_code == 502
    assert 'private token' not in str(response.json())
    assert client.post('/api/servers/discover', json={'source': 'invalid'}, headers=headers(client)).status_code == 400
    save_server('one')
    for path, payload in [('/api/servers', {'url': 'http://plex.example'}), ('/api/servers/one', {'enabled': False}),
                          ('/api/tasks/refresh', {'scan': True})]:
        assert client.post(path, json=payload).status_code == 400
    assert client.delete('/api/servers/one').status_code == 400


@pytest.mark.parametrize('source', ['account', 'local'])
@pytest.mark.parametrize('enabled', [True, False])
def test_discovery_marks_saved_plex_servers_connected_by_identity(client, monkeypatch, source, enabled):
    save_server('one')
    servers.update_server('one', {'enabled': enabled})
    connections = [{'url': 'http://192.168.1.205:32400', 'local': True, 'relay': False},
                   {'url': 'https://alternate.plex.direct:32400', 'local': False, 'relay': False}]
    resources = [{'id': 'one', 'name': 'Same name', 'connections': connections},
                 {'id': 'two', 'name': 'Same name', 'connections': connections}]
    monkeypatch.setattr(servers, 'discover_' + source, Mock(return_value=resources))
    request_headers = headers(client)
    response = client.post('/api/servers/discover', json={'source': source}, headers=request_headers)
    assert response.status_code == 200
    assert response.json()['servers'] == [{**resources[0], 'connected': True}, {**resources[1], 'connected': False}]
    with Session(storage.engine()) as session:
        session.delete(session.get(servers.ServerRecord, 'one'))
        session.commit()
    assert all(not resource['connected'] for resource in
               client.post('/api/servers/discover', json={'source': source}, headers=request_headers).json()['servers'])


def test_add_update_and_remove_server(client, monkeypatch):
    add = Mock(return_value={'id': 'one', 'name': 'Plex'})
    monkeypatch.setattr(servers, 'add_server', add)
    response = client.post('/api/servers', json={'url': 'https://plex.example', 'resource_id': 'one'},
                           headers=headers(client))
    assert response.status_code == 201
    add.assert_called_once_with('https://plex.example', 'one')
    server_ui._refresh.assert_called_once()
    plexapi.plex_listener.assert_called_once()
    save_server('one')
    assert client.post('/api/servers/one', json={'enabled': False}, headers=headers(client)).status_code == 200
    assert servers.get_server('one')['enabled'] is False
    assert client.post('/api/servers/one', json={'enabled': 'false'}, headers=headers(client)).status_code == 400
    assert client.post('/api/servers/missing', json={}, headers=headers(client)).status_code == 404
    assert client.delete('/api/servers/one', headers=headers(client)).status_code == 200
    assert servers.get_server('one') is None


@pytest.mark.parametrize(('error', 'status', 'reason'), [
    (ValidationError(ValidationMessage.PLEX_ADDRESS_INVALID), 400, 'valid Plex server address'),
    (ValueError('private token'), 502, 'Could not connect'),
    (token_store.TokenStorageError('private key'), 500, 'credential store'),
    (ConnectTimeout('private token'), 502, 'timed out'),
    (ReadTimeout('private token'), 502, 'timed out'),
    (SSLError('private token'), 502, 'server certificate'),
    (ConnectionError('private token'), 502, 'hostname, port, and network access'),
    (Unauthorized('private token'), 502, 'denied access'),
    (RequestException('private token'), 502, 'invalid response'),
    (JSONDecodeError('private token', 'private body', 0), 502, 'invalid response'),
    (OSError('private key'), 502, 'Could not connect'),
    (RuntimeError('private token'), 502, 'Could not connect'),
])
def test_connect_failures_distinguish_network_from_credential_store(client, monkeypatch, error, status, reason):
    monkeypatch.setattr(servers, 'add_server', Mock(side_effect=error))
    log = Mock()
    monkeypatch.setattr(server_ui, 'log', log)
    response = client.post('/api/servers', json={'url': 'https://plex.example'}, headers=headers(client))
    assert response.status_code == status
    assert reason in response.json()['message']
    assert 'private' not in str(response.json())
    assert 'private' not in str(log.warning.call_args)
    if not isinstance(error, token_store.TokenStorageError):
        assert 'credential store' not in response.json()['message']
    server_ui._refresh.assert_not_called()
    plexapi.plex_listener.assert_not_called()


@pytest.mark.parametrize('method, operation', [('POST', 'update_server'), ('DELETE', 'remove_server')])
@pytest.mark.parametrize('failure', [ValueError, RuntimeError])
def test_server_edit_unexpected_failure_is_not_exposed(client, monkeypatch, method, operation, failure):
    save_server('one')
    monkeypatch.setattr(servers, operation, Mock(side_effect=failure('private token and database details')))
    log = Mock()
    monkeypatch.setattr(server_ui, 'log', log)
    response = client.request(method, '/api/servers/one', json={'enabled': False}, headers=headers(client))
    assert response.status_code == 500
    assert response.json() == {'message': 'Could not update this Plex server. Check its connection and settings.'}
    assert 'private' not in str(log.warning.call_args)
    assert servers.get_server('one') is not None
    plexapi.plex_listener.assert_not_called()


@pytest.mark.parametrize('address, message', [
    ('http://[invalid', 'Enter a valid Plex server address.'),
    ('http://plex.example:private-port', 'Invalid server port.'),
    ('http://plex.example:99999', 'Invalid server port.'),
])
def test_malformed_server_address_returns_public_validation(client, address, message):
    response = client.post('/api/servers', json={'url': address}, headers=headers(client))
    assert response.status_code == 400
    assert response.json() == {'message': message}
    server_ui._refresh.assert_not_called()
    plexapi.plex_listener.assert_not_called()


def test_removing_server_reports_credential_store_failure_and_retains_connection(client, monkeypatch):
    save_server('one')
    monkeypatch.setattr(token_store, 'delete_token', Mock(side_effect=token_store.TokenStorageError('private key')))
    response = client.delete('/api/servers/one', headers=headers(client))
    assert response.status_code == 500
    assert response.json()['message'] == 'Could not update the secure credential store.'
    assert servers.get_server('one') is not None


@pytest.mark.parametrize('payload', ['not an object', ['list'], 123, None])
def test_server_apis_reject_scalar_json(client, payload):
    save_server('one')
    for path in ('/api/servers/discover', '/api/servers', '/api/servers/one', '/api/tasks/refresh'):
        response = client.post(path, json=payload, headers=headers(client))
        assert response.status_code == 400
        assert response.headers['content-type'] == 'application/json'


def test_server_apis_accept_json_media_type_suffixes(client):
    save_server('one')
    response = client.post('/api/servers/one', content='{"enabled": false}',
                           headers={**headers(client), 'Content-Type': 'application/vnd.themerr+json'})
    assert response.status_code == 200
    assert servers.get_server('one')['enabled'] is False


def test_disconnecting_account_pauses_servers_and_keeps_tracking(client):
    auth.set_token('account-secret')
    save_server('one')
    token_store.save_token(servers.credential_id('one'), 'server-secret')
    with storage.server_scope('one'):
        storage.save_tracking(42, 'movie', {'youtube_theme_url': 'tracked'})
    response = client.post('/api/plex/auth/disconnect', headers=headers(client))
    assert response.status_code == 200
    assert auth.get_token() == ''
    assert token_store.get_token(servers.credential_id('one')) == ''
    assert servers.get_server('one')['enabled'] is False
    with storage.server_scope('one'):
        assert storage.get_tracking(42)['youtube_theme_url'] == 'tracked'


def test_refresh_dispatch_and_task_status(client, configured, monkeypatch):
    from themerr import scheduled_tasks
    run = Mock()
    monkeypatch.setattr(scheduled_tasks, 'run_threaded', run)
    monkeypatch.setattr(server_ui, '_refresh', Mock(return_value=SimpleNamespace(job_id='dashboard-job')))
    response = client.post('/api/tasks/refresh', json={'scan': True}, headers=headers(client))
    assert response.status_code == 202
    assert response.json()['job_id'] == 'dashboard-job'
    run.assert_called_once_with(target=processing.scheduled_update, task_name='Theme scan and queue')
    server_ui._refresh.assert_called_once()
    configured['Themerr']['BOOL_THEMERR_ENABLED'] = False
    assert client.post('/api/tasks/refresh', json={'scan': True}, headers=headers(client)).status_code == 400
    assert client.get('/api/tasks').json().keys() == {'jobs', 'queue_size'}


def test_new_pages_are_usable_without_any_server_and_have_no_background_video(client):
    for path in ('/', '/servers', '/settings/', '/activity'):
        response = client.get(path)
        assert response.status_code == 200
        assert b'<video' not in response.content
        assert b'backgroundVideo' not in response.content
        assert b'Your workspace' not in response.content or b'Themerr' in response.content
    assert b'Connect a server' in client.get('/').content
    assert b'password-form' in client.get('/settings/').content


def test_activity_includes_unresolved_ids_counted_on_overview(client):
    save_server('one')
    data = snapshot('Unresolved metadata')
    data['1']['items'][0]['theme_status'] = 'unresolved'
    with storage.server_scope('one'):
        storage.replace_dashboard(data)
    assert server_ui.dashboard()[2]['attention'] == 1
    activity = client.get('/activity').content
    assert b'Unresolved metadata' in activity
    assert b'TMDB ID unavailable. Review the item metadata in Plex.' in activity
    assert b'Outdated from ThemerrDB' not in activity


@pytest.mark.parametrize(('kind', 'database_id', 'source_database', 'source_id', 'url'), [
    ('movie', '550988', None, None, 'https://www.themoviedb.org/movie/550988'),
    ('show', '1407', None, None, 'https://www.themoviedb.org/tv/1407'),
    ('collection', '10', None, None, 'https://www.themoviedb.org/collection/10'),
    ('movie', None, 'imdb', 'tt0497329', 'https://www.imdb.com/title/tt0497329/'),
    ('show', None, 'thetvdb', '71862', 'https://thetvdb.com/dereferrer/series/71862'),
    ('movie', 'bad/id', None, None, None),
])
def test_provider_links_and_scoped_plex_links(client, kind, database_id, source_database, source_id, url):
    save_server('a')
    data = snapshot('Linked title')
    data['1']['items'][0].update(
        type=kind, database_id=database_id, source_database=source_database, source_id=source_id,
    )
    with storage.server_scope('a'):
        storage.replace_dashboard(data)
    rendered = client.get('/').content
    assert b'https://app.plex.tv/desktop/#!/server/a/details?key=%2Flibrary%2Fmetadata%2F42' in rendered
    link = re.search(rb'<a class="plex-item-link"[^>]*>.*?</a>', rendered).group()
    assert b'target="_blank" rel="noopener noreferrer" title="Open in Plex: Linked title"' in link
    assert b'Plex ID: 42' in link
    if url:
        assert ('href="' + url + '" target="_blank" rel="noopener noreferrer"').encode() in rendered
    else:
        assert b'metadata-link' not in rendered


@pytest.mark.parametrize(('reason', 'action', 'visible'), [
    (None, 'edit', False), ('Video unavailable', 'edit', True),
    ('Video unavailable in your country', 'edit', False), ('Theme upload failed', 'edit', False),
    (None, 'add', True),
])
def test_contribution_actions_reflect_video_issues(client, reason, action, visible):
    save_server('a')
    data = snapshot('Theme row', 'themerr')
    data['1']['items'][0].update(issue_action=action, issue_url='https://github.com/LizardByte/ThemerrDB/issues/new')
    with storage.server_scope('a'):
        storage.replace_dashboard(data)
        storage.set_error(42, reason)
    rendered = client.get('/').content
    assert (b'class="contribute-link"' in rendered) is visible
    assert b'class="media-controls"' in rendered
    assert b'title="Movie" aria-hidden="true"' in rendered
    assert b'data-lucide="film"' in rendered
    assert b'data-lucide="play"' in rendered


def _retry_body(server_id='one', item_id='42'):
    return {
        'server_id': server_id,
        'item_id': item_id,
    }


def test_retry_button_only_shows_for_pending_and_attention_items(client):
    statuses = {
        'a': (
            'themerr',
            'complete',
        ),
        'b': (
            None,
            'pending',
        ),
        'c': (
            None,
            'failed',
        ),
        'd': (
            None,
            'missing',
        ),
        'e': (
            None,
            'unresolved',
        ),
        'f': (
            'themerr',
            'complete',
        ),
    }
    for identifier, (provider, status) in statuses.items():
        save_server(identifier)
        data = snapshot(f'Title {identifier}', provider)
        data['1']['items'][0]['theme_status'] = status
        with storage.server_scope(identifier):
            storage.replace_dashboard(data)
    with storage.server_scope('f'):
        storage.set_error(42, 'Theme is outdated')
    page = client.get('/').text
    shown = set(re.findall(r'data-retry-item data-server-id="([^"]+)" data-item-id="42"', page))
    assert shown == {
        'b',
        'c',
        'e',
        'f',
    }


def test_retry_queues_a_cached_item(client, monkeypatch):
    save_server('one')
    with storage.server_scope('one'):
        storage.replace_dashboard(snapshot('Pending'))
    enqueue = Mock(return_value=True)
    monkeypatch.setattr(processing, 'enqueue', enqueue)
    response = client.post(
        '/api/tasks/retry',
        json=_retry_body(),
        headers=headers(client),
    )
    assert response.status_code == 202
    assert response.json()['message'] == 'Retry queued. Follow progress in Activity.'
    enqueue.assert_called_once_with('42')

    enqueue.return_value = False
    response = client.post(
        '/api/tasks/retry',
        json=_retry_body(),
        headers=headers(client),
    )
    assert response.status_code == 202
    assert response.json()['message'] == 'This item is already queued.'


@pytest.mark.parametrize('body', [
    [],
    {},
    {
        'server_id': 'one',
    },
    _retry_body(item_id=''),
    _retry_body(item_id=42),
    _retry_body(server_id='missing'),
    _retry_body(item_id='99'),
])
def test_retry_rejects_invalid_or_unknown_items(client, monkeypatch, body):
    save_server('one')
    with storage.server_scope('one'):
        storage.replace_dashboard(snapshot('Pending'))
    enqueue = Mock(return_value=True)
    monkeypatch.setattr(processing, 'enqueue', enqueue)
    response = client.post('/api/tasks/retry', json=body, headers=headers(client))
    assert response.status_code == 400
    enqueue.assert_not_called()


def test_retry_respects_disabled_updates_servers_ignored_libraries_and_csrf(client, configured, monkeypatch):
    save_server('one')
    with storage.server_scope('one'):
        storage.replace_dashboard(snapshot('Pending'))
    enqueue = Mock(return_value=True)
    monkeypatch.setattr(processing, 'enqueue', enqueue)
    body = _retry_body()
    assert client.post('/api/tasks/retry', json=body).status_code == 400

    configured['Themerr']['BOOL_THEMERR_ENABLED'] = False
    assert client.post('/api/tasks/retry', json=body, headers=headers(client)).status_code == 400
    configured['Themerr']['BOOL_THEMERR_ENABLED'] = True

    with Session(storage.engine()) as session:
        session.get(servers.ServerRecord, 'one').ignored_libraries = '1'
        session.commit()
    assert client.post('/api/tasks/retry', json=body, headers=headers(client)).status_code == 400

    with Session(storage.engine()) as session:
        record = session.get(servers.ServerRecord, 'one')
        record.ignored_libraries = ''
        record.enabled = False
        session.commit()
    assert client.post('/api/tasks/retry', json=body, headers=headers(client)).status_code == 400
    enqueue.assert_not_called()

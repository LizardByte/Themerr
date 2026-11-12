"""Provider resolution and dashboard status remain independent of native clients."""

from unittest.mock import Mock

import pytest

from jellyfin import backend, metadata
from themerr import storage


@pytest.mark.parametrize('native_type, kind, database_type', [
    ('Movie', 'movie', 'movies'), ('Series', 'show', 'tv_shows'), ('BoxSet', 'collection', 'movie_collections'),
])
def test_provider_ids_map_to_shared_database_categories(monkeypatch, native_type, kind, database_type):
    lookup = Mock(return_value=True)
    monkeypatch.setattr(metadata.themerr_db, 'item_exists', lookup)
    result = metadata.resolve({'Type': native_type, 'Name': 'Example', 'ProviderIds': {'Tmdb': '42'}})
    assert result['type'] == kind
    assert result['database_type'] == database_type
    assert result['database_id'] == '42'
    assert result['issue_action'] == 'edit'
    lookup.assert_called_once_with(database_type, 'themoviedb', '42')


def test_movie_imdb_fallback_and_invalid_provider_values(monkeypatch):
    monkeypatch.setattr(metadata.tmdb, 'get_tmdb_id_from_external_id', lambda *_: None)
    monkeypatch.setattr(metadata.themerr_db, 'item_exists', lambda *_: False)
    result = metadata.resolve({'Type': 'Movie', 'Name': 'Example', 'ProductionYear': 2020,
                               'ProviderIds': {'Tmdb': '../other', 'Imdb': 'tt123'}})
    assert result['database'] == 'imdb'
    assert result['database_id'] == 'tt123'
    assert 'Example+%282020%29' in result['issue_url']
    invalid = metadata.resolve({'Type': 'Movie', 'Name': 'Example', 'ProviderIds': {'Imdb': 'bad/path'}})
    assert invalid['database_id'] is None
    assert invalid['issue_url'] is None


@pytest.mark.parametrize('native_type, provider, value', [
    ('Movie', 'Tmdb', '４２'), ('Movie', 'Imdb', 'tt٤٢'), ('Series', 'Tvdb', '٤٢'),
])
def test_provider_identifiers_do_not_accept_unicode_digits(monkeypatch, native_type, provider, value):
    monkeypatch.setattr(metadata.tmdb, 'get_tmdb_id_from_external_id', Mock(return_value=None))
    lookup = Mock()
    monkeypatch.setattr(metadata.themerr_db, 'item_exists', lookup)
    result = metadata.resolve({'Type': native_type, 'Name': 'Example', 'ProviderIds': {provider: value}})
    assert result['database_id'] is None
    assert result['source_id'] is None
    assert result['issue_url'] is None
    lookup.assert_not_called()


def test_tvdb_series_resolves_to_the_shared_tmdb_database(monkeypatch):
    resolve = Mock(return_value=1396)
    lookup = Mock(return_value=True)
    monkeypatch.setattr(metadata.tmdb, 'get_tmdb_id_from_external_id', resolve)
    monkeypatch.setattr(metadata.themerr_db, 'item_exists', lookup)
    result = metadata.resolve({'Type': 'Series', 'Name': 'Breaking Bad', 'ProviderIds': {'Tvdb': '81189'}})
    resolve.assert_called_once_with('81189', 'tvdb', 'tv', title='Breaking Bad')
    lookup.assert_called_once_with('tv_shows', 'themoviedb', '1396')
    assert (result['source_database'], result['source_id']) == ('thetvdb', '81189')
    assert (result['database'], result['database_id'], result['exists']) == ('themoviedb', '1396', True)


def test_series_with_tmdb_and_tvdb_uses_the_existing_tmdb_id(monkeypatch):
    resolve = Mock()
    monkeypatch.setattr(metadata.tmdb, 'get_tmdb_id_from_external_id', resolve)
    monkeypatch.setattr(metadata.themerr_db, 'item_exists', Mock(return_value=True))
    result = metadata.resolve({'Type': 'Series', 'Name': 'Breaking Bad',
                               'ProviderIds': {'Tvdb': '81189', 'Tmdb': '1396'}})
    assert (result['database'], result['database_id']) == ('themoviedb', '1396')
    resolve.assert_not_called()


def test_unmapped_tvdb_series_does_not_treat_a_tvdb_id_as_a_tmdb_id(monkeypatch):
    monkeypatch.setattr(metadata.tmdb, 'get_tmdb_id_from_external_id', Mock(return_value=None))
    lookup = Mock()
    monkeypatch.setattr(metadata.themerr_db, 'item_exists', lookup)
    result = metadata.resolve({'Type': 'Series', 'Name': 'Unmapped show', 'ProviderIds': {'Tvdb': '81189'}})
    assert (result['source_database'], result['source_id']) == ('thetvdb', '81189')
    assert result['database_id'] is None
    assert result['exists'] is False
    assert result['issue_url'] is None
    lookup.assert_not_called()


def test_dashboard_snapshot_uses_uuid_keys_and_owned_theme_status(configured, monkeypatch):
    connection = Mock()
    library, first, second = '1' * 32, '2' * 32, '3' * 32
    server_id = 'jellyfin:' + '4' * 32
    monkeypatch.setattr(backend.servers, 'client', lambda _: connection)
    monkeypatch.setattr(backend.connector, 'verify', lambda _: {})
    monkeypatch.setattr(metadata, 'resolve', lambda _: {
        'type': 'movie', 'database_type': 'movies', 'database': 'themoviedb', 'database_id': '42',
        'source_database': 'themoviedb', 'source_id': '42', 'exists': True,
        'issue_action': 'edit', 'issue_url': 'https://example.test',
    })
    connection.json.side_effect = [
        [{'ItemId': library, 'Name': 'Movies', 'CollectionType': 'movies'}],
        {'Items': [{'Id': first}], 'TotalRecordCount': 1},
        {'Items': [{'Id': first, 'Name': 'With theme'}, {'Id': second, 'Name': 'Pending'}], 'TotalRecordCount': 2},
        {'owned': True},
    ]
    assert backend.JellyfinMediaServer(server_id).cache_dashboard()
    with storage.server_scope(server_id):
        snapshot = storage.get_dashboard()[library]
    assert snapshot['media_count'] == 2
    assert snapshot['media_percent_complete'] == 50
    assert snapshot['items'][0]['theme_provider'] == 'themerr'
    assert snapshot['items'][1]['theme_status'] == 'pending'
    assert storage.get_dashboard() is None


def test_legacy_import_is_optional_and_only_requests_unowned_existing_themes(
        configured, connector_bundle, monkeypatch):
    from jellyfin import connector
    item = '2' * 32
    connection = Mock()
    original = {'present': True, 'owned': False, 'sha256': None}
    connection.json.return_value = original
    assert configured['Jellyfin']['IMPORT_LEGACY_OWNERSHIP'] is True
    configured['Jellyfin']['IMPORT_LEGACY_OWNERSHIP'] = False
    assert backend._theme_state(connection, item) == original
    assert connection.json.call_count == 1
    assert connection.json.call_args.args == (
        'GET',
        f'/Themerr/Items/{item}/Theme',
    )
    connection.reset_mock()
    configured['Jellyfin']['IMPORT_LEGACY_OWNERSHIP'] = True
    imported = {'present': True, 'owned': True, 'sha256': 'a' * 64}
    connection.json.side_effect = [original, imported]
    assert backend._theme_state(connection, item) == imported
    assert connection.json.call_args.args == ('POST', '/Themerr/Items/' + item + '/Theme/Import')
    assert connection.json.call_args.kwargs == {'headers': {'X-Themerr-Connector': connector.bundle()['build']}}

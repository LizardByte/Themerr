"""Collection members remain visible to Jellyfin dashboard refreshes and scans."""

# standard imports
from unittest.mock import Mock

# lib imports
import pytest

# local imports
from jellyfin import backend, metadata
from themerr import storage

LIBRARY = '1' * 32
SERVER = f'jellyfin:{"2" * 32}'
MOVIE = '3' * 32
UNTHEMED_MOVIE = '4' * 32
COLLECTION = '5' * 32


@pytest.fixture
def collection_library(configured, monkeypatch):
    items = [
        {
            'Id': MOVIE,
            'Name': 'Avatar',
            'Type': 'Movie',
            'ProviderIds': {'Tmdb': '19995'},
        },
        {
            'Id': UNTHEMED_MOVIE,
            'Name': 'Avatar: The Way of Water',
            'Type': 'Movie',
            'ProviderIds': {'Tmdb': '76600'},
        },
        {
            'Id': COLLECTION,
            'Name': 'Avatar - Saga',
            'Type': 'BoxSet',
            'ProviderIds': {'Tmdb': '87096'},
        },
    ]
    themed = {
        MOVIE,
        COLLECTION,
    }

    def response(method, path, *, params=None, error_messages=None):
        assert method == 'GET'
        if path == '/Library/VirtualFolders':
            return [{
                'ItemId': LIBRARY,
                'Name': 'Movies',
                'CollectionType': 'movies',
            }]
        if path.endswith('/Theme'):
            return {
                'present': True,
                'owned': True,
            }
        assert path == '/Items'
        assert params['ParentId'] == LIBRARY
        assert params['Recursive'] is True
        assert params['IncludeItemTypes'] == 'Movie,Series,BoxSet'
        # API-key queries hide collection members unless collapsing is explicitly disabled.
        rows = items if params.get('CollapseBoxSetItems') is False else [items[-1]]
        if params.get('HasThemeSong'):
            rows = [item for item in rows if item['Id'] in themed]
        start = params['StartIndex']
        return {
            'Items': rows[start:start + 1],
            'TotalRecordCount': len(rows),
        }

    connection = Mock()
    connection.json.side_effect = response
    monkeypatch.setattr(backend.servers, 'client', lambda _: connection)
    monkeypatch.setattr(backend.servers, 'get_server', lambda _: {'ignored_libraries': ''})
    monkeypatch.setattr(backend.connector, 'verify', lambda _: {})
    monkeypatch.setattr(metadata.themerr_db, 'item_exists', lambda *_: True)
    return connection


def test_dashboard_includes_collection_members_and_their_theme_status(collection_library):
    assert backend.JellyfinMediaServer(SERVER).cache_dashboard()
    with storage.server_scope(SERVER):
        section = storage.get_dashboard()[LIBRARY]
    assert [item['rating_key'] for item in section['items']] == [
        MOVIE,
        UNTHEMED_MOVIE,
        COLLECTION,
    ]
    assert [item['theme_status'] for item in section['items']] == [
        'complete',
        'pending',
        'complete',
    ]
    assert [item['theme_provider'] for item in section['items']] == [
        'themerr',
        None,
        'themerr',
    ]
    assert section['media_count'] == 2
    assert section['media_percent_complete'] == 50
    assert section['collection_count'] == 1
    assert section['collection_percent_complete'] == 100
    assert section['total_count'] == 3


@pytest.mark.parametrize('collections_enabled', [
    True,
    False,
])
def test_scan_enqueues_collection_members_independently_of_collection_support(
        collection_library, configured, collections_enabled):
    configured['Jellyfin']['BOOL_COLLECTION_SUPPORT'] = collections_enabled
    queued = []
    backend.JellyfinMediaServer(SERVER).scan(queued.append)
    expected = [
        MOVIE,
        UNTHEMED_MOVIE,
    ]
    if collections_enabled:
        expected.append(COLLECTION)
    assert queued == expected

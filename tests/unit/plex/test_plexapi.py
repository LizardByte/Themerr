"""Plex-facing behavior is verified with controlled server and item doubles."""

# standard imports
from contextlib import nullcontext
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

# lib imports
import pytest
import requests

# local imports
from common import config
from plex import plexapi
from media_servers import processing
from youtube.youtube_dl import AudioFile


def audio_result(path, codec, available):
    """Provide a disposable validated download context for Plex boundary tests."""
    return nullcontext(AudioFile(path, codec, available, 'verified-digest', 60.0, 100))


@pytest.fixture(autouse=True)
def verified_uploads(request, monkeypatch):
    """Plex item doubles do not serve audio; verification is tested independently."""
    if 'item' in request.fixturenames:
        monkeypatch.setattr(plexapi, '_verify_uploaded_theme', Mock())


def test_setup_plexapi(configured, monkeypatch):
    monkeypatch.setattr(plexapi, 'plex_server', None)
    monkeypatch.setattr(plexapi.auth, 'get_token', lambda: '')
    assert plexapi.setup_plexapi() is None

    monkeypatch.setattr(plexapi.auth, 'get_token', lambda: 'token')
    configured['Plex']['PLEX_URL'] = 'https://plex.example'
    server = object()
    constructor = Mock(return_value=server)
    monkeypatch.setattr(plexapi.plexapi.server, 'PlexServer', constructor)
    assert plexapi.setup_plexapi() is server
    assert plexapi.setup_plexapi() is server
    assert constructor.call_count == 1
    assert constructor.call_args.kwargs['session'].verify is True


def test_listener_starts_after_login_and_stops_on_disconnect(monkeypatch):
    monkeypatch.setattr(plexapi, 'alert_listener', None)
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: object())
    monkeypatch.setattr(plexapi.servers, 'list_servers', lambda **kwargs: [{'id': 'one'}])
    listener = Mock()
    listener._ws = object()
    constructor = Mock(return_value=listener)
    monkeypatch.setattr(plexapi, 'AlertListener', constructor)
    plexapi.plex_listener()
    listener.start.assert_called_once_with()
    assert plexapi._alert_listeners['one'] is listener
    plexapi.stop_plex_listener()
    listener.stop.assert_called_once_with()
    assert plexapi.alert_listener is None


def test_listener_does_not_start_without_connection(monkeypatch):
    monkeypatch.setattr(plexapi, 'alert_listener', None)
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: None)
    monkeypatch.setattr(plexapi.servers, 'list_servers', lambda **_: [])
    constructor = Mock()
    monkeypatch.setattr(plexapi, 'AlertListener', constructor)
    plexapi.plex_listener()
    constructor.assert_not_called()


def test_changing_plex_url_reconnects_active_listener(monkeypatch):
    listener, stop = Mock(), Mock()
    monkeypatch.setattr(plexapi, 'plex_listener', listener)
    monkeypatch.setattr(plexapi, 'stop_plex_listener', stop)
    monkeypatch.setattr(plexapi.auth, 'get_token', lambda: 'issued-token')
    monkeypatch.setattr(plexapi, 'plex_server', object())
    config.on_change_plex_url()
    stop.assert_called_once_with()
    listener.assert_called_once_with()
    assert plexapi.plex_server is None


def test_get_database_info(configured, item, monkeypatch):
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: object())
    monkeypatch.setattr(plexapi.servers, 'list_servers', lambda **kwargs: [{'id': 'one'}])
    item.guids = [SimpleNamespace(id='imdb://tt42'), SimpleNamespace(id='tmdb://123')]
    assert plexapi.get_database_info(item) == (
        'movies', 'themoviedb', 'tv.plex.agents.movie', '123',
    )
    item.guids = []
    assert plexapi.get_database_info(item) == (None, None, None, None)
    item.type = 'show'
    item.guids = [SimpleNamespace(id='tmdb://456')]
    assert plexapi.get_database_info(item) == (
        'tv_shows', 'themoviedb', 'tv.plex.agents.series', '456',
    )
    monkeypatch.setattr(plexapi.tmdb, 'get_tmdb_id_from_external_id', lambda **_: '789')
    item.guids = [SimpleNamespace(id='imdb://tt42')]
    assert plexapi.get_database_info(item) == (
        'tv_shows', 'themoviedb', 'tv.plex.agents.series', '789',
    )

    section = SimpleNamespace(agent='tv.plex.agents.movie', language='en', type='movie')
    server = SimpleNamespace(library=SimpleNamespace(sectionByID=lambda _: section))
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: server)
    monkeypatch.setattr(plexapi.tmdb, 'get_tmdb_id_from_collection', lambda **_: '99')
    item.type = 'collection'
    assert plexapi.get_database_info(item) == (
        'movie_collections', 'themoviedb', 'tv.plex.agents.movie', '99',
    )


def test_legacy_guid_without_guids(configured, item, monkeypatch):
    """Plex can supply only a legacy primary GUID for migrated movies and shows."""
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: object())
    monkeypatch.setattr(plexapi.servers, 'list_servers', lambda **kwargs: [{'id': 'one'}])
    item.guids = []
    item.guid = 'com.plexapp.agents.imdb://tt0298203?lang=en'
    assert plexapi.get_database_info(item) == (
        'movies', 'imdb', 'tv.plex.agents.movie', 'tt0298203',
    )
    assert plexapi.get_external_id(item) == ('imdb', 'tt0298203')

    item.type = 'show'
    item.guid = 'com.plexapp.agents.thetvdb://12345?lang=en'
    monkeypatch.setattr(plexapi.tmdb, 'get_tmdb_id_from_external_id', lambda **kwargs: '789'
                        if kwargs['database'] == 'tvdb' and kwargs['external_id'] == '12345' else None)
    assert plexapi.get_database_info(item) == (
        'tv_shows', 'themoviedb', 'tv.plex.agents.series', '789',
    )
    assert plexapi.get_external_id(item) == ('thetvdb', '12345')


def test_collection_id_from_matching_member(configured, item, monkeypatch):
    item.type = 'collection'
    item.title = 'Batman'
    member = SimpleNamespace(type='movie', guid='com.plexapp.agents.imdb://tt0096895', guids=[])
    item.items = lambda: [member]
    section = SimpleNamespace(agent='tv.plex.agents.movie', type='movie', language='en')
    server = SimpleNamespace(library=SimpleNamespace(sectionByID=lambda _: section))
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: server)
    monkeypatch.setattr(plexapi.tmdb, 'get_tmdb_id_from_collection', lambda **_: None)
    monkeypatch.setattr(plexapi.themerr_db, 'item_exists', lambda **_: True)
    database_request = Mock(return_value={
        'belongs_to_collection': {'id': 120794, 'name': 'Batman Collection'},
    })
    monkeypatch.setattr(plexapi.helpers, 'json_get', database_request)

    assert plexapi.get_database_info(item) == (
        'movie_collections', 'themoviedb', 'tv.plex.agents.movie', '120794',
    )
    assert database_request.call_args.kwargs['url'] == (
        'https://themerr.lizardbyte.dev/ThemerrDB/movies/imdb/tt0096895.json'
    )
    item.title = 'Unrelated collection'
    assert plexapi.get_database_info(item)[-1] is None


def test_theme_failure_is_saved_and_cleared(configured, item, monkeypatch):
    configured['Themerr']['BOOL_OVERWRITE_PLEX_PROVIDED_THEMES'] = True
    monkeypatch.setattr(plexapi.general, 'get_themerr_data', lambda **_: {})
    saved = {}
    monkeypatch.setattr(plexapi.theme_errors, 'set_error', lambda key, reason: saved.update({key: reason}))

    def fail_extract(**kwargs):
        kwargs['on_error']('Video unavailable')
        return nullcontext(None)

    monkeypatch.setattr(plexapi, 'download_youtube', fail_extract)
    plexapi._update_theme(item, {'youtube_theme_url': 'https://youtube.example/theme'})
    assert saved[item.ratingKey] == 'Video unavailable'

    monkeypatch.setattr(plexapi, 'download_youtube',
                        lambda **_: audio_result('https://audio.example/theme', 'mp4a', True))
    monkeypatch.setattr(plexapi, 'add_media', Mock(return_value=True))
    plexapi._update_theme(item, {'youtube_theme_url': 'https://youtube.example/theme'})
    assert saved[item.ratingKey] is None


def test_get_plex_item(monkeypatch):
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: None)
    assert plexapi.get_plex_item(1) is None
    server = SimpleNamespace(fetchItem=Mock(return_value='found'))
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: server)
    assert plexapi.get_plex_item(1) == 'found'
    server.fetchItem.assert_called_once_with(ekey=1)


def test_add_media_success(configured, item, monkeypatch):
    configured['Themerr']['BOOL_IGNORE_LOCKED_FIELDS'] = False
    configured['Themerr']['BOOL_REMOVE_UNUSED_THEMES'] = True
    monkeypatch.setattr(plexapi.general, 'get_themerr_data', lambda **_: {})
    removed, saved, locked = Mock(), Mock(), Mock()
    monkeypatch.setattr(plexapi.general, 'remove_uploaded_media', removed)
    monkeypatch.setattr(plexapi.general, 'update_themerr_data', saved)
    monkeypatch.setattr(plexapi, 'change_lock_status', locked)
    monkeypatch.setattr(plexapi, 'upload_media', Mock(return_value=True))

    assert plexapi.add_media(item, 'themes', 'id', media_url='https://audio.example')
    removed.assert_called_once_with(item=item, media_type='themes')
    assert saved.call_args.kwargs['new_themerr_data'] == {
        'youtube_theme_url': 'id', 'audio_codec': None, 'mp4a_available': None, 'uploaded_theme_key': None,
    }
    locked.assert_called_once_with(item=item, field='theme', lock=False)


def test_successful_upload_records_selected_theme_key(configured, item, monkeypatch):
    configured['Themerr']['BOOL_REMOVE_UNUSED_THEMES'] = False
    item.themes.return_value = [SimpleNamespace(selected=True, provider=None,
                                                ratingKey='upload://themes/new-theme')]
    monkeypatch.setattr(plexapi, 'upload_media', Mock(return_value=True))
    monkeypatch.setattr(plexapi, 'change_lock_status', Mock(return_value=True))
    dashboard_update = Mock()
    monkeypatch.setattr(plexapi.storage, 'mark_dashboard_theme_uploaded', dashboard_update)

    assert plexapi.add_media(item, 'themes', 'https://youtube.example/video',
                             media_url='https://audio.example/stream', audio_codec='mp4a', mp4a_available=True)
    assert plexapi.general.get_themerr_data(item)['uploaded_theme_key'] == 'upload://themes/new-theme'
    assert plexapi.general.get_themerr_data(item)['audio_codec'] == 'mp4a'
    assert plexapi.general.get_themerr_data(item)['mp4a_available'] is True
    assert plexapi.general.get_theme_provider(item) == 'themerr'
    dashboard_update.assert_called_once_with(42, 'themerr')


def test_unknown_upload_is_replaced_even_with_old_tracking(configured, item, monkeypatch):
    configured['Themerr']['BOOL_OVERWRITE_PLEX_PROVIDED_THEMES'] = True
    item.themes.return_value = [SimpleNamespace(selected=True, provider=None,
                                                ratingKey='upload://themes/old-upload')]
    monkeypatch.setattr(plexapi.general, 'get_themerr_data', lambda **_: {
        'youtube_theme_url': 'https://youtube.example/video',
    })
    monkeypatch.setattr(plexapi, 'download_youtube',
                        lambda **_: audio_result('https://audio.example/stream', 'mp4a', True))
    upload = Mock(return_value=True)
    monkeypatch.setattr(plexapi, 'add_media', upload)

    plexapi._update_theme(item, {'youtube_theme_url': 'https://youtube.example/video'})
    upload.assert_called_once()


def test_add_media_skips_locked_or_unchanged(configured, item, monkeypatch):
    configured['Themerr']['BOOL_IGNORE_LOCKED_FIELDS'] = False
    monkeypatch.setattr(plexapi.general, 'get_themerr_data', lambda **_: {
        'youtube_theme_url': 'id', 'uploaded_theme_key': 'upload://themes/42',
    })
    item.themes.return_value = [SimpleNamespace(selected=True, provider=None, ratingKey='upload://themes/42')]
    upload = Mock()
    monkeypatch.setattr(plexapi, 'upload_media', upload)
    item.isLocked.return_value = True
    assert not plexapi.add_media(item, 'themes', 'id', media_url='https://audio.example')
    item.isLocked.return_value = False
    assert plexapi.add_media(item, 'themes', 'id', media_url='https://audio.example')
    upload.assert_not_called()


def _track_theme(item, codec, mp4a_available):
    """Persist one selected Themerr upload with its source and codec information."""
    item.theme = 'existing theme'
    item.themes.return_value = [SimpleNamespace(selected=True, provider=None, ratingKey='upload://themes/tracked')]
    plexapi.general.update_themerr_data(item, {
        'youtube_theme_url': 'https://youtube.example/theme', 'uploaded_theme_key': 'upload://themes/tracked',
        'audio_codec': codec, 'mp4a_available': mp4a_available, 'audio_sha256': 'verified-digest',
    })


@pytest.mark.parametrize('setting,value', [
    ('INT_PLEXAPI_PLEXAPI_TIMEOUT', 1200),
    ('INT_PLEXAPI_UPLOAD_RETRIES_MAX', 9),
    ('INT_UPDATE_THEMES_INTERVAL', 15),
    ('INT_UPDATE_DATABASE_CACHE_INTERVAL', 15),
    ('BOOL_REMOVE_UNUSED_THEMES', False),
    ('BOOL_IGNORE_LOCKED_FIELDS', True),
    ('BOOL_UPDATE_COLLECTION_METADATA', True),
    ('IGNORED_LIBRARY_IDS', '99'),
    ('STR_YOUTUBE_COOKIES', '[]'),
])
def test_operational_settings_do_not_reupload_theme(configured, item, monkeypatch, setting, value):
    _track_theme(item, 'mp4a', True)
    configured['Themerr'][setting] = value
    extract = Mock()
    monkeypatch.setattr(plexapi, 'download_youtube', extract)

    plexapi._update_theme(item, {'youtube_theme_url': 'https://youtube.example/theme'})

    extract.assert_not_called()
    item.uploadTheme.assert_not_called()


@pytest.mark.parametrize('prefer_mp4a,codec,available', [
    (True, 'mp4a', True),
    (False, 'mp4a', True),
    (False, 'opus', True),
    (True, 'opus', False),
])
def test_satisfied_codec_preference_skips_repeated_jobs(
        configured, item, monkeypatch, prefer_mp4a, codec, available):
    _track_theme(item, codec, available)
    configured['Themerr']['BOOL_PREFER_MP4A_CODEC'] = prefer_mp4a
    extract = Mock()
    monkeypatch.setattr(plexapi, 'download_youtube', extract)

    for _ in range(2):
        plexapi._update_theme(item, {'youtube_theme_url': 'https://youtube.example/theme'})

    extract.assert_not_called()
    item.uploadTheme.assert_not_called()


@pytest.mark.parametrize('codec,available', [('opus', True), (None, None)])
def test_aac_preference_uploads_once_and_survives_restart(configured, item, monkeypatch, codec, available):
    _track_theme(item, codec, available)
    configured['Themerr']['BOOL_REMOVE_UNUSED_THEMES'] = False
    configured['Themerr']['BOOL_PREFER_MP4A_CODEC'] = True
    extract = Mock(return_value=audio_result('https://audio.example/aac', 'mp4a', True))
    monkeypatch.setattr(plexapi, 'download_youtube', extract)
    monkeypatch.setattr(plexapi, 'change_lock_status', Mock())

    plexapi._update_theme(item, {'youtube_theme_url': 'https://youtube.example/theme'})
    assert plexapi.general.get_themerr_data(item)['audio_codec'] == 'mp4a'
    plexapi.storage.close()
    plexapi._update_theme(item, {'youtube_theme_url': 'https://youtube.example/theme'})
    configured['Themerr']['BOOL_PREFER_MP4A_CODEC'] = False
    plexapi._update_theme(item, {'youtube_theme_url': 'https://youtube.example/theme'})

    extract.assert_called_once()
    item.uploadTheme.assert_called_once()


@pytest.mark.parametrize('codec', ['opus', None])
def test_aac_unavailable_keeps_existing_theme_without_repeated_extraction(configured, item, monkeypatch, codec):
    _track_theme(item, codec, True if codec else None)
    configured['Themerr']['BOOL_PREFER_MP4A_CODEC'] = True
    extract = Mock(return_value=audio_result('https://audio.example/opus', 'opus', False))
    monkeypatch.setattr(plexapi, 'download_youtube', extract)
    plexapi.theme_errors.set_error(item.ratingKey, 'Previous failure')

    for _ in range(2):
        plexapi._update_theme(item, {'youtube_theme_url': 'https://youtube.example/theme'})

    extract.assert_called_once()
    item.uploadTheme.assert_not_called()
    assert plexapi.general.get_themerr_data(item)['mp4a_available'] is False
    assert plexapi.general.get_themerr_data(item).get('audio_codec') == codec
    assert plexapi.theme_errors.get_errors() == {}


def test_failed_codec_replacement_keeps_original_tracking_and_retries(configured, item, monkeypatch):
    _track_theme(item, 'opus', True)
    configured['Themerr']['BOOL_REMOVE_UNUSED_THEMES'] = False
    configured['Themerr']['BOOL_PREFER_MP4A_CODEC'] = True
    configured['Themerr']['INT_PLEXAPI_UPLOAD_RETRIES_MAX'] = 0
    monkeypatch.setattr(plexapi, 'download_youtube',
                        lambda **_: audio_result('theme.m4a', 'mp4a', True))
    item.uploadTheme.side_effect = requests.HTTPError('406 rejected')

    for _ in range(2):
        plexapi._update_theme(item, {'youtube_theme_url': 'https://youtube.example/theme'})

    assert item.uploadTheme.call_count == 2
    assert plexapi.general.get_themerr_data(item)['audio_codec'] == 'opus'
    assert plexapi.theme_errors.get_errors() == {'42': '406 rejected'}


def test_changed_source_replaces_same_codec(configured, item, monkeypatch):
    _track_theme(item, 'mp4a', True)
    configured['Themerr']['BOOL_REMOVE_UNUSED_THEMES'] = False
    monkeypatch.setattr(plexapi, 'download_youtube',
                        lambda **_: audio_result('theme.m4a', 'mp4a', True))
    monkeypatch.setattr(plexapi, 'change_lock_status', Mock())

    plexapi._update_theme(item, {'youtube_theme_url': 'https://youtube.example/new-theme'})

    item.uploadTheme.assert_called_once()
    assert plexapi.general.get_themerr_data(item)['youtube_theme_url'] == 'https://youtube.example/new-theme'


@pytest.mark.parametrize('media_type,field', [('art', 'art_url'), ('posters', 'poster_url')])
def test_codec_preference_does_not_reupload_collection_artwork(configured, item, media_type, field):
    plexapi.general.update_themerr_data(item, {field: 'unchanged-image'})
    for prefer in (True, False):
        configured['Themerr']['BOOL_PREFER_MP4A_CODEC'] = prefer
        assert plexapi.add_media(item, media_type, 'unchanged-image', media_url='https://image.example')
    item.uploadArt.assert_not_called()
    item.uploadPoster.assert_not_called()


def test_upload_media(configured, item, monkeypatch):
    configured['Themerr']['INT_PLEXAPI_UPLOAD_RETRIES_MAX'] = 1
    configured['Themerr']['INT_PLEXAPI_PLEXAPI_TIMEOUT'] = 7
    assert plexapi.upload_media(item, item.uploadTheme, url='https://audio.example')
    item.uploadTheme.assert_called_once_with(url='https://audio.example', timeout=7)
    assert plexapi.upload_media(item, item.uploadArt, filepath='poster.jpg')
    item.uploadArt.assert_called_once_with(filepath='poster.jpg')

    monkeypatch.setattr(plexapi, 'BadRequest', RuntimeError)
    monkeypatch.setattr(plexapi.time, 'sleep', Mock())
    item.uploadPoster.side_effect = RuntimeError('retry')
    errors = []
    assert not plexapi.upload_media(item, item.uploadPoster, url='https://poster.example',
                                    on_error=errors.append)
    assert item.uploadPoster.call_count == 2
    assert errors == ['retry']


def test_theme_upload_logs_context_and_redacts_signed_url(configured, item, monkeypatch):
    configured['Themerr']['INT_PLEXAPI_UPLOAD_RETRIES_MAX'] = 1
    item.librarySectionTitle = 'Movies'
    item.uploadTheme.side_effect = [requests.HTTPError('406 for https://audio.example/?sig=secret'), None]
    monkeypatch.setattr(plexapi.time, 'sleep', Mock())
    info, error = Mock(), Mock()
    monkeypatch.setattr(plexapi.log, 'info', info)
    monkeypatch.setattr(plexapi.log, 'error', error)

    assert plexapi.upload_media(item, item.uploadTheme, url='https://audio.example/stream')
    messages = [call.args[0] % call.args[1:] for call in info.call_args_list]
    assert any('Submitting theme to Plex' in message and
               "rating_key=42 library_id=1 library_name='Movies' item='Example'" in message and
               'attempt 1/2' in message for message in messages)
    assert any('Plex accepted theme upload' in message for message in messages)
    errors = [call.args[0] % call.args[1:] for call in error.call_args_list]
    assert len(errors) == 1
    assert '406' in errors[0]
    assert 'sig=secret' not in errors[0]


def test_change_lock_status(configured, item, monkeypatch):
    item.isLocked.side_effect = [False, True]
    assert plexapi.change_lock_status(item, 'theme', True)
    item.edit.assert_called_once_with(**{'theme.locked': 1})
    item.reload.assert_called_once_with(theme=True)

    item.isLocked.side_effect = None
    item.isLocked.return_value = True
    assert plexapi.change_lock_status(item, 'theme', True)
    assert item.edit.call_count == 1


def test_change_lock_status_timeout(item, monkeypatch):
    item.isLocked.side_effect = [False, False]
    item.edit.side_effect = requests.ReadTimeout()
    monkeypatch.setattr(plexapi.time, 'sleep', Mock())
    assert not plexapi.change_lock_status(item, 'theme', True)
    assert item.edit.call_count == 3


def test_update_missing_or_not_in_db(configured, item, monkeypatch):
    monkeypatch.setattr(plexapi, 'get_plex_item', lambda **_: None)
    assert plexapi.update_plex_item(42) is False
    monkeypatch.setattr(plexapi, 'get_plex_item', lambda **_: item)
    monkeypatch.setattr(plexapi, 'get_database_info', lambda **_: (
        'movies', 'themoviedb', 'tv.plex.agents.movie', '42',
    ))
    monkeypatch.setattr(plexapi.themerr_db, 'item_exists', lambda **_: False)
    assert plexapi.update_plex_item(42) is False


def test_update_collection_metadata_and_theme(configured, item, monkeypatch):
    item.type = 'collection'
    item.summary = 'Old summary'
    item.editSummary = Mock()
    configured['Themerr']['BOOL_UPDATE_COLLECTION_METADATA'] = True
    configured['Themerr']['BOOL_OVERWRITE_PLEX_PROVIDED_THEMES'] = True
    monkeypatch.setattr(plexapi, 'get_plex_item', lambda **_: item)
    monkeypatch.setattr(plexapi, 'get_database_info', lambda **_: (
        'movie_collections', 'themoviedb', 'tv.plex.agents.movie', '99',
    ))
    monkeypatch.setattr(plexapi.themerr_db, 'item_exists', lambda **_: True)
    database_request = Mock(return_value={
        'poster_path': '/poster.jpg',
        'backdrop_path': '/backdrop.jpg',
        'overview': 'New summary',
        'youtube_theme_url': 'https://youtube.example/theme',
    })
    monkeypatch.setattr(plexapi.helpers, 'json_get', database_request)
    monkeypatch.setattr(plexapi.general, 'get_themerr_data', lambda **_: {})
    monkeypatch.setattr(plexapi, 'download_youtube',
                        lambda **_: audio_result('https://audio.example/theme', 'mp4a', True))
    add = Mock(return_value=True)
    monkeypatch.setattr(plexapi, 'add_media', add)

    plexapi.update_plex_item(42)
    assert database_request.call_args.kwargs['url'] == (
        'https://themerr.lizardbyte.dev/ThemerrDB/movie_collections/themoviedb/99.json'
    )
    assert [call.kwargs['media_type'] for call in add.call_args_list] == ['posters', 'art', 'themes']
    item.editSummary.assert_called_once_with(summary='New summary', locked=False)


def test_update_preserves_locked_and_plex_provided_themes(configured, item, monkeypatch):
    monkeypatch.setattr(plexapi, 'get_plex_item', lambda **_: item)
    monkeypatch.setattr(plexapi, 'get_database_info', lambda **_: (
        'movies', 'themoviedb', 'tv.plex.agents.movie', '42',
    ))
    monkeypatch.setattr(plexapi.themerr_db, 'item_exists', lambda **_: True)
    monkeypatch.setattr(plexapi.helpers, 'json_get', lambda **_: {'youtube_theme_url': 'https://youtube.example'})
    add = Mock()
    monkeypatch.setattr(plexapi, 'add_media', add)
    monkeypatch.setattr(plexapi.general, 'get_theme_provider', lambda **_: 'plex')

    item.isLocked.return_value = True
    assert plexapi.update_plex_item(42)
    add.assert_not_called()

    item.isLocked.return_value = False
    configured['Themerr']['BOOL_OVERWRITE_PLEX_PROVIDED_THEMES'] = False
    assert plexapi.update_plex_item(42)
    add.assert_not_called()


def test_update_handles_themerrdb_failure(configured, item, monkeypatch):
    monkeypatch.setattr(plexapi, 'get_plex_item', lambda **_: item)
    monkeypatch.setattr(plexapi, 'get_database_info', lambda **_: (
        'movies', 'themoviedb', 'tv.plex.agents.movie', '42',
    ))
    monkeypatch.setattr(plexapi.themerr_db, 'item_exists', lambda **_: True)
    monkeypatch.setattr(plexapi.helpers, 'json_get', Mock(side_effect=RuntimeError('unavailable')))

    assert plexapi.update_plex_item(42) is False


def test_listener_handler(configured, monkeypatch):
    monkeypatch.setattr(processing, 'q', Queue())
    configured['Themerr']['BOOL_PLEX_MOVIE_SUPPORT'] = True
    entry = {'type': 1, 'state': 5, 'identifier': 'com.plexapp.plugins.library', 'itemID': '42'}
    plexapi.plex_listener_handler({'type': 'timeline', 'TimelineEntry': [entry, entry]})
    assert processing.q.qsize() == 1
    assert processing.q.get_nowait() == ('default', '42')


def test_scheduled_update(configured, item, monkeypatch):
    configured['Themerr']['BOOL_PLEX_MOVIE_SUPPORT'] = True
    configured['Themerr']['BOOL_PLEX_COLLECTION_SUPPORT'] = False
    section = SimpleNamespace(agent='tv.plex.agents.movie', type='movie', key=1, title='Movies')
    section.all = Mock(return_value=[item])
    section.collections = Mock(return_value=[])
    library = SimpleNamespace(sections=lambda: [section])
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: SimpleNamespace(library=library))
    monkeypatch.setattr(plexapi.themerr_db, 'update_cache', Mock())
    monkeypatch.setattr(processing, 'q', Queue())
    plexapi.scan_items(processing.enqueue)
    assert processing.q.get_nowait() == ('default', '42')
    assert "library_id=1 library_name='Movies' item='Example'" in plexapi._item_log_context(item)


def test_scheduled_update_ignores_configured_library(configured, item, monkeypatch):
    configured['Themerr']['IGNORED_LIBRARY_IDS'] = '2, 3'
    section = SimpleNamespace(agent='tv.plex.agents.movie', type='movie', key=2, title='Movies')
    section.all = Mock(return_value=[item])
    library = SimpleNamespace(sections=lambda: [section])
    monkeypatch.setattr(plexapi, 'setup_plexapi', lambda: SimpleNamespace(library=library))
    monkeypatch.setattr(plexapi.themerr_db, 'update_cache', Mock())
    monkeypatch.setattr(processing, 'q', Queue())

    plexapi.scan_items(processing.enqueue)

    section.all.assert_not_called()
    assert processing.q.empty()

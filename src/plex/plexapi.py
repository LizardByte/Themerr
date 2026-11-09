# standard imports
import hashlib
import os
import time
import threading
from typing import Callable, Optional, Tuple

# lib imports
import requests
from plexapi.alert import AlertListener
from plexapi.base import PlexPartialObject
from plexapi.exceptions import BadRequest
import plexapi.server
from plexapi.utils import reverseSearchType

# local imports
from common import config
from common import definitions
from common import helpers
from common import logger
from plex import auth, servers
from media_servers import processing
from plex.constants import contributes_to, guid_map, media_type_dict
from plex import media as general
from themerr import storage
from themerr import theme_errors
from themerr import themerr_db
from themerr import tmdb
from youtube.youtube_dl import download_youtube

# fix random _strptime import bug in plexapi
import _strptime  # noqa: F401

log = logger.get_logger(__name__)
MOVIE_AGENT = 'tv.plex.agents.movie'
SERIES_AGENT = 'tv.plex.agents.series'
PLEX_SETUP_ERROR = 'Unable to setup plex server, cannot proceed. Ensure Plex is properly configured in the settings.'

plex_server = None
alert_listener = None
_alert_listeners = {}
_listener_lock = threading.RLock()
_library_names: dict[tuple[str, str], str] = {}

# disable auto-reload, because Themerr doesn't rely on it, so it will only slow down the app
# when accessing a missing field
os.environ["PLEXAPI_PLEXAPI_AUTORELOAD"] = "false"


def _item_log_context(item: PlexPartialObject) -> str:
    """Describe an item for progress logs without including media URLs.

    Parameters
    ----------
    item : PlexPartialObject
        Plex item being processed.

    Returns
    -------
    str
        Rating key, library ID and name, and item title.
    """
    library_id = getattr(item, 'librarySectionID', None)
    library_name = (getattr(item, 'librarySectionTitle', None) or
                    _library_names.get((storage.current_server_id(), str(library_id)), 'unknown'))
    return (f'rating_key={item.ratingKey} library_id={library_id} '
            f'library_name={library_name!r} item={item.title!r} server_id={storage.current_server_id()}')


def setup_plexapi() -> Optional[plexapi.server.PlexServer]:
    """
    Create the Plex server object.

    It is required to use PlexAPI in order to add theme music to movies, as the built-in methods for metadata agents
    do not work for movies. This method creates the server object.

    Returns
    -------
    PlexServer
        The PlexServer object.

    Examples
    --------
    >>> setup_plexapi()
    ...
    """
    global plex_server
    server_id = storage.current_server_id()
    if server_id != 'default':
        return servers.connect(server_id)
    plex_token = auth.get_token()
    plex_url = config.CONFIG['Plex']['PLEX_URL']
    if not plex_server:
        if not plex_token:
            log.error('Sign in with Plex in Settings before connecting to the server.')
            return None

        plex_server = connect_plex_server(plex_url=plex_url, plex_token=plex_token)

    return plex_server


def connect_plex_server(plex_url: str, plex_token: str) -> plexapi.server.PlexServer:
    """Connect to a Plex server with a token obtained through Plex sign-in.

    Parameters
    ----------
    plex_url : str
        URL of the selected Plex server.
    plex_token : str
        Plex account token.

    Returns
    -------
    PlexServer
        The connected server.
    """
    sess = requests.Session()
    return plexapi.server.PlexServer(baseurl=plex_url, token=plex_token, session=sess, timeout=10)


def _update_collection_summary(item: PlexPartialObject, data: dict) -> None:
    """Apply a collection summary when it is available and editable.

    Parameters
    ----------
    item : PlexPartialObject
        Collection to update.
    data : dict
        ThemerrDB metadata.
    """
    if item.isLocked(field='summary') and not config.CONFIG['Themerr']['BOOL_IGNORE_LOCKED_FIELDS']:
        log.debug(f'Not overwriting locked summary for collection: {item.title}')
        return
    if 'overview' not in data or item.summary == data['overview']:
        return
    log.info(f'Updating summary for collection: {item.title}')
    try:
        item.editSummary(summary=data['overview'], locked=False)
    except Exception:
        log.exception(f'{item.ratingKey}: Error updating summary')


def _update_collection_metadata(item: PlexPartialObject, agent: str, data: dict) -> None:
    """Update supported collection artwork and summary.

    Parameters
    ----------
    item : PlexPartialObject
        Collection to update.
    agent : str
        Metadata agent identifier.
    data : dict
        ThemerrDB metadata.
    """
    if item.type != 'collection' or agent != MOVIE_AGENT or not config.CONFIG['Themerr'][
            'BOOL_UPDATE_COLLECTION_METADATA']:
        return

    for field, media_type in (('poster_path', 'posters'), ('backdrop_path', 'art')):
        if field in data:
            media_path = data[field]
            add_media(item=item, media_type=media_type, media_url_id=media_path,
                      media_url=f'https://image.tmdb.org/t/p/original{media_path}')
    _update_collection_summary(item, data)


def _update_theme(item: PlexPartialObject, data: dict) -> None:
    """Update an item theme when Plex and Themerr settings permit it.

    Parameters
    ----------
    item : PlexPartialObject
        Plex item to update.
    data : dict
        ThemerrDB metadata.
    """
    if item.isLocked(field='theme') and not config.CONFIG['Themerr']['BOOL_IGNORE_LOCKED_FIELDS']:
        log.debug(f'Not overwriting locked theme for {item.type}: {item.title}')
        theme_errors.set_error(item.ratingKey, None)
        return
    theme_provider = general.get_theme_provider(item=item)
    if (not config.CONFIG['Themerr']['BOOL_OVERWRITE_PLEX_PROVIDED_THEMES'] and theme_provider == 'plex'):
        log.debug(f'Not overwriting Plex provided theme for {item.type}: {item.title}')
        theme_errors.set_error(item.ratingKey, None)
        return
    if 'youtube_theme_url' not in data:
        log.info(f'{item.ratingKey}: No theme song found for {item.title} ({item.year})')
        theme_errors.set_error(item.ratingKey, None)
        return

    yt_video_url = data['youtube_theme_url']
    themerr_data = general.get_themerr_data(item=item)
    same_theme = themerr_data.get('youtube_theme_url') == yt_video_url and theme_provider == 'themerr'
    skip = same_theme and bool(themerr_data.get('audio_sha256')) and (
        not config.CONFIG['Themerr']['BOOL_PREFER_MP4A_CODEC'] or
        themerr_data.get('audio_codec') == 'mp4a' or themerr_data.get('mp4a_available') is False
    )
    if skip:
        log.info(f'Skipping {media_type_dict["themes"]["name"]} for '
                 f'type: {item.type}, title: {item.title}, rating_key: {item.ratingKey}')
        theme_errors.set_error(item.ratingKey, None)
        return

    context = _item_log_context(item)

    def report_extraction_error(reason: str) -> None:
        theme_errors.set_error(item.ratingKey, reason)
        log.warning('Theme audio extraction failed for %s: %s', context, reason)

    log.info('Downloading complete theme audio for %s', context)
    try:
        with download_youtube(url=yt_video_url, on_error=report_extraction_error) as audio:
            if audio:
                log.info('Theme audio verified for %s (codec=%s, duration=%.2fs, bytes=%d)',
                         context, audio.codec, audio.duration, audio.size)
                if add_media(item=item, media_type='themes', media_url_id=yt_video_url, media_file=audio.path,
                             audio_codec=audio.codec, mp4a_available=audio.mp4a_available, audio_sha256=audio.sha256,
                             on_error=lambda reason: theme_errors.set_error(item.ratingKey, reason)):
                    theme_errors.set_error(item.ratingKey, None)
                elif str(item.ratingKey) not in theme_errors.get_errors():
                    theme_errors.set_error(item.ratingKey, 'Theme upload failed')
    except Exception:
        log.exception('Error processing YouTube theme for %s', context)
        theme_errors.set_error(item.ratingKey, 'Video processing failed')
        return


def update_plex_item(rating_key: int) -> bool:
    """
    Automated update of Plex item using only the rating key.

    Given the rating key, this function will automatically handle collecting the required information to update the
    theme song, and any other metadata.

    Parameters
    ----------
    rating_key : int
        The rating key of the item to be updated.

    Returns
    -------
    py:class:`bool`
        True if the item was updated successfully, False otherwise.

    Examples
    --------
    >>> update_plex_item(rating_key=12345)
    """
    item = get_plex_item(rating_key=rating_key)
    if not item:
        log.error(f'Could not find item with rating key: {rating_key}')
        return False

    if not config.CONFIG['Themerr']['BOOL_THEMERR_ENABLED']:
        return False
    if str(getattr(item, 'librarySectionID', '')) in {
        key.strip() for key in _ignored_libraries().split(',') if key.strip()
    }:
        return False

    database_type, database, agent, database_id = get_database_info(item=item)
    log.debug('-' * 50)
    log.debug(f'item title: {item.title}')
    log.debug(f'item type: {item.type}')
    log.debug(f'database_info: {(database_type, database, agent, database_id)}')

    if not (database and database_type and database_id):
        return False
    if not themerr_db.item_exists(database_type=database_type, database=database, id=database_id):
        log.debug(f'{item.type} item does not exist in ThemerrDB, skipping: {item.title} ({database_id})')
        return False

    try:
        data = helpers.json_get(
            cache_time=3600,
            url=f'https://themerr.lizardbyte.dev/ThemerrDB/{database_type}/{database}/{database_id}.json',
        )
    except Exception:
        log.exception(f'{item.ratingKey}: Error retrieving data from ThemerrDB')
        return False
    if not data:
        return False

    log.debug(f'data found for {item.type} {item.title}')
    _update_collection_metadata(item, agent, data)
    _update_theme(item, data)
    return True


def _media_matches(item: PlexPartialObject, media_type: str, tracked: dict, source_id: str,
                   audio_codec: str | None, mp4a_available: bool | None, audio_sha256: str | None) -> bool:
    """Check whether the current tracked upload still satisfies this request.

    Parameters
    ----------
    item : PlexPartialObject
        Target Plex item.
    media_type : str
        Media field to update.
    tracked : dict
        Previously recorded upload metadata.
    source_id : str
        Requested media source version.
    audio_codec : str or None
        Requested theme codec.
    mp4a_available : bool or None
        Whether the source offers AAC audio.
    audio_sha256 : str or None
        Digest requiring an already verified theme upload.

    Returns
    -------
    bool
        Whether an upload can be skipped.
    """
    if tracked.get(media_type_dict[media_type]['themerr_data_key']) != source_id:
        return False
    if media_type != 'themes':
        return True
    same_codec = audio_codec is None or tracked.get('audio_codec') == audio_codec
    aac_unavailable = config.CONFIG['Themerr']['BOOL_PREFER_MP4A_CODEC'] and mp4a_available is False
    has_verified_upload = not audio_sha256 or tracked.get('audio_sha256')
    return bool((same_codec or aac_unavailable) and has_verified_upload and
                general.get_theme_provider(item=item) == 'themerr')


def _theme_upload_metadata(item: PlexPartialObject, audio_codec: str | None, mp4a_available: bool | None,
                           audio_sha256: str | None, remove_unused: bool) -> dict:
    """Record theme details after a successful upload, retaining verified audio.

    Parameters
    ----------
    item : PlexPartialObject
        Updated Plex item.
    audio_codec : str or None
        Codec of the uploaded audio.
    mp4a_available : bool or None
        Whether the source offers AAC audio.
    audio_sha256 : str or None
        Digest of the verified upload.
    remove_unused : bool
        Whether older uploaded files should be removed.

    Returns
    -------
    dict
        Codec, digest, and selected Plex theme key.
    """
    metadata = {'audio_codec': audio_codec, 'mp4a_available': mp4a_available}
    if audio_sha256:
        metadata['audio_sha256'] = audio_sha256
        if remove_unused:
            general.remove_uploaded_media(item=item, media_type='themes', keep_sha256=audio_sha256)
    try:
        selected = next((theme for theme in item.themes() if getattr(theme, 'selected', False)), None)
    except Exception:
        log.exception(f'{item.ratingKey}: Unable to identify the newly uploaded theme')
        selected = None
    metadata['uploaded_theme_key'] = selected.ratingKey if selected is not None else None
    return metadata


def _record_media_upload(item: PlexPartialObject, media_type: str, source_id: str, audio_codec: str | None,
                         mp4a_available: bool | None, audio_sha256: str | None, remove_unused: bool) -> None:
    """Persist successful media uploads and publish their dashboard state.

    Parameters
    ----------
    item : PlexPartialObject
        Updated Plex item.
    media_type : str
        Uploaded Plex media field.
    source_id : str
        Uploaded source version.
    audio_codec : str or None
        Codec of the uploaded theme.
    mp4a_available : bool or None
        Whether the source offers AAC audio.
    audio_sha256 : str or None
        Digest of the verified upload.
    remove_unused : bool
        Whether older uploaded files should be removed.
    """
    metadata = {media_type_dict[media_type]['themerr_data_key']: source_id}
    if media_type == 'themes':
        metadata.update(_theme_upload_metadata(item, audio_codec, mp4a_available, audio_sha256, remove_unused))
    general.update_themerr_data(item=item, new_themerr_data=metadata)
    if media_type == 'themes':
        uploaded_key = str(metadata.get('uploaded_theme_key', ''))
        provider = 'themerr' if uploaded_key.startswith('upload://themes/') else 'uploaded'
        try:
            storage.mark_dashboard_theme_uploaded(item.ratingKey, provider)
        except Exception:
            log.exception(f'Unable to update dashboard after theme upload for {_item_log_context(item)}')
        log.info(f'Theme upload recorded for {_item_log_context(item)} (codec={audio_codec or "unknown"})')


def add_media(
        item: PlexPartialObject,
        media_type: str,
        media_url_id: str,
        media_file: Optional[str] = None,
        media_url: Optional[str] = None,
        on_error: Callable[[str], None] | None = None,
        audio_codec: str | None = None,
        mp4a_available: bool | None = None,
        audio_sha256: str | None = None,
) -> bool:
    """
    Apply media to the specified item.

    Adds theme song to the item specified by the ``rating_key``. If the same theme song is already present, it will be
    skipped.

    Parameters
    ----------
    item : PlexPartialObject
        The Plex item to add the theme to.
    media_type : str
        The type of media to add. Must be one of 'art', 'posters', or 'themes'.
    media_url_id : str
        The url or id of the media.
    media_file : Optional[str]
        Full path to media file.
    media_url : Optional[str]
        URL of media.
    on_error : callable or None, optional
        Receives an upload failure reason.
    audio_codec : str or None, optional
        Codec of the selected theme audio stream.
    mp4a_available : bool or None, optional
        Whether the source offers MP4A AAC audio, including when Opus was selected.
    audio_sha256 : str or None, optional
        Digest of complete validated audio, used to verify Plex's stored file.

    Returns
    -------
    py:class:`bool`
        True if the media was added successfully or already present, False otherwise.

    Examples
    --------
    >>> add_media(item=..., media_type='themes', media_url_id=..., media_url=...)
    >>> add_media(item=..., media_type='themes', media_url_url=..., media_file=...)
    """
    uploaded = False

    themerr_data = general.get_themerr_data(item=item)

    if (item.isLocked(field=media_type_dict[media_type]['plex_field']) and
            not config.CONFIG['Themerr']['BOOL_IGNORE_LOCKED_FIELDS']):
        log.info(f'Not overwriting locked "{media_type_dict[media_type]['name']}" for {item.type}: {item.title}')
        return False

    if not (media_file or media_url):
        log.warning(f'No theme songs provided for type: {item.type}, title: {item.title}, rating_key: {item.ratingKey}')
        return False

    if _media_matches(item, media_type, themerr_data, media_url_id, audio_codec, mp4a_available, audio_sha256):
        if media_type == 'themes' and mp4a_available is not None:
            general.update_themerr_data(item=item, new_themerr_data={'mp4a_available': mp4a_available})
        log.info(f'Skipping {media_type_dict[media_type]['name']} for '
                 f'type: {item.type}, title: {item.title}, rating_key: {item.ratingKey}')
        return True

    log.info('Preparing %s upload for %s', media_type_dict[media_type]['name'], _item_log_context(item))
    remove_unused = config.CONFIG['Themerr'][media_type_dict[media_type]['remove_pref']]
    if remove_unused and not audio_sha256:
        general.remove_uploaded_media(item=item, media_type=media_type)

    if media_file:
        uploaded = upload_media(item=item, method=media_type_dict[media_type]['method'](item),
                                filepath=media_file, on_error=on_error, expected_sha256=audio_sha256)
    if media_url:
        uploaded = upload_media(item=item, method=media_type_dict[media_type]['method'](item),
                                url=media_url, on_error=on_error)

    if uploaded:
        _record_media_upload(item, media_type, media_url_id, audio_codec, mp4a_available, audio_sha256, remove_unused)

        # unlock the field since it contains an automatically added value
        change_lock_status(item=item, field=media_type_dict[media_type]['plex_field'], lock=False)
    else:
        log.debug(f'Could not upload {media_type_dict[media_type]['name']} for '
                  f'type: {item.type}, title: {item.title}, rating_key: {item.ratingKey}')

    return uploaded


def change_lock_status(item: PlexPartialObject, field: str, lock: bool = False) -> bool:
    """
    Change the lock status of the specified field.

    Parameters
    ----------
    item : PlexPartialObject
        The Plex item to unlock the field for.
    field : str
        The field to unlock.
    lock : py:class:`bool`
        True to lock the field, False to unlock the field.

    Returns
    -------
    py:class:`bool`
        True if the lock status matches the requested lock status, False otherwise.

    Examples
    --------
    >>> change_lock_status(item=..., field='theme', lock=False)
    """
    lock_string = 'lock' if lock else 'unlock'

    current_status = item.isLocked(field=field)
    if current_status == lock:
        log.debug(f'Lock field "{field}" is already {lock} for item: {item.title}')
        return current_status == lock

    edits = {
        f'{field}.locked': int(lock),
    }

    count = 0
    successful = False
    exception = None
    while count < 3:  # there are random read timeouts
        try:
            item.edit(**edits)
        except requests.ReadTimeout as e:
            exception = e
            time.sleep(5)
            count += 1
        else:
            successful = True
            break

    if not successful:
        log.error(f'{item.ratingKey}: Error {lock_string}ing field: {exception}')

    # we need to reload the item to get the new lock status
    reload_kwargs = {field: True}
    item.reload(**reload_kwargs)

    locked = item.isLocked(field=field)
    if locked != lock:
        log.error(f'{item.ratingKey}: Error {lock_string}ing field: {locked} != {lock}')

    return locked == lock


def _submit_media(item: PlexPartialObject, method: Callable, source: dict, context: str | None,
                  expected_sha256: str | None, attempt: int, max_attempts: int) -> None:
    """Submit one upload attempt and verify the stored theme when requested.

    Parameters
    ----------
    item : PlexPartialObject
        Target Plex item.
    method : callable
        Plex upload method.
    source : dict
        File path or media URL to submit.
    context : str or None
        Theme upload log context.
    expected_sha256 : str or None
        Expected digest of the uploaded theme.
    attempt : int
        Current attempt number.
    max_attempts : int
        Maximum number of attempts.
    """
    if not any(source.values()):
        return
    is_theme = method == item.uploadTheme
    if is_theme:
        source['timeout'] = int(config.CONFIG['Themerr']['INT_PLEXAPI_PLEXAPI_TIMEOUT'])
        log.info(f'Submitting theme to Plex for {context} '
                 f'(attempt {attempt}/{max_attempts}, timeout={source["timeout"]}s)')
    method(**source)
    if is_theme and expected_sha256:
        _verify_uploaded_theme(item, expected_sha256, source['timeout'])


def upload_media(
        item: PlexPartialObject,
        method: Callable,
        filepath: Optional[str] = None,
        url: Optional[str] = None,
        on_error: Callable[[str], None] | None = None,
        expected_sha256: str | None = None,
) -> bool:
    """
    Upload media to the specified item.

    Uploads art, poster, or theme to the item specified by the ``item``.

    Parameters
    ----------
    item : PlexPartialObject
        The Plex item to upload the theme to.
    method : Callable
        The method to use to upload the theme.
    filepath : Optional[str]
        The path to the theme song.
    url : Optional[str]
        The url to the theme song.
    on_error : callable or None, optional
        Receives the final upload failure reason.
    expected_sha256 : str or None, optional
        Digest to compare with the audio Plex serves after accepting the upload.

    Returns
    -------
    py:class:`bool`
        True if the theme was uploaded successfully, False otherwise.

    Examples
    --------
    >>> upload_media(item=..., method=item.uploadArt, url=...)
    >>> upload_media(item=..., method=item.uploadPoster, url=...)
    >>> upload_media(item=..., method=item.uploadTheme, url=...)
    ...
    """
    source = {'filepath': filepath} if filepath else {'url': url}
    count = 0
    last_error = None
    max_attempts = int(config.CONFIG['Themerr']['INT_PLEXAPI_UPLOAD_RETRIES_MAX']) + 1
    is_theme = method == item.uploadTheme
    context = _item_log_context(item) if is_theme else f'rating_key={item.ratingKey}'
    while count < max_attempts:
        try:
            _submit_media(item, method, source, context, expected_sha256, count + 1, max_attempts)
        except (BadRequest, requests.RequestException) as e:
            last_error = e
            sleep_time = 2 ** count
            reason = theme_errors.normalize_reason(e)
            log.error('Plex media upload failed for %s (attempt %d/%d): %s',
                      context, count + 1, max_attempts, reason)
            if count + 1 < max_attempts:
                log.warning('Retrying Plex media upload for %s in %d seconds',
                            context, sleep_time)
                time.sleep(sleep_time)
            count += 1
        else:
            if is_theme:
                log.info('Plex accepted theme upload for %s', context)
            return True
    if last_error is not None and on_error:
        on_error(theme_errors.normalize_reason(last_error))
    return False


def _verify_uploaded_theme(item: PlexPartialObject, expected_sha256: str, timeout: int) -> None:
    """Verify Plex serves exactly the complete audio file that was uploaded.

    Parameters
    ----------
    item : PlexPartialObject
        Item whose theme was just uploaded.
    expected_sha256 : str
        SHA-256 of the validated local file.
    timeout : int
        Plex read timeout in seconds.

    Raises
    ------
    BadRequest
        Plex selected an incomplete or different theme.
    requests.RequestException
        Plex's stored audio cannot be retrieved.
    """
    item.reload()
    path = item.theme
    if not isinstance(path, str) or not path.startswith('/library/metadata/') or '/theme/' not in path:
        raise BadRequest('Plex did not select the uploaded theme')
    server = item._server
    digest = hashlib.sha256()
    with server._session.get(server.url(path, includeToken=False),
                             headers=server._headers(**{'Accept-Encoding': 'identity'}),
                             stream=True, allow_redirects=False, timeout=timeout) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise BadRequest('Plex did not return the complete uploaded theme')
        for chunk in response.iter_content(chunk_size=64 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise BadRequest('Plex stored incomplete or different theme audio; the upload will be retried')
    log.info('Plex theme file verified for %s', _item_log_context(item))


def _guid_candidates(item: PlexPartialObject) -> list[tuple[str, str, str]]:
    """Collect supported external IDs from modern and legacy Plex GUIDs.

    Parameters
    ----------
    item : PlexPartialObject
        Plex movie or show.

    Returns
    -------
    list[tuple[str, str, str]]
        GUID scheme, ThemerrDB database, and identifier.
    """
    raw_guids = [guid.id for guid in getattr(item, 'guids', []) or []]
    raw_guids.append(getattr(item, 'guid', ''))
    candidates = []
    for raw_guid in raw_guids:
        if not isinstance(raw_guid, str):
            continue
        scheme, separator, identifier = raw_guid.partition('://')
        database = guid_map.get(scheme)
        identifier = identifier.split('?', 1)[0].split('#', 1)[0]
        if separator and database and identifier:
            candidates.append((scheme, database, identifier))
    return candidates


def get_external_id(item: PlexPartialObject) -> tuple[str | None, str | None]:
    """Return an external metadata ID without requiring a TMDB conversion.

    Parameters
    ----------
    item : PlexPartialObject
        Plex movie, show, or collection.

    Returns
    -------
    tuple[str or None, str or None]
        Database name and identifier, if Plex supplied a supported external GUID.
    """
    candidates = _guid_candidates(item)
    for preferred in ('themoviedb', 'imdb', 'thetvdb'):
        for _, database, identifier in candidates:
            if database == preferred:
                return database, identifier
    return None, None


def _movie_database_info(item: PlexPartialObject) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Resolve a movie's preferred external database identifier.

    Parameters
    ----------
    item : PlexPartialObject
        Plex movie.

    Returns
    -------
    tuple
        Database type, database, agent, and identifier.
    """
    candidates = _guid_candidates(item)
    if not candidates:
        return None, None, None, None

    for preferred in ('themoviedb', 'imdb'):
        for _, database, identifier in candidates:
            if database == preferred:
                return 'movies', database, MOVIE_AGENT, identifier
    return 'movies', None, MOVIE_AGENT, None


def _show_database_info(item: PlexPartialObject) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Resolve a show's preferred external database identifier.

    Parameters
    ----------
    item : PlexPartialObject
        Plex show.

    Returns
    -------
    tuple
        Database type, database, agent, and identifier.
    """
    candidates = _guid_candidates(item)
    if not candidates:
        return 'tv_shows', None, None, None

    external_guid = None
    for scheme, database, identifier in candidates:
        if database == 'themoviedb':
            return 'tv_shows', database, SERIES_AGENT, identifier
        if database in ('imdb', 'thetvdb') and external_guid is None:
            external_guid = ('tvdb' if database == 'thetvdb' else 'imdb', identifier)

    if external_guid:
        database_id = tmdb.get_tmdb_id_from_external_id(
            external_id=external_guid[1], database=external_guid[0], item_type='tv', title=item.title,
        )
        if database_id:
            return 'tv_shows', 'themoviedb', SERIES_AGENT, database_id
    return 'tv_shows', None, SERIES_AGENT, None


def _collection_database_info(
    item: PlexPartialObject, plex,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Resolve a collection using its library agent and title.

    Parameters
    ----------
    item : PlexPartialObject
        Plex collection.
    plex : PlexServer
        Connected Plex server.

    Returns
    -------
    tuple
        Database type, database, agent, and identifier.
    """
    section = plex.library.sectionByID(item.librarySectionID)
    if section.type != 'movie':
        return None, None, section.agent, None
    database_id = tmdb.get_tmdb_id_from_collection(search_query=item.title, language=section.language)
    if database_id is None:
        database_id = _collection_id_from_members(item)
    return 'movie_collections', 'themoviedb', section.agent, database_id


def _collection_member_id(member: PlexPartialObject) -> tuple[str, str] | None:
    """Find a member movie that has ThemerrDB metadata to inspect.

    Parameters
    ----------
    member : PlexPartialObject
        Plex collection member.

    Returns
    -------
    tuple of str or None
        Database and movie ID when usable for collection lookup.
    """
    if getattr(member, 'type', None) != 'movie':
        return None
    database, identifier = get_external_id(member)
    if database not in ('imdb', 'themoviedb') or not identifier:
        return None
    if not themerr_db.item_exists(database_type='movies', database=database, id=identifier):
        return None
    return database, identifier


def _collection_id_from_members(item: PlexPartialObject) -> Optional[str]:
    """Resolve a Plex collection from its movies' ThemerrDB collection metadata.

    Plex collection GUIDs are local UUIDs. A matching TMDB collection can be
    identified through a member movie, but only when its collection name agrees
    with the Plex collection title.

    Parameters
    ----------
    item : PlexPartialObject
        Plex movie collection.

    Returns
    -------
    str or None
        Verified TMDB collection ID when a member supplies one.
    """
    requested_name = themerr_db._title_key(item.title).removesuffix(' collection')
    try:
        members = item.items()
    except Exception:
        log.exception('%s: Unable to inspect collection members', item.ratingKey)
        return None

    checked = 0
    for member in members:
        if checked >= 3:
            break
        source_id = _collection_member_id(member)
        if source_id is None:
            continue
        database, identifier = source_id
        checked += 1
        try:
            metadata = helpers.json_get(
                cache_time=86400,
                url=f'https://themerr.lizardbyte.dev/ThemerrDB/movies/{database}/{identifier}.json',
            )
            collection = metadata.get('belongs_to_collection') or {}
            collection_name = themerr_db._title_key(collection.get('name', '')).removesuffix(' collection')
            if collection_name == requested_name and collection.get('id'):
                return str(collection['id'])
        except (AttributeError, KeyError, TypeError, ValueError):
            log.warning('%s: Invalid collection metadata for movie %s', item.ratingKey, identifier)
        except Exception:
            log.exception('%s: Unable to look up collection metadata', item.ratingKey)
    return None


def get_database_info(item: PlexPartialObject) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Get the database info for the specified item.

    Get the ``database_type``, ``database``, ``agent``, ``database_id`` which can be used to locate the theme song
    in ThemerrDB.

    Parameters
    ----------
    item : PlexPartialObject
        The Plex item to get the database info for.

    Returns
    -------
    Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]
        The ``database_type``, ``database``, ``agent``, ``database_id``.

    Examples
    --------
    >>> get_database_info(item=...)
    """
    log.debug(f'Getting database info for item: {item.title}')

    plex = setup_plexapi()
    if not plex:
        log.error(PLEX_SETUP_ERROR)
        return None, None, None, None

    if item.type == 'movie':
        database_info = _movie_database_info(item)
    elif item.type == 'show':
        database_info = _show_database_info(item)
    elif item.type == 'collection':
        database_info = _collection_database_info(item, plex)
    else:
        database_info = None, None, None, None

    log.debug(f'Database info for item: {item.title}, database_info: {database_info}')
    return database_info


def get_plex_item(rating_key: int) -> Optional[PlexPartialObject]:
    """
    Get any item from the Plex Server.

    This function is used to get an item from the Plex Server. It can then be used to get the metadata for the item.

    Parameters
    ----------
    rating_key : int
        The ``rating_key`` of the item to get.

    Returns
    -------
    PlexPartialObject
        The Plex item from the Plex Server.

    Examples
    --------
    >>> get_plex_item(rating_key=1)
    ...
    """
    plex = setup_plexapi()
    if not plex:
        log.error(PLEX_SETUP_ERROR)
        return None
    item = plex.fetchItem(ekey=rating_key)

    return item


def plex_listener() -> None:
    """Start one independently scoped event listener per enabled server."""
    with _listener_lock:
        _start_plex_listeners()


def _start_plex_listeners() -> None:
    """Replace the listener set while its lifecycle lock is held."""
    stop_plex_listener()
    registered = servers.list_servers(enabled_only=True)
    if not registered:
        return
    for record in registered:
        server_id = record['id']
        try:
            with storage.server_scope(server_id):
                plex = setup_plexapi()
            listener = AlertListener(server=plex,
                                     callback=lambda data, sid=server_id: _server_event(sid, data),
                                     callbackError=log.error)
            _alert_listeners[server_id] = listener
            listener.start()
        except Exception:
            log.exception('Unable to start Plex listener for server %s', server_id)
            servers.record_refresh(server_id, 'Could not start the Plex event listener.')


def _server_event(server_id: str, data: dict) -> None:
    """Carry server identity from a listener into the processing queue."""
    if not (servers.get_server(server_id) or {}).get('enabled'):
        return
    with storage.server_scope(server_id):
        plex_listener_handler(data)


def stop_plex_listener(server_id: str | None = None) -> None:
    """Stop listeners for one server, or all servers.

    Parameters
    ----------
    server_id : str or None, optional
        Machine identifier, or ``None`` for every listener.
    """
    global alert_listener
    with _listener_lock:
        keys = [server_id] if server_id else list(_alert_listeners)
        for key in keys:
            listener = _alert_listeners.pop(key, None)
            if listener and getattr(listener, '_ws', None) is not None:
                listener.stop()
        if server_id is None and alert_listener is not None:
            if getattr(alert_listener, '_ws', None) is not None:
                alert_listener.stop()
            alert_listener = None


def plex_listener_handler(data: dict) -> None:
    """
    Process events from ``plex_listener()``.

    Check if we need to add an item to the queue. This is used to automatically add themes to items from the
    new Plex Movie agent, since metadata agents cannot extend it.

    Parameters
    ----------
    data : dict
        Data received from the Plex server.

    Examples
    --------
    >>> plex_listener_handler(data={'type': 'timeline'})
    ...
    """
    if not config.CONFIG['Themerr']['BOOL_THEMERR_ENABLED'] or data['type'] != 'timeline':
        return

    for entry in data['TimelineEntry']:
        if entry['state'] != 5 or entry['identifier'] != 'com.plexapp.plugins.library':
            continue

        media_type = reverseSearchType(libtype=entry['type'])
        supported = (
            (media_type == 'movie' and config.CONFIG['Themerr']['BOOL_PLEX_MOVIE_SUPPORT']) or
            (media_type == 'show' and config.CONFIG['Themerr']['BOOL_PLEX_SERIES_SUPPORT'])
        )
        if not supported:
            continue

        rating_key = int(entry['itemID'])
        processing.enqueue(str(rating_key))


def _items_for_section(section) -> list:
    """Collect supported media and collections from one Plex section.

    Parameters
    ----------
    section : LibrarySection
        Section being scanned.

    Returns
    -------
    list
        Items eligible for the scheduled update.
    """
    if section.type == 'movie':
        media_items = section.all() if config.CONFIG['Themerr']['BOOL_PLEX_MOVIE_SUPPORT'] else []
        collections = section.collections() if config.CONFIG['Themerr']['BOOL_PLEX_COLLECTION_SUPPORT'] else []
        return media_items + collections
    if section.type == 'show' and config.CONFIG['Themerr']['BOOL_PLEX_SERIES_SUPPORT']:
        return section.all()
    return []


def scan_items(enqueue: Callable[[str], bool]) -> None:
    """
    Update all items in the Plex Server.

    This is used to update all items in the Plex Server. It is called from a scheduled task.

    Parameters
    ----------
    enqueue : callable
        Queue an opaque item ID within the current server's storage scope.

    Examples
    --------
    >>> scan_items(enqueue=...)
    """
    plex = setup_plexapi()
    if not plex:
        log.error(PLEX_SETUP_ERROR)
        return

    log.info('Refreshing ThemerrDB index before the theme scan')
    themerr_db.update_cache()
    log.info('ThemerrDB index ready; loading Plex libraries')

    plex_library = plex.library

    sections = plex_library.sections()
    _library_names.update({(storage.current_server_id(), str(section.key)): section.title for section in sections})
    log.info('Scanning %d Plex libraries for theme updates', len(sections))
    ignored_library_ids = {
        library_id.strip()
        for library_id in _ignored_libraries().split(',')
        if library_id.strip()
    }

    queued = 0
    for section in sections:
        if str(section.key) in ignored_library_ids:
            log.debug(f'Skipping ignored Plex library: {section.title}')
            continue
        if section.agent not in contributes_to:
            # with legacy agents, not all items in the library had to match to the library agent
            # not the case with new agents (probably)
            continue  # skip unsupported metadata agents

        # check if the agent is enabled
        if not general.continue_update(item_agent=section.agent):
            log.debug(f'{definitions.Names.name} is disabled for agent "{section.agent}"')
            continue

        section_queued = 0
        for item in _items_for_section(section):
            if enqueue(str(item.ratingKey)):
                section_queued += 1
        queued += section_queued
        log.info('Queued %d items from library %r (ID %s)', section_queued, section.title, section.key)
    log.info('Theme scan finished queuing %d items', queued)


def _ignored_libraries() -> str:
    """Read ignored library IDs for the current server."""
    record = servers.get_server(storage.current_server_id())
    return record['ignored_libraries'] if record else config.CONFIG['Themerr']['IGNORED_LIBRARY_IDS']

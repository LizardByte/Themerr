# standard imports
import hashlib
import os
import shutil
from typing import Optional

# lib imports
from plexapi.base import PlexPartialObject

# local imports
from common import config
from common import logger
from plex.constants import metadata_type_map
from themerr import storage
from plex import servers
from plex import ssh

log = logger.get_logger(__name__)


def _get_metadata_path(item: PlexPartialObject) -> str:
    """
    Get the metadata path of the item.

    Get the hashed path of the metadata directory for the item specified by the ``item``.

    Parameters
    ----------
    item : PlexPartialObject
        The item to get the theme upload path for.

    Returns
    -------
    str
        The path to the metadata directory.

    Examples
    --------
    >>> _get_metadata_path(item=...)
    "...bundle"
    """
    guid = item.guid
    # Plex metadata paths require SHA-1; this value is not used for security.
    full_hash = hashlib.sha1(guid.encode('utf-8'), usedforsecurity=False).hexdigest()  # NOSONAR python:S4790
    record = servers.get_server(storage.current_server_id())
    directory = record['data_directory'] if record else config.CONFIG['Plex']['PLEX_APP_SUPPORT_PATH']
    if not directory:
        return ''
    metadata_path = os.path.join(
        directory, 'Metadata', metadata_type_map[item.type],
        full_hash[0], full_hash[1:] + '.bundle')
    return metadata_path


def continue_update(item_agent: str) -> bool:
    """
    Check if the specified agent should continue updating.

    Parameters
    ----------
    item_agent : str
        The agent to check.

    Returns
    -------
    py:class:`bool`
        True if the agent should continue updating, False otherwise.

    Examples
    --------
    >>> continue_update(item_agent='tv.plex.agents.movie')
    True
    >>> continue_update(item_agent='tv.plex.agents.series')
    True
    """
    if item_agent == 'tv.plex.agents.movie':
        return config.CONFIG['Themerr']['BOOL_PLEX_MOVIE_SUPPORT']
    elif item_agent == 'tv.plex.agents.series':
        return config.CONFIG['Themerr']['BOOL_PLEX_SERIES_SUPPORT']
    else:
        return False


def get_media_upload_path(item: PlexPartialObject, media_type: str) -> str:
    """
    Get the path to the theme upload directory.

    Get the hashed path of the theme upload directory for the item specified by the ``item``.

    Parameters
    ----------
    item : PlexPartialObject
        The item to get the theme upload path for.
    media_type : str
        The media type to get the theme upload path for. Must be one of 'art', 'posters', or 'themes'.

    Returns
    -------
    str
        The path to the theme upload directory.

    Raises
    ------
    ValueError
        If the ``media_type`` is not one of 'art', 'posters', or 'themes'.

    Examples
    --------
    >>> get_media_upload_path(item=..., media_type='art')
    "...bundle/Uploads/art..."
    >>> get_media_upload_path(item=..., media_type='posters')
    "...bundle/Uploads/posters..."
    >>> get_media_upload_path(item=..., media_type='themes')
    "...bundle/Uploads/themes..."
    """
    allowed_media_types = ['art', 'posters', 'themes']
    if media_type not in allowed_media_types:
        raise ValueError(
            'This error should be reported to https://github.com/Themerr/Themerr/issues;'
            f'media_type must be one of: {allowed_media_types}'
        )

    metadata_path = _get_metadata_path(item=item)
    theme_upload_path = os.path.join(metadata_path, 'Uploads', media_type) if metadata_path else ''
    return theme_upload_path


def get_theme_provider(item: PlexPartialObject) -> Optional[str]:
    """
    Get the theme provider.

    Get the theme provider for the item specified by the ``item``.

    Parameters
    ----------
    item : PlexPartialObject
        The item to get the theme provider for.

    Returns
    -------
    str
        The theme provider.

    Examples
    --------
    >>> get_theme_provider(item=...)
    ...
    """
    provider_map = {
        'local': 'user',  # new agents, local media
        'com.plexapp.agents.localmedia': 'user',  # legacy agents, local media
        'com.plexapp.agents.plexthememusic': 'plex',  # legacy agents
    }

    rating_key_map = {
        'metadata://themes/tv.plex.agents.movies_': 'plex',  # new movie agent (placeholder if Plex adds theme support)
        'metadata://themes/tv.plex.agents.series_': 'plex',  # new tv agent
        'metadata://themes/com.plexapp.agents.plexthememusic_': 'plex',  # legacy agents
    }

    themes = item.themes()
    if not themes:
        log.debug(f'No themes found for item: {item.title}')
        return

    selected = next((theme for theme in themes if getattr(theme, 'selected')), None)
    if not selected:
        log.debug(f'No selected theme found for item: {item.title}')
        return

    if selected.ratingKey.startswith('upload://themes/'):
        themerr_data = get_themerr_data(item=item)
        return 'themerr' if themerr_data.get('uploaded_theme_key') == selected.ratingKey else 'uploaded'

    provider = provider_map.get(selected.provider)
    if not provider:
        # New Plex agents identify their themes by rating key rather than provider.
        provider = next((value for prefix, value in rating_key_map.items()
                         if selected.ratingKey.startswith(prefix)), selected.provider)

    return provider


def get_themerr_data(item: PlexPartialObject) -> dict:
    """
    Get persisted upload metadata for the specified item.

    Parameters
    ----------
    item : PlexPartialObject
        The item to get the Themerr data for.

    Returns
    -------
    dict
        Tracked upload fields, or an empty dict when none exist.
    """
    return storage.get_tracking(item.ratingKey)


def remove_uploaded_media(item: PlexPartialObject, media_type: str, keep_sha256: str | None = None) -> None:
    """
    Remove themes for the specified item.

    Deletes the themes upload directory for the item specified by the ``item``.

    Parameters
    ----------
    item : PlexPartialObject
        The item to remove the themes from.
    media_type : str
        The media type to remove the themes from. Must be one of 'art', 'posters', or 'themes'.
    keep_sha256 : str or None, optional
        Keep files matching the verified upload instead of removing the whole directory.

    Returns
    -------
    py:class:`bool`
        True if the themes were removed successfully, False otherwise.

    Examples
    --------
    >>> remove_uploaded_media(item=..., media_type='themes')
    ...
    """
    server_id = storage.current_server_id()
    if server_id != 'default' and ssh.settings(server_id):
        ssh.remove_uploaded_media(server_id, item, media_type, keep_sha256)
        return
    theme_upload_path = get_media_upload_path(item=item, media_type=media_type)
    if not os.path.isdir(theme_upload_path):
        return
    if keep_sha256:
        _remove_unused_uploads(theme_upload_path, keep_sha256, item.ratingKey)
    else:
        shutil.rmtree(path=theme_upload_path, ignore_errors=True, onerror=remove_uploaded_media_error_handler)


def _remove_unused_uploads(directory: str, keep_sha256: str, rating_key: int) -> None:
    """Remove old uploads while retaining the verified theme.

    Parameters
    ----------
    directory : str
        Plex media upload directory.
    keep_sha256 : str
        Digest of the upload to retain.
    rating_key : int
        Item identifier for cleanup failure messages.
    """
    for current, _, files in os.walk(directory):
        for name in files:
            path = os.path.join(current, name)
            try:
                with open(path, 'rb') as uploaded:
                    matches = hashlib.file_digest(uploaded, 'sha256').hexdigest() == keep_sha256
                if not matches:
                    os.remove(path)
            except OSError:
                log.exception(f'Unable to remove an unused theme for item {rating_key}')


def remove_uploaded_media_error_handler(func: any, path: any, exc_info: any) -> None:
    """
    Error handler for removing themes.

    Handles errors that occur when removing themes using ``shutil``.

    Parameters
    ----------
    func : any
        The function that caused the error.
    path : any
        The path that caused the error.
    exc_info : any
        The exception information.
    """
    log.error(f'Error removing themes with function: {func}, path: {path}, exception info: {exc_info}')


def update_themerr_data(item: PlexPartialObject, new_themerr_data: dict) -> None:
    """
    Update tracked Themerr upload metadata in SQLite.

    This records successful uploads so they can be identified on later scans.

    Parameters
    ----------
    item : PlexPartialObject
        The Plex item whose media was uploaded.
    new_themerr_data : dict
        Updated upload metadata.
    """
    storage.save_tracking(item.ratingKey, item.type, new_themerr_data)

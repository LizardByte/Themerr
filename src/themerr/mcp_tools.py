"""Bounded MCP operations over saved servers and existing application services."""

# standard imports
from collections import Counter
from datetime import datetime, timezone

# lib imports
from sqlalchemy import select
from sqlalchemy.orm import Session

# local imports
from common import config, log_viewer, logger
from media_servers import get_backend, processing
from themerr import cache, scheduled_tasks, storage, themerr_db

_DATABASE_TYPES = {
    'movie': 'movies',
    'show': 'tv_shows',
    'collection': 'movie_collections',
}


class ToolInputError(ValueError):
    """A fixed client error safe to return through the MCP protocol."""


def _servers(server_id=None):
    records = get_backend().list_servers()
    if server_id is not None:
        records = [record for record in records if record['id'] == server_id]
        if not records:
            raise ToolInputError('Server not found.')
    return records


def _page(limit, offset):
    if not 1 <= limit <= 200 or offset < 0:
        raise ToolInputError('Choose a limit between 1 and 200 and a nonnegative offset.')


def _selection(server_id=None, library_id=None):
    records = _servers(server_id)
    if library_id is not None and server_id is None:
        raise ToolInputError('Select a server when selecting a library.')
    query = select(storage.LibraryItem).where(storage.LibraryItem.server_id.in_([row['id'] for row in records]))
    if library_id is not None:
        with Session(storage.engine()) as database:
            if database.get(storage.LibrarySection, (
                records[0]['id'],
                library_id,
            )) is None:
                raise ToolInputError('Library not found in the cached snapshot. Refresh libraries first.')
        query = query.where(storage.LibraryItem.section_key == library_id)
    return (
        records,
        query,
    )


def _freshness(records):
    return [{
        'server_id': row['id'],
        'last_refresh': row.get('last_refresh'),
        'last_error': logger.redact(row['last_error']) if row.get('last_error') else None,
    } for row in records]


def _item(row):
    return {
        'server_id': row.server_id,
        'item_id': row.rating_key,
        'library_id': row.section_key,
        'title': row.title,
        'year': row.year,
        'type': row.type,
        'theme': row.theme,
        'theme_status': row.theme_status,
        'theme_provider': row.theme_provider,
        'database': row.database,
        'database_id': row.database_id,
        'source_database': row.source_database,
        'source_id': row.source_id,
        'contribution_url': row.issue_url,
    }


def list_servers(limit=100, offset=0):
    """Return saved connection state without credentials or filesystem directories."""
    _page(limit, offset)
    records = _servers()
    return {
        'servers': [{
            'server_id': row['id'],
            'name': row['name'],
            'backend': get_backend().display_name(row['id']),
            'enabled': row['enabled'],
            'last_refresh': row.get('last_refresh'),
            'last_error': logger.redact(row['last_error']) if row.get('last_error') else None,
            'ignored_library_ids': [value for value in row.get('ignored_libraries', '').split(',') if value],
        } for row in records[offset:offset + limit]],
        'next_offset': offset + limit if len(records) > offset + limit else None,
    }


def list_libraries(server_id=None, limit=100, offset=0):
    """List cached libraries, including those on paused or unavailable servers."""
    _page(limit, offset)
    records = _servers(server_id)
    query = select(storage.LibrarySection).where(
        storage.LibrarySection.server_id.in_([row['id'] for row in records]),
    ).order_by(storage.LibrarySection.server_id, storage.LibrarySection.key).offset(offset).limit(limit + 1)
    with Session(storage.engine()) as database:
        rows = database.scalars(query).all()
        libraries = [{
            'server_id': row.server_id,
            'library_id': row.key,
            'title': row.title,
            'type': row.type,
        } for row in rows[:limit]]
    return {
        'libraries': libraries,
        'next_offset': offset + limit if len(rows) > limit else None,
        'cached': True,
        'snapshots': _freshness(records),
    }


def search_items(query='', server_id=None, library_id=None, media_type=None, status=None,
                 provider=None, year=None, limit=50, offset=0):
    """Search cached library memberships with literal title matching and pagination."""
    _page(limit, offset)
    records, selection = _selection(server_id, library_id)
    if query:
        selection = selection.where(storage.LibraryItem.title.icontains(query, autoescape=True))
    for field, value in (
        (
            storage.LibraryItem.type,
            media_type,
        ),
        (
            storage.LibraryItem.theme_status,
            status,
        ),
        (
            storage.LibraryItem.theme_provider,
            provider,
        ),
        (
            storage.LibraryItem.year,
            year,
        ),
    ):
        if value is not None:
            selection = selection.where(field == value)
    selection = selection.order_by(
        storage.LibraryItem.title,
        storage.LibraryItem.server_id,
        storage.LibraryItem.rating_key,
        storage.LibraryItem.section_key,
    ).offset(offset).limit(limit + 1)
    with Session(storage.engine()) as database:
        rows = database.scalars(selection).all()
        items = [_item(row) for row in rows[:limit]]
    return {
        'items': items,
        'next_offset': offset + limit if len(rows) > limit else None,
        'cached': True,
        'snapshots': _freshness(records),
    }


def get_theme_coverage(server_id=None, library_id=None):
    """Count unique server items, avoiding duplicates across library memberships."""
    records, selection = _selection(server_id, library_id)
    with Session(storage.engine()) as database:
        rows = database.scalars(selection).all()
        unique = {(
            row.server_id,
            row.rating_key,
        ): row for row in rows}
        statuses = Counter(row.theme_status for row in unique.values())
        installed = sum(row.theme for row in unique.values())
    total = len(unique)
    return {
        'total': total,
        'installed': installed,
        'coverage_percent': round(installed / total * 100, 1) if total else 0,
        'statuses': dict(statuses),
        'cached': True,
        'snapshots': _freshness(records),
    }


def inspect_theme(server_id, item_id):
    """Inspect cached theme metadata, recorded errors, and successful upload tracking."""
    records, selection = _selection(server_id)
    with Session(storage.engine()) as database:
        row = database.scalars(selection.where(storage.LibraryItem.rating_key == item_id)).first()
        if row is None:
            raise ToolInputError('Item not found in the cached snapshot. Refresh libraries first.')
        result = _item(row)
        trusted_id = row.rating_key
    with storage.server_scope(records[0]['id']):
        reason = storage.get_errors().get(trusted_id)
        tracking = storage.get_tracking(trusted_id)
    result.update({
        'error': logger.redact(reason) if reason else None,
        'tracking': {key: value for key, value in tracking.items() if key in (
            'youtube_theme_url',
            'audio_codec',
            'audio_sha256',
            'mp4a_available',
        )},
        'cached': True,
        'snapshots': _freshness(records),
    })
    return result


def check_themerrdb(media_type, database_id, database='themoviedb'):
    """Check an external identifier against the hourly ThemerrDB index.

    Parameters
    ----------
    media_type : str
        ``movie``, ``show``, or ``collection``.
    database_id : str
        Numeric TMDB identifier or an IMDb identifier starting with ``tt``.
    database : str
        ``themoviedb`` for any supported media type, or ``imdb`` for movies.

    Returns
    -------
    dict
        Membership result, checked identifier, and index refresh timestamp.

    Raises
    ------
    ToolInputError
        When the identifier is invalid or the requested index is unavailable.
    """
    database_type = _DATABASE_TYPES.get(media_type)
    if database_type is None or database not in themerr_db.db_field_name[database_type]:
        raise ToolInputError('Use TMDB IDs for movies, shows, or collections; IMDb IDs are only supported for movies.')
    digits = database_id.removeprefix('tt') if database == 'imdb' else database_id
    if not digits.isascii() or not digits.isdecimal() or (database == 'imdb' and not database_id.startswith('tt')):
        raise ToolInputError('Use a numeric TMDB ID or an IMDb ID starting with tt.')
    themerr_db.update_cache()
    if database not in themerr_db.database_cache.get(database_type, {}):
        raise ToolInputError('The ThemerrDB index is unavailable. Try again after its next hourly refresh.')
    return {
        'exists': themerr_db.item_exists(database_type, database, database_id),
        'media_type': media_type,
        'database_type': database_type,
        'database': database,
        'database_id': database_id,
        'cached': True,
        'last_refresh': datetime.fromtimestamp(themerr_db.last_cache_update, timezone.utc).isoformat(),
    }


def get_activity():
    """Report recent dispatch jobs and queue state separately from upload completion."""
    return {
        'jobs': scheduled_tasks.job_history(),
        **processing.queue_state(),
    }


def get_logs(source='all', level=None, query='', limit=100, cursor=None):
    """Filter a bounded redacted log batch; an optional cursor selects session history."""
    _page(limit, 0)
    if source not in (
        'all',
        *logger.LOG_NAMES,
    ) or (cursor is not None and cursor < 0):
        raise ToolInputError('Invalid log selection.')
    data = (log_viewer.snapshot(source, limit) if cursor is None
            else log_viewer.session_snapshot(source, limit, cursor))
    data['entries'] = [entry for entry in data['entries']
                       if (level is None or entry['level'] == level)
                       and query.casefold() in entry['message'].casefold()]
    return data


def refresh_libraries():
    """Dispatch a dashboard refresh without queuing a theme scan."""
    job = scheduled_tasks.run_threaded(target=cache.cache_data, task_name='Dashboard refresh')
    return {
        'job_id': job.job_id,
        'status': 'dispatched',
    }


def retry_items(server_id, item_ids):
    """Queue verified cached items while preserving processing and overwrite policies."""
    if not 1 <= len(item_ids) <= 100:
        raise ToolInputError('Select between 1 and 100 item IDs.')
    records, selection = _selection(server_id)
    record = records[0]
    if not config.CONFIG['Themerr']['BOOL_THEMERR_ENABLED'] or not record['enabled']:
        raise ToolInputError('Enable theme updates and this server before retrying items.')
    with Session(storage.engine()) as database:
        rows = database.scalars(selection.where(storage.LibraryItem.rating_key.in_(item_ids))).all()
        known = {row.rating_key: row for row in rows}
        if set(item_ids) != set(known):
            raise ToolInputError('An item was not found in the cached snapshot. Refresh libraries first.')
        ignored = set(record.get('ignored_libraries', '').split(',')) - {''}
        if any(row.section_key in ignored for row in rows):
            raise ToolInputError('An item belongs to an ignored library.')
        trusted_ids = sorted(known)
    queued = []
    existing = []
    with storage.server_scope(record['id']):
        for item_id in trusted_ids:
            if processing.enqueue(item_id):
                queued.append(item_id)
            else:
                existing.append(item_id)
    logger.get_logger(__name__).info('Retry requested for server %s: %d queued, %d already active',
                                     record['id'], len(queued), len(existing))
    return {
        'queued_item_ids': queued,
        'already_queued_or_active_item_ids': existing,
        'status': 'dispatched',
    }

"""Authenticated server management, discovery, and dashboard views."""

# standard imports
import re

# lib imports
from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

# local imports
from common.http import read_json, render_template
from common import config, logger
from common.validation import ValidationError
from media_servers import get_backend, processing
from media_servers.base import MediaServerError
from themerr import storage, theme_errors

router = APIRouter()
log = logger.get_logger(__name__)


def _failure(error: Exception, message: str, status: int = 502):
    """Report the failure category without exposing upstream tokens or response bodies.

    Parameters
    ----------
    error : Exception
        Original failure; only its class name is logged.
    message : str
        Safe explanation for the administrator.
    status : int, optional
        HTTP response status.

    Returns
    -------
    JSONResponse
        JSON response with the requested status code.
    """
    log.warning('%s (%s)', message, type(error).__name__)
    return JSONResponse({'message': message}, status_code=status)


async def _payload(request: Request) -> dict:
    """Reject JSON arrays and scalar values at the API boundary."""
    payload = await read_json(request)
    return _validated_payload(payload)


def _validated_payload(payload) -> dict:
    """Require an object before passing submitted preferences to the server store."""
    if not isinstance(payload, dict):
        raise HTTPException(400, 'Invalid JSON object.')
    return payload


def _metadata_url(item: dict) -> str | None:
    """Link validated external IDs to their metadata provider.

    Parameters
    ----------
    item : dict
        Cached media item and resolved external identifiers.

    Returns
    -------
    str or None
        Provider URL when the ID and media type are supported.
    """
    identifier = str(item.get('database_id') or item.get('source_id') or '')
    database = item.get('source_database')
    if item.get('database_id'):
        database = 'imdb' if identifier.startswith('tt') else 'themoviedb'
    if database == 'imdb' and re.fullmatch(r'tt\d+', identifier):
        return 'https://www.imdb.com/title/' + identifier + '/'
    if not identifier.isascii() or not identifier.isdigit():
        return None
    if database == 'themoviedb':
        category = {'movie': 'movie', 'show': 'tv', 'collection': 'collection'}.get(item.get('type'))
        return 'https://www.themoviedb.org/' + category + '/' + identifier if category else None
    if database == 'thetvdb' and item.get('type') == 'show':
        return 'https://thetvdb.com/dereferrer/series/' + identifier
    return None


def dashboard() -> tuple[dict, dict, dict]:
    """Assemble library snapshots without merging rating keys from different servers.

    Returns
    -------
    tuple of dict
        Libraries, scoped errors, and real summary counts.
    """
    libraries, errors = {}, {}
    backend = get_backend()
    registered = backend.list_servers()
    for record in registered:
        server = backend.server(record['id'])
        with storage.server_scope(record['id']):
            snapshot = storage.get_dashboard() or {}
            failures = storage.get_errors()
        for key, section in snapshot.items():
            identity = record['id'] + ':' + key
            urls = server.web_urls(str(section['key']))
            libraries[identity] = {**section, 'server_id': record['id'], 'server_name': record['name'],
                                   'server_label': backend.display_name(record['id']),
                                   'enabled': record['enabled'], 'server_url': urls['server'],
                                   'library_url': urls['library']}
            for item in section['items']:
                item['server_id'] = record['id']
                item['error'] = failures.get(item['rating_key'])
                item['server_item_url'] = server.web_urls(
                    str(section['key']), item['rating_key'])['item']
                item['metadata_url'] = _metadata_url(item)
                item['show_edit'] = theme_errors.is_video_issue(item['error'])
                errors[record['id'] + ':' + item['rating_key']] = item['error']
    items = [item for section in libraries.values() for item in section['items']]
    installed = sum(item['theme'] for item in items)
    return libraries, errors, {'total': len(items), 'installed': installed,
                               'coverage': round(installed / len(items) * 100) if items else 0,
                               'attention': sum(bool(item['error']) or item['theme_status'] in ('failed', 'unresolved')
                                                for item in items), 'servers': len(registered),
                               'libraries': len(libraries)}


@router.api_route('/servers', methods=['GET', 'HEAD'], name='server_ui.server_page', response_model=None)
def server_page(request: Request):
    """Render saved connections and discovery options.

    Returns
    -------
    str
        Server management page.
    """
    from plex import ssh

    saved_servers = get_backend().list_servers()
    return render_template(
        request,
        'servers.html',
        title='Servers',
        servers=saved_servers,
        plex_ssh_settings={server['id']: ssh.settings(server['id']) for server in saved_servers
                           if server['type'] == 'plex'},
        plex_connected=get_backend().account_connected(),
        jellyfin_settings=config.CONFIG['Jellyfin'],
    )


@router.api_route('/api/servers/{server_id}/libraries', methods=['GET', 'HEAD'],
                  name='server_ui.server_libraries', response_model=None)
def server_libraries(server_id: str):
    """List library names and IDs for one saved server.

    Parameters
    ----------
    server_id : str
        Saved server identifier.

    Returns
    -------
    Response
        Library choices, using cached metadata when the server is paused or offline.
    """
    record = get_backend().get_server(server_id)
    if record is None:
        return JSONResponse({'message': 'Server not found.'}, status_code=404)
    with Session(storage.engine()) as session:
        cached = [{'id': str(key), 'title': title} for key, title in session.execute(
            select(storage.LibrarySection.key, storage.LibrarySection.title).where(
                storage.LibrarySection.server_id == server_id).order_by(storage.LibrarySection.title))]
    if record['enabled']:
        try:
            libraries = get_backend().server(server_id).libraries()
            if libraries is not None:
                return JSONResponse({'libraries': libraries, 'cached': False})
        except Exception as exc:
            log.warning('Could not load media-server libraries (%s)', type(exc).__name__)
    return JSONResponse({'libraries': cached, 'cached': True})


@router.api_route(
    '/api/servers/{server_id}',
    methods=['POST', 'DELETE'],
    name='server_ui.edit_server',
    response_model=None,
)
def edit_server(request: Request, server_id: str, payload=Depends(read_json)):
    """Save processing preferences or remove a server.

    Parameters
    ----------
    server_id : str
        Server identifier.

    Returns
    -------
    Response
        Updated settings or removal result.
    """
    if not get_backend().get_server(server_id):
        return JSONResponse({'message': 'Server not found.'}, status_code=404)
    if request.method != 'DELETE':
        payload = _validated_payload(payload)
    try:
        if request.method == 'DELETE':
            get_backend().remove_server(server_id)
        else:
            get_backend().update_server(server_id, payload)
    except ValidationError as exc:
        return _failure(exc, exc.reason.value, 400)
    except MediaServerError as exc:
        return _failure(exc, str(exc), exc.status_code)
    except Exception as exc:
        return _failure(exc, f'Could not update this {get_backend().display_name(server_id)} server. '
                        'Check its connection and settings.', 500)
    get_backend().start_listeners()
    return JSONResponse({'message': 'Server removed.' if request.method == 'DELETE' else 'Server settings saved.'})


def _refresh():
    """Dispatch one guarded dashboard refresh.

    Returns
    -------
    Thread
        Thread carrying the refresh job identifier.
    """
    from themerr import cache, scheduled_tasks
    return scheduled_tasks.run_threaded(target=cache.cache_data, task_name='Dashboard refresh')


@router.api_route('/api/tasks/refresh', methods=['POST'], name='server_ui.refresh', response_model=None)
def refresh(payload=Depends(_payload)):
    """Queue a dashboard refresh and optional theme scan.

    Returns
    -------
    Response
        Dispatch confirmation; processing continues in the background.
    """
    from common import config
    from themerr import scheduled_tasks
    if payload.get('scan'):
        if not config.CONFIG['Themerr']['BOOL_THEMERR_ENABLED']:
            return JSONResponse(
                {'message': 'Enable theme updates in Settings before starting a scan.'},
                status_code=400,
            )
        scheduled_tasks.run_threaded(target=processing.scheduled_update, task_name='Theme scan and queue')
    job = _refresh()
    return JSONResponse(
        {'message': 'Refreshing libraries. Follow progress in Activity.', 'job_id': job.job_id},
        status_code=202,
    )


@router.api_route('/api/tasks/retry', methods=['POST'], name='server_ui.retry', response_model=None)
def retry(payload=Depends(_payload)):
    """Queue one cached library item for another theme attempt.

    Returns
    -------
    Response
        Dispatch confirmation, or a fixed client error when the item cannot be retried.
    """
    from themerr import mcp_tools
    server_id = payload.get('server_id')
    item_id = payload.get('item_id')
    if not all(isinstance(value, str) and value for value in (
        server_id,
        item_id,
    )):
        return JSONResponse({'message': 'Select an item to retry.'}, status_code=400)
    try:
        result = mcp_tools.retry_items(server_id, [item_id])
    except mcp_tools.ToolInputError as error:
        return JSONResponse({'message': str(error)}, status_code=400)
    message = 'Retry queued. Follow progress in Activity.' if result['queued_item_ids'] \
        else 'This item is already queued.'
    return JSONResponse({'message': message}, status_code=202)


@router.api_route('/api/themerrdb', methods=['GET', 'HEAD'], name='server_ui.database_status', response_model=None)
def database_status():
    """Return the latest successful ThemerrDB Pages deployment from the hourly cache.

    Returns
    -------
    Response
        Deployment metadata and check timestamps.
    """
    from themerr import deployment_status
    return JSONResponse(deployment_status.publication_status())


@router.api_route('/activity', methods=['GET', 'HEAD'], name='server_ui.activity', response_model=None)
def activity(request: Request):
    """Show real task state and actionable media failures.

    Returns
    -------
    str
        Activity page with job history and failure details.
    """
    from themerr import scheduled_tasks
    libraries, _, _ = dashboard()
    failures = [{
        'server': section['server_name'], 'server_label': section['server_label'], 'library': section['title'], **item,
        'reason': item['error'] or (
            f'TMDB ID unavailable. Review the item metadata in {get_backend().display_name(item["server_id"])}.'
            if item['theme_status'] == 'unresolved' else 'The theme could not be added.'
        ),
    } for section in libraries.values() for item in section['items']
        if item['error'] or item['theme_status'] in ('failed', 'unresolved')]
    return render_template(
        request,
        'activity.html',
        title='Activity',
        jobs=scheduled_tasks.job_history(),
        failures=failures,
        queue_size=processing.q.qsize(),
    )


@router.api_route('/api/tasks', methods=['GET', 'HEAD'], name='server_ui.task_status', response_model=None)
def task_status():
    """Return bounded job history for the activity view.

    Returns
    -------
    Response
        Recent tasks and current queued item count.
    """
    from themerr import scheduled_tasks
    return JSONResponse({'jobs': scheduled_tasks.job_history(), 'queue_size': processing.q.qsize()})

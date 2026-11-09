"""Persist ThemerrDB's published deployment timestamp and an hourly check limit."""

# standard imports
from datetime import datetime, timezone
import json
from threading import Lock
import time

# lib imports
import requests
from sqlalchemy.orm import Session

# local imports
from common import logger
from themerr import storage

log = logger.get_logger(__name__)
_lock = Lock()
_CACHE_KEY = 'themerrdb_deployment'
_CACHE_VERSION = 3
_INTERVAL = 3600
_DEPLOYMENT_URL = 'https://themerr.lizardbyte.dev/ThemerrDB/deployment.json'


def _load() -> dict:
    """Load installation-wide deployment metadata from SQLite."""
    with Session(storage.engine()) as database:
        row = database.get(storage.AppSetting, _CACHE_KEY)
        try:
            data = json.loads(row.value) if row else {}
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    """Persist the attempt deadline before making an external request."""
    with Session(storage.engine()) as database:
        row = database.get(storage.AppSetting, _CACHE_KEY)
        if row is None:
            database.add(storage.AppSetting(key=_CACHE_KEY, value=json.dumps(data)))
        else:
            row.value = json.dumps(data)
        database.commit()


def _fetch_publication() -> dict:
    """Validate the timestamp published with the deployed database."""
    with requests.get(
        _DEPLOYMENT_URL,
        headers={
            'Accept': 'application/json',
            'Cache-Control': 'no-cache',
        },
        timeout=10, allow_redirects=False,
    ) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('Unexpected deployment response')
        metadata = response.json()
    if not isinstance(metadata, dict):
        raise ValueError('Invalid deployment metadata')
    date = datetime.fromisoformat(metadata['deployed_at'])
    if date.tzinfo is None:
        raise ValueError('Deployment timestamp must include a timezone')
    return {'updated_at': date.astimezone(timezone.utc).isoformat()}


def publication_status() -> dict:
    """Return the published database deployment, checking at most once per hour.

    Checks are serialized across application threads. The attempt deadline is saved
    before the request, so failures and application restarts also respect the limit.
    Failed checks preserve the last known successful deployment.

    Returns
    -------
    dict
        Published deployment date and repository link, last attempt and next check timestamps,
        and whether the last check failed. Dates use UTC; timestamps use Unix seconds.
    """
    with _lock:
        state = _load()
        now = time.time()
        if state.get('version') != _CACHE_VERSION or now >= state.get('next_check', 0):
            state.update(version=_CACHE_VERSION, checked_at=now, next_check=now + _INTERVAL, stale=True)
            _save(state)
            try:
                publication = _fetch_publication()
                state.pop('updated_at', None)
                state.pop('run_id', None)
                state.update(publication, stale=False)
            except (requests.RequestException, ValueError, TypeError, KeyError) as error:
                log.warning('Unable to check ThemerrDB deployment (%s); retrying in one hour', type(error).__name__)
            _save(state)
        return {
            'updated_at': state.get('updated_at'),
            'url': 'https://github.com/Themerr/ThemerrDB',
            'checked_at': state.get('checked_at'),
            'next_check': state.get('next_check'),
            'stale': state.get('stale', True),
        }

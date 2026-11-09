"""Desktop release alerts and aggregate dashboard coverage progress."""

# standard imports
import json
import os
from pathlib import Path
from threading import RLock
import time
from urllib.parse import quote

# lib imports
from packaging.version import InvalidVersion, Version
import requests
from sqlalchemy.orm import Session

# local imports
from common import config, definitions, locales, logger, version
from media_servers import get_backend
from themerr import storage

log = logger.get_logger(__name__)
_lock = RLock()
_notifier = None
_RELEASE_KEY = 'release_notifications'
_COVERAGE_KEY = 'coverage_notifications'
_RELEASE_API = 'https://api.github.com/repos/Themerr/Themerr/releases'
_RELEASE_PAGE = 'https://github.com/Themerr/Themerr/releases'


def _load(key: str) -> dict:
    with Session(storage.engine()) as database:
        row = database.get(storage.AppSetting, key)
        try:
            data = json.loads(row.value) if row else {}
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


def _save(key: str, data: dict) -> None:
    with Session(storage.engine()) as database:
        database.merge(storage.AppSetting(key=key, value=json.dumps(data)))
        database.commit()


def _desktop_available() -> bool:
    return not definitions.Modes.DOCKER and (
        definitions.Platform.os_platform != 'linux' or bool(os.environ.get('DBUS_SESSION_BUS_ADDRESS'))
    )


def _send(title: str, message: str) -> bool:
    global _notifier
    if not _desktop_available():
        return False
    with _lock:
        try:
            if _notifier is None:
                from desktop_notifier import Icon
                from desktop_notifier.sync import DesktopNotifierSync
                _notifier = DesktopNotifierSync(
                    app_name=definitions.Names.name,
                    app_icon=Icon(path=Path(definitions.Paths.ROOT_DIR) / 'web' / 'images' / 'icon-default.png'),
                    notification_limit=5,
                )
            _notifier.send(title=title, message=message)
        except Exception as error:
            # Notification availability must never change the outcome of a refresh.
            log.warning('Desktop notification unavailable (%s)', type(error).__name__)
            return False
    return True


def _release_candidate(release: object, prereleases: bool) -> tuple[Version, str] | None:
    if not isinstance(release, dict) or type(release.get('draft')) is not bool or (
            type(release.get('prerelease')) is not bool or not isinstance(release.get('tag_name'), str)):
        raise ValueError('Invalid release metadata')
    if release['draft'] or (release['prerelease'] and not prereleases):
        return None
    try:
        available = Version(release['tag_name'])
    except InvalidVersion:
        return None
    if not prereleases and (available.is_prerelease or available.is_devrelease):
        return None
    return available, release['tag_name']


def _fetch_release(prereleases: bool) -> tuple[Version, str] | None:
    with requests.get(
        _RELEASE_API if prereleases else _RELEASE_API + '/latest',
        params={'per_page': 100} if prereleases else None,
        headers={'Accept': 'application/vnd.github+json', 'Cache-Control': 'no-cache'},
        timeout=10, allow_redirects=False,
    ) as response:
        if response.status_code == 404 and not prereleases:
            return None
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('Unexpected release response')
        payload = response.json()
    releases = payload if prereleases else [payload]
    if not isinstance(releases, list):
        raise ValueError('Invalid release list')
    candidates = []
    for release in releases:
        candidate = _release_candidate(release, prereleases)
        if candidate is not None:
            candidates.append(candidate)
    return max(candidates, key=lambda candidate: candidate[0]) if candidates else None


def check_for_releases() -> None:
    """Check hourly for a newer release and remember alerts across restarts.

    Pre-releases require the installation's explicit opt-in. Source checkouts
    compare against 0.0.0; installations without a desktop skip the check.
    """
    if not config.CONFIG['Notifications']['NEW_RELEASE'] or not _desktop_available():
        return
    try:
        installed = Version(version.VERSION)
        with _lock:
            prereleases = config.CONFIG['Notifications']['FOLLOW_PRERELEASES']
            state = _load(_RELEASE_KEY)
            now = time.time()
            if state.get('prereleases') == prereleases and now < state.get('next_check', 0):
                return
            state.update(prereleases=prereleases, next_check=now + 3600)
            _save(_RELEASE_KEY, state)
            release = _fetch_release(prereleases)
            if release is None:
                return
            available, tag = release
            if available <= installed or available <= Version(state.get('notified_version', '0.0.0')):
                return
            _ = locales.get_text()
            if _send(_('New release available'), _('%(app)s %(version)s is available.\n%(url)s') % {
                    'app': definitions.Names.name, 'version': tag,
                    'url': _RELEASE_PAGE + '/tag/' + quote(tag, safe=''),
            }):
                state['notified_version'] = str(available)
                _save(_RELEASE_KEY, state)
    except Exception as error:
        log.warning('Release notification check failed (%s)', type(error).__name__)


def refresh_completed() -> None:
    """Notify only when total theme coverage rises between successful refreshes.

    Counts include media and collections across all saved servers, matching the
    dashboard. The first refresh or a changed server selection sets a baseline.
    """
    try:
        with _lock:
            records = get_backend().list_servers()
            items = []
            for record in records:
                with storage.server_scope(record['id']):
                    snapshot = storage.get_dashboard() or {}
                items.extend(item for section in snapshot.values() for item in section['items'])
            current = {'servers': sorted((record['id'], record['enabled']) for record in records),
                       'total': len(items), 'installed': sum(bool(item['theme']) for item in items)}
            # JSON turns tuples into lists; use the same representation for comparison.
            current['servers'] = [list(record) for record in current['servers']]
            previous = _load(_COVERAGE_KEY)
            _save(_COVERAGE_KEY, current)
            if not config.CONFIG['Notifications']['COVERAGE_INCREASE'] or (
                    previous.get('servers') != current['servers'] or not previous.get('total') or not current['total']):
                return
            if current['installed'] * previous['total'] <= previous['installed'] * current['total']:
                return
            before = previous['installed'] / previous['total'] * 100
            after = current['installed'] / current['total'] * 100
            _ = locales.get_text()
            _send(_('Refresh completed'), _(
                'Theme coverage increased by %(increase)s percentage points: %(before)s%% → %(after)s%%. '
                '%(installed)s of %(total)s items have themes.'
            ) % {'increase': f'{after - before:.3g}', 'before': f'{before:.2f}', 'after': f'{after:.2f}',
                 'installed': current['installed'], 'total': current['total']})
    except Exception as error:
        log.warning('Coverage notification unavailable (%s)', type(error).__name__)

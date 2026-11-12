"""Jellyfin implementations of shared dashboard, scanning, upload, and streaming contracts."""

# standard imports
from urllib.parse import urlencode

# local imports
from common import config, helpers, logger
from jellyfin import connector, metadata, servers
from jellyfin.audio import theme_file
from jellyfin.client import identifier
from media_servers.base import MediaServer, MediaServerBackend, MediaServerError
from themerr import storage, theme_errors
from youtube.youtube_dl import download_youtube

log = logger.get_logger(__name__)
_SETTINGS = {
    'Movie': 'BOOL_MOVIE_SUPPORT',
    'Series': 'BOOL_SERIES_SUPPORT',
    'BoxSet': 'BOOL_COLLECTION_SUPPORT',
}
_LIBRARY_FOLDERS = '/Library/VirtualFolders'
_ITEM_PREFIX = '/Items/'


def _items(connection, **params):
    """Iterate complete bounded pages, including libraries larger than one API page."""
    offset = 0
    seen = set()
    while True:
        page = connection.json(
            'GET',
            '/Items',
            params={
                'Recursive': True,
                'CollapseBoxSetItems': False,
                'IncludeItemTypes': 'Movie,Series,BoxSet',
                'Fields': 'ProviderIds,LockData',
                'StartIndex': offset,
                'Limit': 200,
                **params,
            },
        )
        rows = page['Items']
        for row in rows:
            key = identifier(row['Id'])
            if key not in seen:
                seen.add(key)
                yield row
        offset += len(rows)
        if not rows or offset >= page['TotalRecordCount']:
            break


def _eligible(item):
    """Honor Jellyfin category settings and the shared locked-metadata preference."""
    setting = _SETTINGS.get(item.get('Type'))
    return bool(
        setting
        and config.CONFIG['Jellyfin'][setting]
        and (not item.get('IsLocked') or config.CONFIG['Jellyfin']['BOOL_IGNORE_LOCKED_FIELDS'])
    )


def _theme_state(connection, item_id):
    """Optionally import digest-verified legacy ownership through the authenticated connector."""
    route = f'/Themerr/Items/{identifier(item_id)}/Theme'
    state = connection.json(
        'GET',
        route,
        error_messages={
            400: 'This Jellyfin item needs an accessible, dedicated media folder. '
                 'Put each movie in its own folder, rescan the Jellyfin library, and retry.',
        },
    )
    if state.get('present') and not state['owned'] and config.CONFIG['Jellyfin']['IMPORT_LEGACY_OWNERSHIP']:
        state = connection.json('POST', f'{route}/Import', headers={'X-Themerr-Connector': connector.bundle()['build']})
    return state


class JellyfinMediaServer(MediaServer):
    """Apply native Jellyfin operations within an independent storage scope."""

    def libraries(self):
        connection = servers.client(self.server_id)
        return [
            {
                'id': identifier(row['ItemId']),
                'title': row['Name'],
            }
            for row in connection.json('GET', _LIBRARY_FOLDERS)
        ]

    def web_urls(self, library_id, item_id=None):
        record = servers.get_server(self.server_id)
        url = f'{record["url"]}/web/index.html#!/'
        library_query = urlencode(
            {
                'topParentId': identifier(library_id),
                'serverId': self.server_id.removeprefix('jellyfin:'),
            }
        )
        urls = {
            'server': f'{url}home.html',
            'library': f'{url}movies.html?{library_query}',
        }
        if item_id is not None:
            urls['item'] = f'{url}details?{
                urlencode(
                    {
                        "id": identifier(item_id),
                        "serverId": self.server_id.removeprefix("jellyfin:"),
                    }
                )
            }'
        return urls

    def cache_dashboard(self):
        with storage.server_scope(self.server_id):
            connection = servers.client(self.server_id)
            revision = storage.dashboard_revision()
            snapshots = {}
            errors = storage.get_errors()
            try:
                connector.verify(connection)
                connected = True
            except MediaServerError:
                connected = False
            for library in connection.json('GET', _LIBRARY_FOLDERS):
                key = identifier(library['ItemId'])
                themed = {identifier(row['Id']) for row in _items(connection, ParentId=key, HasThemeSong=True)}
                rows = [
                    self._dashboard_item(item, themed, errors, connection if connected else None)
                    for item in _items(connection, ParentId=key)
                ]
                media = [row for row in rows if row['type'] != 'collection']
                collections = [row for row in rows if row['type'] == 'collection']
                snapshots[key] = {
                    'key': key,
                    'title': library['Name'],
                    'agent': 'jellyfin',
                    'type': {
                        'tvshows': 'show',
                        'boxsets': 'collection',
                    }.get(library.get('CollectionType'), 'movie'),
                    'items': rows,
                    'media_count': len(media),
                    'media_percent_complete': self._percent(media),
                    'collection_count': len(collections),
                    'collection_percent_complete': self._percent(collections),
                    'collections_enabled': config.CONFIG['Jellyfin']['BOOL_COLLECTION_SUPPORT'],
                    'total_count': len(rows),
                }
            storage.replace_dashboard(snapshots, since_revision=revision)
            return True

    @staticmethod
    def _percent(rows):
        """Calculate installed-theme coverage for one category."""
        return int(sum(row['theme'] for row in rows) / len(rows) * 100) if rows else 0

    @staticmethod
    def _dashboard_item(item, themed, errors, connection):
        """Map native metadata to the existing dashboard dictionary."""
        details = metadata.resolve(item)
        key = identifier(item['Id'])
        theme = key in themed
        status = 'unresolved'
        if theme:
            status = 'complete'
        elif key in errors:
            status = 'failed'
        if not theme and key not in errors and details['database_id']:
            status = 'pending' if details['exists'] else 'missing'
        owned = False
        if theme and connection is not None:
            owned = _theme_state(connection, key)['owned']
        provider = None
        if owned:
            provider = 'themerr'
        elif theme:
            provider = 'uploaded'
        return {
            **{name: value for name, value in details.items() if name != 'exists'},
            'rating_key': key,
            'title': item['Name'],
            'year': item.get('ProductionYear'),
            'agent': 'jellyfin',
            'theme': theme,
            'theme_provider': provider,
            'theme_status': status,
        }

    def scan(self, enqueue):
        with storage.server_scope(self.server_id):
            connection = servers.client(self.server_id)
            connector.verify(connection)
            ignored = set(servers.get_server(self.server_id)['ignored_libraries'].split(','))
            for library in connection.json('GET', _LIBRARY_FOLDERS):
                key = identifier(library['ItemId'])
                if key in ignored:
                    continue
                for item in _items(connection, ParentId=key):
                    if _eligible(item):
                        enqueue(identifier(item['Id']))

    def update_item(self, item_id):
        with storage.server_scope(self.server_id):
            item_id = identifier(item_id)
            if not config.CONFIG['Themerr']['BOOL_THEMERR_ENABLED']:
                return False
            try:
                return self._update(item_id)
            except MediaServerError as exc:
                log.warning('Theme update failed for item %s on server %s: %s', item_id, self.server_id, str(exc))
                theme_errors.set_error(item_id, str(exc))
                return False

    def _update(self, item_id):
        """Resolve a theme and upload verified audio only when native ownership permits it."""
        connection = servers.client(self.server_id)
        descriptor = connector.verify(connection)
        # The single-item endpoint requires a user context, which API keys do not carry.
        item = next(_items(connection, Ids=item_id, Limit=1), None)
        if item is None:
            raise MediaServerError('This Jellyfin item is unavailable.', 404)
        if not _eligible(item):
            return False
        ignored = set(servers.get_server(self.server_id)['ignored_libraries'].split(',')) - {''}
        if ignored:
            ancestors = connection.json('GET', f'{_ITEM_PREFIX}{item_id}/Ancestors')
            if ignored.intersection(identifier(row['Id']) for row in ancestors):
                return False
        state = _theme_state(connection, item_id)
        if state['present'] and not state['owned'] and not config.CONFIG['Jellyfin']['BOOL_OVERWRITE_USER_THEMES']:
            theme_errors.set_error(item_id, None)
            return True
        details = metadata.resolve(item)
        if not details['exists']:
            theme_errors.set_error(item_id, None)
            return False
        data = helpers.json_get(
            cache_time=3600,
            url=f'https://themerr.lizardbyte.dev/ThemerrDB/{details["database_type"]}/'
            f'{details["database"]}/{details["database_id"]}.json',
        )
        source = data.get('youtube_theme_url') if data else None
        if not source:
            return False
        tracked = storage.get_tracking(item_id)
        if self._unchanged(source, state, tracked):
            theme_errors.set_error(item_id, None)
            return True
        return self._upload(connection, item_id, item, source, descriptor)

    @staticmethod
    def _unchanged(source, state, tracked):
        """Require current bytes to match tracking before skipping a source or codec update."""
        return bool(
            state['owned']
            and state.get('sha256') == tracked.get('audio_sha256')
            and source == tracked.get('youtube_theme_url')
            and (
                not config.CONFIG['Themerr']['BOOL_PREFER_MP4A_CODEC']
                or tracked.get('audio_codec') == 'mp4a'
                or tracked.get('mp4a_available') is False
            )
        )

    @staticmethod
    def _upload(connection, item_id, item, source, descriptor):
        """Send validated bytes and persist tracking only after verifying the connector's digest."""
        with download_youtube(source, on_error=lambda reason: theme_errors.set_error(item_id, reason)) as downloaded:
            if downloaded is None:
                return False
            with theme_file(downloaded) as audio:
                return JellyfinMediaServer._send_audio(connection, item_id, item, source, descriptor, audio)

    @staticmethod
    def _send_audio(connection, item_id, item, source, descriptor, audio):
        """Verify acknowledged bytes before recording a successful upload."""
        headers = {
            'Content-Type': 'audio/mp4' if audio.codec == 'mp4a' else 'audio/ogg',
            'X-Themerr-Connector': descriptor['build'],
            'X-Themerr-SHA256': audio.sha256,
            'X-Themerr-Ignore-Locked': str(config.CONFIG['Jellyfin']['BOOL_IGNORE_LOCKED_FIELDS']).lower(),
            'X-Themerr-Overwrite-User': str(config.CONFIG['Jellyfin']['BOOL_OVERWRITE_USER_THEMES']).lower(),
            'X-Themerr-Backup-User': str(config.CONFIG['Jellyfin']['BOOL_BACKUP_USER_THEMES']).lower(),
        }
        with open(audio.path, 'rb') as stream:
            result = connection.json(
                'POST', f'/Themerr/Items/{item_id}/Theme', data=stream, headers=headers, timeout=120
            )
        if not result.get('owned') or result.get('sha256') != audio.sha256:
            raise MediaServerError('Jellyfin could not verify the uploaded theme.', 502)
        storage.save_tracking(
            item_id,
            metadata.TYPES[item['Type']][0],
            {
                'youtube_theme_url': source,
                'audio_codec': audio.codec,
                'mp4a_available': audio.mp4a_available,
                'audio_sha256': audio.sha256,
            },
        )
        storage.mark_dashboard_theme_uploaded(item_id, 'themerr')
        theme_errors.set_error(item_id, None)
        return True

    def open_poster(self, item_id):
        item_id = identifier(item_id)
        connection = servers.client(self.server_id)
        try:
            return connection.request(
                'GET', f'{_ITEM_PREFIX}{identifier(item_id)}/Images/Primary', params={'MaxWidth': 600}, stream=True
            )
        except MediaServerError as exc:
            if exc.status_code == 404:
                return None
            raise

    def open_theme(self, item_id, headers):
        item_id = identifier(item_id)
        connection = servers.client(self.server_id)
        songs = connection.json(
            'GET', f'{_ITEM_PREFIX}{identifier(item_id)}/ThemeSongs', params={'InheritFromParent': False}
        )['Items']
        if not songs:
            raise MediaServerError('This item has no theme.', 404)
        return connection.request(
            'GET',
            f'/Audio/{identifier(songs[0]["Id"])}/stream',
            params={'Static': True},
            headers=dict(headers),
            stream=True,
        )


class JellyfinBackend(MediaServerBackend):
    """Manage Jellyfin connections and use shared scheduled scans for theme updates."""

    name = 'Jellyfin'

    def server(self, server_id):
        return JellyfinMediaServer(server_id)

    list_servers = staticmethod(servers.list_servers)
    get_server = staticmethod(servers.get_server)
    update_server = staticmethod(servers.update_server)
    remove_server = staticmethod(servers.remove_server)
    record_refresh = staticmethod(servers.record_refresh)

    def account_connected(self):
        return False

    def start_listeners(self):
        """Start connector maintenance alongside shared scheduled theme scans."""
        from jellyfin import maintenance

        maintenance.start()

    def stop_listeners(self):
        """Stop connector maintenance when Themerr shuts down."""
        from jellyfin import maintenance

        maintenance.stop()

    def web_router(self):
        from jellyfin.web import router

        return router

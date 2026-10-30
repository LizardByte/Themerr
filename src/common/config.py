"""
src/common/config.py

Responsible for config related functions.
"""
# standard imports
import base64
import copy
import sys
from pathlib import Path
from typing import Optional, List

# lib imports
from configobj import ConfigObj
from validate import Validator, ValidateError

# local imports
from common import definitions
from common import credentials
from common import logger
from common import locales

# get log
log = logger.get_logger(name=__name__)

# get the config filename
FILENAME = definitions.Files.CONFIG

# access the config dictionary here
CONFIG = None

# Keep schema labels as source messages; templates translate them at render time.
_ = str

# increase CONFIG_VERSION default and max when changing default values
# then do `if CONFIG_VERSION == x:` something to change the old default value to the new default value
# then update the CONFIG_VERSION number


# https://regexpattern.com/windows-folder-path/
# https://regexpattern.com/linux-folder-path/
regex_directory = (r'^[a-zA-Z]:\\(?:[^\\/:*?"<>|]+\\?)*$' if definitions.Platform.os_platform == 'win32'
                   else r'^/(?:[^/]+/?)*$')


def on_change_tray_toggle() -> bool:
    """
    Toggle the tray icon.

    This is needed, since ``tray_icon`` cannot be imported at the module level without a circular import.

    Returns
    -------
    bool
        ``True`` if successful, otherwise ``False``.

    See Also
    --------
    common.tray_icon.tray_toggle : ``on_change_tray_toggle`` is an alias of this function.

    Examples
    --------
    >>> on_change_tray_toggle()
    True
    """
    from common import tray_icon
    return tray_icon.tray_toggle()


def on_change_plex_url() -> None:
    """Reconnect to Plex after its configured URL changes.

    Stop the existing listener and start a new connection when a Plex token is available.

    Examples
    --------
    >>> on_change_plex_url()
    """
    from plex.config import reconnect
    reconnect()


_CONFIG_SPEC_DICT = {
    'Info': {
        'type': 'section',
        'name': _('Info'),
        'description': _('For information purposes only.'),
        'icon': 'info',
        'CONFIG_VERSION': {
            'type': 'integer',
            'name': _('Config version'),
            'description': _('The configuration version.'),
            'default': 0,  # increment when updating config
            'min': 0,
            'max': 0,  # increment when updating config
            'extra_class': 'col-md-3',
            'locked': True,
        },
        'FIRST_RUN_COMPLETE': {
            'type': 'boolean',
            'name': _('First run complete'),
            'description': _('Todo: Indicates if the user has completed the initial setup.'),
            'default': False,
            'locked': True,
        },
    },
    'General': {
        'type': 'section',
        'name': _('General'),
        'description': _('General settings.'),
        'icon': 'settings',
        'LOCALE': {
            'type': 'option',
            'name': _('Locale'),
            'description': _('The localization setting to use.'),
            'default': 'en',
            'options': locales.supported_locales,
            'option_names': locales.get_locale_names(),
            'refresh': True,
            'extra_class': 'col-lg-6',
        },
        'LAUNCH_BROWSER': {
            'type': 'boolean',
            'name': _('Launch Browser on Startup '),
            'description': _(f'Open browser when {definitions.Names.name} starts.'),
            'default': True,
        },
        'SYSTEM_TRAY': {
            'type': 'boolean',
            'name': _('Enable System Tray Icon'),
            'description': _(f'Show {definitions.Names.name} shortcut in the system tray.'),
            'default': True,
            'on_change': on_change_tray_toggle,
        },
    },
    'Notifications': {
        'type': 'section',
        'name': _('Notifications'),
        'description': _('Desktop notifications appear on the computer running Themerr. '
                         'Docker and headless installations do not display them.'),
        'icon': 'bell',
        'NEW_RELEASE': {
            'type': 'boolean',
            'name': _('New release available'),
            'description': _('Notify when a newer Themerr release is available. Checks run once per hour.'),
            'default': True,
        },
        'FOLLOW_PRERELEASES': {
            'type': 'boolean',
            'name': _('Follow pre-releases'),
            'description': _('Include pre-releases in new release notifications.'),
            'default': False,
        },
        'COVERAGE_INCREASE': {
            'type': 'boolean',
            'name': _('Theme coverage increased'),
            'description': _('Notify after a successful refresh when total theme coverage increases. '
                             'Refreshes with no increase stay quiet.'),
            'default': True,
        },
    },
    'Logging': {
        'type': 'section',
        'name': _('Logging'),
        'description': _('Logging settings.'),
        'icon': 'file-code',
        'LOG_DIR': {
            'type': 'string',
            'name': _('Log directory'),
            'advanced': True,
            'description': _('The directory where to store the log files.'),
            'pattern': regex_directory,
            'extra_class': 'col-lg-8',
            'button_directory': True,
        },
        'DEBUG_LOGGING': {
            'type': 'boolean',
            'name': _('Debug logging'),
            'advanced': True,
            'description': _('Enable debug logging.'),
            'default': True,
        },
    },
    'Network': {
        'type': 'section',
        'name': _('Network'),
        'description': _('Network settings.'),
        'icon': 'network',
        'HTTP_HOST': {
            'type': 'string',
            'name': _('HTTP host address'),
            'advanced': True,
            'description': _('The HTTP address to bind to.'),
            'default': '0.0.0.0',
            'pattern': r'\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)[.]){3}'
                    r'(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b',
            # https://codverter.com/blog/articles/tech/20190105-extract-ipv4-ipv6-ip-addresses-using-regex.html
            'extra_class': 'col-md-4',
        },
        'HTTP_PORT': {
            'type': 'integer',
            'name': _('HTTP port'),
            'advanced': True,
            'description': _('Port to bind web server to. Note that ports below 1024 may require root.'),
            'default': 9494,
            'min': 21,
            'max': 65535,
            'extra_class': 'col-md-3',
        },
        'HTTP_ROOT': {
            'type': 'string',
            'name': _('HTTP root'),
            'beta': True,
            'description': _('Todo: The base URL of the web server. Used for reverse proxies.'),
            'extra_class': 'col-lg-6',
        },
        'MCP_ALLOWED_HOSTS': {
            'type': 'string',
            'name': _('MCP allowed hosts'),
            'description': _('Addresses MCP clients may use to connect, for example themerr.example:9494. '
                             'Separate addresses with commas. Use :* to allow any port.'),
            'default': 'localhost,localhost:*,127.0.0.1,127.0.0.1:*,[::1],[::1]:*',
            'extra_class': 'col-lg-12',
        },
        'MCP_HTTP': {
            'type': 'boolean',
            'name': _('Serve MCP over HTTP'),
            'default': False,
            'description': _('Use the Connector HTTP port configured under Jellyfin for clients that cannot trust '
                             'Themerr’s self-signed HTTPS certificate. Token authentication is still required. '
                             'Restart Themerr after changing this setting.'),
        },
        'SSL': {
            'type': 'boolean',
            'name': _('SSL'),
            'default': True,
            'description': _('Run the web server with HTTPS. '
                             'Disabling this can be a security risk, do so at your own risk.'),
        },
    },
    'Updater': {
        'type': 'section',
        'name': _('Updater'),
        'description': _('Updater settings.'),
        'icon': 'refresh-cw',
        'AUTO_UPDATE': {
            'type': 'boolean',
            'name': _('Auto update'),
            'beta': True,
            'description': _(f'Todo: Automatically update {definitions.Names.name}.'),
            'default': False,
        },
    },
    'Jellyfin': {
        'type': 'section',
        'name': _('Jellyfin'),
        'description': _('Jellyfin theme processing settings. Connections are managed on the Servers page.'),
        'icon': 'server',
        'BOOL_MOVIE_SUPPORT': {
            'type': 'boolean', 'name': _('Movie support'), 'default': True,
            'description': _('Add themes to Jellyfin movies.'),
        },
        'BOOL_SERIES_SUPPORT': {
            'type': 'boolean', 'name': _('Series support'), 'default': True,
            'description': _('Add themes to Jellyfin series.'),
        },
        'BOOL_COLLECTION_SUPPORT': {
            'type': 'boolean', 'name': _('Collections'), 'default': True,
            'description': _('Add themes inside Jellyfin’s collection directories. Media files can remain elsewhere.'),
        },
        'BOOL_OVERWRITE_USER_THEMES': {
            'type': 'boolean', 'name': _('Overwrite user themes'), 'default': False,
            'description': _('Replace existing theme files, including themes created by the older '
                             'Themerr-jellyfin plugin.'),
        },
        'BOOL_BACKUP_USER_THEMES': {
            'type': 'boolean', 'name': _('Back up replaced user themes'), 'default': True,
            'description': _('Keep existing user themes in a backup folder when replacing them. '
                             'Disable to permanently remove replaced themes.'),
        },
        'IMPORT_LEGACY_OWNERSHIP': {
            'type': 'boolean', 'name': _('Import older Themerr-jellyfin ownership'), 'default': True,
            'description': _('Recognize themes recorded in the older plugin’s database only when their SHA-256 '
                             'still matches. Changed files stay protected. The old database is left untouched.'),
        },
        'BOOL_IGNORE_LOCKED_FIELDS': {
            'type': 'boolean', 'name': _('Ignore locked metadata'), 'default': False,
            'description': _('Allow Jellyfin theme updates for items with locked metadata.'),
        },
        'AUTO_UPDATE_CONNECTOR': {
            'type': 'boolean', 'name': _('Update connector automatically'), 'default': True,
            'description': _('Install the matching connector when Themerr starts or detects a version mismatch. '
                             'Set the reachable Themerr address on the Servers page first.'),
        },
        'AUTO_RESTART': {
            'type': 'boolean', 'name': _('Restart Jellyfin automatically'), 'default': True,
            'description': _('Restart Jellyfin after installing or updating the connector. '
                             'A pending restart is shown on the Servers page before it happens.'),
        },
        'WAIT_FOR_IDLE': {
            'type': 'boolean', 'name': _('Wait for playback to finish'), 'default': True,
            'description': _('Defer connector restarts until there are no playing or paused streams.'),
        },
        'REMOVE_LEGACY_PLUGIN': {
            'type': 'boolean', 'name': _('Remove the older Themerr-jellyfin plugin'), 'default': True,
            'description': _('After installing the connector, uninstall the older Themerr plugin and remove only '
                             'its dedicated repository. Existing theme files are kept.'),
        },
        'REPOSITORY_HTTP_PORT': {
            'type': 'integer', 'name': _('Connector HTTP port'), 'default': 9495, 'min': 0, 'max': 65535,
            'description': _('Serve connector downloads and optionally MCP over HTTP. '
                             'The admin UI stays on HTTPS. Use 0 to disable. '
                             'Restart Themerr after changing this port.'),
        },
    },
    'Plex': {
        'type': 'section',
        'name': _('Plex'),
        'description': _('Plex settings.'),
        'icon': 'chevron-right',
        'PLEX_URL': {
            'type': 'string',
            'name': _('Plex URL'),
            'description': _('The URL to the Plex server.'),
            'default': 'http://127.0.0.1:32400',
            'pattern': r'^https?:\/\/(?:[a-zA-Z0-9-]+\.?)+(:\d{1,5})?$',
            'extra_class': 'col-lg-6',
            'on_change': on_change_plex_url,
            'locked': True,  # Retained only to identify and adopt the former single-server connection.
        },
        'PLEX_APP_SUPPORT_PATH': {
            'type': 'string',
            'name': _('Plex data directory'),
            'description': _('https://support.plex.tv/articles/'
                             '202915258-where-is-the-plex-media-server-data-directory-located/'),
            'pattern': regex_directory,
            'extra_class': 'col-lg-8',
            'button_directory': True,
            'locked': True,
        },
    },
    'Themerr': {
        'type': 'section',
        'name': _('Themerr'),
        'description': _('Themerr settings.'),
        'icon': 'music',
        'BOOL_SCAN_ON_BOOT': {
            'type': 'boolean',
            'name': _('Scan on startup'),
            'description': _('Run the theme scan when Themerr starts. Scheduled scans still run when this is '
                             'disabled. Changes apply the next time Themerr starts.'),
            'default': True,
        },
        'BOOL_THEMERR_ENABLED': {
            'type': 'boolean',
            'name': _('Themerr Enabled'),
            'description': _('When enabled, Themerr will attempt to update themes.'),
            'default': True,
        },
        'BOOL_PLEX_COLLECTION_SUPPORT': {
            'type': 'boolean',
            'name': _('Collections'),
            'description': _('Add themes to collections.'),
            'default': True,
        },
        'BOOL_PLEX_MOVIE_SUPPORT': {
            'type': 'boolean',
            'name': _('Movie support'),
            'description': _('Add themes to movies using the Plex Movie agent.'),
            'default': True,
        },
        'BOOL_PLEX_SERIES_SUPPORT': {
            'type': 'boolean',
            'name': _('Series support'),
            'description': _('Add themes to series using the Plex Series agent.'),
            'default': True,
        },
        'BOOL_OVERWRITE_PLEX_PROVIDED_THEMES': {
            'type': 'boolean',
            'name': _('Overwrite Plex themes'),
            'description': _('When enabled, Themerr will overwrite themes provided by Plex.'),
            'default': True,
        },
        'BOOL_PREFER_MP4A_CODEC': {
            'type': 'boolean',
            'name': _('Prefer MP4A AAC Codec'),
            'description': _('This can improve theme compatibility with Apple devices.'),
            'default': True,
        },
        'BOOL_REMOVE_UNUSED_THEMES': {
            'type': 'boolean',
            'name': _('Remove unused themes'),
            'description': _('Frees up space in your Plex metadata directory. '
                             'This option requires that the "Plex data directory" path is set'),
            'default': True,
        },
        'BOOL_REMOVE_UNUSED_ART': {
            'type': 'boolean',
            'name': _('Remove unused art'),
            'description': _('Applies to collection. '
                             'Frees up space in your Plex metadata directory. '
                             'This option requires that the "Plex data directory" path is set'),
            'default': True,
        },
        'BOOL_REMOVE_UNUSED_POSTERS': {
            'type': 'boolean',
            'name': _('Remove unused posters'),
            'description': _('Applies to collection. '
                             'Frees up space in your Plex metadata directory. '
                             'This option requires that the "Plex data directory" path is set'),
            'default': True,
        },
        'BOOL_UPDATE_COLLECTION_METADATA': {
            'type': 'boolean',
            'name': _('Collection metadata'),
            'description': _('Update collection metadata (poster, art, and summary) during scheduled update.'),
            'default': False,
        },
        'BOOL_IGNORE_LOCKED_FIELDS': {
            'type': 'boolean',
            'name': _('Ignore locked fields.'),
            'description': _('If you used Themerr v2024.813.13709 or lower, '
                             'you may need to enable this to update items.'),
            'default': False,
        },
        'IGNORED_LIBRARY_IDS': {
            'type': 'string',
            'name': _('Ignored library IDs'),
            'description': _('Comma-separated library IDs to exclude from scheduled updates. IDs appear on Home.'),
            'default': '',
            'advanced': True,
        },
        'INT_UPDATE_THEMES_INTERVAL': {
            'type': 'integer',
            'name': _('Update themes interval'),
            'description': _('Interval for automatic update task, in minutes (min: 15).'),
            'default': 60,
            'advanced': True,
            'extra_class': 'col-md-2',
        },
        'INT_UPDATE_DATABASE_CACHE_INTERVAL': {
            'type': 'integer',
            'name': _('Update database cache interval'),
            'description': _('Interval for database cache update task, in minutes (min: 15).'),
            'default': 60,
            'advanced': True,
            'extra_class': 'col-md-2',
        },
        'INT_PLEXAPI_PLEXAPI_TIMEOUT': {
            'type': 'integer',
            'name': _('PlexAPI timeout'),
            'description': _('Increase this slightly if you experience timeouts when adding themes. (min: 1)'),
            'default': 180,
            'advanced': True,
            'extra_class': 'col-md-2',
        },
        'INT_PLEXAPI_UPLOAD_RETRIES_MAX': {
            'type': 'integer',
            'name': _('Theme upload retries'),
            'description': _('If uploading themes fail, retry this many times. (min: 0)'),
            'default': 3,
            'advanced': True,
            'extra_class': 'col-md-2',
        },
        'INT_PLEXAPI_UPLOAD_THREADS': {
            'type': 'integer',
            'name': _('Multiprocessing thread count'),
            'description': _('The number of threads to use when adding themes. Restart required. (min: 1)'),
            'default': 3,
            'advanced': True,
            'extra_class': 'col-md-2',
        },
        'STR_YOUTUBE_COOKIES': {
            'type': 'string',
            'secret': True,
            'name': _('YouTube Cookies'),
            'description': _(
                'Optional: export youtube.com cookies with Get cookies.txt LOCALLY using its JSON format, '
                'then paste the entire JSON array here and save. '
                'Saved cookies are encrypted and masked. Select the eye icon to reveal them. '
                'Leave this blank to keep them, '
                'or select Clear saved cookies to remove them.'
            ),
            'help_url': f'{definitions.DOCUMENTATION_URL}docs/about/usage.html#youtube-cookies',
            'help_label': _('Cookie export instructions'),
            'advanced': True,
        },
    },
}

# Keep existing config keys and values while presenting server-specific settings separately.
PLEX_SETTING_KEYS = frozenset({
    'BOOL_PLEX_COLLECTION_SUPPORT', 'BOOL_PLEX_MOVIE_SUPPORT', 'BOOL_PLEX_SERIES_SUPPORT',
    'BOOL_OVERWRITE_PLEX_PROVIDED_THEMES', 'BOOL_REMOVE_UNUSED_THEMES', 'BOOL_REMOVE_UNUSED_ART',
    'BOOL_REMOVE_UNUSED_POSTERS', 'BOOL_UPDATE_COLLECTION_METADATA', 'BOOL_IGNORE_LOCKED_FIELDS',
    'INT_PLEXAPI_PLEXAPI_TIMEOUT', 'INT_PLEXAPI_UPLOAD_RETRIES_MAX',
})


def settings_groups():
    """Group existing configuration fields by their server integration for display.

    Retain the stored section and key names while separating the settings page into integrations.

    Returns
    -------
    list of dict
        Display sections with their original configuration source and field keys.

    Examples
    --------
    >>> settings_groups()[0]['section']
    'Themerr'
    """
    groups = []
    for section in ('Themerr', 'Plex', 'Jellyfin', 'General', 'Notifications', 'Logging', 'Network'):
        source = 'Themerr' if section == 'Plex' else section
        keys = [key for key, value in _CONFIG_SPEC_DICT[source].items() if isinstance(value, dict)]
        if section == 'Themerr':
            keys = [key for key in keys if key not in PLEX_SETTING_KEYS]
        elif section == 'Plex':
            keys = [key for key in keys if key in PLEX_SETTING_KEYS]
        groups.append({'section': section, 'source': source, 'keys': keys})
    return groups


def is_masked_field(section: str, key: str) -> bool:
    """
    Check if a field is masked.

    This function will check if a field is masked in the config spec dictionary.

    Parameters
    ----------
    section : str
        The section of the config.
    key : str
        The key of the config field.

    Returns
    -------
    bool
        True if the field is masked, otherwise False.

    Examples
    --------
    >>> is_masked_field(section='General', key='API_KEY')
    True
    """
    return _CONFIG_SPEC_DICT.get(section, {}).get(key, {}).get('mask', False)


def encode_value(value: str) -> str:
    """
    Encode a value using base64.

    This function will encode a value using base64.

    Parameters
    ----------
    value : str
        The value to encode.

    Returns
    -------
    str
        The encoded value.

    Examples
    --------
    >>> encode_value('some text')
    'c29tZSB0ZXh0'
    """
    return base64.b64encode(value.encode('utf-8')).decode('utf-8')


def decode_value(value: str) -> str:
    """
    Decode a base64 encoded value.

    This function will decode a base64 encoded value.

    Parameters
    ----------
    value : str
        The value to decode.

    Returns
    -------
    str
        The decoded value. If the value cannot be decoded, an empty string is returned.

    Examples
    --------
    >>> decode_value('c29tZSB0ZXh0')
    'some text'
    """
    try:
        return base64.b64decode(value.encode('utf-8')).decode('utf-8')
    except Exception as e:
        log.error(msg=f"Unable to decode value: {e}")
        return ''


def decode_config(config: ConfigObj) -> dict:
    """
    Prepare configuration fields for display, redacting encrypted secrets.

    Create a decoded copy of the config object while keeping secret settings empty.

    Parameters
    ----------
    config : ConfigObj
        The config object to decode.

    Returns
    -------
    dict
        A decoded copy of the config object.

    Examples
    --------
    >>> config_object = create_config(config_file='config.ini')
    >>> decode_config(config=config_object)
    {...}
    """
    _config = copy.deepcopy(config)  # we need to do a deepcopy to avoid modifying the original config

    for section, options in _config.items():
        for key, value in options.items():
            if _CONFIG_SPEC_DICT.get(section, {}).get(key, {}).get('secret'):
                _config[section][key] = ''
            elif is_masked_field(section=section, key=key):
                _config[section][key] = decode_value(value)
    return _config


def youtube_cookies() -> str:
    """Decrypt the configured YouTube cookies on demand.

    Keep the setting encrypted until the downloader or an explicit settings
    reveal needs the browser session.

    Returns
    -------
    str
        JSON cookie export, or an empty string when unconfigured.

    Examples
    --------
    >>> configuration = create_config(config_file='config.ini')
    >>> youtube_cookies()
    ''
    """
    return credentials.decrypt_setting(CONFIG['Themerr']['STR_YOUTUBE_COOKIES'])


def _format_config_checks(spec: dict) -> str:
    """Format the validation arguments for a ConfigObj specification.

    Parameters
    ----------
    spec : dict
        One configuration option or section specification.

    Returns
    -------
    str
        Parenthesized validation arguments, or an empty string.
    """
    parts = []
    for check in ('min', 'max', 'options', 'default'):
        if check not in spec:
            continue
        value = spec[check]
        if check == 'options':
            parts.extend(f'"{option}"' if isinstance(option, str) else str(option) for option in value)
        elif isinstance(value, str):
            parts.append(f'{check}="{value}"')
        else:
            parts.append(f'{check}={value}')
    return f'({", ".join(parts)})' if parts else ''


def convert_config(d: dict = _CONFIG_SPEC_DICT, _config_spec: Optional[List] = None) -> List:
    """
    Convert a config spec dictionary to a config spec list.

    A config spec dictionary is a custom type of dictionary that will be converted into a standard config spec list
    which can later be used by ``configobj``.

    Parameters
    ----------
    d : dict
        The dictionary to convert.
    _config_spec : Optional[List]
        This should not be set when using this function, but since this function calls itself it needs to pass in the
        list that is being built in order to return the correct list.

    Returns
    -------
    list
        A list representing a configspec for ``configobj``.

    Examples
    --------
    >>> convert_config(d=_CONFIG_SPEC_DICT)
    [...]
    """
    if _config_spec is None:
        _config_spec = []

    for k, v in d.items():
        if isinstance(v, dict):
            # if a default value is not set, then set it to None
            if 'default' not in v:
                v['default'] = ''

            check_value = _format_config_checks(v)

            if v['type'] == 'section':  # config section
                _config_spec.append(f'[{k}]')
            else:  # int option
                _config_spec.append(f"{k} = {v['type']}{check_value}")

        if isinstance(v, dict):
            # continue parsing nested dictionary
            convert_config(d=v, _config_spec=_config_spec)

    return _config_spec


def create_config(config_file: str, config_spec: dict = _CONFIG_SPEC_DICT) -> ConfigObj:
    """
    Create a config file and `ConfigObj` using a config spec dictionary.

    A config spec dictionary is a strictly formatted dictionary that will be converted into a standard config spec list
    to be later used by ``configobj``.

    The created config is validated against a Validator object. This function will remove keys from the user's
    config.ini if they no longer exist in the config spec.

    Parameters
    ----------
    config_file : str
        Full filename of config file.
    config_spec : dict, default = _CONFIG_SPEC_DICT
        Config spec to use.

    Returns
    -------
    ConfigObj
        Dictionary of config keys and values.

    Raises
    ------
    SystemExit
        If config_spec is not valid.

    Examples
    --------
    >>> create_config(config_file='config.ini')
    ConfigObj({...})
    """
    # convert config spec dictionary to list
    config_spec_list = convert_config(d=config_spec)

    config = ConfigObj(
        configspec=config_spec_list,
        encoding='UTF-8',
        list_values=True,
        stringify=True,
        write_empty_values=False,
    )
    config_valid = validate_config(config=config)

    if not config_valid:
        # logger may not be initialized
        log_msg = "Unable to initialize due to a corrupted config spec. Exiting..."
        log.error(msg=log_msg)
        raise SystemExit(log_msg)

    user_config = ConfigObj(
        infile=config_file,
        configspec=config_spec_list,
        encoding='UTF-8',
        list_values=True,
        stringify=True,
        write_empty_values=False,
    )
    user_config_valid = validate_config(config=user_config)
    if not user_config_valid:
        # write to stderr and logger
        log_msg = "Invalid 'config.ini' file, attempting to correct.\n"
        log.error(msg=log_msg)
        sys.stderr.write(log_msg)

    # dictionary comprehension
    if config_valid and user_config_valid:
        # Remove obsolete sections and root values before reading section options.
        user_config = {
            key: {
                k: v for k, v in value.items() if k in config[key]
            } for key, value in user_config.items() if key in config
        }

        # merge user config into default config
        config.merge(indict=user_config)

        # validate merged config
        validate_config(config=config)

    config.filename = config_file
    if not save_config(config=config):
        raise OSError('Unable to securely save the configuration. '
                      'Check directory permissions, the credential store, or external key file.')

    if config_spec == _CONFIG_SPEC_DICT:  # set CONFIG dictionary
        global CONFIG
        CONFIG = config

    return config


def save_config(config: ConfigObj = CONFIG) -> bool:
    """
    Save the config to file.

    Saves the `ConfigObj` to the specified file.

    Parameters
    ----------
    config : ConfigObj, default = CONFIG
        Config to save.

    Returns
    -------
    bool
        True if save successful, otherwise False.

    Examples
    --------
    >>> config_object = create_config(config_file='config.ini')
    >>> save_config(config=config_object)
    True
    """
    try:
        saved = copy.deepcopy(config)
        for section, options in saved.items():
            for key, value in options.items():
                if not _CONFIG_SPEC_DICT.get(section, {}).get(key, {}).get('secret') or not value:
                    continue
                if not value.startswith(credentials.SETTING_PREFIX):
                    saved[section][key] = credentials.encrypt_setting(value)
        Path(saved.filename).parent.mkdir(parents=True, exist_ok=True)
        saved.write()
        config.merge(saved)
    except credentials.TokenStorageError as exc:
        log.error('Unable to securely save settings: %s', exc)
        return False
    except Exception:
        return False
    else:
        return True


def validate_config(config: ConfigObj) -> bool:
    """
    Validate ConfigObj dictionary.

    Ensures that the given `ConfigObj` is valid.

    Parameters
    ----------
    config : ConfigObj
        Config to validate.

    Returns
    -------
    bool
        True if validation passes, otherwise False.

    Examples
    --------
    >>> config_object = create_config(config_file='config.ini')
    >>> validate_config(config=config_object)
    True
    """
    validator = Validator()
    try:
        result = config.validate(
            validator=validator,
            copy=False  # don't write out default values
        )
        return result is True
    except ValidateError as e:
        log_msg = f"Config validation error: {e}.\n"
        log.error(msg=log_msg)
        sys.stderr.write(log_msg)
        return False

"""
src/common/tray_icon.py

Responsible for system tray icon and related functions.
"""
# standard imports
import functools
import os
import sys

# lib imports
from PIL import Image

# local imports
import common
from common import config
from common import definitions
from common import helpers
from common import locales
from common import logger
from common import threads
from common import webapp
from common import version

# setup
_ = locales.get_text()
icon_running = False
icon_supported = False
log = logger.get_logger(name=__name__)

# conditional imports
try:
    from pystray import Icon, MenuItem, Menu
    if sys.platform == 'win32':
        from pystray._util import win32 as pystray_win32
except Exception:
    # A desktop backend may be unavailable, notably on headless Linux runners.
    Icon = MenuItem = Menu = None
    icon_class = None
else:
    if sys.platform == 'win32':
        class MenuOnLeftClickIcon(Icon):
            """Show the native Windows tray menu on either mouse button.

            Pystray normally runs the default action on a left click. This
            backend maps a left release to the native popup menu event.

            Parameters
            ----------
            *args : tuple
                Positional arguments passed to the pystray icon.
            **kwargs : dict
                Keyword arguments passed to the pystray icon.

            Examples
            --------
            >>> issubclass(MenuOnLeftClickIcon, Icon)
            True
            """

            def _on_notify(self, wparam, lparam):
                if lparam == pystray_win32.WM_LBUTTONUP:
                    lparam = pystray_win32.WM_RBUTTONUP
                return super()._on_notify(wparam, lparam)

        icon_class = MenuOnLeftClickIcon
    else:
        icon_class = Icon
    icon_supported = True

# additional setup
icon_object: Icon | bool = False


def tray_initialize() -> Icon | bool:
    """
    Initialize the system tray icon.

    Some features of the tray icon may not be available, depending on the operating system. An attempt is made to setup
    the tray icon with all the available features supported by the OS.

    Returns
    -------
    Union[Icon, bool]
        Icon
            Instance of pystray.Icon if icon is supported.
        bool
            False if icon is not supported.

    Examples
    --------
    >>> tray_initialize()
    """
    if not icon_supported:
        return False
    tray_icon = icon_class(name=definitions.Names.name.lower())
    tray_icon.title = definitions.Names.name

    image = Image.open(os.path.join(definitions.Paths.ROOT_DIR, 'web', 'images', 'favicon.ico'))
    tray_icon.icon = image

    # NOTE: Open the application. "%(app_name)s" = "Themerr". Do not translate "%(app_name)s".
    first_menu_entry = MenuItem(text=_('Open %(app_name)s') % {'app_name': definitions.Names.name},
                                action=functools.partial(open_link, '/'))

    if tray_icon.HAS_MENU:
        menu = (
            first_menu_entry,
            Menu.SEPARATOR,
            MenuItem(text=_('About'), action=Menu(
                MenuItem(text=_('Version %(version)s') % {'version': version.VERSION}, action=None, enabled=False),
                Menu.SEPARATOR,
                MenuItem(text=_('Repository'),
                         action=functools.partial(open_link, 'https://github.com/Themerr/Themerr')),
                # NOTE: Open GitHub Releases. "%(github)s" = "GitHub". Do not translate "%(github)s".
                MenuItem(text=_('%(github)s Releases') % {'github': 'GitHub'},
                         action=functools.partial(
                             open_link, 'https://github.com/Themerr/Themerr/releases/latest')),
                MenuItem(text=_('Documentation'), action=functools.partial(open_link, definitions.DOCUMENTATION_URL)),
                MenuItem(text=_('API documentation'), action=functools.partial(open_link, '/api/docs')),
                MenuItem(text='ThemerrDB',
                         action=functools.partial(open_link, 'https://github.com/Themerr/ThemerrDB')),
            )),
            MenuItem(
                # NOTE: Donate to LizardByte.
                text=_('Donate'), action=Menu(
                    MenuItem(text=_('GitHub Sponsors'),
                             action=functools.partial(open_link, 'https://github.com/sponsors/LizardByte')),
                    MenuItem(text='Patreon', action=functools.partial(open_link, 'https://www.patreon.com/LizardByte')),
                    MenuItem(text='PayPal',
                             action=functools.partial(open_link, 'https://www.paypal.com/paypalme/ReenigneArcher')),
                )
            ),
            Menu.SEPARATOR,
            # NOTE: Open web browser when application starts. Do not translate "%(app_name)s".
            MenuItem(text=_('Open browser when %(app_name)s starts') % {'app_name': definitions.Names.name},
                     action=tray_browser, checked=lambda item: config.CONFIG['General']['LAUNCH_BROWSER']),
            # NOTE: Disable or turn off icon.
            MenuItem(text=_('Disable icon'), action=tray_disable),
            Menu.SEPARATOR,
            # NOTE: Restart the program.
            MenuItem(text=_('Restart'), action=tray_restart),
            # NOTE: Quit, Stop, End, or Shutdown the program.
            MenuItem(text=_('Quit'), action=tray_quit),
        )

    else:
        menu = (
            first_menu_entry,
        )

    tray_icon.menu = Menu(*menu)

    return tray_icon


def tray_browser():
    """
    Toggle the config option 'LAUNCH_BROWSER'.

    This functions switches the `LAUNCH_BROWSER` config option from True to False, or False to True.

    Examples
    --------
    >>> tray_browser()
    """
    # toggle the value of LAUNCH_BROWSER
    config.CONFIG['General']['LAUNCH_BROWSER'] = not config.CONFIG['General']['LAUNCH_BROWSER']

    config.save_config(config.CONFIG)


def tray_disable():
    """
    Turn off the config option 'SYSTEM_TRAY'.

    This function ends and disables the `SYSTEM_TRAY` config option.

    Examples
    --------
    >>> tray_disable()
    """
    tray_end()
    config.CONFIG['General']['SYSTEM_TRAY'] = False
    config.save_config(config.CONFIG)


def tray_end() -> bool:
    """
    End the system tray icon.

    Hide and then stop the system tray icon.

    Returns
    -------
    bool
        ``True`` if successful, otherwise ``False``.

    Examples
    --------
    >>> tray_end()
    """
    if icon_class is None:
        return False
    if isinstance(icon_object, icon_class):
        try:  # this shouldn't be possible to call, other than through pytest
            icon_object.visible = False
        except AttributeError:
            pass

        try:
            icon_object.stop()
        except AttributeError:
            pass
        except Exception as e:
            log.error(f'Exception when stopping system tray icon: {e}')
        else:
            global icon_running
            icon_running = False
            return True
    return False


def tray_run_threaded() -> bool:
    """
    Run the system tray in a thread.

    This function exectues various other functions to simplify starting the tray icon.

    Returns
    -------
    bool
        ``True`` if successful, otherwise ``False``.

    See Also
    --------
    tray_initialize : This function first, initializes the tray icon using ``tray_initialize()``.
    tray_run : Then, ``tray_run`` is executed in a thread.
    pyra.threads.run_in_thread : Run a method within a thread.

    Examples
    --------
    >>> tray_run_threaded()
    True
    """
    if icon_supported:
        global icon_object
        icon_object = tray_initialize()
        threads.run_in_thread(target=tray_run, name='pystray', daemon=True).start()
        return True
    else:
        return False


def tray_toggle() -> bool:
    """
    Toggle the system tray icon.

    Hide/unhide the system tray icon.

    Returns
    -------
    bool
        ``True`` if successful, otherwise ``False``.

    Examples
    --------
    >>> tray_toggle()
    """
    if icon_supported:
        if icon_running:
            result = tray_end()
        else:
            result = tray_run_threaded()
    else:
        result = False

    return result


def tray_quit():
    """
    Shutdown Themerr.

    Set the 'common.SIGNAL' variable to 'shutdown'.

    Examples
    --------
    >>> tray_quit()
    """
    common.SIGNAL = 'shutdown'


def tray_restart():
    """
    Restart Themerr.

    Set the 'common.SIGNAL' variable to 'restart'.

    Examples
    --------
    >>> tray_restart()
    """
    common.SIGNAL = 'restart'


def tray_run():
    """
    Start the tray icon.

    Run the system tray icon in detached mode.

    Examples
    --------
    >>> tray_run()
    """
    if icon_class is not None:
        global icon_running

        if isinstance(icon_object, icon_class):
            try:
                icon_object.run_detached()
            except AttributeError:
                pass
            except NotImplementedError as e:
                log.error(f'Error running system tray icon: {e}')
            else:
                icon_running = True


def open_link(url: str, *args) -> bool:
    """
    Open a tray link in the default web browser.

    Relative links use the running web server's address at click time. Bound
    callbacks accept pystray's icon and menu item arguments.

    Parameters
    ----------
    url : str
        Fixed external URL or application path supplied by the tray menu.
    *args : tuple
        Icon and menu item supplied by pystray; ignored.

    Returns
    -------
    bool
        True if opening page was successful, otherwise False.

    Examples
    --------
    >>> open_link('https://github.com/Themerr/Themerr')
    True
    """
    if url.startswith('/'):
        if webapp.URL is None:
            return False
        url = webapp.URL.rstrip('/') + url
    return helpers.open_url_in_browser(url=url)

"""System tray behavior with an in-memory pystray backend."""

# standard imports
from types import SimpleNamespace
from unittest.mock import Mock

# lib imports
import pytest

# local imports
import common
from common import tray_icon


class FakeIcon:
    HAS_DEFAULT_ACTION = True
    HAS_MENU = True

    def __init__(self, name):
        self.name = name
        self.visible = True
        self.stopped = False
        self.started = False

    def stop(self):
        self.stopped = True

    def run_detached(self):
        self.started = True


class FakeMenu(tuple):
    SEPARATOR = object()

    def __new__(cls, *items):
        return super().__new__(cls, items)

    def __call__(self, icon):
        return None


def test_initialize_and_browser(configured, monkeypatch):
    monkeypatch.setattr(tray_icon, 'icon_supported', True)
    monkeypatch.setattr(tray_icon, 'Icon', FakeIcon)
    monkeypatch.setattr(tray_icon, 'icon_class', FakeIcon)
    monkeypatch.setattr(tray_icon, 'MenuItem', lambda **kwargs: kwargs)
    monkeypatch.setattr(tray_icon, 'Menu', FakeMenu)
    monkeypatch.setattr(tray_icon.Image, 'open', lambda _: object())
    save = Mock()
    monkeypatch.setattr(tray_icon.config, 'save_config', save)

    icon = tray_icon.tray_initialize()
    assert icon.name == 'themerr'
    assert isinstance(icon.menu, FakeMenu)
    assert callable(icon.menu)
    assert len(icon.menu) > 1
    original = configured['General']['LAUNCH_BROWSER']
    tray_icon.tray_browser()
    assert configured['General']['LAUNCH_BROWSER'] is not original
    save.assert_called_once_with(configured)

    monkeypatch.setattr(FakeIcon, 'HAS_MENU', False)
    minimal_menu = tray_icon.tray_initialize().menu
    assert isinstance(minimal_menu, FakeMenu)
    assert len(minimal_menu) == 1


def test_start_stop_toggle_and_signals(configured, monkeypatch):
    monkeypatch.setattr(tray_icon, 'icon_supported', False)
    assert tray_icon.tray_initialize() is False
    assert tray_icon.tray_run_threaded() is False
    assert tray_icon.tray_toggle() is False

    monkeypatch.setattr(tray_icon, 'icon_supported', True)
    monkeypatch.setattr(tray_icon, 'icon_class', FakeIcon)
    icon = FakeIcon('test')
    monkeypatch.setattr(tray_icon, 'icon_object', icon)
    monkeypatch.setattr(tray_icon, 'icon_running', False)
    monkeypatch.setattr(tray_icon, 'tray_initialize', lambda: icon)
    worker = SimpleNamespace(start=Mock())
    monkeypatch.setattr(tray_icon.threads, 'run_in_thread', lambda **_: worker)
    assert tray_icon.tray_toggle() is True
    worker.start.assert_called_once()
    tray_icon.tray_run()
    assert icon.started
    assert tray_icon.icon_running
    assert tray_icon.tray_toggle() is True
    assert icon.stopped
    assert not icon.visible
    assert not tray_icon.icon_running

    save = Mock()
    monkeypatch.setattr(tray_icon.config, 'save_config', save)
    tray_icon.tray_disable()
    assert configured['General']['SYSTEM_TRAY'] is False
    save.assert_called_once_with(configured)

    tray_icon.tray_quit()
    assert common.SIGNAL == 'shutdown'
    tray_icon.tray_restart()
    assert common.SIGNAL == 'restart'


def test_about_and_donation_links_use_the_native_callback_contract(configured, monkeypatch):
    # The dummy backend lets Linux runners import pystray without a desktop.
    monkeypatch.setenv('PYSTRAY_BACKEND', 'dummy')
    from pystray import Menu, MenuItem
    monkeypatch.setattr(tray_icon, 'icon_supported', True)
    monkeypatch.setattr(tray_icon, 'icon_class', FakeIcon)
    monkeypatch.setattr(tray_icon, 'MenuItem', MenuItem)
    monkeypatch.setattr(tray_icon, 'Menu', Menu)
    monkeypatch.setattr(tray_icon.Image, 'open', lambda _: object())
    monkeypatch.setattr(tray_icon.version, 'VERSION', '2026.1003.120000')
    monkeypatch.setattr(tray_icon.webapp, 'URL', None)
    opened = Mock(return_value=True)
    monkeypatch.setattr(tray_icon.helpers, 'open_url_in_browser', opened)
    icon = tray_icon.tray_initialize()
    about = next(item.submenu for item in icon.menu.items if item.text == 'About')
    details = about.items[0]
    assert details.text == 'Version 2026.1003.120000'
    assert not details.enabled
    assert icon.title == 'Themerr'
    assert not icon.menu.items[0](icon)
    opened.assert_not_called()

    destinations = {
        'Repository': 'https://github.com/Themerr/Themerr',
        'GitHub Releases': 'https://github.com/Themerr/Themerr/releases/latest',
        'Documentation': 'https://docs.lizardbyte.dev/projects/themerr/latest/',
        'API documentation': '/api/docs',
        'ThemerrDB': 'https://github.com/Themerr/ThemerrDB',
        'GitHub Sponsors': 'https://github.com/sponsors/LizardByte',
        'Patreon': 'https://www.patreon.com/LizardByte',
        'PayPal': 'https://www.paypal.com/paypalme/ReenigneArcher',
    }
    donations = next(item.submenu for item in icon.menu.items if item.text == 'Donate')
    for base in ('http://127.0.0.1:9495', 'https://127.0.0.1:9496/'):
        monkeypatch.setattr(tray_icon.webapp, 'URL', base)
        assert icon.menu.items[0](icon)
        opened.assert_called_with(url=base.rstrip('/') + '/')
        for item in (*about.items, *donations.items):
            if item.text not in destinations:
                continue
            assert item(icon)
            target = destinations[item.text]
            opened.assert_called_with(url=base.rstrip('/') + target if target.startswith('/') else target)


def test_link_callback_returns_browser_failure(monkeypatch):
    monkeypatch.setattr(tray_icon.helpers, 'open_url_in_browser', lambda url: False)
    assert tray_icon.open_link('https://github.com/Themerr/Themerr') is False


@pytest.mark.skipif(not hasattr(tray_icon, 'MenuOnLeftClickIcon'), reason='Windows tray backend only')
def test_windows_left_click_opens_menu(monkeypatch):
    notified = []
    monkeypatch.setattr(tray_icon.Icon, '_on_notify', lambda self, wparam, lparam: notified.append(lparam))
    icon = object.__new__(tray_icon.MenuOnLeftClickIcon)
    icon._visible = False
    icon._running = False
    icon._icon_handle = None

    icon._on_notify(0, tray_icon.pystray_win32.WM_LBUTTONUP)
    icon._on_notify(0, tray_icon.pystray_win32.WM_RBUTTONUP)

    assert notified == [tray_icon.pystray_win32.WM_RBUTTONUP] * 2

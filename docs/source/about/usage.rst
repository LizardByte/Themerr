Usage
=====

Start Themerr, then open its web UI at https://localhost:9494 (or the host and port you configured).

The web sidebar footer shows the installed version. In the system tray, open **About** to see the version and
links to the repository, releases, online documentation, API documentation, and ThemerrDB. The API documentation
requires signing in to the web UI.

Notifications
-------------

Under **Settings > Notifications**, choose alerts for new Themerr releases and increased theme coverage.
Both are enabled by default. **Follow pre-releases** is off by default; enable it to include preview releases.
Release checks run at startup and once per hour. An alert for the same or an older version is not repeated,
including after restarting Themerr. Release builds embed their version; source checkouts without a stamped
version compare against ``0.0.0`` and can notify when an eligible release is available.

After a successful dashboard refresh, Themerr compares total theme coverage with the last successful refresh.
It counts media and collections across saved servers and reports the increase in percentage points, the new
coverage, and how many items have themes. The first refresh establishes a baseline. Adding or removing a server,
or changing which servers are enabled, also establishes a new baseline. A failed or partial refresh, or one with
no increase, does not send an alert. Theme uploads are included in the next successful refresh's comparison.

Notifications appear on the computer running Themerr, even when the web UI is open on another device.
Docker and headless installations do not display desktop notifications. Linux requires a desktop notification
service and a session D-Bus connection. On macOS, launch the signed ``themerr.app`` bundle and allow
notifications when prompted. Operating system notification settings, including Do Not Disturb, still apply.

Admin account
-------------

On first start, Themerr prints a one-time setup link in the console and opens it if browser launching is enabled.
Use this link to create the installation's single admin account with a password of at least 12 characters.
For a remote or Docker installation, replace ``127.0.0.1`` in the link with the reachable hostname, retaining the setup
token. Opening the normal address before setup shows instructions for obtaining this link.

The admin password is stored as a salted scrypt hash in SQLite. Sign in with this account to access libraries,
settings, server connections, and theme playback. Sessions expire after 12 hours of inactivity and
restarting the application signs them out. Every form and modifying API request requires a CSRF token, and the
web UI cannot be embedded in an iframe.

Change your password under **Settings > Security**. This signs out other sessions. If you forget it, stop the
application and run its executable with ``--reset-admin-password`` from a local console. For a source checkout:

.. code-block:: shell

   uv run --locked python src/main.py --reset-admin-password

Supply ``--config`` if you normally use a different configuration file. The command prompts for a new password
without echoing it and exits without starting the server.

Plex servers
------------

Open **Servers**, select **Sign in with Plex**, complete the sign-in in the Plex browser window, and return to Themerr.
The application admin account and your Plex account have separate roles: the former protects the web UI;
the latter authorizes access to your Plex servers. Plex sign-in is the only way to authorize a server connection.

Select **Find account servers** to list servers available to your account, including shared servers. Choose a
reachable address from the server's connection list and select **Connect**. Repeat for each server you want to manage.
You can also use **Discover on LAN** to find Plex's GDM announcements, or enter an HTTP or HTTPS base address
manually, such as ``http://192.168.1.10:32400``. Discovery and manual addresses still require access through your
signed-in Plex account. LAN discovery depends on multicast traffic reaching the machine running Themerr;
containers and separate subnets may need the manual address option.

Account discovery lists advertised addresses; it does not confirm that they are reachable. The connection list
labels local, remote, and relay addresses and their HTTP or HTTPS protocol. Plex's advertised ``https://...plex.direct``
addresses use its server certificate. Changing such an address to ``https://IP:32400`` can cause a certificate
mismatch. See `Plex secure connections <https://support.plex.tv/articles/206225077-how-to-use-secure-server-connections/>`_.
You can enter a known HTTP address manually if your Plex server allows insecure connections.

If LAN discovery finds nothing, check **Enable local network discovery (GDM)** in Plex's network settings and
whether multicast can reach Themerr's machine. If connecting times out, verify the chosen address and port are
reachable from that machine, including its firewall and network route. Re-pairing Plex will not fix an unreachable
server address.

Each saved server has its own processing toggle, ignored library IDs, data directory, dashboard snapshot,
upload history, and errors. Pausing a server prevents new work; an upload already in progress can finish.
Removing a server erases its saved connection and local records, while themes already uploaded to Plex remain there.
Select **Disconnect Plex** to erase Plex credentials and pause saved Plex servers. After signing in again, reconnect
each server to resume processing with its retained upload history.

On desktop systems, account and server tokens are saved in the operating system's credential store.
For Docker or headless systems, provide ``THEMERR_TOKEN_KEY_FILE`` pointing to a persistent Fernet key file outside
the configuration directory. Themerr uses that key to encrypt tokens stored in SQLite. Keep the key file private;
losing it requires signing in again. Docker sign-in requires this key file.

Expand **Processing settings** on a server card to set its Plex data directory if you want Themerr to remove
older uploaded media from Plex's metadata directory. Use the folder button to browse directories on the machine
running Themerr. The same button is available for the log directory in Settings.

When Themerr runs on another machine, use the Plex server's reachable URL and mount its data directory if you
want local filesystem cleanup, or configure **SSH cleanup** on its server card. Otherwise, disable the three
**Remove unused** settings.

Enable movie, series, and collection updates as needed. Themerr listens for supported Plex library
events and also scans on the configured schedule. The home page reports theme status for each supported
library item and links to ThemerrDB contribution forms when a TMDB ID is known. It shows an IMDb or TVDB ID when Plex
supplies one but a TMDB ID cannot be resolved. A Plex ID is shown for collections without a verified external ID;
Plex's ``collection://`` GUID is local to the server. Themes with no matching provider or Themerr upload record are
labeled **Unknown provider**. Themerr can replace these themes when a matching theme exists in ThemerrDB and
the overwrite settings allow it.

Use the Overview search and server, type, and status filters to find individual items. **Refresh libraries** updates
the dashboard and reloads the page when the refresh finishes, retaining those filters. Active theme playback is
preserved instead of reloading; the completion message tells you when the new snapshot is ready.
**Activity** shows scheduled task starts, completion, duration, the upload queue, and per-item failure reasons.
Its **Scan for themes** button also starts a processing scan when theme updates are enabled.

**Logs** shows recent records from ``themerr`` (application), ``backend`` (Uvicorn), and ``yt-dlp``
(YouTube extraction). Choose a source, level, and record limit; search messages, thread names, or timestamps.
The arrow buttons jump between visible warnings, errors, and critical errors, wrapping at either end.
Navigation turns off **Follow latest** so you can inspect the selected record. **Live refresh** updates every
three seconds; turn it off to pause, or use **Refresh** for a single update. **Download filtered logs** saves
all loaded records matching the current filters, including multiline tracebacks. The latest-record options
contain at most 2,000 records and a bounded amount of rotated history. **Since startup** loads all records from
the current application session, including records that have rotated out of the regular log files. This history
is kept in temporary disk storage until Themerr exits and starts fresh after a restart. It covers messages emitted
after application logging is initialized. Session history loads in batches and displays 500 records per page;
use **Older records** and **Newer records** to browse. Search, filters, warning navigation, and downloads cover
all loaded pages. Live refresh appends new session records without reloading earlier batches.
The timestamps use the clock of the machine running Themerr. Access requires an administrator session.

The Overview's **ThemerrDB > Last deployed** indicator shows the age of its latest successful Pages deployment,
not the last local library refresh. Hover over the age for the completion time, or follow the link to its workflow run.
The GitHub check runs at most once per hour across page loads and application restarts, including failed checks.
If GitHub is unavailable, the last known deployment remains visible with a warning until the next check.

The search field's clear button removes just the title search, retaining the other filters. Plex and metadata IDs
open the item on Plex or its metadata provider in a new tab. Media type icons remain visible beside theme playback.
**Edit** appears for source video issues such as removal, privacy, or age restrictions. Local network, upload, and
regional failures do not by themselves require replacing the ThemerrDB video. ThemerrDB checks US availability
when accepting themes; a regional failure elsewhere cannot establish that it is currently unavailable in the US.

Select the play button beside an item's title to listen to its currently selected media-server theme, regardless of provider.
The button changes to pause during playback, and the ring around it shows playback progress. Pausing retains your
position; selecting another item stops the previous theme. Items without an installed theme have no play button.

The player at the bottom of the workspace shows the item's poster, title, year, media type, and server.
Select the title to open the item in its media server. If its poster is unavailable, a music icon appears instead.
Use the playback slider to seek, the volume slider to adjust sound, and the previous and next buttons to
browse installed themes. **Surprise me** picks a random installed theme across your servers.
**Shuffle** plays themes in random order without repeats until the library has played; **Repeat this theme**
loops the current selection. Otherwise playback advances in library order and stops after the last theme.

Playback and player controls stay active when moving between workspace pages, including browser Back and
Forward and library refreshes. A full browser reload, signing out, or leaving the application stops playback.

On supported browsers, system media controls and keyboard media keys can play, pause, and move between themes.
Themerr also supplies the item's title, poster, and playback position to those controls. The browser and operating
system determine which controls and metadata are displayed.

To exclude a library from updates, enter its ID in **Ignored library IDs** in its server's processing settings.
The home page shows each library's ID beside its name. Separate multiple IDs with commas.

TMDB IDs for titles already in ThemerrDB are resolved from ThemerrDB's index. Movie collections can also be resolved
from matching collection metadata on their member movies. Themerr asks the item's Plex server's TMDB proxy to
resolve other IMDb or TVDB IDs and collection names. If the Plex proxy is unavailable, you can set the optional
``TMDB_API_READ_ACCESS_TOKEN`` environment variable to your TMDB API Read Access Token. Keep this token outside the
web settings and configuration file.

SSH cleanup
~~~~~~~~~~~

SSH cleanup uses Python's Paramiko library to remove uploaded themes, posters and artwork through SFTP.
It does not install a Plex plugin, start a remote helper, or execute shell commands. Enable SSH with an SFTP
subsystem on the Plex host, and use an account with read and deletion permissions on its ``Metadata`` directory.
A dedicated account restricted to SFTP and the required data directory is suitable; administrator access is
not required. The Plex API still handles metadata discovery and uploads.

For Windows, install and start OpenSSH Server using Microsoft's
`Windows OpenSSH setup guide
<https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse>`_.
In **Servers > SSH cleanup**, enter that computer's hostname or IP address and SSH port, usually ``22``.
Use Themerr over HTTPS when configuring it from another machine; remote credential submissions over HTTP are rejected.

**SSH username**

Enter a login account on the computer running Plex. Your Plex account and your Themerr admin account do not
determine this username. On Windows, sign in as the account you want to use, open PowerShell on that computer,
and run:

.. code-block:: powershell

   whoami

If a local account reports ``PLEX-PC\plex``, enter ``plex``. For an Active Directory account, keep both the domain
and username returned by ``whoami``. Use the account's login name, rather than its display name.
For the simplest Windows setup, use a local account with permission to read and delete Plex uploads.
Windows OpenSSH does not support Microsoft Entra accounts; see Microsoft's
`Windows SSH account configuration
<https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration>`_.
On Unix, enter the login name of the account with access to the Plex data directory.

**Remote data directory**

Enter the directory containing ``Metadata`` on the Plex computer. You can paste a native Windows path such as
``C:\Users\Plex\AppData\Local\Plex Media Server``. Themerr converts it to
``/C:/Users/Plex/AppData/Local/Plex Media Server`` before checking it through SFTP. Windows paths using forward
slashes and existing SFTP paths are also accepted. On Linux, a typical directory is
``/var/lib/plexmediaserver/Library/Application Support/Plex Media Server``.
Traversal, UNC paths, drive-relative paths and environment-variable expressions are rejected.
For a restricted SFTP account, use the absolute path visible inside that account's filesystem.
Both data directories are saved separately for each Plex server. **SSH cleanup > Remote Plex data directory** is
that server's path reached through SFTP. **Processing settings > Plex data directory** is the local or mounted path
through which the computer running Themerr reaches that same server's data. Each connected server can use its own
paths. The SSH form shows the verified SFTP path after saving.

**Server host fingerprint**

Obtain the fingerprint directly from the server or its administrator, rather than trusting an unverified
network discovery. This identifies the SSH server itself and is required for both authentication choices.
It is separate from a user key pair generated for signing in. For an OpenSSH server using its Ed25519 host key:

.. code-block:: shell

   # Linux
   ssh-keygen -l -E sha256 -f /etc/ssh/ssh_host_ed25519_key.pub

.. code-block:: powershell

   # Windows, from an administrator PowerShell session
   ssh-keygen -l -E sha256 -f "$env:ProgramData\ssh\ssh_host_ed25519_key.pub"

Copy the ``SHA256:...`` value. Themerr verifies that fingerprint before sending authentication credentials.
If your server negotiates a different host-key algorithm, supply that host key's fingerprint instead.
A changed fingerprint requires checking the replacement host key on the server before updating Themerr.

**Password authentication: the easiest setup**

New connections default to **Password**. Enter the password for the server account named above.
On Windows, use its account password, not its Windows Hello PIN. The SSH server must permit password authentication.
You do not need to generate a user key pair, enter a private key, or choose a key passphrase with this option.

**Private-key authentication: optional**

A user key pair lets Themerr sign in using a private key instead of the server account's password.
Themerr supports Ed25519, ECDSA and RSA private keys. To create a dedicated Ed25519 pair on a computer you control:

.. code-block:: powershell

   # Windows
   ssh-keygen -t ed25519 -f "$env:USERPROFILE\themerr_ssh"

.. code-block:: shell

   # Linux or macOS
   ssh-keygen -t ed25519 -f "$HOME/themerr_ssh"

When prompted, choose a passphrase to protect the private key file. It is a password you choose for that file;
it does not come from the SSH host fingerprint or your Windows login. If you create the key without a passphrase,
leave Themerr's **Private key passphrase** field empty. See the
`OpenSSH key-generation manual <https://man.openbsd.org/ssh-keygen>`_ for the command options.

The command creates two files. ``themerr_ssh.pub`` contains the public key: install its complete line for the selected
account on the Plex computer. ``themerr_ssh`` contains the private key: open it in a text editor and paste its entire
contents, including the ``BEGIN`` and ``END`` lines, into Themerr's **SSH private key** field. Select **Private key**
authentication and enter the passphrase chosen during generation. The fingerprint printed while generating this
user key is not the server host fingerprint required above.

For a standard Windows account, the public key goes in that account's
``C:\Users\<account>\.ssh\authorized_keys``. With Windows OpenSSH's default administrator configuration, an
administrator account uses ``C:\ProgramData\ssh\administrators_authorized_keys`` instead. Follow Microsoft's
`public-key installation and file-permission instructions
<https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement#deploy-the-public-key>`_.
On Unix, the usual location is the selected server account's ``~/.ssh/authorized_keys``.

**Verify and save**

Select **Verify and save SSH**. Themerr checks the connection and ``Metadata`` directory before saving, without
deleting files. The host fingerprint and SSH username are saved with ordinary connection settings: the fingerprint
identifies a public key, and the username identifies an account. They are admin-only settings, protected from
unauthenticated changes, but do not require encryption. Private keys, passwords and passphrases use the same OS
vault or external Fernet key as Plex tokens and are never returned by the settings API.
Blank secret fields retain saved credentials only for the same host, port, username, fingerprint and authentication
method. Changing those requires new credentials. **Check connection** performs a read-only verification;
**Disable SSH** erases SSH settings and credentials.

When configured, SSH takes priority over a local mount. Failed SSH operations do not fall back to local deletion.
Cleanup uses item GUIDs obtained from Plex and fixed upload directories, rejects traversal and links escaping the
configured root, and retains the verified new theme when removing older uploads. If the verified theme is missing,
existing uploads are kept. Keep other accounts from changing these directories during cleanup; SFTP cannot make
path verification and deletion one atomic filesystem operation. A live deployment still needs correct SFTP
permissions and the data directory belonging to the selected Plex server.

Jellyfin servers
----------------

Themerr supports Jellyfin 10.11.x and stable Jellyfin 12.x releases starting at 12.1, including 12.2.
Use the latest hotfix in your series. Open **Servers**, select the **Jellyfin** tab, and enter the
server address, such as `http://localhost:8096`. Create an API key under **Jellyfin Dashboard > Advanced >
API Keys**, paste it into Themerr, and select **Connect**. Use **Discover Jellyfin on LAN** to find nearby
servers; you still need an API key. If discovery finds nothing, enter the address manually.

On the connected server card, save a Themerr address that the Jellyfin server can reach. For a local
installation using Themerr's default certificate, use `http://localhost:9495`. If Jellyfin runs on
another computer, replace `localhost` with Themerr's hostname or IP address. Docker installations
must expose port 9495. A trusted HTTPS address also works.

Themerr automatically installs and updates its **Themerr Connector**. The server card shows its progress.
By default, Themerr waits for playback to finish before restarting Jellyfin and refreshing its libraries.
If your installation needs a manual restart, follow the message on the card. Change these preferences
under **Settings > Jellyfin**. When automatic updates are disabled, use **Install matching connector**
when the button appears. **Force restart** interrupts playback and should only be used when you are
ready to restart the server.

Enable movie, series, and collection support under **Settings > Jellyfin**, then choose the libraries to
process on each server card. Jellyfin TV libraries can use TMDB or TheTVDB metadata. Collections created
by Jellyfin's TMDb Box Sets plugin appear in its **Collections** library. Include that library to add
collection themes. Jellyfin must be able to write to your media folders, and each movie needs its own folder.

For example, use ``Movies/Example (2020)/Example.mkv`` rather than placing ``Example.mkv`` directly in
``Movies`` alongside other movies. Jellyfin uses a fixed theme filename in the movie's folder, so a
shared folder cannot hold a separate theme for each movie. After reorganizing a movie, rescan the
Jellyfin library and retry its theme in Themerr.

Existing user themes are protected by default. Under **Settings > Jellyfin**, enable **Overwrite user themes**
if you want Themerr to replace them. **Back up replaced user themes** controls whether replaced themes are kept.
If you used the older Themerr-jellyfin plugin, Themerr recognizes its unchanged themes by default and
removes the old plugin and its repository after connecting. You can disable either choice in the same
settings section before connecting the server.

For playback in Jellyfin, open **Jellyfin user settings > Display** and enable **Theme songs** under
**Library**. The server card links to this page. Repeat this setting in each browser or app you use.

Removing a server from Themerr removes its saved connection; themes already installed in Jellyfin remain.
For Docker and headless installations, set up the persistent credential encryption key described in
:doc:`docker` before connecting.

Local data
----------

Themerr stores its server registry, admin password hash, dashboard snapshots, upload records, processing errors,
and non-secret client IDs in
``themerr.db`` beside the active configuration file (``config/themerr.db`` by default, or
``/config/themerr.db`` in Docker). Docker and headless installs also store encrypted tokens and API keys there.
Existing installations keep using their previous database and saved credentials.

The ``/status`` endpoint returns a JSON health response. Open **Documentation** in the sidebar or
system tray to read the online project documentation.

YouTube cookies
---------------

Cookies are optional. They can help when YouTube asks you to sign in or rejects anonymous requests.
The **YouTube Cookies** setting accepts a JSON array of browser cookies. Paste the exported contents, including
the opening ``[`` and closing ``]``. A file path, a ``Cookie:`` request header, and Netscape ``cookies.txt`` contents
are not accepted by this setting.

For Chrome or another compatible Chromium browser:

1. Install `Get cookies.txt LOCALLY
   <https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc>`_,
   which is linked from the `yt-dlp cookie guide
   <https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp>`_.
   In the browser's extension settings, allow this extension in incognito/private windows.
2. Open a new incognito/private window and visit YouTube. Sign in if the video requires an account.
3. In that same tab, visit https://www.youtube.com/robots.txt. Keep it as the only tab in the private window.
4. Open the extension, set **Export Format** to **JSON**, and select **Copy** or **Export** for the current site.
   If you export a file, open it in a text editor and copy its entire contents. Avoid **Export All Cookies**;
   Themerr only needs the YouTube cookies.
5. Close the private window. These steps follow `yt-dlp's YouTube export guidance
   <https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies>`_ to reduce cookie rotation.
6. In Themerr, open **Settings** and find **YouTube Cookies** in the **Themerr** section. Paste the JSON and
   select **Save changes**. It will be used on the next extraction; a restart is not required.

If YouTube starts asking you to sign in again, repeat the export and replace the saved JSON.
Cookies cannot make a deleted or unavailable video accessible.

Treat cookies like passwords: they can grant access to your browser session. Keep the export and Themerr
configuration private, and never include cookie values in screenshots, logs, or issue reports.
For Docker and headless installations, complete the credential-storage setup described in :doc:`docker`
before saving cookies.

Theme format
------------

Themerr selects the largest available Opus or MP4A audio stream. Enable Prefer MP4A AAC Codec for
Plex clients that cannot play Opus theme audio. If MP4A is unavailable, Opus is used.

Themes are skipped on later jobs unless the source changes or the AAC preference requires a different codec.
Locked themes and the setting for preserving Plex-provided themes still apply. A failed replacement keeps the
previous theme.

.. note::

   A theme can only be added when its item exists in ThemerrDB. See
   :ref:`contributing/database <contributing/database:database>` to contribute a missing theme.

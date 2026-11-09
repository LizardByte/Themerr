Installation
============

Themerr runs as a separate application alongside Plex Media Server or Jellyfin. It does not use the Plex plug-in framework.
The application needs network access to your media server, ThemerrDB, and YouTube. To remove old Plex plug-in behavior, stop Plex,
remove the old Themerr bundle from its Plug-Ins directory, and restart Plex before starting the standalone app.

Release archive
---------------

Download the archive for your operating system and architecture from https://github.com/Themerr/Themerr/releases/latest.
Extract it and run the ``themerr`` executable, or the ``themerr.app`` bundle on macOS.
The macOS bundle stores configuration under ``~/Library/Application Support/Themerr/config``.
Existing installations keep using their saved configuration directory.
Deno is bundled for yt-dlp's YouTube challenge solver. Open the web UI
at https://localhost:9494 to connect Plex or Jellyfin. Set the Plex data directory only if you want
to remove old uploaded media. The default web server uses a locally generated certificate, so your browser may ask you to trust it.

Docker
------

Docker images are available from Docker Hub and GitHub Container Registry. See :ref:`Docker <about/docker:docker>`
for volume and port settings.

Source
------

Install Python 3.14, uv, npm, and `Deno <https://docs.deno.com/runtime/getting_started/installation/>`_ 2.3 or newer.
Then run from the repository root:

.. code-block:: shell

   uv sync --locked
   npm ci --ignore-scripts
   npm run build
   uv run --locked python src/main.py

For a packaged build with web assets, see :ref:`Build <contributing/build:build>`.

<div align="center">
  <img
    src="https://raw.githubusercontent.com/Themerr/Themerr/refs/heads/master/web/images/icon-default.png"
    alt="Themerr icon"
    width="256"
  />
  <h1 align="center">Themerr</h1>
  <h4 align="center">Theme song manager for Plex and Jellyfin using ThemerrDB.</h4>
</div>

<div align="center">
  <a href="https://github.com/Themerr/Themerr"><img src="https://img.shields.io/github/stars/Themerr/Themerr.svg?logo=github&style=for-the-badge" alt="GitHub stars"></a>
  <a href="https://github.com/Themerr/Themerr/releases/latest"><img src="https://img.shields.io/github/downloads/Themerr/Themerr/total.svg?style=for-the-badge&logo=github" alt="GitHub Releases"></a>
  <a href="https://hub.docker.com/r/lizardbyte/themerr"><img src="https://img.shields.io/docker/pulls/lizardbyte/themerr.svg?style=for-the-badge&logo=docker" alt="Docker"></a>
  <a href="https://github.com/Themerr/Themerr/actions/workflows/ci.yml?query=branch%3Amaster"><img src="https://img.shields.io/github/actions/workflow/status/Themerr/Themerr/ci.yml.svg?branch=master&label=build&logo=github&style=for-the-badge" alt="GitHub Workflow Status"></a>
  <a href="https://codecov.io/gh/Themerr/Themerr"><img src="https://img.shields.io/endpoint.svg?url=https%3A%2F%2Fapp.lizardbyte.dev%2Fdashboard%2Fshields%2Fcodecov%2FThemerr.json&style=for-the-badge&logo=codecov" alt="Codecov"></a>
  <a href="https://sonarcloud.io/project/overview?id=Themerr_Themerr"><img src="https://img.shields.io/sonar/quality_gate/Themerr_Themerr.svg?server=https%3A%2F%2Fsonarcloud.io&style=for-the-badge&logo=sonarqubecloud&label=sonarcloud" alt="SonarCloud"></a>
</div>

# Overview

## ℹ️ About

Themerr adds theme music to Plex and Jellyfin movies, TV shows, and collections from [ThemerrDB](https://github.com/Themerr/ThemerrDB).
It listens for Plex library updates and can also scan supported libraries on a schedule. YouTube audio streams are
resolved with `yt-dlp`.

It works with the Plex Movie (`tv.plex.agents.movie`) and Plex Series (`tv.plex.agents.series`) agents. Install and
configure the application separately from your media server. Plex does not require a plug-in; Themerr installs
its matching connector on Jellyfin.

LizardByte has the full documentation hosted on [Read the Docs](https://docs.lizardbyte.dev/projects/themerr/latest/).

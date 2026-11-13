using System.Collections.Concurrent;
using System.Reflection;
using MediaBrowser.Common.Api;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Data.Sqlite;
using Microsoft.Extensions.Logging;

namespace Themerr.Connector;

/// <summary>
/// Provides Jellyfin administrator endpoints for build identity, theme upload, and ownership import.
/// </summary>
/// <remarks>
/// Item identifiers are resolved by Jellyfin; requests never choose filesystem paths.
/// Mutations require the matching Themerr build fingerprint and serialize writes for each item.
/// </remarks>
/// <seealso cref="ThemeFiles"/>
[ApiController]
[Route("Themerr")]
[Authorize(Policy = Policies.RequiresElevation)]
public sealed class Controller : ControllerBase
{
    private static readonly ConcurrentDictionary<Guid, SemaphoreSlim> Locks = new();
    private readonly ILibraryManager _library;
    private readonly ThemeFiles _themes;
    private readonly LegacyOwnership _legacy;
    private readonly ILogger<Controller> _logger;

    /// <summary>
    /// Initializes a new instance of the <see cref="Controller"/> class.
    /// </summary>
    /// <param name="library">Jellyfin's item registry.</param>
    /// <param name="paths">Jellyfin's trusted server data directories.</param>
    /// <param name="logger">The controller's error and import logger.</param>
    public Controller(ILibraryManager library, IApplicationPaths paths, ILogger<Controller> logger)
    {
        _library = library;
        _themes = new(new ThemeOwnership(paths.DataPath));
        _legacy = new(paths.DataPath);
        _logger = logger;
    }

    /// <summary>Reports the protocol, source fingerprint, and supported Jellyfin ABI.</summary>
    /// <returns>The identity that Themerr verifies before modifying a theme.</returns>
    [HttpGet("Connector")]
    public object Identity() => new
    {
        protocol = 1,
        build = Metadata("ThemerrBuild"),
        targetAbi = Metadata("JellyfinAbi"),
    };

    /// <summary>Reports whether an item's theme exists and still matches connector ownership.</summary>
    /// <param name="itemId">The native Jellyfin item identifier.</param>
    /// <returns>Theme state, or a fixed error for missing items, unsafe paths, or storage failure.</returns>
    [HttpGet("Items/{itemId:guid}/Theme")]
    public ActionResult<ThemeState> State(Guid itemId)
    {
        var item = _library.GetItemById(itemId);
        if (item is null)
        {
            return NotFound();
        }

        var root = Root(item);
        if (root is null)
        {
            return BadRequest();
        }

        try
        {
            return _themes.State(itemId, root, item.GetThemeSongs().Count);
        }
        catch (ThemeConflictException)
        {
            return Conflict();
        }
        catch (Exception error) when (error is SqliteException or IOException or UnauthorizedAccessException)
        {
            return StorageFailure(error);
        }
    }

    /// <summary>Writes a validated upload without replacing protected user themes.</summary>
    /// <param name="itemId">The native Jellyfin item identifier.</param>
    /// <param name="cancellationToken">Cancellation for receiving and refreshing the theme.</param>
    /// <returns>The saved theme state, or a fixed validation, ownership, or storage error.</returns>
    /// <remarks>
    /// The request supplies the matching build fingerprint, audio MIME type, and SHA-256 digest.
    /// Explicit overwrite and backup headers control replacement of user-provided themes.
    /// </remarks>
    // NOSONAR csharpsquid:S6932: binary streaming validates protocol headers before receiving audio.
#pragma warning disable S6932
    [HttpPost("Items/{itemId:guid}/Theme")]

    // NOSONAR csharpsquid:S5693: authenticated audio is bounded to 100 MiB here and while streaming.
#pragma warning disable S5693
    [RequestSizeLimit(104857600)]
#pragma warning restore S5693
    public async Task<ActionResult<ThemeState>> Upload(Guid itemId, CancellationToken cancellationToken)
    {
        if (Request.Headers["X-Themerr-Connector"] != Metadata("ThemerrBuild"))
        {
            return Conflict();
        }

        var item = _library.GetItemById(itemId);
        if (item is null)
        {
            return NotFound();
        }

        var root = Root(item);
        if (root is null || (item.IsLocked && Request.Headers["X-Themerr-Ignore-Locked"] != "true"))
        {
            return Conflict();
        }

        var gate = Locks.GetOrAdd(itemId, _ => new SemaphoreSlim(1, 1));
        await gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var state = await _themes.Save(
                itemId,
                root,
                Request.Body,
                Request.ContentType,
                Request.Headers["X-Themerr-SHA256"].ToString(),
                item.GetThemeSongs().Count,
                cancellationToken,
                Request.Headers["X-Themerr-Overwrite-User"] == "true",
                Request.Headers["X-Themerr-Backup-User"] != "false").ConfigureAwait(false);
            await item.RefreshMetadata(cancellationToken).ConfigureAwait(false);
            return state;
        }
        catch (ThemeConflictException)
        {
            return Conflict();
        }
        catch (InvalidDataException)
        {
            return BadRequest();
        }
        catch (Exception error) when (error is SqliteException or IOException or UnauthorizedAccessException)
        {
            return StorageFailure(error);
        }
        finally
        {
            gate.Release();
        }
    }

#pragma warning restore S6932

    /// <summary>Recognizes an unchanged theme owned by the older Themerr-jellyfin plugin.</summary>
    /// <param name="itemId">The native Jellyfin item identifier.</param>
    /// <param name="cancellationToken">Cancellation while waiting for the item's upload lock.</param>
    /// <returns>Verified ownership state, or a fixed error for unsafe paths or storage failure.</returns>
    /// <remarks>The old database is read-only; an absent or changed hash leaves the theme protected.</remarks>
    // NOSONAR csharpsquid:S6932: import retains the binary upload protocol's explicit build-header check.
#pragma warning disable S6932
    [HttpPost("Items/{itemId:guid}/Theme/Import")]
    public async Task<ActionResult<ThemeState>> Import(Guid itemId, CancellationToken cancellationToken)
    {
        if (Request.Headers["X-Themerr-Connector"] != Metadata("ThemerrBuild"))
        {
            return Conflict();
        }

        var item = _library.GetItemById(itemId);
        if (item is null)
        {
            return NotFound();
        }

        var root = Root(item);
        if (root is null)
        {
            return BadRequest();
        }

        var gate = Locks.GetOrAdd(itemId, _ => new SemaphoreSlim(1, 1));
        await gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            string? digest;
            try
            {
                digest = _legacy.Hash(itemId);
            }
            catch (SqliteException error)
            {
                _logger.LogWarning(error, "Could not read older Themerr ownership; existing themes remain protected.");
                digest = null;
            }

            return _themes.Import(itemId, root, digest, item.GetThemeSongs().Count);
        }
        catch (ThemeConflictException)
        {
            return Conflict();
        }
        catch (Exception error) when (error is SqliteException or IOException or UnauthorizedAccessException)
        {
            return StorageFailure(error);
        }
        finally
        {
            gate.Release();
        }
    }

#pragma warning restore S6932

    /// <summary>Reads code-owned assembly metadata used for compatibility checks.</summary>
    /// <param name="key">The fixed assembly metadata name.</param>
    /// <returns>The metadata value embedded by the connector build.</returns>
    private static string Metadata(string key) => typeof(Plugin).Assembly.GetCustomAttributes<AssemblyMetadataAttribute>()
        .Single(a => a.Key == key).Value!;

    /// <summary>Resolves a supported local item's directory without accepting request paths.</summary>
    /// <param name="item">The item obtained from Jellyfin's library registry.</param>
    /// <returns>The canonical item directory, or null for unsupported or shared locations.</returns>
    private static string? Root(BaseItem item) =>
        item is MediaBrowser.Controller.Entities.Movies.Movie or MediaBrowser.Controller.Entities.TV.Series or
            MediaBrowser.Controller.Entities.Movies.BoxSet &&
        item.IsFileProtocol && !item.IsInMixedFolder && Directory.Exists(item.ContainingFolderPath)
            ? Path.GetFullPath(item.ContainingFolderPath)
            : null;

    /// <summary>Logs storage details while returning a fixed client-facing error.</summary>
    /// <param name="error">The storage exception to record server-side.</param>
    /// <returns>An HTTP 500 response without filesystem or database details.</returns>
    private ObjectResult StorageFailure(Exception error)
    {
        _logger.LogError(error, "Could not access connector theme storage.");
        return StatusCode(500, "Could not access connector theme storage.");
    }
}

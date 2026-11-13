using Microsoft.Data.Sqlite;
using Microsoft.EntityFrameworkCore;

namespace Themerr.Connector;

/// <summary>Reads only verified ownership from the old plugin's fixed database, without migrating it.</summary>
/// <param name="serverDataPath">Jellyfin's trusted server data root.</param>
/// <seealso cref="ThemeFiles.Import"/>
public sealed class LegacyOwnership(string serverDataPath)
{
    /// <summary>Finds a unique older plugin record marked as Themerr-owned SHA-256 audio.</summary>
    /// <param name="itemId">The native Jellyfin item identifier.</param>
    /// <returns>The recorded digest, or null when ownership is absent or ambiguous.</returns>
    /// <remarks>The caller verifies this digest against the actual theme before recording ownership.</remarks>
    /// <exception cref="ThemeConflictException">The old database resources are symlinks or junctions.</exception>
    public string? Hash(Guid itemId)
    {
        var directory = Path.Combine(serverDataPath, "Themerr");
        var database = Path.Combine(directory, "themerr.db");
        ThemeOwnership.ValidateDatabase(directory, database);
        if (!File.Exists(database))
        {
            return null;
        }

        var connection = new SqliteConnectionStringBuilder
        {
            DataSource = database,
            Mode = SqliteOpenMode.ReadOnly,
            Pooling = false,
        }.ToString();
        using var context = new LegacyOwnershipContext(
            new DbContextOptionsBuilder<LegacyOwnershipContext>().UseSqlite(connection).Options);
        var compact = itemId.ToString("N");
        var standard = itemId.ToString("D");
        var rows = context.Themes.AsNoTracking().Where(value => value.ItemId == compact || value.ItemId == standard)
            .Take(2).ToList();
        return rows.Count == 1 && rows[0].ThemeProvider == "themerr" && rows[0].ThemeHashAlgorithm == "SHA256"
            ? rows[0].ThemeHash
            : null;
    }
}

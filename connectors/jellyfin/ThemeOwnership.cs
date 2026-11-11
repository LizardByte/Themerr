using System.Collections.Concurrent;
using Microsoft.Data.Sqlite;
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

namespace Themerr.Connector;

/// <summary>Stores ownership in one database beneath Jellyfin's trusted server data directory.</summary>
/// <seealso cref="OwnershipContext"/>
public sealed class ThemeOwnership
{
    private static readonly ConcurrentDictionary<string, object> Gates = new(StringComparer.Ordinal);
    private readonly string _directory;
    private readonly string _database;

    /// <summary>Initializes a new instance of the <see cref="ThemeOwnership"/> class.</summary>
    /// <param name="serverDataPath">Jellyfin's trusted server data root.</param>
    public ThemeOwnership(string serverDataPath)
    {
        _directory = Path.Combine(serverDataPath, "themerr-connector");
        _database = Path.Combine(_directory, "ownership.db");
    }

    /// <summary>Reads the last recorded filename and digest without creating a database.</summary>
    /// <param name="itemId">The native Jellyfin item identifier.</param>
    /// <returns>The recorded ownership, or null when the database or item is absent.</returns>
    /// <exception cref="ThemeConflictException">The database directory or companion files are links.</exception>
    public Ownership? Find(Guid itemId)
    {
        ValidatePaths();
        if (!File.Exists(_database))
        {
            return null;
        }

        using var context = Open(SqliteOpenMode.ReadOnly);
        var id = itemId.ToString("N");
        var entry = context.Themes.AsNoTracking().SingleOrDefault(value => value.ItemId == id);
        return entry is null ? null : new(entry.File, entry.Sha256);
    }

    /// <summary>Commits ownership only after the corresponding theme mutation succeeds.</summary>
    /// <param name="itemId">The native Jellyfin item identifier.</param>
    /// <param name="file">The code-owned theme filename.</param>
    /// <param name="digest">The verified lowercase SHA-256 digest.</param>
    /// <param name="writeTheme">The validated file mutation, receiving the previous ownership or null.</param>
    /// <remarks>
    /// The database lock serializes writes; a failed callback rolls back ownership.
    /// The callback uses the supplied ownership to avoid a second connection reading the locked table.
    /// </remarks>
    public void Record(Guid itemId, string file, string digest, Action<Ownership?> writeTheme)
    {
        lock (Gates.GetOrAdd(_database, _ => new object()))
        {
            ValidatePaths();
            Directory.CreateDirectory(_directory);
            using var context = Open(SqliteOpenMode.ReadWriteCreate);
            Migrate(context);

            // Acquire the database write lock before changing audio; failures roll back ownership.
            using var transaction = context.Database.BeginTransaction();
            var id = itemId.ToString("N");
            var entry = context.Themes.Find(id);
            Ownership? previous = entry is null ? null : new(entry.File, entry.Sha256);
            if (entry is null)
            {
                entry = new OwnershipEntry { ItemId = id };
                context.Themes.Add(entry);
            }

            entry.File = file;
            entry.Sha256 = digest;
            context.SaveChanges();
            writeTheme(previous);
            transaction.Commit();
        }
    }

    /// <summary>Rejects links at a fixed database, directory, or SQLite companion file.</summary>
    /// <param name="root">The trusted data directory.</param>
    /// <param name="database">The fixed database filename beneath that directory.</param>
    /// <exception cref="ThemeConflictException">A resource is a symlink, junction, or reparse point.</exception>
    internal static void ValidateDatabase(string root, string database)
    {
        var directory = new DirectoryInfo(root);
        if (directory.LinkTarget is not null ||
            (directory.Exists && (directory.Attributes & FileAttributes.ReparsePoint) != 0))
        {
            throw new ThemeConflictException();
        }

        // SQLite can create these fixed companion files while a transaction is in progress.
        foreach (var path in new[]
        {
            database,
            $"{database}-journal",
            $"{database}-wal",
            $"{database}-shm",
        })
        {
            var info = new FileInfo(path);
            if (info.LinkTarget is not null || (info.Exists && (info.Attributes & FileAttributes.ReparsePoint) != 0))
            {
                throw new ThemeConflictException();
            }
        }
    }

    /// <summary>Applies EF migrations while retaining the connector's existing SQLite records.</summary>
    /// <param name="context">The writable ownership context.</param>
    private static void Migrate(OwnershipContext context)
    {
        var history = context.GetService<IHistoryRepository>();
        if (!history.GetAppliedMigrations().Any())
        {
            var existing = false;
            try
            {
                _ = context.Themes.AsNoTracking().Take(1).ToList();
                existing = true;
            }
            catch (SqliteException error) when (error.SqliteErrorCode == 1)
            {
                // An absent table is expected before the first migration creates it.
            }

            if (existing)
            {
                // Adopt the connector's original SQLite schema using EF-generated history commands.
                // No rows are copied, discarded, or rebuilt.
                using var transaction = context.Database.BeginTransaction();
                context.Database.ExecuteSqlRaw(history.GetCreateIfNotExistsScript());
                context.Database.ExecuteSqlRaw(history.GetInsertScript(new HistoryRow(
                    InitialOwnership.Id, typeof(DbContext).Assembly.GetName().Version!.ToString())));
                transaction.Commit();
            }
        }

        context.Database.Migrate();
    }

    /// <summary>Opens an EF context using fixed, validated database resources.</summary>
    /// <param name="mode">The SQLite access mode required for the operation.</param>
    /// <returns>The ownership context that the caller must dispose.</returns>
    private OwnershipContext Open(SqliteOpenMode mode)
    {
        ValidatePaths();
        var connection = new SqliteConnectionStringBuilder
        {
            DataSource = _database,
            Mode = mode,
            Pooling = false,
        }.ToString();
        return new OwnershipContext(new DbContextOptionsBuilder<OwnershipContext>().UseSqlite(connection).Options);
    }

    /// <summary>Validates this store's fixed directory and database resources.</summary>
    private void ValidatePaths() => ValidateDatabase(_directory, _database);
}

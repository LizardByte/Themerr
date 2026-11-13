using System.Diagnostics;
using Microsoft.Data.Sqlite;
using Themerr.Connector;
using Xunit;

namespace Themerr.Connector.Tests;

/// <summary>Tests database persistence, transactions, migrations, and filesystem boundaries.</summary>
public sealed class ThemeOwnershipTests : IDisposable
{
    private readonly string _root = Path.Combine(Path.GetTempPath(), $"themerr-ownership-test-{Guid.NewGuid()}");

    private readonly ThemeOwnership _ownership;

    /// <summary>Initializes a new instance of the <see cref="ThemeOwnershipTests"/> class.</summary>
    public ThemeOwnershipTests()
    {
        Directory.CreateDirectory(_root);
        _ownership = new ThemeOwnership(_root);
    }

    private string DirectoryPath => Path.Combine(_root, "themerr-connector");

    private string DatabasePath => Path.Combine(DirectoryPath, "ownership.db");

    /// <inheritdoc/>
    public void Dispose() => Directory.Delete(_root, true);

    /// <summary>Checks that an ownership read does not create storage files.</summary>
    [Fact]
    public void ReadingMissingOwnershipDoesNotCreateFiles()
    {
        Assert.Null(_ownership.Find(Guid.NewGuid()));
        Assert.Empty(Directory.GetFileSystemEntries(_root));
    }

    /// <summary>Checks that one database retains separate items and format changes across store instances.</summary>
    [Fact]
    public void MultipleItemsAndFormatChangesPersistInOneDatabaseAcrossInstances()
    {
        var first = Guid.NewGuid();
        var second = Guid.NewGuid();
        _ownership.Record(first, "theme.m4a", "first", _ => { });
        _ownership.Record(second, "theme.m4a", "second", _ => { });
        _ownership.Record(first, "theme.opus", "replacement", _ => { });
        var reopened = new ThemeOwnership(_root);
        Assert.Equal(new Ownership("theme.opus", "replacement"), reopened.Find(first));
        Assert.Equal(new Ownership("theme.m4a", "second"), reopened.Find(second));
        Assert.Null(reopened.Find(Guid.NewGuid()));
        Assert.Equal([DatabasePath], Directory.GetFiles(DirectoryPath));
        using var database = new SqliteConnection(new SqliteConnectionStringBuilder
        {
            DataSource = DatabasePath,
            Pooling = false,
        }.ToString());
        database.Open();
        using var count = database.CreateCommand();
        count.CommandText = "SELECT COUNT(*) FROM theme_ownership";
        Assert.Equal(2L, count.ExecuteScalar());
    }

    /// <summary>Checks that failed audio mutations roll back ownership changes.</summary>
    [Fact]
    public void FailedAudioWritesNeverCommitOwnership()
    {
        var id = Guid.NewGuid();
        Assert.Throws<IOException>(() => _ownership.Record(id, "theme.m4a", "failed", _ => throw new IOException()));
        Assert.Null(_ownership.Find(id));
        _ownership.Record(id, "theme.m4a", "original", _ => { });
        Assert.Throws<IOException>(() => _ownership.Record(id, "theme.opus", "failed", _ => throw new IOException()));
        Assert.Equal(new Ownership("theme.m4a", "original"), new ThemeOwnership(_root).Find(id));
    }

    /// <summary>Checks that file mutations receive the previous ownership before replacement.</summary>
    [Fact]
    public void AudioWritesReceivePreviousOwnership()
    {
        var id = Guid.NewGuid();
        _ownership.Record(id, "theme.m4a", "original", previous => Assert.Null(previous));
        _ownership.Record(id, "theme.opus", "replacement", previous =>
            Assert.Equal(new Ownership("theme.m4a", "original"), previous));
        Assert.Equal(new Ownership("theme.opus", "replacement"), _ownership.Find(id));
    }

    /// <summary>Checks that concurrent writes retain separate ownership records.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task ConcurrentItemsKeepSeparateRecords()
    {
        var ids = Enumerable.Range(0, 8).Select(_ => Guid.NewGuid()).ToArray();
        await Task.WhenAll(ids.Select(id => Task.Run(() =>
            new ThemeOwnership(_root).Record(id, "theme.m4a", id.ToString("N"), _ => { }))));
        foreach (var id in ids)
        {
            Assert.Equal(id.ToString("N"), _ownership.Find(id)!.Sha256);
        }

        Assert.Equal([DatabasePath], Directory.GetFiles(DirectoryPath));
    }

    /// <summary>Checks that invalid databases cannot authorize ownership or modify audio.</summary>
    [Fact]
    public void BrokenDatabasesNeverAuthorizeOrWriteAudio()
    {
        Directory.CreateDirectory(DirectoryPath);
        File.WriteAllText(DatabasePath, "invalid database");
        Assert.Throws<SqliteException>(() => _ownership.Find(Guid.NewGuid()));
        var wroteAudio = false;
        Assert.Throws<SqliteException>(() => _ownership.Record(
            Guid.NewGuid(),
            "theme.m4a",
            "digest",
            _ => wroteAudio = true));
        Assert.False(wroteAudio);
        Assert.Equal("invalid database", File.ReadAllText(DatabasePath));
    }

    /// <summary>Checks that EF migrations adopt the earlier connector schema without losing ownership rows.</summary>
    [Fact]
    public void EfMigrationsAdoptTheExistingConnectorDatabaseWithoutLosingOwnership()
    {
        Directory.CreateDirectory(DirectoryPath);
        var id = Guid.NewGuid();
        using (var database = new SqliteConnection(new SqliteConnectionStringBuilder
        {
            DataSource = DatabasePath,
            Pooling = false,
        }.ToString()))
        {
            database.Open();
            using var seed = database.CreateCommand();

            // Fixture representing the pre-EF connector schema, rather than a new EF-created database.
            seed.CommandText = """
                CREATE TABLE theme_ownership (item_id TEXT PRIMARY KEY NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL);
                INSERT INTO theme_ownership VALUES ($id, 'theme.mp3', 'original')
                """;
            seed.Parameters.AddWithValue("$id", id.ToString("N"));
            seed.ExecuteNonQuery();
        }

        Assert.Equal(new Ownership("theme.mp3", "original"), _ownership.Find(id));
        var next = Guid.NewGuid();
        _ownership.Record(next, "theme.m4a", "new", _ => { });
        Assert.Equal(new Ownership("theme.mp3", "original"), _ownership.Find(id));
        Assert.Equal(new Ownership("theme.m4a", "new"), _ownership.Find(next));
    }

    /// <summary>Checks that database and SQLite companion links cannot access sibling resources.</summary>
    /// <param name="filename">The database filename or recorded value exercised by the test.</param>
    [Theory]
    [InlineData("ownership.db")]
    [InlineData("ownership.db-journal")]
    [InlineData("ownership.db-wal")]
    [InlineData("ownership.db-shm")]
    public void DatabaseAndJournalLinksCannotAccessSiblingFiles(string filename)
    {
        Directory.CreateDirectory(DirectoryPath);
        var outside = Path.Combine(_root, "outside.db");
        File.WriteAllText(outside, "outside");
        try
        {
            File.CreateSymbolicLink(Path.Combine(DirectoryPath, filename), outside);
        }
        catch (UnauthorizedAccessException)
        {
            Assert.Skip("Symbolic link creation privilege unavailable.");
        }
        catch (IOException error) when (OperatingSystem.IsWindows() && (error.HResult & 0xffff) == 1314)
        {
            Assert.Skip("Symbolic link creation privilege unavailable.");
        }

        Assert.Throws<ThemeConflictException>(() => _ownership.Find(Guid.NewGuid()));
        Assert.Throws<ThemeConflictException>(() => _ownership.Record(Guid.NewGuid(), "theme.m4a", "digest", _ => { }));
        Assert.Equal("outside", File.ReadAllText(outside));
    }

    /// <summary>Checks that a Windows directory junction cannot redirect ownership storage.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task DatabaseDirectoryJunctionCannotAccessSiblingFiles()
    {
        if (!OperatingSystem.IsWindows())
        {
            Assert.Skip("Windows junction test.");
        }

        var sibling = $"{_root}-sibling";
        Directory.CreateDirectory(sibling);
        var outside = Path.Combine(sibling, "ownership.db");
        await File.WriteAllTextAsync(outside, "outside", TestContext.Current.CancellationToken);
        var command = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "cmd.exe"))
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (var argument in new[]
        {
            "/c",
            "mklink",
            "/J",
            DirectoryPath,
            sibling,
        })
        {
            command.ArgumentList.Add(argument);
        }

        try
        {
            using var process = Process.Start(command)!;
            await process.WaitForExitAsync(TestContext.Current.CancellationToken);
            Assert.Equal(0, process.ExitCode);
            Assert.Throws<ThemeConflictException>(() => _ownership.Find(Guid.NewGuid()));
            Assert.Throws<ThemeConflictException>(() => _ownership.Record(Guid.NewGuid(), "theme.m4a", "digest", _ => { }));
            Assert.Equal("outside", await File.ReadAllTextAsync(outside, TestContext.Current.CancellationToken));
        }
        finally
        {
            if (Directory.Exists(DirectoryPath))
            {
                Directory.Delete(DirectoryPath);
            }

            Directory.Delete(sibling, true);
        }
    }
}

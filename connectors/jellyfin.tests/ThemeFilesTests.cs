using System.Diagnostics;
using System.Security.Cryptography;
using Themerr.Connector;
using Xunit;

namespace Themerr.Connector.Tests;

/// <summary>Tests upload integrity, ownership, protected themes, and filesystem boundaries.</summary>
[Collection(nameof(SqliteSharedCacheCollection))]
public sealed class ThemeFilesTests : IDisposable
{
    private readonly string _root = Path.Combine(Path.GetTempPath(), $"themerr-connector-test-{Guid.NewGuid()}");
    private readonly byte[] _audio = "complete validated audio"u8.ToArray();
    private readonly Guid _itemId = Guid.NewGuid();
    private readonly ThemeFiles _themes;
    private readonly ThemeOwnership _ownership;

    /// <summary>Initializes a new instance of the <see cref="ThemeFilesTests"/> class.</summary>
    public ThemeFilesTests()
    {
        Directory.CreateDirectory(_root);
        _ownership = new ThemeOwnership(Path.Combine(_root, "server-data"));
        _themes = new ThemeFiles(_ownership);
    }

    private string Digest => Convert.ToHexString(SHA256.HashData(_audio)).ToLowerInvariant();

    /// <inheritdoc/>
    public void Dispose() => Directory.Delete(_root, true);

    /// <summary>Checks that theme directory junctions cannot redirect deletion into sibling media.</summary>
    /// <param name="nested">Whether the junction is nested inside the theme-music directory.</param>
    /// <returns>The asynchronous test execution.</returns>
    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task WindowsJunctionsNeverDeleteSiblingThemes(bool nested)
    {
        if (!OperatingSystem.IsWindows())
        {
            Assert.Skip("Windows junction test.");
        }

        var sibling = $"{_root}-sibling";
        Directory.CreateDirectory(sibling);
        var outside = Path.Combine(sibling, "outside.mp3");
        await File.WriteAllTextAsync(outside, "outside theme", TestContext.Current.CancellationToken);
        var music = FilePath("theme-music");
        if (nested)
        {
            Directory.CreateDirectory(music);
        }

        var link = nested ? Path.Combine(music, "nested") : music;
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
            link,
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
            await Assert.ThrowsAsync<ThemeConflictException>(() => Save(count: 1, overwrite: true, backup: false));
            Assert.Equal("outside theme", await File.ReadAllTextAsync(outside, TestContext.Current.CancellationToken));
        }
        finally
        {
            if (Directory.Exists(link))
            {
                Directory.Delete(link);
            }

            Directory.Delete(sibling, true);
        }
    }

    /// <summary>Checks that explicit replacement can remove user themes when backups are disabled.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task ReplacementCanRemoveUserThemesWithoutCreatingBackups()
    {
        await File.WriteAllTextAsync(FilePath("theme.mp3"), "user theme", TestContext.Current.CancellationToken);
        var music = Directory.CreateDirectory(FilePath("theme-music"));
        await File.WriteAllTextAsync(Path.Combine(music.FullName, "user.mp3"), "another theme", TestContext.Current.CancellationToken);
        await Save(count: 2, overwrite: true, backup: false);
        Assert.True(_themes.State(_itemId, _root).Owned);
        Assert.False(File.Exists(FilePath("theme.mp3")));
        Assert.False(Directory.Exists(music.FullName));
        Assert.False(Directory.Exists(FilePath(".themerr-user-themes")));
        Assert.False(Directory.Exists(FilePath(".themerr-user-theme-music")));
    }

    /// <summary>Checks that unsupported audio MIME types are rejected before file creation.</summary>
    /// <param name="type">The candidate audio MIME type.</param>
    /// <returns>The asynchronous test execution.</returns>
    [Theory]
    [InlineData(null)]
    [InlineData("../../file")]
    [InlineData("audio/mp3")]
    public async Task RejectsUnknownContentTypes(string? type)
    {
        await Assert.ThrowsAsync<InvalidDataException>(() => Save(type));
        Assert.Empty(Directory.GetFiles(_root));
    }

    /// <summary>Checks that malformed or mismatched audio digests are rejected.</summary>
    /// <param name="digest">The candidate SHA-256 digest.</param>
    /// <returns>The asynchronous test execution.</returns>
    [Theory]
    [InlineData("short")]
    [InlineData("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")]
    [InlineData("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")]
    public async Task RejectsInvalidOrMismatchedHashes(string digest)
    {
        await Assert.ThrowsAsync<InvalidDataException>(() => Save(digest: digest));
        Assert.Empty(Directory.GetFiles(_root));
    }

    /// <summary>Checks that empty uploads are rejected and temporary files are removed.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task RejectsEmptyAudioAndCleansTemporaryFiles()
    {
        await Assert.ThrowsAsync<InvalidDataException>(() => Save(stream: new MemoryStream()));
        Assert.Empty(Directory.GetFiles(_root));
    }

    /// <summary>Checks that verified uploads retain ownership when switching audio formats.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task SavesVerifiedAudioAndSwitchesFormats()
    {
        Assert.False(_themes.State(_itemId, _root).Present);
        var saved = await Save();
        Assert.True(saved.Owned);
        Assert.Equal(Digest, saved.Sha256);
        Assert.True(_themes.State(_itemId, _root).Owned);
        Assert.False(_themes.State(_itemId, _root, 2).Owned);
        await Assert.ThrowsAsync<ThemeConflictException>(() => Save(count: 2));
        await Save("audio/ogg", Digest.ToUpperInvariant());
        Assert.False(File.Exists(FilePath("theme.m4a")));
        Assert.Equal(_audio, await File.ReadAllBytesAsync(FilePath("theme.opus"), TestContext.Current.CancellationToken));
        Assert.Equal([FilePath("theme.opus")], Directory.GetFiles(_root));
        Assert.True(new ThemeFiles(new ThemeOwnership(FilePath("server-data"))).State(_itemId, _root).Owned);
        Assert.False(_themes.State(Guid.NewGuid(), _root).Owned);
    }

    /// <summary>Checks that uploads and replacements succeed when the host enables SQLite shared cache.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task SavesAndReplacesThemesWithSharedCacheEnabled()
    {
        SQLitePCL.Batteries_V2.Init();
        Assert.Equal(SQLitePCL.raw.SQLITE_OK, SQLitePCL.raw.sqlite3_enable_shared_cache(1));
        try
        {
            await Save();
            Assert.Equal(_audio, await File.ReadAllBytesAsync(FilePath("theme.m4a"), TestContext.Current.CancellationToken));
            Assert.Equal(new Ownership("theme.m4a", Digest), _ownership.Find(_itemId));

            var replacement = "different replacement audio"u8.ToArray();
            var digest = Convert.ToHexString(SHA256.HashData(replacement)).ToLowerInvariant();
            var saved = await Save("audio/ogg", digest, stream: new MemoryStream(replacement));
            Assert.True(saved.Owned);
            Assert.Equal(digest, saved.Sha256);
            Assert.True(_themes.State(_itemId, _root).Owned);
            Assert.Equal(new Ownership("theme.opus", digest), _ownership.Find(_itemId));
            Assert.Equal(replacement, await File.ReadAllBytesAsync(FilePath("theme.opus"), TestContext.Current.CancellationToken));
            Assert.Equal([FilePath("theme.opus")], Directory.GetFiles(_root));
            Assert.False(Directory.Exists(FilePath(".themerr-user-themes")));
        }
        finally
        {
            Assert.Equal(SQLitePCL.raw.SQLITE_OK, SQLitePCL.raw.sqlite3_enable_shared_cache(0));
        }
    }

    /// <summary>Checks that user themes and manually edited uploads remain protected by default.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task PreservesUserThemesByDefaultIncludingManuallyEditedUploads()
    {
        await File.WriteAllTextAsync(FilePath("theme.mp3"), "user theme", TestContext.Current.CancellationToken);
        await Assert.ThrowsAsync<ThemeConflictException>(() => Save());
        Assert.Equal("user theme", await File.ReadAllTextAsync(FilePath("theme.mp3"), TestContext.Current.CancellationToken));
        File.Delete(FilePath("theme.mp3"));
        await Save();
        await File.WriteAllTextAsync(FilePath("theme.m4a"), "manually changed", TestContext.Current.CancellationToken);
        Assert.False(_themes.State(_itemId, _root).Owned);
        await Assert.ThrowsAsync<ThemeConflictException>(() => Save());
        Assert.Equal("manually changed", await File.ReadAllTextAsync(FilePath("theme.m4a"), TestContext.Current.CancellationToken));
    }

    /// <summary>Checks that explicit replacement backs up user themes and records the new upload.</summary>
    /// <param name="name">The fixed theme resource name exercised by the test.</param>
    /// <returns>The asynchronous test execution.</returns>
    [Theory]
    [InlineData("theme.mp3")]
    [InlineData("theme.m4a")]
    [InlineData("theme.opus")]
    public async Task ExplicitReplacementBacksUpUserFiles(string name)
    {
        await File.WriteAllTextAsync(FilePath(name), "user theme", TestContext.Current.CancellationToken);
        var state = await Save(overwrite: true);
        Assert.True(state.Owned);
        Assert.True(_themes.State(_itemId, _root).Owned);
        Assert.Equal("user theme", await File.ReadAllTextAsync(Path.Combine(_root, ".themerr-user-themes", name), TestContext.Current.CancellationToken));
        Assert.Equal(_audio, await File.ReadAllBytesAsync(FilePath("theme.m4a"), TestContext.Current.CancellationToken));
    }

    /// <summary>Checks that theme-music backups are preserved instead of overwritten by later replacements.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task ReplacementBacksUpThemeMusicFolderAndNeverOverwritesEarlierBackups()
    {
        var music = FilePath("theme-music");
        Directory.CreateDirectory(music);
        await File.WriteAllTextAsync(Path.Combine(music, "my song.mp3"), "user theme", TestContext.Current.CancellationToken);
        Assert.True(_themes.State(_itemId, _root).Present);
        await Assert.ThrowsAsync<ThemeConflictException>(() => Save(count: 1));
        await Save(count: 1, overwrite: true);
        Assert.Equal("user theme", await File.ReadAllTextAsync(Path.Combine(_root, ".themerr-user-theme-music", "my song.mp3"), TestContext.Current.CancellationToken));
        Directory.CreateDirectory(music);
        await File.WriteAllTextAsync(Path.Combine(music, "second.mp3"), "another theme", TestContext.Current.CancellationToken);
        await Assert.ThrowsAsync<ThemeConflictException>(() => Save(count: 2, overwrite: true));
        Assert.True(File.Exists(Path.Combine(music, "second.mp3")));
    }

    /// <summary>Checks that themes outside recognized resources remain protected.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task NativeThemesOutsideFixedResourcesStayProtected()
    {
        await Assert.ThrowsAsync<ThemeConflictException>(() => Save(count: 1, overwrite: true));
        Assert.False(File.Exists(FilePath("theme.m4a")));
    }

    /// <summary>Checks that stored filenames cannot select filesystem resources.</summary>
    /// <param name="filename">The database filename or recorded value exercised by the test.</param>
    /// <returns>The asynchronous test execution.</returns>
    [Theory]
    [InlineData("../outside.m4a")]
    [InlineData("theme.mp3")]
    public async Task DatabaseValuesNeverBecomePaths(string filename)
    {
        await File.WriteAllTextAsync(FilePath("theme.m4a"), "user theme", TestContext.Current.CancellationToken);
        _ownership.Record(_itemId, filename, Digest, _ => { });
        Assert.False(_themes.State(_itemId, _root).Owned);
        await Assert.ThrowsAsync<ThemeConflictException>(() => Save());
    }

    /// <summary>Checks that obsolete JSON sidecars are ignored and left untouched.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task JsonSidecarsAreIgnoredAndNeverMigratedOrRemoved()
    {
        await File.WriteAllBytesAsync(FilePath("theme.m4a"), _audio, TestContext.Current.CancellationToken);
        var json = $$"""{"File":"theme.m4a","Sha256":"{{Digest}}"}""";
        await File.WriteAllTextAsync(FilePath(".themerr-connector.json"), json, TestContext.Current.CancellationToken);
        Assert.False(_themes.State(_itemId, _root).Owned);
        await Assert.ThrowsAsync<ThemeConflictException>(() => Save());
        await Save(overwrite: true, backup: false);
        Assert.Equal(json, await File.ReadAllTextAsync(FilePath(".themerr-connector.json"), TestContext.Current.CancellationToken));
        Assert.True(_themes.State(_itemId, _root).Owned);
    }

    /// <summary>Checks that a missing theme file cannot retain ownership.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task OwnershipWithoutItsFileIsNotOwned()
    {
        await Save();
        File.Delete(FilePath("theme.m4a"));
        Assert.False(_themes.State(_itemId, _root).Present);
        Assert.False(_themes.State(_itemId, _root).Owned);
    }

    /// <summary>Checks that file and directory links cannot escape into sibling media.</summary>
    /// <param name="name">The fixed theme resource name exercised by the test.</param>
    /// <param name="directory">Whether to create a directory link instead of a file link.</param>
    /// <returns>The asynchronous test execution.</returns>
    [Theory]
    [InlineData("theme.m4a", false)]
    [InlineData("theme-music", true)]
    [InlineData(".themerr-user-themes", true)]
    public async Task RejectsLinksIntoSiblingDirectories(string name, bool directory)
    {
        var sibling = $"{_root}-sibling";
        Directory.CreateDirectory(sibling);
        var target = Path.Combine(sibling, "outside.m4a");
        await File.WriteAllTextAsync(target, "outside", TestContext.Current.CancellationToken);
        try
        {
            try
            {
                if (directory)
                {
                    Directory.CreateSymbolicLink(FilePath(name), sibling);
                }
                else
                {
                    File.CreateSymbolicLink(FilePath(name), target);
                }
            }
            catch (UnauthorizedAccessException)
            {
                Assert.Skip("Symbolic link creation privilege unavailable.");
            }
            catch (IOException error) when (OperatingSystem.IsWindows() && (error.HResult & 0xffff) == 1314)
            {
                Assert.Skip("Symbolic link creation privilege unavailable.");
            }

            await File.WriteAllTextAsync(FilePath("theme.mp3"), "user theme", TestContext.Current.CancellationToken);
            await Assert.ThrowsAsync<ThemeConflictException>(() => Save(overwrite: true));
            Assert.Equal("outside", await File.ReadAllTextAsync(target, TestContext.Current.CancellationToken));
        }
        finally
        {
            Directory.Delete(sibling, true);
        }
    }

    private string FilePath(string name) => Path.Combine(_root, name);

    private Task<ThemeState> Save(
        string? type = "audio/mp4",
        string? digest = null,
        int count = 0,
        bool overwrite = false,
        Stream? stream = null,
        bool backup = true) => _themes.Save(
            _itemId,
            _root,
            stream ?? new MemoryStream(_audio),
            type,
            digest ?? Digest,
            count,
            CancellationToken.None,
            overwrite,
            backup);
}

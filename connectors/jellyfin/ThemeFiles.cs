using System.Security.Cryptography;

namespace Themerr.Connector;

/// <summary>Writes fixed theme filenames beneath Jellyfin-owned item directories.</summary>
/// <param name="ownership">The shared upload ownership database.</param>
/// <remarks>
/// Existing themes are owned only when their current SHA-256 digest matches the database.
/// Symlinks, junctions, ambiguous themes, and changes during upload remain protected.
/// </remarks>
/// <seealso cref="ThemeOwnership"/>
public sealed class ThemeFiles(ThemeOwnership ownership)
{
    private const long MaximumBytes = 104857600;
    private const string UserBackup = ".themerr-user-themes";
    private const string ThemeMusic = "theme-music";
    private const string UserMusicBackup = ".themerr-user-theme-music";
    private static readonly string[] ThemeNames =
        [
            "theme.m4a",
            "theme.opus",
            "theme.mp3",
            "theme.aac",
            "theme.ogg",
            "theme.oga",
            "theme.flac",
            "theme.wav",
            "theme.wma",
            "theme.aiff",
            "theme.aif",
            "theme.webm",
        ];

    private static readonly Dictionary<string, string> Formats = new()
    {
        ["audio/mp4"] = "theme.m4a",
        ["audio/ogg"] = "theme.opus",
    };

    /// <summary>Determines theme presence and verifies any recorded ownership.</summary>
    /// <param name="itemId">The native Jellyfin item identifier.</param>
    /// <param name="root">The trusted item directory resolved by Jellyfin.</param>
    /// <param name="themeSongCount">The number of themes reported by Jellyfin.</param>
    /// <returns>Presence, verified ownership, and the digest of a single tracked theme.</returns>
    /// <exception cref="ThemeConflictException">A theme resource is a symlink or junction.</exception>
    public ThemeState State(Guid itemId, string root, int themeSongCount = 0) =>
        State(root, themeSongCount, ownership.Find(itemId));

    /// <summary>Validates and atomically replaces one supported audio theme.</summary>
    /// <param name="itemId">The native Jellyfin item identifier.</param>
    /// <param name="root">The trusted item directory resolved by Jellyfin.</param>
    /// <param name="body">The uploaded audio stream.</param>
    /// <param name="contentType">The supported MP4 or Ogg audio MIME type.</param>
    /// <param name="expectedDigest">The expected SHA-256 digest in hexadecimal form.</param>
    /// <param name="themeSongCount">The number of themes reported by Jellyfin.</param>
    /// <param name="cancellationToken">Cancellation while receiving audio.</param>
    /// <param name="overwriteUser">Whether to replace themes without verified ownership.</param>
    /// <param name="backupUser">Whether to retain overwritten themes in fixed backup directories.</param>
    /// <returns>The saved and verified theme state.</returns>
    /// <exception cref="ThemeConflictException">An existing theme is protected or changed during upload.</exception>
    /// <exception cref="InvalidDataException">The format, size, or digest is invalid.</exception>
    public async Task<ThemeState> Save(
        Guid itemId,
        string root,
        Stream body,
        string? contentType,
        string expectedDigest,
        int themeSongCount,
        CancellationToken cancellationToken,
        bool overwriteUser = false,
        bool backupUser = true)
    {
        if (contentType is null || !Formats.TryGetValue(contentType, out var file) ||
            expectedDigest.Length != 64 || !expectedDigest.All(Uri.IsHexDigit))
        {
            throw new InvalidDataException();
        }

        var state = State(itemId, root, themeSongCount);
        if (state.Present && !state.Owned && !overwriteUser)
        {
            throw new ThemeConflictException();
        }

        var originals = Snapshot(root);
        var path = Path.Combine(root, file);
        RejectLink(path);
        var temporary = Path.Combine(root, Path.GetRandomFileName());
        try
        {
            var digest = await Write(body, temporary, cancellationToken).ConfigureAwait(false);
            if (!string.Equals(digest, expectedDigest, StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidDataException();
            }

            ownership.Record(itemId, file, digest, previous =>
            {
                // Recheck ownership and link boundaries after receiving the complete body.
                var current = State(root, themeSongCount, previous);
                if ((current.Present && !current.Owned && !overwriteUser) ||
                    !originals.SequenceEqual(Snapshot(root)))
                {
                    throw new ThemeConflictException();
                }

                RejectLink(path);
                if (current.Present && !current.Owned)
                {
                    ReplaceUserThemes(root, themeSongCount, backupUser);
                }

                File.Move(temporary, path, true);
                foreach (var other in ThemeNames.Where(value => value != file))
                {
                    var old = Path.Combine(root, other);
                    RejectLink(old);
                    if (state.Owned && File.Exists(old))
                    {
                        File.Delete(old);
                    }
                }
            });
            return new(true, true, digest);
        }
        finally
        {
            File.Delete(temporary);
        }
    }

    /// <summary>Records older plugin ownership only when the current theme digest matches.</summary>
    /// <param name="itemId">The native Jellyfin item identifier.</param>
    /// <param name="root">The trusted item directory resolved by Jellyfin.</param>
    /// <param name="expectedDigest">The SHA-256 digest read from the older plugin, or null.</param>
    /// <param name="themeSongCount">The number of themes reported by Jellyfin.</param>
    /// <returns>The existing state, with ownership recognized only after digest verification.</returns>
    /// <exception cref="ThemeConflictException">A theme resource is unsafe or changes during import.</exception>
    public ThemeState Import(Guid itemId, string root, string? expectedDigest, int themeSongCount = 0)
    {
        var current = State(itemId, root, themeSongCount);
        if (current.Owned || expectedDigest is null || expectedDigest.Length != 64 ||
            !expectedDigest.All(Uri.IsHexDigit) || themeSongCount > 1 || Directory.Exists(Path.Combine(root, ThemeMusic)))
        {
            return current;
        }

        var files = ThemeNames.Where(name => File.Exists(Path.Combine(root, name))).ToArray();
        if (files.Length != 1)
        {
            return current;
        }

        var original = Snapshot(root);
        var file = files[0];
        using (var stream = File.OpenRead(Path.Combine(root, file)))
        {
            var digest = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
            if (!string.Equals(digest, expectedDigest, StringComparison.OrdinalIgnoreCase))
            {
                return current;
            }

            ownership.Record(itemId, file, digest, _ =>
            {
                if (!original.SequenceEqual(Snapshot(root)))
                {
                    throw new ThemeConflictException();
                }
            });
        }

        return State(itemId, root, themeSongCount);
    }

    /// <summary>Verifies theme files against ownership already read by the caller.</summary>
    /// <param name="root">The trusted item directory resolved by Jellyfin.</param>
    /// <param name="themeSongCount">The number of themes reported by Jellyfin.</param>
    /// <param name="record">The ownership to verify, or null when the item has no record.</param>
    /// <returns>Presence, verified ownership, and the digest of a single tracked theme.</returns>
    /// <exception cref="ThemeConflictException">A theme resource is a symlink or junction.</exception>
    private static ThemeState State(string root, int themeSongCount, Ownership? record)
    {
        RejectDirectoryLink(root);
        var files = ThemeNames.Where(name => File.Exists(Path.Combine(root, name))).ToArray();
        var music = Path.Combine(root, ThemeMusic);
        RejectDirectoryLink(music);
        var present = files.Length > 0 || themeSongCount > 0 || Directory.Exists(music);

        // A database value selects a code-owned filename; it never becomes a path.
        var file = ThemeNames.FirstOrDefault(value => value == record?.File);
        if (file is null || files.Length != 1 || themeSongCount > 1 || Directory.Exists(music))
        {
            return new(present, false, null);
        }

        var path = Path.Combine(root, file);
        RejectLink(path);
        if (!File.Exists(path))
        {
            return new(present, false, null);
        }

        using var stream = File.OpenRead(path);
        var digest = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
        return new(true, digest == record!.Sha256, digest);
    }

    /// <summary>Receives bounded audio into a new temporary file while hashing it.</summary>
    /// <param name="body">The incoming audio stream.</param>
    /// <param name="temporary">The code-generated temporary filename.</param>
    /// <param name="cancellationToken">Cancellation while reading or writing.</param>
    /// <returns>The audio's lowercase SHA-256 digest.</returns>
    /// <exception cref="InvalidDataException">The audio is empty or exceeds the size limit.</exception>
    private static async Task<string> Write(Stream body, string temporary, CancellationToken cancellationToken)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        await using var output = new FileStream(
            temporary,
            FileMode.CreateNew,
            FileAccess.Write,
            FileShare.None,
            65536,
            FileOptions.Asynchronous);
        var buffer = new byte[65536];
        long size = 0;
        int read;
        while ((read = await body.ReadAsync(buffer, cancellationToken).ConfigureAwait(false)) != 0)
        {
            size += read;
            if (size > MaximumBytes)
            {
                throw new InvalidDataException();
            }

            hash.AppendData(buffer, 0, read);
            await output.WriteAsync(buffer.AsMemory(0, read), cancellationToken).ConfigureAwait(false);
        }

        if (size == 0)
        {
            throw new InvalidDataException();
        }

        return Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
    }

    /// <summary>Rejects a symlink or reparse point at a fixed file resource.</summary>
    /// <param name="path">The trusted directory combined with a fixed filename.</param>
    /// <exception cref="ThemeConflictException">The file is a link or reparse point.</exception>
    private static void RejectLink(string path)
    {
        var info = new FileInfo(path);
        if (info.LinkTarget is not null || (info.Exists && (info.Attributes & FileAttributes.ReparsePoint) != 0))
        {
            throw new ThemeConflictException();
        }
    }

    /// <summary>Rejects a symlink or junction at a theme directory.</summary>
    /// <param name="path">The trusted item or fixed backup directory.</param>
    /// <exception cref="ThemeConflictException">The directory is a link or reparse point.</exception>
    private static void RejectDirectoryLink(string path)
    {
        var info = new DirectoryInfo(path);
        if (info.LinkTarget is not null || (info.Exists && (info.Attributes & FileAttributes.ReparsePoint) != 0))
        {
            throw new ThemeConflictException();
        }
    }

    /// <summary>Captures theme digests and directory timestamps to detect concurrent changes.</summary>
    /// <param name="root">The trusted item directory.</param>
    /// <returns>The ordered snapshot used before committing theme mutations.</returns>
    private static string[] Snapshot(string root)
    {
        var snapshot = new List<string>();
        foreach (var name in ThemeNames)
        {
            var path = Path.Combine(root, name);
            RejectLink(path);
            if (!File.Exists(path))
            {
                continue;
            }

            using var stream = File.OpenRead(path);
            snapshot.Add($"{name}:{Convert.ToHexString(SHA256.HashData(stream))}");
        }

        var music = Path.Combine(root, ThemeMusic);
        RejectDirectoryLink(music);
        if (Directory.Exists(music))
        {
            snapshot.Add(Directory.GetLastWriteTimeUtc(music).Ticks.ToString());
        }

        return snapshot.ToArray();
    }

    /// <summary>Moves or removes only recognized user theme resources after validation.</summary>
    /// <param name="root">The trusted item directory.</param>
    /// <param name="themeSongCount">The number of themes reported by Jellyfin.</param>
    /// <param name="backupUser">Whether to preserve the recognized themes in backup directories.</param>
    /// <exception cref="ThemeConflictException">A resource is unsafe, unknown, or already backed up.</exception>
    private static void ReplaceUserThemes(string root, int themeSongCount, bool backupUser)
    {
        var backup = Path.Combine(root, UserBackup);
        var music = Path.Combine(root, ThemeMusic);
        var musicBackup = Path.Combine(root, UserMusicBackup);
        RejectDirectoryLink(music);
        var names = ThemeNames.Where(name => File.Exists(Path.Combine(root, name))).ToArray();

        // Native themes outside these fixed resources must remain untouched.
        if (themeSongCount > names.Length && !Directory.Exists(music))
        {
            throw new ThemeConflictException();
        }

        if (!backupUser)
        {
            // Validate every entry before recursive deletion, including directory junctions.
            if (Directory.Exists(music))
            {
                ValidateTree(new DirectoryInfo(music));
            }

            foreach (var name in names)
            {
                RejectLink(Path.Combine(root, name));
            }

            foreach (var name in names)
            {
                File.Delete(Path.Combine(root, name));
            }

            if (Directory.Exists(music))
            {
                Directory.Delete(music, true);
            }

            return;
        }

        RejectDirectoryLink(backup);
        RejectDirectoryLink(musicBackup);
        if (names.Any(name => File.Exists(Path.Combine(backup, name))) ||
            (Directory.Exists(music) && Directory.Exists(musicBackup)))
        {
            throw new ThemeConflictException();
        }

        Directory.CreateDirectory(backup);
        foreach (var name in names)
        {
            var source = Path.Combine(root, name);
            var destination = Path.Combine(backup, name);
            RejectLink(source);
            RejectLink(destination);
        }

        foreach (var name in names)
        {
            File.Move(Path.Combine(root, name), Path.Combine(backup, name));
        }

        if (Directory.Exists(music))
        {
            Directory.Move(music, musicBackup);
        }
    }

    /// <summary>Rejects links throughout a theme-music directory before recursive removal.</summary>
    /// <param name="directory">The fixed theme-music directory being replaced.</param>
    /// <exception cref="ThemeConflictException">The directory tree contains a link or reparse point.</exception>
    private static void ValidateTree(DirectoryInfo directory)
    {
        RejectDirectoryLink(directory.FullName);
        foreach (var entry in directory.GetFileSystemInfos())
        {
            if (entry.LinkTarget is not null || (entry.Attributes & FileAttributes.ReparsePoint) != 0)
            {
                throw new ThemeConflictException();
            }

            if (entry is DirectoryInfo child)
            {
                ValidateTree(child);
            }
        }
    }
}

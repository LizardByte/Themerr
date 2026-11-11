using Xunit;

namespace Themerr.Connector.Tests;

/// <summary>Isolates tests that change SQLite's process-wide shared-cache setting.</summary>
/// <seealso cref="ThemeFilesTests"/>
[CollectionDefinition(nameof(SqliteSharedCacheCollection), DisableParallelization = true)]
public sealed class SqliteSharedCacheCollection
{
}

using System.Reflection;
using MediaBrowser.Common.Api;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.MediaInfo;
using MediaBrowser.Model.Serialization;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging.Abstractions;
using Moq;
using Xunit;

namespace Themerr.Connector.Tests;

/// <summary>Tests administrator authorization, connector identity, and fixed endpoint errors.</summary>
public sealed class ControllerTests : IDisposable
{
    private readonly string _root = Path.Combine(Path.GetTempPath(), $"themerr-controller-test-{Guid.NewGuid()}");
    private readonly Mock<ILibraryManager> _library = new();
    private readonly Controller _controller;

    /// <summary>Initializes a new instance of the <see cref="ControllerTests"/> class.</summary>
    public ControllerTests()
    {
        Directory.CreateDirectory(_root);
        var media = new Mock<IMediaSourceManager>();
        media.Setup(value => value.GetPathProtocol(It.IsAny<string>())).Returns(MediaProtocol.File);
        BaseItem.MediaSourceManager = media.Object;
        BaseItem.LibraryManager = _library.Object;
        _library.Setup(value => value.GetItemList(It.IsAny<InternalItemsQuery>())).Returns([]);
        var paths = new Mock<IApplicationPaths>();
        paths.SetupGet(value => value.DataPath).Returns(Path.Combine(_root, "server-data"));
        _controller = new Controller(_library.Object, paths.Object, NullLogger<Controller>.Instance)
        { ControllerContext = new ControllerContext { HttpContext = new DefaultHttpContext() } };
    }

    /// <inheritdoc/>
    public void Dispose() => Directory.Delete(_root, true);

    /// <summary>Checks that every connector endpoint requires Jellyfin administrator authorization.</summary>
    [Fact]
    public void EndpointsRequireAnAdministrator()
    {
        var policy = typeof(Controller).GetCustomAttribute<AuthorizeAttribute>();
        Assert.Equal(Policies.RequiresElevation, policy?.Policy);
        Assert.Empty(typeof(Controller).GetMethods().SelectMany(method => method.GetCustomAttributes<AllowAnonymousAttribute>()));
    }

    /// <summary>Checks that the identity endpoint reports the embedded source fingerprint and Jellyfin ABI.</summary>
    [Fact]
    public void IdentityReportsEmbeddedBuildAndAbi()
    {
        var value = _controller.Identity();
        Assert.Equal(1, value.GetType().GetProperty("protocol")!.GetValue(value));
        Assert.Equal("development", value.GetType().GetProperty("build")!.GetValue(value));
        Assert.Equal(
            typeof(Plugin).Assembly.GetCustomAttributes<AssemblyMetadataAttribute>()
            .Single(attribute => attribute.Key == "JellyfinAbi").Value,
            value.GetType().GetProperty("targetAbi")!.GetValue(value));
    }

    /// <summary>Checks the plugin identity and connector-only description.</summary>
    [Fact]
    public void PluginHasStableIdentityAndNoBackgroundProcessing()
    {
        var paths = new Mock<IApplicationPaths>();
        paths.SetupGet(value => value.PluginsPath).Returns(_root);
        paths.SetupGet(value => value.PluginConfigurationsPath).Returns(_root);
        var plugin = new Plugin(paths.Object, Mock.Of<IXmlSerializer>());
        Assert.Equal("Themerr Connector", plugin.Name);
        Assert.Equal(new Guid("f9a117dc-b44a-4507-9706-241837784369"), plugin.Id);
        Assert.Contains("connector", plugin.Description);
    }

    /// <summary>Checks that unknown item identifiers are rejected without file access.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task UnknownIdsNeverAccessTheFilesystem()
    {
        Assert.IsType<NotFoundResult>(_controller.State(Guid.NewGuid()).Result);
        _controller.Request.Headers["X-Themerr-Connector"] = "development";
        Assert.IsType<NotFoundResult>((await _controller.Upload(Guid.NewGuid(), CancellationToken.None)).Result);
        Assert.Empty(Directory.GetFiles(_root));
    }

    /// <summary>Checks that a mismatched build is rejected before resolving an item.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task UploadRejectsWrongBuildBeforeResolvingAnItem()
    {
        _controller.Request.Headers["X-Themerr-Connector"] = "different";
        Assert.IsType<ConflictResult>((await _controller.Upload(Guid.NewGuid(), CancellationToken.None)).Result);
        _library.Verify(value => value.GetItemById(It.IsAny<Guid>()), Times.Never);
        Assert.IsType<ConflictResult>((await _controller.Import(Guid.NewGuid(), CancellationToken.None)).Result);
        _library.Verify(value => value.GetItemById(It.IsAny<Guid>()), Times.Never);
    }

    /// <summary>Checks that a collection uses its native metadata directory for themes.</summary>
    [Fact]
    public void CollectionsUseTheirNativeMetadataDirectory()
    {
        var id = Guid.NewGuid();
        _library.Setup(value => value.GetItemById(id)).Returns(new BoxSet
        {
            Id = id,
            Path = _root,
        });
        var result = _controller.State(id);
        Assert.Null(result.Result);
        Assert.False(result.Value!.Present);
    }

    /// <summary>Checks that locked metadata and unsupported item types block theme mutations.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task LockedAndUnsupportedItemsAreRejected()
    {
        var id = Guid.NewGuid();
        _controller.Request.Headers["X-Themerr-Connector"] = "development";
        _library.Setup(value => value.GetItemById(id)).Returns(new BoxSet
        {
            Id = id,
            Path = _root,
            IsLocked = true,
        });
        Assert.IsType<ConflictResult>((await _controller.Upload(id, CancellationToken.None)).Result);
        _library.Setup(value => value.GetItemById(id)).Returns(new Folder
        {
            Id = id,
            Path = _root,
        });
        Assert.IsType<BadRequestResult>(_controller.State(id).Result);
        Assert.IsType<ConflictResult>((await _controller.Upload(id, CancellationToken.None)).Result);
    }

    /// <summary>Checks that movies in shared folders cannot read, import, or overwrite a shared theme.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task SharedMovieFoldersRemainProtected()
    {
        var id = Guid.NewGuid();
        var movie = Path.Combine(_root, "Example.mkv");
        var theme = Path.Combine(_root, "theme.m4a");
        await File.WriteAllTextAsync(movie, "original movie", TestContext.Current.CancellationToken);
        await File.WriteAllTextAsync(theme, "another movie's theme", TestContext.Current.CancellationToken);
        _library.Setup(value => value.GetItemById(id)).Returns(new Movie
        {
            Id = id,
            Path = movie,
            IsInMixedFolder = true,
        });
        _controller.Request.Headers["X-Themerr-Connector"] = "development";
        _controller.Request.Headers["X-Themerr-Overwrite-User"] = "true";
        Assert.IsType<BadRequestResult>(_controller.State(id).Result);
        Assert.IsType<ConflictResult>((await _controller.Upload(id, CancellationToken.None)).Result);
        Assert.IsType<BadRequestResult>((await _controller.Import(id, CancellationToken.None)).Result);
        Assert.Equal("original movie", await File.ReadAllTextAsync(movie, TestContext.Current.CancellationToken));
        Assert.Equal("another movie's theme", await File.ReadAllTextAsync(theme, TestContext.Current.CancellationToken));
        Assert.False(Directory.Exists(Path.Combine(_root, "server-data")));
    }

    /// <summary>Checks that invalid audio and protected user themes return fixed client errors.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task InvalidAndProtectedBodiesReturnFixedErrors()
    {
        var id = Guid.NewGuid();
        _library.Setup(value => value.GetItemById(id)).Returns(new BoxSet
        {
            Id = id,
            Path = _root,
        });
        _controller.Request.Headers["X-Themerr-Connector"] = "development";
        Assert.IsType<BadRequestResult>((await _controller.Upload(id, CancellationToken.None)).Result);
        await File.WriteAllTextAsync(Path.Combine(_root, "theme.mp3"), "user theme", TestContext.Current.CancellationToken);
        _controller.Request.ContentType = "audio/mp4";
        _controller.Request.Headers["X-Themerr-SHA256"] = new string('a', 64);
        Assert.IsType<ConflictResult>((await _controller.Upload(id, CancellationToken.None)).Result);
    }

    /// <summary>Checks that database failures hide storage details and leave audio unchanged.</summary>
    /// <returns>The asynchronous test execution.</returns>
    [Fact]
    public async Task DatabaseErrorsReturnFixedMessagesAndLeaveAudioUntouched()
    {
        var id = Guid.NewGuid();
        _library.Setup(value => value.GetItemById(id)).Returns(new BoxSet
        {
            Id = id,
            Path = _root,
        });
        var data = Path.Combine(_root, "server-data", "themerr-connector");
        Directory.CreateDirectory(data);
        await File.WriteAllTextAsync(Path.Combine(data, "ownership.db"), "private database contents", TestContext.Current.CancellationToken);
        var state = Assert.IsType<ObjectResult>(_controller.State(id).Result);
        Assert.Equal(500, state.StatusCode);
        Assert.Equal("Could not access connector theme storage.", state.Value);
        _controller.Request.Headers["X-Themerr-Connector"] = "development";
        _controller.Request.ContentType = "audio/mp4";
        _controller.Request.Headers["X-Themerr-SHA256"] = new string('a', 64);
        var upload = Assert.IsType<ObjectResult>((await _controller.Upload(id, CancellationToken.None)).Result);
        Assert.Equal(500, upload.StatusCode);
        Assert.Equal(state.Value, upload.Value);
        Assert.Empty(Directory.GetFiles(_root));
    }
}

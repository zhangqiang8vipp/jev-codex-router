using System.Windows.Threading;
using JevCodexAutoToggle.Services;

namespace JevCodexAutoToggle;

internal sealed class OverlayController : IDisposable
{
    private readonly Dispatcher _dispatcher;
    private readonly OverlayWindow _overlay = new();
    private readonly CodexUiTracker _tracker = new();
    private readonly RouterControlClient _router = new();
    private readonly DispatcherTimer _uiTimer;
    private readonly DispatcherTimer _statusTimer;
    private readonly CancellationTokenSource _cts = new();

    private AutoSnapshot _snapshot = new(false, false, "Connecting to Jev Router…", null, null);
    private bool _busy;

    public OverlayController(Dispatcher dispatcher)
    {
        _dispatcher = dispatcher;
        _overlay.ToggleRequested += OnToggleRequested;
        _overlay.SetState(_snapshot, busy: false);

        _uiTimer = new DispatcherTimer(
            TimeSpan.FromMilliseconds(400),
            DispatcherPriority.Background,
            (_, _) => RefreshAnchor(),
            dispatcher);

        _statusTimer = new DispatcherTimer(
            TimeSpan.FromSeconds(1),
            DispatcherPriority.Background,
            async (_, _) => await RefreshStatusAsync(),
            dispatcher);
    }

    public void Start()
    {
        _uiTimer.Start();
        _statusTimer.Start();
        _ = RefreshStatusAsync();
    }

    private void RefreshAnchor()
    {
        CodexAnchor? anchor;
        try
        {
            anchor = _tracker.TryFindReasoningAnchor();
        }
        catch
        {
            anchor = null;
        }

        if (anchor is null || !NativeWindowStyles.IsForegroundProcess(anchor.ProcessId))
        {
            _overlay.Hide();
            return;
        }

        _overlay.SetAnchor(anchor);
        if (!_overlay.IsVisible) _overlay.Show();
    }

    private async Task RefreshStatusAsync()
    {
        if (_busy || _cts.IsCancellationRequested) return;
        try
        {
            _snapshot = await _router.GetStatusAsync(_cts.Token);
        }
        catch (OperationCanceledException)
        {
            return;
        }
        catch (Exception ex)
        {
            _snapshot = new AutoSnapshot(false, false, ex.GetType().Name, null, null);
        }

        _overlay.SetState(_snapshot, _busy);
    }

    private async void OnToggleRequested(object? sender, EventArgs e)
    {
        if (_busy || !_snapshot.Available) return;
        _busy = true;
        _overlay.SetState(_snapshot, busy: true);

        try
        {
            _snapshot = await _router.SetAutoAsync(!_snapshot.Auto, _cts.Token);
        }
        catch (OperationCanceledException)
        {
            return;
        }
        catch (Exception ex)
        {
            _snapshot = new AutoSnapshot(
                _snapshot.Auto,
                false,
                $"{ex.GetType().Name}: {ex.Message}",
                _snapshot.RedirectModel,
                _snapshot.Route);
        }
        finally
        {
            _busy = false;
            _overlay.SetState(_snapshot, busy: false);
        }
    }

    public void Dispose()
    {
        _cts.Cancel();
        _uiTimer.Stop();
        _statusTimer.Stop();
        _overlay.Close();
        _router.Dispose();
        _cts.Dispose();
    }
}

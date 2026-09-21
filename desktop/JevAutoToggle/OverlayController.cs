using System.Windows.Threading;
using JevCodexAutoToggle.Services;

namespace JevCodexAutoToggle;

internal sealed class OverlayController : IDisposable
{
    private readonly Dispatcher _dispatcher;
    private readonly OverlayWindow _overlay = new();
    private readonly CodexUiTracker _tracker = new();
    private readonly RouterControlClient _router = new();
    private readonly OverlayDiagnostics _diagnostics = new();
    private readonly DispatcherTimer _uiTimer;
    private readonly DispatcherTimer _statusTimer;
    private readonly CancellationTokenSource _cts = new();

    private AutoSnapshot _snapshot = new(false, false, "Connecting to Jev Router…", null, null);
    private bool _busy;
    private bool _statusInFlight;
    private bool _anchorVisible;
    private string? _lastAnchorLabel;

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

        if (anchor is null)
        {
            _anchorVisible = false;
            _lastAnchorLabel = null;
            _overlay.Hide();
            _diagnostics.Write(false, null, _snapshot, "reasoning anchor not found in foreground window");
            return;
        }

        _anchorVisible = true;
        _lastAnchorLabel = anchor.Label;
        _overlay.SetAnchor(anchor);
        if (!_overlay.IsVisible) _overlay.Show();
        _diagnostics.Write(true, _lastAnchorLabel, _snapshot);
    }

    private async Task RefreshStatusAsync()
    {
        if (_busy || _statusInFlight || _cts.IsCancellationRequested) return;
        _statusInFlight = true;
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
        finally
        {
            _statusInFlight = false;
        }

        _overlay.SetState(_snapshot, _busy);
        _diagnostics.Write(_anchorVisible, _lastAnchorLabel, _snapshot);
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

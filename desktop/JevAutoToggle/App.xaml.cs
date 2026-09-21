using System.Threading;
using System.Windows;

namespace JevCodexAutoToggle;

public partial class App : Application
{
    private Mutex? _singleInstance;
    private bool _ownsMutex;
    private EventWaitHandle? _shutdownEvent;
    private RegisteredWaitHandle? _shutdownRegistration;
    private OverlayController? _controller;

    private void App_OnStartup(object sender, StartupEventArgs e)
    {
        _singleInstance = new Mutex(true, @"Local\JevCodexAutoToggle", out var createdNew);
        _ownsMutex = createdNew;
        if (!createdNew)
        {
            Shutdown();
            return;
        }

        // Future upgrades ask the running overlay to leave cooperatively before
        // the installer falls back to Stop-Process. This releases WPF satellite
        // assemblies cleanly and avoids locked PresentationCore resource DLLs.
        _shutdownEvent = new EventWaitHandle(
            false,
            EventResetMode.AutoReset,
            @"Local\JevCodexAutoToggle.Shutdown");
        _shutdownRegistration = ThreadPool.RegisterWaitForSingleObject(
            _shutdownEvent,
            (_, _) => Dispatcher.BeginInvoke(new Action(Shutdown)),
            null,
            Timeout.Infinite,
            executeOnlyOnce: false);

        _controller = new OverlayController(Dispatcher);
        _controller.Start();
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _controller?.Dispose();
        _shutdownRegistration?.Unregister(null);
        _shutdownRegistration = null;
        _shutdownEvent?.Dispose();
        _shutdownEvent = null;
        if (_ownsMutex)
        {
            try { _singleInstance?.ReleaseMutex(); } catch (ApplicationException) { }
        }
        _singleInstance?.Dispose();
        base.OnExit(e);
    }
}

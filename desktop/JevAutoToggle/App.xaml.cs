using System.Threading;
using System.Windows;

namespace JevCodexAutoToggle;

public partial class App : Application
{
    private Mutex? _singleInstance;
    private OverlayController? _controller;

    private void App_OnStartup(object sender, StartupEventArgs e)
    {
        _singleInstance = new Mutex(true, @"Local\JevCodexAutoToggle", out var createdNew);
        if (!createdNew)
        {
            Shutdown();
            return;
        }

        _controller = new OverlayController(Dispatcher);
        _controller.Start();
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _controller?.Dispose();
        _singleInstance?.ReleaseMutex();
        _singleInstance?.Dispose();
        base.OnExit(e);
    }
}

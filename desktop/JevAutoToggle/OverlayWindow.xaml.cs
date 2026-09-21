using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using JevCodexAutoToggle.Services;

namespace JevCodexAutoToggle;

public partial class OverlayWindow : Window
{
    private IntPtr _targetWindowHandle;

    public event EventHandler? ToggleRequested;

    public OverlayWindow()
    {
        InitializeComponent();
    }

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        var handle = new WindowInteropHelper(this).Handle;
        var style = NativeWindowStyles.GetWindowLongPtr(handle, NativeWindowStyles.GwlExStyle).ToInt64();
        style |= NativeWindowStyles.WsExNoActivate | NativeWindowStyles.WsExToolWindow;
        _ = NativeWindowStyles.SetWindowLongPtr(
            handle,
            NativeWindowStyles.GwlExStyle,
            new IntPtr(style));
    }

    internal void SetAnchor(CodexAnchor anchor)
    {
        _targetWindowHandle = anchor.WindowHandle;
        var dpi = _targetWindowHandle != IntPtr.Zero
            ? NativeWindowStyles.GetDpiForWindow(_targetWindowHandle)
            : 96u;
        if (dpi == 0) dpi = 96;
        var scale = dpi / 96.0;

        var anchorLeft = anchor.Bounds.Left / scale;
        var anchorTop = anchor.Bounds.Top / scale;
        var anchorHeight = anchor.Bounds.Height / scale;

        Left = anchorLeft - Width - 8;
        Top = anchorTop + (anchorHeight - Height) / 2.0;
    }

    internal void SetState(AutoSnapshot snapshot, bool busy)
    {
        AutoButton.IsEnabled = !busy && snapshot.Available;
        AutoButton.Opacity = busy ? 0.68 : 1.0;

        if (!snapshot.Available || !string.IsNullOrWhiteSpace(snapshot.Error))
        {
            SetResource("Pill", Border.BackgroundProperty, "ErrorBackground");
            SetResource("Pill", Border.BorderBrushProperty, "ErrorBorder");
            SetResource("Label", TextBlock.ForegroundProperty, "ErrorText");
            SetResource("StatusDot", System.Windows.Shapes.Shape.FillProperty, "ErrorText");
            AutoButton.ToolTip = snapshot.Error ?? "Jev Auto control unavailable.";
            return;
        }

        if (snapshot.Auto)
        {
            SetResource("Pill", Border.BackgroundProperty, "OnBackground");
            SetResource("Pill", Border.BorderBrushProperty, "OnBorder");
            SetResource("Label", TextBlock.ForegroundProperty, "OnText");
            SetResource("StatusDot", System.Windows.Shapes.Shape.FillProperty, "OnDot");

            var route = snapshot.Route;
            AutoButton.ToolTip = route is { Model: not null }
                ? $"Auto ON\nLast route: {ShortModel(route.Model)} · {route.Effort ?? "default"}"
                : "Auto ON\nJev decides model + reasoning effort for each call.";
        }
        else
        {
            SetResource("Pill", Border.BackgroundProperty, "OffBackground");
            SetResource("Pill", Border.BorderBrushProperty, "OffBorder");
            SetResource("Label", TextBlock.ForegroundProperty, "OffText");
            SetResource("StatusDot", System.Windows.Shapes.Shape.FillProperty, "OffDot");
            AutoButton.ToolTip = string.IsNullOrWhiteSpace(snapshot.RedirectModel)
                ? "Auto OFF\nCodex native model + reasoning controls are active."
                : $"Auto OFF\nRestored Codex Router redirect: {snapshot.RedirectModel}";
        }
    }

    private static string ShortModel(string model) =>
        model switch
        {
            "gpt-5.6-luna" => "Luna",
            "gpt-5.6-terra" => "Terra",
            "gpt-5.6-sol" => "Sol",
            "gpt-6-astra" => "Astra",
            _ => model
        };

    private void SetResource(string name, DependencyProperty property, string resource)
    {
        AutoButton.ApplyTemplate();
        if (AutoButton.Template.FindName(name, AutoButton) is not DependencyObject target) return;
        target.SetValue(property, FindResource(resource) as Brush);
    }

    private void AutoButton_OnClick(object sender, RoutedEventArgs e) =>
        ToggleRequested?.Invoke(this, EventArgs.Empty);
}

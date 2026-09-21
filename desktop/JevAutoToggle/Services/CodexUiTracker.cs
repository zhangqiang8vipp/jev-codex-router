using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Automation;

namespace JevCodexAutoToggle.Services;

internal sealed record CodexAnchor(IntPtr WindowHandle, int ProcessId, Rect Bounds, string Label);

internal sealed class CodexUiTracker
{
    // These labels identify the native reasoning selector itself, rather than a
    // selected effort value. They are safe to trust on the foreground window
    // without guessing the packaged app's process name.
    private static readonly string[] StrongAnchorNames =
    [
        "选择强度",
        "Select reasoning",
        "Reasoning effort",
        "Choose reasoning"
    ];

    private static readonly string[] ChineseEffortNames =
    [
        "轻度", "中等", "高", "极高", "最高"
    ];

    private static readonly string[] EnglishEffortNames =
    [
        "low", "medium", "high", "extra high", "xhigh", "max", "ultra"
    ];

    public CodexAnchor? TryFindReasoningAnchor()
    {
        var foreground = NativeWindowStyles.GetForegroundWindow();
        if (foreground == IntPtr.Zero)
            return null;

        try
        {
            var window = AutomationElement.FromHandle(foreground);
            if (window is null || window.Current.IsOffscreen)
                return null;

            var windowBounds = window.Current.BoundingRectangle;
            if (windowBounds.IsEmpty || windowBounds.Width < 500 || windowBounds.Height < 350)
                return null;

            // First prefer the selector's own localized name. This survives
            // packaged-process renames and WebView host process changes.
            var strong = FindStrongAnchors(window);
            var best = PickBest(window, foreground, windowBounds, strong, requireCodexEvidence: false);
            if (best is not null)
                return best;

            // Some builds expose only the selected value (e.g. Ultra/High).
            // Keep this fallback conservative by requiring Codex-like window
            // evidence before accepting a generic effort word.
            if (!LooksLikeCodexWindow(window))
                return null;

            var fallback = FindEffortValueControls(window);
            return PickBest(window, foreground, windowBounds, fallback, requireCodexEvidence: true);
        }
        catch (ElementNotAvailableException) { return null; }
        catch (COMException) { return null; }
        catch (InvalidOperationException) { return null; }
    }

    private static AutomationElementCollection FindStrongAnchors(AutomationElement window)
    {
        var conditions = StrongAnchorNames
            .Select(name => (System.Windows.Automation.Condition)
                new PropertyCondition(AutomationElement.NameProperty, name))
            .ToArray();

        return window.FindAll(
            TreeScope.Descendants,
            conditions.Length == 1
                ? conditions[0]
                : new System.Windows.Automation.OrCondition(conditions));
    }

    private static AutomationElementCollection FindEffortValueControls(AutomationElement window)
    {
        return window.FindAll(
            TreeScope.Descendants,
            new System.Windows.Automation.OrCondition(
                new PropertyCondition(
                    AutomationElement.ControlTypeProperty,
                    ControlType.Button),
                new PropertyCondition(
                    AutomationElement.ControlTypeProperty,
                    ControlType.ComboBox),
                new PropertyCondition(
                    AutomationElement.ControlTypeProperty,
                    ControlType.Custom)));
    }

    private static CodexAnchor? PickBest(
        AutomationElement window,
        IntPtr foreground,
        Rect windowBounds,
        AutomationElementCollection candidates,
        bool requireCodexEvidence)
    {
        CodexAnchor? best = null;
        double bestScore = double.MinValue;

        foreach (AutomationElement candidate in candidates)
        {
            try
            {
                if (candidate.Current.IsOffscreen || !candidate.Current.IsEnabled)
                    continue;

                var label = (candidate.Current.Name ?? string.Empty).Trim();
                if (string.IsNullOrWhiteSpace(label))
                    continue;

                if (requireCodexEvidence && !LooksLikeEffortValue(label))
                    continue;

                var bounds = candidate.Current.BoundingRectangle;
                if (bounds.IsEmpty || bounds.Width < 36 || bounds.Height < 18)
                    continue;

                // The composer controls sit in the lower-right region of the
                // foreground Codex window. This rejects menus/toolbars elsewhere.
                var lower = bounds.Top >= windowBounds.Top + windowBounds.Height * 0.55;
                var right = bounds.Left >= windowBounds.Left + windowBounds.Width * 0.40;
                if (!lower || !right)
                    continue;

                var bottomProximity = 1.0 - Math.Min(
                    1.0,
                    Math.Abs(windowBounds.Bottom - bounds.Bottom) / Math.Max(1.0, windowBounds.Height));
                var rightness = (bounds.Left - windowBounds.Left) / Math.Max(1.0, windowBounds.Width);
                var strongBonus = StrongAnchorNames.Any(
                    name => label.Equals(name, StringComparison.OrdinalIgnoreCase)) ? 3.0 : 0.0;
                var score = strongBonus + bottomProximity + rightness;

                if (score <= bestScore)
                    continue;

                bestScore = score;
                best = new CodexAnchor(
                    foreground,
                    window.Current.ProcessId,
                    bounds,
                    label);
            }
            catch (ElementNotAvailableException) { }
            catch (COMException) { }
        }

        return best;
    }

    private static bool LooksLikeCodexWindow(AutomationElement window)
    {
        string title;
        try
        {
            title = (window.Current.Name ?? string.Empty).Trim();
        }
        catch
        {
            return false;
        }

        if (title.Contains("Codex", StringComparison.OrdinalIgnoreCase))
            return true;

        // Current Chinese builds expose distinctive composer labels even when
        // the top-level packaged window title/process name is generic.
        foreach (var evidence in new[] { "帮我批准", "随心输入" })
        {
            try
            {
                var node = window.FindFirst(
                    TreeScope.Descendants,
                    new PropertyCondition(AutomationElement.NameProperty, evidence));
                if (node is not null)
                    return true;
            }
            catch (ElementNotAvailableException) { }
            catch (COMException) { }
        }

        return false;
    }

    private static bool LooksLikeEffortValue(string label)
    {
        var normalized = label.Trim().ToLowerInvariant();
        return ChineseEffortNames.Any(
                   name => normalized.Equals(name, StringComparison.OrdinalIgnoreCase))
               || EnglishEffortNames.Any(
                   name => normalized.Equals(name, StringComparison.OrdinalIgnoreCase));
    }
}

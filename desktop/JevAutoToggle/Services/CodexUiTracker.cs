using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Automation;

namespace JevCodexAutoToggle.Services;

internal sealed record CodexAnchor(IntPtr WindowHandle, int ProcessId, Rect Bounds, string Label);

internal sealed class CodexUiTracker
{
    private static readonly TimeSpan ProcessCacheLifetime = TimeSpan.FromSeconds(2);
    private HashSet<int> _cachedProcessIds = new();
    private DateTimeOffset _processCacheAt = DateTimeOffset.MinValue;

    private static readonly string[] ChineseEffortNames =
    [
        "选择强度", "轻度", "中等", "高", "极高", "最高"
    ];

    private static readonly string[] EnglishEffortNames =
    [
        "select reasoning", "reasoning effort", "low", "medium", "high",
        "extra high", "xhigh", "max", "ultra"
    ];

    public CodexAnchor? TryFindReasoningAnchor()
    {
        var processIds = FindCodexProcessIds();
        if (processIds.Count == 0) return null;

        try
        {
            var desktop = AutomationElement.RootElement;
            var windows = desktop.FindAll(
                TreeScope.Children,
                System.Windows.Automation.Condition.TrueCondition);

            CodexAnchor? best = null;
            double bestScore = double.MinValue;

            foreach (AutomationElement window in windows)
            {
                try
                {
                    if (!processIds.Contains(window.Current.ProcessId) || window.Current.IsOffscreen)
                        continue;

                    var windowBounds = window.Current.BoundingRectangle;
                    if (windowBounds.IsEmpty || windowBounds.Width < 500 || windowBounds.Height < 350)
                        continue;

                    var controls = window.FindAll(
                        TreeScope.Descendants,
                        new System.Windows.Automation.OrCondition(
                            new PropertyCondition(
                                AutomationElement.ControlTypeProperty,
                                ControlType.Button),
                            new PropertyCondition(
                                AutomationElement.ControlTypeProperty,
                                ControlType.ComboBox)));

                    foreach (AutomationElement button in controls)
                    {
                        try
                        {
                            if (button.Current.IsOffscreen || !button.Current.IsEnabled) continue;
                            var label = (button.Current.Name ?? string.Empty).Trim();
                            if (!LooksLikeReasoningControl(label)) continue;

                            var bounds = button.Current.BoundingRectangle;
                            if (bounds.IsEmpty || bounds.Width < 36 || bounds.Height < 18) continue;

                            // The composer controls live low and toward the right.  This
                            // intentionally prefers geometry over any one localized label.
                            var lower = bounds.Top >= windowBounds.Top + windowBounds.Height * 0.55;
                            var right = bounds.Left >= windowBounds.Left + windowBounds.Width * 0.40;
                            if (!lower || !right) continue;

                            var bottomProximity = 1.0 - Math.Min(
                                1.0,
                                Math.Abs(windowBounds.Bottom - bounds.Bottom) / Math.Max(1.0, windowBounds.Height));
                            var rightness = (bounds.Left - windowBounds.Left) / Math.Max(1.0, windowBounds.Width);
                            var exactBonus = IsExactEffortLabel(label) ? 2.0 : 0.0;
                            var score = exactBonus + bottomProximity + rightness;

                            if (score <= bestScore) continue;
                            bestScore = score;
                            best = new CodexAnchor(
                                window.Current.NativeWindowHandle == 0
                                    ? IntPtr.Zero
                                    : new IntPtr(window.Current.NativeWindowHandle),
                                window.Current.ProcessId,
                                bounds,
                                label);
                        }
                        catch (ElementNotAvailableException) { }
                        catch (COMException) { }
                    }
                }
                catch (ElementNotAvailableException) { }
                catch (COMException) { }
            }

            return best;
        }
        catch (ElementNotAvailableException) { return null; }
        catch (COMException) { return null; }
    }

    private HashSet<int> FindCodexProcessIds()
    {
        if (DateTimeOffset.UtcNow - _processCacheAt < ProcessCacheLifetime)
            return _cachedProcessIds;

        var ids = new HashSet<int>();
        foreach (var processName in new[] { "Codex", "ChatGPT" })
        {
            foreach (var process in Process.GetProcessesByName(processName))
            {
                using (process)
                {
                    try
                    {
                        if (processName.Equals("ChatGPT", StringComparison.OrdinalIgnoreCase))
                        {
                            string? path = null;
                            try { path = process.MainModule?.FileName; } catch { }
                            if (!string.IsNullOrWhiteSpace(path)
                                && !path.Contains("OpenAI.Codex", StringComparison.OrdinalIgnoreCase))
                            {
                                continue;
                            }
                        }

                        ids.Add(process.Id);
                    }
                    catch (InvalidOperationException) { }
                }
            }
        }
        _cachedProcessIds = ids;
        _processCacheAt = DateTimeOffset.UtcNow;
        return ids;
    }

    private static bool LooksLikeReasoningControl(string label)
    {
        if (string.IsNullOrWhiteSpace(label)) return false;
        var normalized = label.Trim().ToLowerInvariant();
        if (ChineseEffortNames.Any(name => normalized.Contains(name, StringComparison.OrdinalIgnoreCase)))
            return true;
        return EnglishEffortNames.Any(name => normalized.Contains(name, StringComparison.OrdinalIgnoreCase));
    }

    private static bool IsExactEffortLabel(string label)
    {
        var normalized = label.Trim().ToLowerInvariant();
        return ChineseEffortNames.Any(name => normalized.Equals(name, StringComparison.OrdinalIgnoreCase))
               || EnglishEffortNames.Any(name => normalized.Equals(name, StringComparison.OrdinalIgnoreCase));
    }
}

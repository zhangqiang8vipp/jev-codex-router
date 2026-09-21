using System.IO;
using System.Text.Json;
using JevCodexAutoToggle.Services;

namespace JevCodexAutoToggle;

internal sealed class OverlayDiagnostics
{
    private readonly string _path;
    private string _lastSignature = "";
    private DateTimeOffset _lastWrite = DateTimeOffset.MinValue;

    public OverlayDiagnostics()
    {
        var stateDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            ".codex",
            "codex-router");
        Directory.CreateDirectory(stateDir);
        _path = Path.Combine(stateDir, "jev-auto-toggle.status.json");
    }

    public void Write(bool visible, string? anchorLabel, AutoSnapshot snapshot, string? note = null)
    {
        var signature = string.Join(
            "|",
            visible,
            anchorLabel ?? "",
            snapshot.Auto,
            snapshot.Available,
            snapshot.Error ?? "",
            snapshot.RedirectModel ?? "",
            snapshot.Route?.Model ?? "",
            snapshot.Route?.Effort ?? "",
            note ?? "");

        var now = DateTimeOffset.Now;
        if (signature == _lastSignature && now - _lastWrite < TimeSpan.FromSeconds(5))
            return;

        var payload = new
        {
            at = now.ToString("O"),
            visible,
            anchor_label = anchorLabel,
            auto = snapshot.Auto,
            available = snapshot.Available,
            error = snapshot.Error,
            redirect_model = snapshot.RedirectModel,
            route = snapshot.Route,
            note
        };

        try
        {
            var temp = _path + ".tmp";
            File.WriteAllText(
                temp,
                JsonSerializer.Serialize(payload, new JsonSerializerOptions { WriteIndented = true }));
            File.Move(temp, _path, overwrite: true);
            _lastSignature = signature;
            _lastWrite = now;
        }
        catch
        {
            // Diagnostics must never affect the overlay.
        }
    }
}

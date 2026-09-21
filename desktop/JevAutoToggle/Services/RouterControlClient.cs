using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;

namespace JevCodexAutoToggle.Services;

internal sealed record RouteSnapshot(string? Model, string? Effort, string? Gate, string? At);
internal sealed record AutoSnapshot(bool Auto, bool Available, string? Error, RouteSnapshot? Route);

internal sealed class RouterControlClient : IDisposable
{
    private readonly HttpClient _http = new()
    {
        BaseAddress = new Uri("http://127.0.0.1:4319/"),
        Timeout = TimeSpan.FromSeconds(3)
    };

    public async Task<AutoSnapshot> GetStatusAsync(CancellationToken cancellationToken)
    {
        using var response = await _http.GetAsync("control/status", cancellationToken);
        var raw = await response.Content.ReadAsStringAsync(cancellationToken);
        return Parse(raw, response.IsSuccessStatusCode);
    }

    public async Task<AutoSnapshot> SetAutoAsync(bool enabled, CancellationToken cancellationToken)
    {
        using var response = await _http.PostAsJsonAsync(
            "control/auto",
            new { enabled },
            cancellationToken);
        var raw = await response.Content.ReadAsStringAsync(cancellationToken);
        return Parse(raw, response.IsSuccessStatusCode);
    }

    private static AutoSnapshot Parse(string raw, bool success)
    {
        try
        {
            using var document = JsonDocument.Parse(raw);
            var root = document.RootElement;
            var auto = root.TryGetProperty("auto", out var autoNode) && autoNode.ValueKind == JsonValueKind.True;
            var available = root.TryGetProperty("available", out var availableNode)
                            && availableNode.ValueKind == JsonValueKind.True;
            string? error = null;
            if (root.TryGetProperty("error", out var errorNode) && errorNode.ValueKind == JsonValueKind.String)
                error = errorNode.GetString();

            RouteSnapshot? route = null;
            if (root.TryGetProperty("route", out var routeNode) && routeNode.ValueKind == JsonValueKind.Object)
            {
                route = new RouteSnapshot(
                    StringProperty(routeNode, "model"),
                    StringProperty(routeNode, "effort"),
                    StringProperty(routeNode, "gate"),
                    StringProperty(routeNode, "at"));
            }

            if (!success && string.IsNullOrWhiteSpace(error))
                error = "Auto control request failed.";

            return new AutoSnapshot(auto, available, error, route);
        }
        catch (JsonException)
        {
            return new AutoSnapshot(false, false, "Invalid response from Jev Router.", null);
        }
    }

    private static string? StringProperty(JsonElement element, string name)
    {
        if (!element.TryGetProperty(name, out var node) || node.ValueKind != JsonValueKind.String)
            return null;
        return node.GetString();
    }

    public void Dispose() => _http.Dispose();
}

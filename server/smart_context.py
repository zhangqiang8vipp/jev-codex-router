        return None
    return os.path.realpath(os.path.expanduser(candidate))


def is_short_followup(task: str) -> bool:
    """A shape signal only; this does not choose a model by itself."""
    compact = " ".join((task or "").strip().split())
    return bool(compact) and len(compact) <= SHORT_FOLLOWUP_CHARS and len(compact.split()) <= 10


def _stable_explicit_id(
    payload: Dict[str, Any],
    headers: Optional[Any] = None,
) -> Optional[str]:
    if headers is not None:
        try:
            header_map = {str(k).lower(): v for k, v in headers.items()}
        except (AttributeError, TypeError, ValueError):
            header_map = {}
        for key in ("thread-id", "session-id", "session_id", "x-codex-parent-thread-id"):
            value = header_map.get(key)
            if isinstance(value, str) and value:
                return f"header.{key}:{value}"

    for key in ("conversation_id", "thread_id", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return f"{key}:{value}"
    conversation = payload.get("conversation")
    if isinstance(conversation, str) and conversation:
        return f"conversation:{conversation}"
    if isinstance(conversation, dict):
        value = conversation.get("id")
        if isinstance(value, str) and value:
            return f"conversation:{value}"
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("conversation_id", "thread_id", "session_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return f"metadata.{key}:{value}"
    return None


def session_key(
    payload: Dict[str, Any],
    cwd: Optional[str] = None,
    headers: Optional[Any] = None,
) -> Optional[str]:
    """Best-effort stable thread key, always returned as a one-way hash.

    Prefer explicit conversation/session identifiers, including the protected
    caller headers Codex Router preserves. Raw identifiers are never persisted.
    """
    basis = _stable_explicit_id(payload, headers)
    inp = payload.get("input")
    if basis is None and isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            value = item.get("id")
            if isinstance(value, str) and value:
                basis = f"item:{value}"
                break
    if basis is None:
        texts = _user_texts(payload)
        goal = None
        for text in texts[:2]:
            match = GOAL_RX.search(text)
            if match:
                goal = " ".join(match.group(1).split())[:1000]
                break
        seed = goal or (" ".join(texts[0].split())[:1000] if texts else "")
        if not seed:
            return None
        basis = f"fallback:{cwd or ''}:{seed}"
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()


class SessionStore:
    """Small JSON store for bounded routing observations."""

    def __init__(self, path: str, ttl_s: int = SESSION_TTL_S, max_entries: int = SESSION_MAX_ENTRIES):
        self.path = os.path.expanduser(path)
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._lock = threading.RLock()

    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                value = json.load(fh)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _prune(self, data: Dict[str, Dict[str, Any]], now: float) -> Dict[str, Dict[str, Any]]:
        live = {
            key: value
            for key, value in data.items()
            if isinstance(value, dict) and now - float(value.get("seen_at") or 0) <= self.ttl_s
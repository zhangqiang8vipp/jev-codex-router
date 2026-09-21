"""Shared Jev decision contract: one model/effort choice plus bounded context."""
import math

POLICY_VERSION = "joint-v3-route-lease"
LUNA = "gpt-5.6-luna"
TERRA = "gpt-5.6-terra"
SOL = "gpt-5.6-sol"
ASTRA = "gpt-6-astra"
TIERS = (LUNA, TERRA, SOL, ASTRA)
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# Capability descriptions are priors, not benchmark-derived success rates.
# No task labels, keywords, target model shares, or confidence cutoffs select a route.
MODEL_PROFILES = {
    LUNA: "Cost-optimized GPT-5.6 model for clear, high-volume and mechanical work.",
    TERRA: "Balanced GPT-5.6 model for everyday production coding and judgment.",
    SOL: "Higher-capacity GPT-5.6 model for complex professional and cross-cutting work.",
    ASTRA: "Most capable model, intended for the hardest end-to-end reasoning work.",
}
DEPTH_PROFILES = {
    "low": "A small reasoning budget.",
    "medium": "A moderate reasoning budget.",
    "high": "A substantial reasoning budget.",
    "xhigh": "An extended reasoning budget.",
    "max": "The largest supported reasoning budget.",
}
ROUTE_PAIRS = {f"{model}:{depth}": (model, depth)
               for model in TIERS for depth in EFFORTS}
QUESTIONS = {
    "route": {
        "type": "choice",
        "instructions": {
            "question": "Which model AND reasoning effort should be leased for the current execution phase?",
            "objective": (
                "Select the cheapest model/effort pair that is sufficiently capable for the "
                "current user turn or execution phase, not merely the next trivial tool call. "
                "The selected pair may stay leased across tool continuations until a real "
                "boundary or repeated failure, so include the likely reasoning needed to carry "
                "this phase forward correctly. Consider corrections, retries, and the cost of "
                "a wrong answer. Judge capability and effort jointly: more effort on a smaller "
                "model is not automatically equivalent to a stronger model."
            ),
            "evidence": (
                "Use the current request, recent assistant intent, available tool evidence, "
                "and bounded session/repository observations to determine what remains to be "
                "decided. Previous route, project identity, diff size, and failure streak are "
                "evidence, not difficulty labels. A tool result does not by itself make the "
                "next decision easy or difficult. Text length, an error keyword, repository "
                "size, and the general subject are not difficulty measurements."
            ),
            "continuity": (
                "For a short continuation such as continue/继续, interpret it in light of the "
                "previous assistant intent and session route instead of treating the short text "
                "as a new trivial task. Normal tool call/result loops are continuity-constrained "
                "and usually reuse an existing route without asking this question again. When "
                "this question is asked to recover a missing lease, choose a pair suitable for "
                "the remaining execution phase rather than only the immediate tool result."
            ),
            "neutrality": (
                "There is no target model distribution. Do not prefer Luna merely because it "
                "is cheap, Terra or Sol as a compromise when uncertain, or Astra merely because "
                "it is strongest. Prefer lower resource use only among pairs you judge adequate."
            ),
            "model_profiles": MODEL_PROFILES,
            "effort_profiles": DEPTH_PROFILES,
            "speed": "Every option uses standard speed. Fast mode is unavailable.",
        },
        "criteria": {key: {"model": model, "reasoning_effort": depth}
                     for key, (model, depth) in ROUTE_PAIRS.items()},
    },
}


def route(tier, depth, conf=None, step=None):
    """Apply a valid Jev pair verbatim; guardrails live outside this pure contract."""
    if tier not in TIERS or depth not in EFFORTS:
        raise ValueError("invalid model/effort pair")
    return tier, depth, "default", "apply"


def decision_from_answers(answers):
    """Validate the typed Jev answer without treating confidence as correctness."""
    answer = answers.get("route") if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        raise ValueError("missing joint route decision")
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in ROUTE_PAIRS:
        raise ValueError("unknown joint route choice")
    probabilities = answer.get("probabilities")
    if probabilities is not None:
        if not isinstance(probabilities, dict) or set(probabilities) != set(ROUTE_PAIRS):
            raise ValueError("incomplete route distribution")
        values = list(probabilities.values())
        if any(isinstance(p, bool) or not isinstance(p, (int, float))
               or not math.isfinite(p) or not 0 <= p <= 1 for p in values):
            raise ValueError("invalid route probabilities")
        if abs(sum(values) - 1) > 0.02 or probabilities[choice] < max(values) - 1e-6:
            raise ValueError("inconsistent route distribution")
    conf = answer.get("confidence")
    if (isinstance(conf, bool) or not isinstance(conf, (int, float))
            or not math.isfinite(conf) or not 0 <= conf <= 1):
        conf = None
    model, effort = ROUTE_PAIRS[choice]
    return {
        "model": model, "effort": effort, "speed": "default", "gate": "apply",
        "confidence": conf, "probabilities": probabilities,
        "chosen_probability": probabilities.get(choice) if probabilities else None,
        "policy_version": POLICY_VERSION,
    }
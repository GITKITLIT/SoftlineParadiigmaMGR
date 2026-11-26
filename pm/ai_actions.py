"""Optional Qwen-selected analysis lenses. All evidence is pre-collected locally."""

ACTIONS = {
    "compare_control": ("orders",),
    "inspect_funnel": ("funnel", "metrika"),
    "review_backlog": ("release", "backlog"),
    "review_research": ("interview", "research", "competitor_local", "competitor_web"),
}


def prioritize(evidence, action):
    if action == "finish":
        return evidence
    kinds = ACTIONS[action]
    matches = [item for item in evidence if item["kind"] in kinds]
    return matches + [item for item in evidence if item["kind"] not in kinds]

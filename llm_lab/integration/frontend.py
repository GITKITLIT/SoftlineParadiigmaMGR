"""Compose the script hashed by Deployment and served by AI Core."""


def with_chat(script, root):
    component = (root / "llm_lab/integration/chat.js").read_text(encoding="utf-8")
    return script + "\n;\n" + component

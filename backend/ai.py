"""
ai.py - one small helper that asks the local AI model (Ollama) for a short explanation.

The AI is only ever used to WRITE WORDS. It never makes a decision. If anything goes
wrong (Ollama is off, the model is missing, a slow reply...) the caller's template
sentence is returned instead, so nothing that uses this helper can fail because of the AI.
"""

import os


def short_explanation(prompt: str, template: str) -> str:
    """Ask the AI to write a short explanation from `prompt`. Returns `template` if the AI fails."""
    try:
        # Imported here so that even a broken AI library cannot stop the program.
        from langchain_ollama import ChatOllama

        model = os.getenv("OLLAMA_MODEL")   # the model name comes ONLY from .env
        if os.getenv("LLM_PROVIDER", "ollama") != "ollama" or not model:
            return template

        llm = ChatOllama(model=model, temperature=0, client_kwargs={"timeout": 60})
        reply = str(llm.invoke(prompt).content)
        # Models often add an intro like "Here are two sentences:". Keep only the real sentences.
        lines = [line.strip() for line in reply.splitlines() if line.strip() and not line.strip().endswith(":")]
        reply = " ".join(lines)
        if reply:
            return reply
    except Exception:
        pass  # any AI problem -> use the template below
    return template

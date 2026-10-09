"""Gemini chat models, one setting per LLM node (agent spec §0)."""
import warnings

from langchain_google_genai import ChatGoogleGenerativeAI

from ..config import get_settings

TEMPERATURE = {"researcher": 0.0, "summarize": 0.2, "analyst": 0.3, "extract": 0.0}
# Some Gemini models use fixed sampling and warn on every call that temperature is ignored; that's expected here.
warnings.filterwarnings("ignore", message=r"Model '.*' uses fixed sampling defaults", category=UserWarning)


def chat_model(node: str):
    """None when no API key is configured: callers fall back to the fixed plan and template text."""
    s = get_settings()
    if not s.gemini_api_key:
        return None
    model = getattr(s, f"gemini_model_{node}") or s.gemini_model
    return ChatGoogleGenerativeAI(model=model, google_api_key=s.gemini_api_key, temperature=TEMPERATURE[node],
                                  max_retries=2, timeout=60)


def model_name(llm) -> str | None:
    return getattr(llm, "model", None) if llm is not None else None

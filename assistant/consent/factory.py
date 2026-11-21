"""Build the consent classifier and its log from settings.

Mirrors ``assistant.llm.factory``: the choice of implementation is made once,
from configuration, and nothing downstream has to know which one it got.

In rule mode this returns nothing at all. That is the point of the default: no
client is constructed, no key is required and no log file is created, so the
assistant runs exactly as it did before this package existed.
"""

from __future__ import annotations

from ..config import Settings
from .gate import ConsentMode
from .log import ConsentLog
from .model import ConsentModel, OpenAiConsentModel
from .stub import StubConsentModel

CONSENT_DB_SUFFIX = "-consent"


def consent_log_path_for(settings: Settings) -> str:
    """Beside the state database, like the review audit log is."""
    return f"{settings.state_db_path}{CONSENT_DB_SUFFIX}"


def build_consent_model(settings: Settings) -> ConsentModel | None:
    """Return the classifier for the configured mode, or None in rule mode.

    The provider follows ``ASSISTANT_LLM``, so a deployment already running the
    deterministic stand in does not suddenly start calling a real model because
    consent was switched on.
    """
    if ConsentMode(settings.consent_mode) is ConsentMode.RULE:
        return None
    if settings.llm_provider == "fake":
        return StubConsentModel()
    return OpenAiConsentModel(
        api_key=settings.require_openai_api_key(),
        model=settings.model,
        timeout_seconds=settings.consent_timeout_seconds,
    )


def build_consent_log(settings: Settings) -> ConsentLog | None:
    """Return the decision log for the configured mode, or None in rule mode."""
    if ConsentMode(settings.consent_mode) is ConsentMode.RULE:
        return None
    return ConsentLog(consent_log_path_for(settings))

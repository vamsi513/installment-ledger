"""Typed consent classification for replies to a payment proposal.

The package is self contained: the schema, the model interface, the
deterministic stand in, the append only decision log, and the gate that holds
the threshold, the timeout, the fallback and the rollout mode.

Nothing here can reach the ledger. It decides what a reply meant; the graph and
the MCP tools decide what happens next, and the review queue still sits between
any decision made here and a payment.
"""

from .factory import (
    CONSENT_DB_SUFFIX,
    build_consent_log,
    build_consent_model,
    consent_log_path_for,
)
from .gate import (
    ConsentMode,
    ConsentOutcome,
    Proposal,
    classify_reply,
    decide_consent,
)
from .log import ConsentAction, ConsentFailure, ConsentLog, ConsentRecord, DecidedBy
from .model import (
    SYSTEM_PROMPT,
    ConsentModel,
    ConsentModelError,
    OpenAiConsentModel,
    build_user_prompt,
)
from .schema import ConsentClassification, ConsentLabel, unclear
from .stub import ScriptedConsentModel, StubConsentModel

__all__ = [
    "CONSENT_DB_SUFFIX",
    "SYSTEM_PROMPT",
    "ConsentAction",
    "ConsentClassification",
    "ConsentFailure",
    "ConsentLabel",
    "ConsentLog",
    "ConsentMode",
    "ConsentModel",
    "ConsentModelError",
    "ConsentOutcome",
    "ConsentRecord",
    "DecidedBy",
    "OpenAiConsentModel",
    "Proposal",
    "ScriptedConsentModel",
    "StubConsentModel",
    "build_consent_log",
    "build_consent_model",
    "build_user_prompt",
    "classify_reply",
    "consent_log_path_for",
    "decide_consent",
    "unclear",
]

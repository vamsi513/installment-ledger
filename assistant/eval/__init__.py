"""The evaluation harness.

Scenarios are hand written in ``scenarios.json``. Scoring is rule based only:
every check is a comparison against the transcript or against the ledger's own
state afterwards. Nothing here asks a model whether an answer was good, and
passing these rules is not evidence that the assistant is correct in general.
See ``rules.py`` for exactly what each rule does and does not test.
"""

from .rules import RULE_NAMES, ScenarioScore, score_scenario

__all__ = ["RULE_NAMES", "ScenarioScore", "score_scenario"]

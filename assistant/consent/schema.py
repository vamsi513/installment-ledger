"""What the model is allowed to say about a reply to a payment proposal.

The schema is the contract. A model answer that does not fit it is not argued
with, not repaired and not partially believed: it becomes ``unclear``, which
makes the agent ask again. That is the whole reason the schema is strict.

``modified_amount_cents`` is a ``StrictInt`` on purpose. Pydantic's ordinary
coercion would accept ``1234.0`` and ``True`` for an integer field, and money in
this codebase is integer cents everywhere or it is nothing.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class ConsentLabel(StrEnum):
    """The four things a reply to a proposal can be."""

    #: The user agreed to the proposal as it stands.
    CONFIRM = "confirm"
    #: The user does not want the payment made.
    DECLINE = "decline"
    #: The user wants a different amount, so the proposal has to be made again.
    MODIFY = "modify"
    #: Anything else, including silence, a question, or a reply that could be read
    #: two ways. Never treated as agreement.
    UNCLEAR = "unclear"


class ConsentClassification(BaseModel):
    """One judgement about one reply.

    ``extra="forbid"`` matters: a model that invents a field is a model that did
    not answer the question asked, and the safe reading of that is ``unclear``.
    """

    model_config = ConfigDict(extra="forbid")

    label: ConsentLabel
    confidence: float = Field(ge=0.0, le=1.0)
    #: Set only when ``label`` is ``modify``. Integer cents, strictly.
    modified_amount_cents: StrictInt | None = None

    @model_validator(mode="after")
    def _modify_carries_an_amount(self) -> ConsentClassification:
        """A modify without a usable amount is not a modify.

        Rejecting it here is what turns it into ``unclear`` at the call site,
        rather than leaving the agent holding a label it cannot act on. An
        amount on any other label is also refused, because it would be a value
        nothing reads and a reader would reasonably assume it was used.
        """
        if self.label is ConsentLabel.MODIFY:
            if self.modified_amount_cents is None:
                raise ValueError("label 'modify' requires modified_amount_cents")
            if self.modified_amount_cents <= 0:
                raise ValueError("modified_amount_cents must be a positive number of cents")
        elif self.modified_amount_cents is not None:
            raise ValueError(f"label {self.label.value!r} must not carry modified_amount_cents")
        return self


def unclear(confidence: float = 0.0) -> ConsentClassification:
    """The value every failure path collapses to."""
    return ConsentClassification(label=ConsentLabel.UNCLEAR, confidence=confidence)

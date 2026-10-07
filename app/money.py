"""Money handling rules.

Every stored and transported amount in this service is an integer number of
cents. ``Decimal`` appears in exactly one place, interest computation, and the
only rounding rule is the one below.
"""

from decimal import ROUND_HALF_UP, Decimal

ONE_CENT = Decimal(1)


def round_cents(amount: Decimal) -> int:
    """Round a Decimal count of cents to a whole cent, half away from zero.

    This is the single rounding rule for the service. It is applied once, to a
    total, never to each installment, so rounding cannot accumulate.
    """
    return int(amount.quantize(ONE_CENT, rounding=ROUND_HALF_UP))

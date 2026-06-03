"""The risk gate: the one place every order — manual or auto — must pass.

The gate is a pure `state-in -> decision-out` function plus a file-backed kill switch,
so it is fully testable offline and cannot be bypassed by the UI. Fail-closed is the
rule: any missing or ambiguous input is rejected rather than traded on.
"""
from trader.risk.gate import (
    AccountState,
    KillSwitch,
    OrderIntent,
    RiskDecision,
    RiskGate,
)

__all__ = [
    "AccountState",
    "KillSwitch",
    "OrderIntent",
    "RiskDecision",
    "RiskGate",
]

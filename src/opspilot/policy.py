from __future__ import annotations

from .models import RemediationAction, RiskLevel


class PolicyDecision:
    def __init__(self, allowed: bool, reason: str) -> None:
        self.allowed = allowed
        self.reason = reason


class SafetyPolicy:
    """Central approval policy kept separate from model reasoning."""

    def evaluate(self, action: RemediationAction) -> PolicyDecision:
        if action.risk in {RiskLevel.MEDIUM, RiskLevel.HIGH} and not action.approved:
            return PolicyDecision(False, "Human approval is required for mutating medium/high-risk actions.")
        if not action.command_preview.startswith("opspilot "):
            return PolicyDecision(False, "The command is outside the sandbox allow-list.")
        return PolicyDecision(True, "Action satisfies the approval and sandbox policies.")


from __future__ import annotations

from app.models import NormalizedMessage, ReplyDecision
from app.routing.permission_service import PermissionService


class ReplyRouter:
    def __init__(self, permission_service: PermissionService) -> None:
        self._permission_service = permission_service

    def decide(self, message: NormalizedMessage) -> ReplyDecision:
        if message.scope_type == "private":
            if self._permission_service.is_private_user_allowed(message.user_id):
                return ReplyDecision("reply", "private_allowed", True, True)
            return ReplyDecision("silence", "private_not_allowed", False, False)

        if message.scope_type == "group":
            if message.group_id is None or not self._permission_service.is_group_allowed(message.group_id):
                return ReplyDecision("silence", "group_not_allowed", False, False)
            if not message.is_at_self:
                return ReplyDecision("silence", "group_not_triggered", False, False)
            return ReplyDecision("reply", "group_mention", True, True)

        return ReplyDecision("silence", "unsupported_scope", False, False)

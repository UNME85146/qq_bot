from __future__ import annotations

from app.models import QQConfig


class PermissionService:
    def __init__(self, config: QQConfig) -> None:
        self._config = config

    def is_root_user(self, user_id: str) -> bool:
        return str(user_id) in self._config.root_user_ids

    def is_owner_user(self, user_id: str) -> bool:
        user_id = str(user_id)
        return user_id in self._config.root_user_ids or user_id in self._config.owner_user_ids

    def is_private_user_allowed(self, user_id: str) -> bool:
        user_id = str(user_id)
        return user_id in self._config.root_user_ids or user_id in self._config.allowed_private_user_ids

    def is_group_allowed(self, group_id: str) -> bool:
        return str(group_id) in self._config.allowed_group_ids

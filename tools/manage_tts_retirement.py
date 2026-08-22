from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
PACKAGE_PATTERN = re.compile(r"retired-local-tts-\d{8}T\d{6}Z$")
MANIFEST_NAME = "retirement-manifest.json"
DEFAULT_RETENTION_DAYS = 90


class TtsRetirementError(RuntimeError):
    pass


@dataclass(frozen=True)
class TtsRetirementSpec:
    service_name: str
    root: Path
    package_root: Path
    receipt_root: Path
    sources: dict[str, Path]
    backup_names: dict[str, str]
    retention_days: int = DEFAULT_RETENTION_DAYS

    @classmethod
    def production(cls, *, retention_days: int = DEFAULT_RETENTION_DAYS):
        root = Path("/opt/qq_bot")
        return cls(
            service_name="qq-bot-tts.service",
            root=root,
            package_root=root / "runtime_artifacts",
            receipt_root=root / "runtime_artifacts" / "tts-retirement-receipts",
            sources={
                "unit": Path("/etc/systemd/system/qq-bot-tts.service"),
                "adapter": root / "scripts/server/moss_tts_adapter.py",
                "environment": Path("/opt/moss_tts_nano"),
                "cache": root / "data/tts",
            },
            backup_names={
                "unit": "qq-bot-tts.service",
                "adapter": "moss_tts_adapter.py",
                "environment": "moss_tts_nano",
                "cache": "data-tts",
            },
            retention_days=retention_days,
        )


class TtsRetirementManager:
    def __init__(
        self,
        spec: TtsRetirementSpec,
        *,
        now=lambda: datetime.now(UTC),
        systemctl=None,
        runtime_ready=None,
    ) -> None:
        self.spec = spec
        self._now = now
        self._systemctl_override = systemctl
        self._runtime_ready_override = runtime_ready

    def plan(
        self,
        *,
        operation: str = "retire",
        package_name: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        if operation == "retire":
            sources = {
                name: inspect_path(path, include_hash=True)
                for name, path in self.spec.sources.items()
            }
            present = [name for name, item in sources.items() if item["exists"]]
            if not present:
                legacy = self._single_legacy_package()
                if legacy is not None:
                    operation = "adopt_existing_retirement"
                    package_name = legacy.name
            payload = {
                "schema_version": SCHEMA_VERSION,
                "operation": operation,
                "service": self.spec.service_name,
                "service_state": self._service_state(),
                "sources": sources,
                "package": package_name,
                "retention_days": self.spec.retention_days,
            }
            if operation == "adopt_existing_retirement":
                package = self._resolve_package(
                    package_name,
                    managed_required=False,
                )
                payload["legacy_package_items"] = self._package_item_snapshot(package)
        elif operation == "rollback":
            package = self._resolve_package(package_name)
            manifest = self._load_manifest(package)
            payload = {
                "schema_version": SCHEMA_VERSION,
                "operation": "rollback",
                "service": self.spec.service_name,
                "package": package.name,
                "manifest_sha256": file_sha256(package / MANIFEST_NAME),
                "package_items": self._package_item_snapshot(package),
                "destinations_absent": all(
                    not Path(item["source"]).exists() for item in manifest["items"]
                ),
            }
        elif operation == "delete":
            package = self._resolve_package(package_name)
            status = self.package_status(package, verify_hashes=True)
            payload = {
                "schema_version": SCHEMA_VERSION,
                "operation": "delete",
                "package": package.name,
                "manifest_sha256": file_sha256(package / MANIFEST_NAME),
                "approval_id": sanitize_approval_id(approval_id),
                "retention_expired": status["retention_expired"],
                "package_health": status["health"],
                "rehearsal": status["rehearsal"],
                "last_recovery_package": len(self._managed_packages()) == 1,
            }
        else:
            raise TtsRetirementError("unsupported plan operation")
        payload["plan_hash"] = payload_sha256(payload)
        return payload

    def apply(self, *, plan_hash: str) -> dict[str, Any]:
        plan = self.plan(operation="retire")
        require_plan_hash(plan, plan_hash)
        if plan["operation"] == "adopt_existing_retirement":
            return self._adopt_existing(plan)
        if not any(item["exists"] for item in plan["sources"].values()):
            raise TtsRetirementError("historical TTS sources are already absent")
        if not all(item["exists"] for item in plan["sources"].values()):
            raise TtsRetirementError("historical TTS source set is incomplete")
        package = self.spec.package_root / self._package_name()
        self._validate_new_package_path(package)
        previous_service = plan["service_state"]
        moved: list[tuple[Path, Path]] = []
        package.mkdir(parents=True, mode=0o750)
        receipt = ""
        try:
            self._stop_disable_service()
            for name, source in self.spec.sources.items():
                target = package / self.spec.backup_names[name]
                shutil.move(str(source), str(target))
                moved.append((source, target))
            manifest = self._write_manifest(
                package,
                plan_hash=plan_hash,
                previous_service=previous_service,
            )
            self._daemon_reload()
            if any(path.exists() for path in self.spec.sources.values()):
                raise TtsRetirementError("source remained after retirement")
            if tcp_listening("127.0.0.1", 18100):
                raise TtsRetirementError("historical TTS listener remained active")
            if not self._runtime_ready():
                raise TtsRetirementError("main QQ bot runtime readiness failed")
            receipt = self._write_receipt(
                event="tts_retirement_applied",
                payload={
                    "package": package.name,
                    "plan_hash": plan_hash,
                    "manifest_sha256": file_sha256(package / MANIFEST_NAME),
                },
            )
        except BaseException as exc:
            rollback_errors = self._restore_moved(moved, previous_service)
            if not rollback_errors:
                shutil.rmtree(package, ignore_errors=True)
            suffix = "rollback_completed" if not rollback_errors else "rollback_incomplete"
            raise TtsRetirementError(f"retirement failed; {suffix}") from exc
        return {
            "status": "retired",
            "package": package.name,
            "manifest": manifest,
            "receipt": receipt,
        }

    def status(self, *, verify_hashes: bool = False) -> dict[str, Any]:
        service_state = self._service_state()
        source_presence = {
            name: path.exists() for name, path in self.spec.sources.items()
        }
        packages = []
        for package in self._all_packages():
            if (package / MANIFEST_NAME).is_file():
                try:
                    packages.append(
                        self.package_status(package, verify_hashes=verify_hashes)
                    )
                except (TtsRetirementError, KeyError, TypeError, ValueError):
                    packages.append(
                        {
                            "package": package.name,
                            "managed": True,
                            "health": "invalid_manifest",
                            "retention_expired": None,
                            "rehearsal": "unknown",
                        }
                    )
            else:
                packages.append(
                    {
                        "package": package.name,
                        "managed": False,
                        "health": "legacy_unmanaged",
                        "retention_expired": None,
                        "rehearsal": "missing",
                    }
                )
        retired = (
            not any(source_presence.values())
            and service_state["active"] is False
            and service_state["enabled"] is False
            and not tcp_listening("127.0.0.1", 18100)
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "historical_tts": "retired" if retired else "not_retired",
            "service": service_state,
            "listener_18100": tcp_listening("127.0.0.1", 18100),
            "source_presence": source_presence,
            "rollback_packages": packages,
        }

    def package_status(
        self,
        package: Path,
        *,
        verify_hashes: bool,
    ) -> dict[str, Any]:
        manifest = self._load_manifest(package)
        expires_at = parse_timestamp(manifest["expires_at"])
        health = "present"
        for item in manifest["items"]:
            target = package / item["backup_name"]
            if not target.exists() and not target.is_symlink():
                health = "missing"
                break
            if verify_hashes and inspect_path(target, include_hash=True)["sha256"] != item["sha256"]:
                health = "hash_mismatch"
                break
        rehearsal = self._latest_rehearsal(package)
        return {
            "package": package.name,
            "managed": True,
            "health": health,
            "created_at": manifest["created_at"],
            "expires_at": manifest["expires_at"],
            "retention_days": manifest["retention_days"],
            "retention_expired": self._now() >= expires_at,
            "rehearsal": rehearsal,
        }

    def rollback(self, *, package_name: str, plan_hash: str) -> dict[str, Any]:
        plan = self.plan(operation="rollback", package_name=package_name)
        require_plan_hash(plan, plan_hash)
        if not plan["destinations_absent"]:
            raise TtsRetirementError("rollback destination already exists")
        package = self._resolve_package(package_name)
        if self.package_status(package, verify_hashes=True)["health"] != "present":
            raise TtsRetirementError("rollback package health verification failed")
        manifest = self._load_manifest(package)
        restored: list[tuple[Path, Path]] = []
        receipt = ""
        try:
            for item in manifest["items"]:
                source = package / item["backup_name"]
                destination = Path(item["source"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                restored.append((source, destination))
            self._daemon_reload()
            previous = manifest["previous_service"]
            if previous.get("enabled"):
                self._systemctl("enable", self.spec.service_name, check=True)
            if previous.get("active"):
                self._systemctl("start", self.spec.service_name, check=True)
            self._require_service_state(previous)
            if not self._runtime_ready():
                raise TtsRetirementError("main QQ bot runtime readiness failed")
            receipt = self._write_receipt(
                event="tts_retirement_rolled_back",
                payload={"package": package.name, "plan_hash": plan_hash},
            )
        except BaseException as exc:
            errors = []
            for source, destination in reversed(restored):
                try:
                    shutil.move(str(destination), str(source))
                except OSError:
                    errors.append(str(destination))
            try:
                self._stop_disable_service()
                self._daemon_reload()
            except TtsRetirementError:
                errors.append("service_state")
            suffix = "rollback_reverted" if not errors else "rollback_incomplete"
            raise TtsRetirementError(f"TTS rollback failed; {suffix}") from exc
        return {"status": "restored", "package": package.name, "receipt": receipt}

    def rehearse(self, *, package_name: str) -> dict[str, Any]:
        package = self._resolve_package(package_name)
        manifest = self._load_manifest(package)
        staging = package / f".rehearsal-{int(time.time())}-{os.getpid()}"
        if staging.exists():
            raise TtsRetirementError("rehearsal staging path already exists")
        staging.mkdir(mode=0o700)
        try:
            for item in manifest["items"]:
                source = package / item["backup_name"]
                target = staging / item["backup_name"]
                copy_with_hardlinks(source, target)
                if inspect_path(target, include_hash=True)["sha256"] != item["sha256"]:
                    raise TtsRetirementError("rehearsal hash verification failed")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        directory = package / "rehearsals"
        directory.mkdir(mode=0o750, exist_ok=True)
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "result": "passed",
            "completed_at": self._now().isoformat(timespec="seconds"),
            "manifest_sha256": file_sha256(package / MANIFEST_NAME),
            "method": "same-filesystem-hardlink-restore-and-full-hash",
        }
        path = directory / f"rehearsal-{self._timestamp()}.json"
        write_json_atomic(path, receipt, mode=0o640)
        return receipt

    def delete(
        self,
        *,
        package_name: str,
        plan_hash: str,
        approval_id: str,
        confirm_without_rollback: bool,
    ) -> dict[str, Any]:
        plan = self.plan(
            operation="delete",
            package_name=package_name,
            approval_id=approval_id,
        )
        require_plan_hash(plan, plan_hash)
        if not plan["retention_expired"]:
            raise TtsRetirementError("retention period has not expired")
        if plan["package_health"] != "present":
            raise TtsRetirementError("rollback package health verification failed")
        rehearsal = plan["rehearsal"]
        if not isinstance(rehearsal, dict) or rehearsal.get("result") != "passed":
            raise TtsRetirementError("a successful recovery rehearsal is required")
        if plan["last_recovery_package"] and not confirm_without_rollback:
            raise TtsRetirementError("deleting the last rollback package needs explicit confirmation")
        package = self._resolve_package(package_name)
        approval_receipt = self._write_receipt(
            event="tts_retirement_package_delete_approved",
            payload={
                "package": package.name,
                "plan_hash": plan_hash,
                "approval_id": sanitize_approval_id(approval_id),
                "last_recovery_package": plan["last_recovery_package"],
            },
        )
        try:
            shutil.rmtree(package)
        except OSError as exc:
            try:
                self._write_receipt(
                    event="tts_retirement_package_delete_failed",
                    payload={
                        "package": package.name,
                        "plan_hash": plan_hash,
                        "approval_receipt": approval_receipt,
                    },
                )
            except OSError:
                pass
            raise TtsRetirementError("rollback package deletion failed") from exc
        audit_status = "complete"
        try:
            receipt = self._write_receipt(
                event="tts_retirement_package_deleted",
                payload={
                    "package": package.name,
                    "plan_hash": plan_hash,
                    "approval_receipt": approval_receipt,
                },
            )
        except OSError:
            receipt = approval_receipt
            audit_status = "approval_only"
        return {
            "status": "deleted",
            "package": package.name,
            "approval_receipt": approval_receipt,
            "receipt": receipt,
            "audit_status": audit_status,
        }

    def _adopt_existing(self, plan: dict[str, Any]) -> dict[str, Any]:
        package = self._resolve_package(plan["package"], managed_required=False)
        if (package / MANIFEST_NAME).exists():
            raise TtsRetirementError("retirement package is already managed")
        if plan["service_state"].get("active"):
            raise TtsRetirementError("historical TTS service is still active")
        if tcp_listening("127.0.0.1", 18100):
            raise TtsRetirementError("historical TTS listener is still active")
        if not self._runtime_ready():
            raise TtsRetirementError("main QQ bot runtime readiness failed")
        missing = [
            name
            for name, backup_name in self.spec.backup_names.items()
            if not (package / backup_name).exists()
        ]
        if missing:
            raise TtsRetirementError("legacy rollback package is incomplete")
        try:
            manifest = self._write_manifest(
                package,
                plan_hash=plan["plan_hash"],
                previous_service={
                    "active": False,
                    "enabled": False,
                    "installed": True,
                    "state_source": "conservative_legacy_adoption",
                },
            )
            receipt = self._write_receipt(
                event="tts_retirement_package_adopted",
                payload={
                    "package": package.name,
                    "plan_hash": plan["plan_hash"],
                    "manifest_sha256": file_sha256(package / MANIFEST_NAME),
                },
            )
        except (OSError, TtsRetirementError):
            (package / MANIFEST_NAME).unlink(missing_ok=True)
            raise
        return {
            "status": "retired",
            "package": package.name,
            "adopted_existing": True,
            "manifest": manifest,
            "receipt": receipt,
        }

    def _write_manifest(
        self,
        package: Path,
        *,
        plan_hash: str,
        previous_service: dict[str, Any],
    ) -> dict[str, Any]:
        created_at = self._now()
        items = []
        for name, source in self.spec.sources.items():
            target = package / self.spec.backup_names[name]
            inspected = inspect_path(target, include_hash=True)
            if not inspected["exists"]:
                raise TtsRetirementError(f"backup item missing: {name}")
            items.append(
                {
                    "name": name,
                    "source": str(source),
                    "backup_name": self.spec.backup_names[name],
                    "sha256": inspected["sha256"],
                    "bytes": inspected["bytes"],
                    "files": inspected["files"],
                }
            )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": created_at.isoformat(timespec="seconds"),
            "expires_at": (
                created_at + timedelta(days=self.spec.retention_days)
            ).isoformat(timespec="seconds"),
            "retention_days": self.spec.retention_days,
            "plan_hash": plan_hash,
            "previous_service": previous_service,
            "items": items,
        }
        write_json_atomic(package / MANIFEST_NAME, manifest, mode=0o640)
        return manifest

    def _package_item_snapshot(self, package: Path) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                **inspect_path(
                    package / self.spec.backup_names[name],
                    include_hash=True,
                ),
            }
            for name in self.spec.sources
        ]

    def _load_manifest(self, package: Path) -> dict[str, Any]:
        path = package / MANIFEST_NAME
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TtsRetirementError("rollback package manifest is missing or invalid") from exc
        if not isinstance(manifest, dict):
            raise TtsRetirementError("rollback package manifest must be an object")
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise TtsRetirementError("unsupported rollback package manifest schema")
        if not isinstance(manifest.get("items"), list) or len(manifest["items"]) != 4:
            raise TtsRetirementError("rollback package manifest item set is invalid")
        if not all(isinstance(item, dict) for item in manifest["items"]):
            raise TtsRetirementError("rollback package manifest item is invalid")
        previous_service = manifest.get("previous_service")
        if not isinstance(previous_service, dict) or not all(
            isinstance(previous_service.get(key), bool)
            for key in ("active", "enabled", "installed")
        ):
            raise TtsRetirementError("rollback package service state is invalid")
        expected = set(self.spec.sources)
        if {item.get("name") for item in manifest["items"]} != expected:
            raise TtsRetirementError("rollback package manifest names are invalid")
        for item in manifest["items"]:
            name = item["name"]
            if item.get("source") != str(self.spec.sources[name]):
                raise TtsRetirementError("rollback source path is outside the whitelist")
            if item.get("backup_name") != self.spec.backup_names[name]:
                raise TtsRetirementError("rollback backup path is outside the whitelist")
        return manifest

    def _service_state(self) -> dict[str, bool]:
        installed = self._systemctl("status", self.spec.service_name, check=False)[0] != 4
        active = self._systemctl("is-active", self.spec.service_name, check=False)[0] == 0
        enabled = self._systemctl("is-enabled", self.spec.service_name, check=False)[0] == 0
        return {"installed": installed, "active": active, "enabled": enabled}

    def _stop_disable_service(self) -> None:
        before = self._service_state()
        if before["active"]:
            self._systemctl("stop", self.spec.service_name, check=True)
        if before["enabled"]:
            self._systemctl("disable", self.spec.service_name, check=True)
        self._require_service_state({"active": False, "enabled": False})

    def _require_service_state(self, expected: dict[str, Any]) -> None:
        observed = self._service_state()
        for key in ("active", "enabled"):
            if key in expected and observed[key] != bool(expected[key]):
                raise TtsRetirementError(f"service {key} state verification failed")

    def _daemon_reload(self) -> None:
        self._systemctl("daemon-reload", check=True)

    def _systemctl(self, *args: str, check: bool = False) -> tuple[int, str]:
        try:
            if self._systemctl_override is not None:
                returncode, output = self._systemctl_override(*args, check=check)
            else:
                result = subprocess.run(
                    ["systemctl", *args],
                    text=True,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                returncode, output = result.returncode, result.stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise TtsRetirementError(f"systemctl command failed: {args[0]}") from exc
        if check and returncode != 0:
            raise TtsRetirementError(f"systemctl command failed: {args[0]}")
        return returncode, output

    def _runtime_ready(self) -> bool:
        if self._runtime_ready_override is not None:
            return bool(self._runtime_ready_override())
        runtime_python = self.spec.root / ".venv" / "bin" / "python"
        if not runtime_python.is_file():
            return False
        try:
            result = subprocess.run(
                [
                    str(runtime_python),
                    str(self.spec.root / "tools/inspect_runtime_status.py"),
                    "--summary",
                    "--require-ready",
                ],
                cwd=self.spec.root,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _restore_moved(
        self,
        moved: list[tuple[Path, Path]],
        previous_service: dict[str, Any],
    ) -> list[str]:
        errors = []
        for source, target in reversed(moved):
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(source))
            except OSError:
                errors.append(str(source))
        try:
            self._daemon_reload()
            if previous_service.get("enabled"):
                self._systemctl("enable", self.spec.service_name, check=True)
            if previous_service.get("active"):
                self._systemctl("start", self.spec.service_name, check=True)
            self._require_service_state(previous_service)
        except TtsRetirementError:
            errors.append("service_state")
        return errors

    def _resolve_package(
        self,
        package_name: str | None,
        *,
        managed_required: bool = True,
    ) -> Path:
        if package_name is None or PACKAGE_PATTERN.fullmatch(package_name) is None:
            raise TtsRetirementError("invalid rollback package name")
        package = self.spec.package_root / package_name
        if package.resolve().parent != self.spec.package_root.resolve():
            raise TtsRetirementError("rollback package escaped the package root")
        if not package.is_dir() or package.is_symlink():
            raise TtsRetirementError("rollback package does not exist or is unsafe")
        if managed_required and not (package / MANIFEST_NAME).is_file():
            raise TtsRetirementError("rollback package is not managed")
        return package

    def _validate_new_package_path(self, package: Path) -> None:
        if package.resolve().parent != self.spec.package_root.resolve():
            raise TtsRetirementError("new rollback package escaped the package root")
        if package.exists() or package.is_symlink():
            raise TtsRetirementError("new rollback package already exists")

    def _all_packages(self) -> list[Path]:
        if not self.spec.package_root.is_dir():
            return []
        return sorted(
            (
                path
                for path in self.spec.package_root.iterdir()
                if path.is_dir()
                and not path.is_symlink()
                and PACKAGE_PATTERN.fullmatch(path.name)
            ),
            key=lambda path: path.name,
            reverse=True,
        )

    def _managed_packages(self) -> list[Path]:
        return [path for path in self._all_packages() if (path / MANIFEST_NAME).is_file()]

    def _single_legacy_package(self) -> Path | None:
        legacy = [path for path in self._all_packages() if not (path / MANIFEST_NAME).exists()]
        return legacy[0] if len(legacy) == 1 and not self._managed_packages() else None

    def _latest_rehearsal(self, package: Path) -> dict[str, Any] | str:
        directory = package / "rehearsals"
        if not directory.is_dir():
            return "missing"
        receipts = sorted(directory.glob("rehearsal-*.json"), reverse=True)
        if not receipts:
            return "missing"
        try:
            return json.loads(receipts[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "invalid"

    def _write_receipt(self, *, event: str, payload: dict[str, Any]) -> str:
        self.spec.receipt_root.mkdir(parents=True, mode=0o750, exist_ok=True)
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "event": event,
            "created_at": self._now().isoformat(timespec="seconds"),
            **payload,
        }
        path = self.spec.receipt_root / f"{event}-{self._timestamp()}.json"
        write_json_atomic(path, receipt, mode=0o640)
        return str(path)

    def _package_name(self) -> str:
        return f"retired-local-tts-{self._timestamp()}"

    def _timestamp(self) -> str:
        return self._now().strftime("%Y%m%dT%H%M%SZ")


def inspect_path(path: Path, *, include_hash: bool) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"exists": False, "path": str(path), "bytes": 0, "files": 0, "sha256": None}
    if path.is_symlink():
        raise TtsRetirementError("top-level retirement path must not be a symlink")
    digest = hashlib.sha256()
    total_bytes = 0
    files = 0
    entries = [path] if path.is_file() else [path, *sorted(path.rglob("*"), key=str)]
    for entry in entries:
        relative = "." if entry == path else entry.relative_to(path).as_posix()
        metadata = entry.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if entry.is_symlink():
            target = os.readlink(entry)
            resolved = (entry.parent / target).resolve()
            if (
                path.is_dir()
                and path.resolve() not in (resolved, *resolved.parents)
                and not _is_allowed_external_symlink(path, entry, target)
            ):
                raise TtsRetirementError("retirement tree contains an escaping symlink")
            digest.update(f"L|{relative}|{mode:o}|{target}\n".encode())
            files += 1
        elif entry.is_dir():
            digest.update(f"D|{relative}|{mode:o}\n".encode())
        elif entry.is_file():
            file_hash = file_sha256(entry) if include_hash else "unverified"
            digest.update(
                f"F|{relative}|{mode:o}|{metadata.st_size}|{file_hash}\n".encode()
            )
            total_bytes += metadata.st_size
            files += 1
        else:
            raise TtsRetirementError("retirement tree contains a special file")
    return {
        "exists": True,
        "path": str(path),
        "bytes": total_bytes,
        "files": files,
        "sha256": digest.hexdigest() if include_hash else None,
    }


def _is_allowed_external_symlink(root: Path, entry: Path, target: str) -> bool:
    visited: set[Path] = set()
    while True:
        try:
            relative = entry.relative_to(root).as_posix()
        except ValueError:
            return False
        if re.fullmatch(r"\.venv/bin/python(?:3(?:\.\d+)?)?", relative) is None:
            return False
        if re.fullmatch(r"/usr/bin/python3(?:\.\d+)?", target):
            return True
        if re.fullmatch(r"python(?:3(?:\.\d+)?)?", target) is None:
            return False
        entry = entry.parent / target
        if entry in visited or not entry.is_symlink():
            return False
        visited.add(entry)
        target = os.readlink(entry)


def copy_with_hardlinks(source: Path, target: Path) -> None:
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    elif source.is_dir():
        target.mkdir(mode=stat.S_IMODE(source.stat().st_mode))
        for child in source.iterdir():
            copy_with_hardlinks(child, target / child.name)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)
    else:
        raise TtsRetirementError("cannot rehearse a special file")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def payload_sha256(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "plan_hash"}
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def require_plan_hash(plan: dict[str, Any], provided: str) -> None:
    if not provided or provided != plan["plan_hash"]:
        raise TtsRetirementError("plan hash mismatch; rerun plan")


def sanitize_approval_id(value: str | None) -> str:
    cleaned = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}", cleaned) is None:
        raise TtsRetirementError("approval id must be a non-secret ticket identifier")
    return cleaned


def parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise TtsRetirementError("manifest timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TtsRetirementError("manifest timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise TtsRetirementError("manifest timestamp lacks timezone")
    return parsed.astimezone(UTC)


def write_json_atomic(path: Path, payload: dict[str, Any], *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def tcp_listening(host: str, port: int, *, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the historical local TTS retirement transaction."
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=int(os.getenv("QQ_BOT_TTS_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--operation", choices=("retire", "rollback", "delete"), default="retire")
    plan.add_argument("--package")
    plan.add_argument("--approval-id")
    status = subparsers.add_parser("status")
    status.add_argument("--verify-hashes", action="store_true")
    apply = subparsers.add_parser("apply")
    apply.add_argument("--plan-hash", required=True)
    apply.add_argument("--confirm-retire-local-tts", action="store_true")
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--package", required=True)
    rollback.add_argument("--plan-hash", required=True)
    rollback.add_argument("--confirm-rollback-local-tts", action="store_true")
    rehearse = subparsers.add_parser("rehearse")
    rehearse.add_argument("--package", required=True)
    rehearse.add_argument("--apply", action="store_true")
    rehearse.add_argument("--confirm-rehearsal", action="store_true")
    delete = subparsers.add_parser("delete")
    delete.add_argument("--package", required=True)
    delete.add_argument("--plan-hash", required=True)
    delete.add_argument("--approval-id", required=True)
    delete.add_argument("--apply", action="store_true")
    delete.add_argument("--confirm-delete-package", required=True)
    delete.add_argument("--confirm-delete-without-rollback", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.retention_days <= 0:
        raise SystemExit("--retention-days must be positive")
    manager = TtsRetirementManager(
        TtsRetirementSpec.production(retention_days=args.retention_days)
    )
    try:
        if args.command == "plan":
            result = manager.plan(
                operation=args.operation,
                package_name=args.package,
                approval_id=args.approval_id,
            )
        elif args.command == "status":
            result = manager.status(verify_hashes=args.verify_hashes)
        elif args.command == "apply":
            if not args.confirm_retire_local_tts:
                raise TtsRetirementError("apply needs --confirm-retire-local-tts")
            result = manager.apply(plan_hash=args.plan_hash)
        elif args.command == "rollback":
            if not args.confirm_rollback_local_tts:
                raise TtsRetirementError("rollback needs --confirm-rollback-local-tts")
            result = manager.rollback(
                package_name=args.package,
                plan_hash=args.plan_hash,
            )
        elif args.command == "rehearse":
            if not args.apply or not args.confirm_rehearsal:
                raise TtsRetirementError(
                    "rehearsal needs --apply and --confirm-rehearsal"
                )
            result = manager.rehearse(package_name=args.package)
        else:
            if (
                not args.apply
                or args.confirm_delete_package != args.package
            ):
                raise TtsRetirementError(
                    "delete needs --apply and exact --confirm-delete-package"
                )
            result = manager.delete(
                package_name=args.package,
                plan_hash=args.plan_hash,
                approval_id=args.approval_id,
                confirm_without_rollback=args.confirm_delete_without_rollback,
            )
    except TtsRetirementError as exc:
        print(json.dumps({"status": "failed", "category": str(exc)}, ensure_ascii=False))
        return 2
    except OSError:
        print(json.dumps({"status": "failed", "category": "filesystem_error"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

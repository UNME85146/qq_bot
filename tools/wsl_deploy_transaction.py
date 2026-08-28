from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    from .deploy_wsl import (
        historical_tts_adapter_preflight_error,
        normalize_runtime_fingerprint,
        normalize_systemd_state,
    )
except ImportError:  # The immutable transaction bundle keeps wrappers together.
    from deploy_wsl import (
        historical_tts_adapter_preflight_error,
        normalize_runtime_fingerprint,
        normalize_systemd_state,
    )

try:
    import fcntl
except ModuleNotFoundError:  # Windows imports this module for unit tests only.
    fcntl = None  # type: ignore[assignment]


PRESERVE_TOP = {
    ".env",
    ".git",
    ".venv",
    ".venvs",
    "AGENTS.md",
    "QQ真人感虚拟角色Bot_总设计方案.md",
    "data",
    "docs",
    "logs",
    "run",
    "runtime_artifacts",
    "操作手册.md",
    "__pycache__",
    ".pytest_cache",
    "qq_realistic_role_bot.egg-info",
}
PRESERVE_CONFIG = {"config.json", "persona_profile.local.json"}
PRESERVE_RUNTIME_FILES = {"scripts/server/moss_tts_adapter.py"}
HISTORICAL_TTS_ADAPTER_PATH = Path("scripts/server/moss_tts_adapter.py")
COMPLETED_JOURNAL_STATUSES = {"success", "rolled_back"}
PLAN_VALIDITY_MINUTES = 30
AUTHORIZED_TARGET = {
    "distro": "Ubuntu-24.04",
    "root": "/opt/qq_bot",
    "service": "qq-bot.service",
    "napcatContainer": "napcat",
    "videoCacheHost": "/opt/napcat/cache/qq-bot-media",
    "videoCacheContainer": "/app/napcat/cache/qq-bot-media",
}


class DeploymentError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute one plan-bound WSL deployment.")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--plan-hash", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--migration-tool", required=True)
    parser.add_argument("--orchestrator", required=True)
    parser.add_argument("--powershell-wrapper", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--journal", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if fcntl is None or not hasattr(os, "geteuid"):
        raise SystemExit("WSL deployment transaction requires Linux flock support")
    if os.geteuid() != 0:
        raise SystemExit("WSL deployment transaction must run as root")
    receipt_path = Path(args.receipt)
    journal_path = Path(args.journal)
    plan: dict[str, Any] = {}
    try:
        plan = _read_json(Path(args.plan))
        _validate_plan_hash(plan, args.plan_hash)
        _validate_plan_fresh(plan)
        _require_wrapper_sha(
            (
                ("tools/deploy_wsl.py", Path(args.orchestrator)),
                ("tools/migrate_wsl_config.py", Path(args.migration_tool)),
                ("tools/wsl_deploy_transaction.py", Path(__file__).resolve()),
                ("scripts/deploy_wsl.ps1", Path(args.powershell_wrapper)),
            ),
            str(_object(plan.get("source")).get("wrapperSha256") or ""),
        )
        target = _object(plan.get("target"))
        _require_authorized_target(target)
        root = Path(str(target.get("root") or "")).resolve()
        if not root.is_dir() or str(root) != str(target.get("root")):
            raise DeploymentError("runtime root identity mismatch")
        lock_path = Path("/run/lock/qq-bot-wsl-deploy.lock")
        with _operation_lock(lock_path):
            _ensure_no_active_journal(journal_path, str(plan["planHash"]))
            return _execute(args, plan, target, root, journal_path)
    except BaseException as exc:
        result = "rejected_before_mutation"
        journal: dict[str, Any] | None = None
        if journal_path.is_file():
            try:
                value = _read_json(journal_path)
                status = str(value.get("status") or "")
                if value.get("planHash") != plan.get("planHash"):
                    if status not in COMPLETED_JOURNAL_STATUSES:
                        result = "state_unknown"
                else:
                    journal = value
                    result = "state_unknown"
            except BaseException:
                result = "state_unknown"
        receipt = {
            "schemaVersion": 1,
            "result": result,
            "planHash": str(plan.get("planHash") or args.plan_hash),
            "completedAt": datetime.now(UTC).isoformat(),
            "errorType": type(exc).__name__,
            "error": _safe_error(exc),
            "journal": journal,
            "replayAllowed": result == "rejected_before_mutation",
            "qqMessagesSentByAcceptance": False,
        }
        _write_json(receipt_path, receipt)
        print(f"deployment rejected: {receipt['error']}", file=sys.stderr)
        return 19


@contextmanager
def _operation_lock(path: Path):
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise DeploymentError("operation lock identity is unsafe")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError(
                "another WSL deployment holds the operation lock"
            ) from exc
        yield
    finally:
        os.close(descriptor)


def _ensure_no_active_journal(path: Path, plan_hash: str) -> None:
    if not path.exists():
        return
    journal = _read_json(path)
    if journal.get("status") not in COMPLETED_JOURNAL_STATUSES:
        raise DeploymentError(
            "unresolved previous deployment state; inspect the persistent journal"
        )
    journal_plan_hash = str(journal.get("planHash") or "")
    if not journal_plan_hash:
        raise DeploymentError(
            "completed deployment journal is missing its plan hash"
        )
    if journal_plan_hash == plan_hash:
        raise DeploymentError(
            "deployment plan was already consumed; generate a new plan"
        )


def _write_journal(path: Path, payload: dict[str, Any]) -> None:
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or parent.stat().st_uid != 0:
        raise DeploymentError("deployment journal directory identity is unsafe")
    os.chmod(parent, 0o700)
    _write_json(path, payload)


def _journal_payload(
    plan: dict[str, Any],
    *,
    status: str,
    phase: str,
    backup: Path | None,
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "status": status,
        "phase": phase,
        "planHash": plan["planHash"],
        "commit": plan["source"]["commit"],
        "backup": str(backup) if backup else None,
        "updatedAt": datetime.now(UTC).isoformat(),
    }


def _execute(
    args,
    plan: dict[str, Any],
    target: dict[str, Any],
    root: Path,
    journal_path: Path,
) -> int:
    archive = Path(args.archive)
    profile = Path(args.profile)
    dependency_lock = Path(args.lock)
    source = _object(plan.get("source"))
    _require_sha(archive, str(source.get("archiveSha256")), "release archive")
    _require_sha(profile, str(source.get("replacementPersonaSha256")), "persona profile")
    _require_sha(dependency_lock, str(source.get("dependencyLockSha256")), "dependency lock")
    _validate_target_environment(plan, target, root)

    service = str(target["service"])
    service_user = _run(
        ["systemctl", "show", service, "--property=User", "--value"]
    ).stdout.strip()
    service_group = _run(
        ["systemctl", "show", service, "--property=Group", "--value"]
    ).stdout.strip()
    if not service_user or service_user == "root":
        raise DeploymentError("service must use a non-root application identity")
    if not service_group:
        service_group = _run(["id", "-gn", service_user]).stdout.strip()

    staging_root = Path(args.plan).parent
    shutil.chown(staging_root, "root", service_group)
    os.chmod(staging_root, 0o710)
    work = Path(args.plan).parent / "transaction"
    if work.exists():
        shutil.rmtree(work)
    release = work / "release"
    migrated_config = work / "config.json"
    test_database = work / "migration-test.db"
    work.mkdir(mode=0o750)
    shutil.chown(work, "root", service_group)
    release.mkdir(mode=0o750)
    _extract_release(archive, release)
    _chown_tree(release, service_user, service_group)
    os.chmod(dependency_lock, 0o600)
    shutil.chown(dependency_lock, service_user, service_group)
    test_profile = work / "persona_profile.local.json"
    shutil.copy2(profile, test_profile)
    os.chmod(test_profile, 0o600)
    shutil.chown(test_profile, service_user, service_group)
    _run(
        [
            "python3",
            args.migration_tool,
            "--input",
            str(root / "config/config.json"),
            "--output",
            str(migrated_config),
            "--profile",
            str(test_profile),
            "--video-cache-host",
            str(target["videoCacheHost"]),
            "--video-cache-container",
            str(target["videoCacheContainer"]),
        ]
    )
    os.chmod(migrated_config, 0o600)
    shutil.chown(migrated_config, service_user, service_group)

    plan_hash = str(plan["planHash"])
    commit = str(source["commit"])
    versioned_venv = root / ".venvs" / f"{commit}-{plan_hash[:12]}"
    if versioned_venv.exists() or versioned_venv.is_symlink():
        raise DeploymentError(f"staged venv already exists: {versioned_venv}")
    (root / ".venvs").mkdir(mode=0o750, exist_ok=True)
    shutil.chown(root / ".venvs", service_user, service_group)

    receipt_path = Path(args.receipt)
    backup: Path | None = None
    service_was_active = _is_active(service)
    mutation_started = False
    cache_state: dict[str, Any] = {}
    database_path: Path | None = None
    database_backup: Path | None = None
    before_counts: dict[str, int] = {}
    database_state: dict[str, Any] = {}
    old_venv: dict[str, Any] = {}
    venv_switch_started = False
    pre_napcat: dict[str, Any] | None = None
    pre_tts: dict[str, Any] | None = None
    _write_journal(
        journal_path,
        _journal_payload(plan, status="in_progress", phase="preflight", backup=None),
    )
    try:
        _prestage_and_test(
            release=release,
            versioned_venv=versioned_venv,
            dependency_lock=dependency_lock,
            config=migrated_config,
            profile=test_profile,
            service_user=service_user,
            service_group=service_group,
        )
        _validate_plan_fresh(plan)
        pre_runtime = _runtime_fingerprints(target, root)
        if pre_runtime != plan.get("runtimeFingerprints"):
            raise DeploymentError("runtime fingerprint drift before stop")

        pre_napcat = _napcat_state(str(target["napcatContainer"]))
        pre_tts = _tts_state()
        listener_host, listener_port = _onebot_listener(root / "config/config.json")
        if service_was_active:
            _run(["systemctl", "stop", service])
        if _is_active(service) or _tcp_listening(listener_host, listener_port):
            raise DeploymentError("qq-bot.service did not quiesce before backup")
        _write_journal(
            journal_path,
            _journal_payload(
                plan,
                status="in_progress",
                phase="service_stopped",
                backup=None,
            ),
        )

        stopped_runtime = _runtime_fingerprints(target, root)
        expected_stopped = dict(plan["runtimeFingerprints"])
        expected_stopped["serviceState"] = "inactive"
        if stopped_runtime != expected_stopped:
            raise DeploymentError("runtime fingerprint drift after stop")

        backup = root.parent / f"{root.name}_runtime_backup_{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
        backup.mkdir(mode=0o750)
        _backup_code(root, backup / "code.tar")
        _backup_private(root, backup)
        _backup_systemd(service, backup)
        old_venv = _capture_venv(root, backup)
        database_path = _database_path(root, migrated_config)
        if database_path.exists() and not database_path.is_file():
            raise DeploymentError("runtime database path is not a regular file")
        database_state = _capture_database_state(database_path)
        if database_state["existed"]:
            database_backup = backup / "data" / database_path.name
            database_backup.parent.mkdir(parents=True, exist_ok=True)
            _backup_database(database_path, database_backup)
            shutil.chown(database_backup, service_user, service_group)
            before_counts = _table_counts(database_backup)
            shutil.copy2(database_backup, test_database)
            shutil.chown(test_database, service_user, service_group)
            _initialize_database(
                versioned_venv,
                release,
                test_database,
                service_user,
                service_group,
                prepare_parent=True,
            )
            _require_quick_check(test_database)
            if _changed_existing_counts(before_counts, _table_counts(test_database)):
                raise DeploymentError("disposable database migration changed existing row counts")

        cache_state = _capture_cache(Path(str(target["videoCacheHost"])))
        manifest = _write_backup_manifest(
            backup=backup,
            plan=plan,
            database_path=database_path,
            database_backup=database_backup,
            database_state=database_state,
            old_venv=old_venv,
            cache_state=cache_state,
            service_user=service_user,
            service_group=service_group,
            pre_napcat=pre_napcat,
            pre_tts=pre_tts,
        )
        _verify_backup_manifest(backup, manifest)
        _write_journal(
            journal_path,
            _journal_payload(
                plan,
                status="in_progress",
                phase="backup_verified",
                backup=backup,
            ),
        )

        _require_sha(root / "config/config.json", plan["runtimeFingerprints"]["runtimeConfigSha256"], "runtime config CAS")
        old_profile_path = Path(plan["runtimeFingerprints"]["runtimePersonaPath"])
        _require_sha(old_profile_path, plan["runtimeFingerprints"]["runtimePersonaSha256"], "runtime persona CAS")

        _validate_plan_fresh(plan)
        _write_journal(
            journal_path,
            _journal_payload(
                plan,
                status="in_progress",
                phase="mutation_started",
                backup=backup,
            ),
        )
        mutation_started = True
        _sync_release(release, root, service_user, service_group)
        _install_private_file(migrated_config, root / "config/config.json", service_user, service_group)
        _install_private_file(profile, root / "config/persona_profile.local.json", service_user, service_group)
        venv_switch_started = True
        _switch_venv(root, versioned_venv, backup, old_venv)
        napcat_uid = int(pre_napcat["runtimeUid"])
        napcat_gid = int(pre_napcat["runtimeGid"])
        _prepare_cache(
            Path(str(target["videoCacheHost"])),
            service_user,
            service_group,
            plan_hash,
            cache_state,
            napcat_gid=napcat_gid,
        )
        _validate_cache_mount(
            str(target["napcatContainer"]),
            Path(str(target["videoCacheHost"])),
            str(target["videoCacheContainer"]),
            runtime_uid=napcat_uid,
            runtime_gid=napcat_gid,
        )
        if database_path is not None:
            if not database_state.get("existed") and database_path.exists():
                raise DeploymentError("runtime database appeared before initialization")
            if not database_state.get("existed"):
                database_state["createdByDeployment"] = True
            _initialize_database(
                root / ".venv",
                root,
                database_path,
                service_user,
                service_group,
            )
            _require_quick_check(database_path)
            after_counts = _table_counts(database_path)
            if _changed_existing_counts(before_counts, after_counts):
                raise DeploymentError("live database migration changed existing row counts")
        else:
            after_counts = {}

        ownership = _verify_application_ownership(
            root=root,
            release=release,
            config=root / "config/config.json",
            profile=root / "config/persona_profile.local.json",
            database=database_path,
            versioned_venv=versioned_venv,
            cache=Path(str(target["videoCacheHost"])),
            user=service_user,
            group=service_group,
            cache_group=napcat_gid,
        )
        started_at = datetime.now(UTC)
        _run(["systemctl", "start", service])
        _write_journal(
            journal_path,
            _journal_payload(
                plan,
                status="in_progress",
                phase="service_started",
                backup=backup,
            ),
        )
        stable = _stable_readiness(service, root, started_at)
        if _napcat_state(str(target["napcatContainer"])) != pre_napcat:
            raise DeploymentError("NapCat state changed during deployment")
        if _tts_state() != pre_tts:
            raise DeploymentError("historical TTS state changed during deployment")
        if _journal_has_traceback(service, started_at):
            raise DeploymentError("new service journal contains a Python traceback")
        _finish_cache(
            Path(str(target["videoCacheHost"])),
            plan_hash,
            cache_state,
        )

        receipt = {
            "schemaVersion": 1,
            "result": "success",
            "planHash": plan_hash,
            "commit": commit,
            "completedAt": datetime.now(UTC).isoformat(),
            "backup": str(backup),
            "service": stable,
            "databaseQuickCheck": "ok" if database_path is not None else "not-created",
            "databaseExistedBefore": bool(database_state.get("existed")),
            "schemaVersionApplied": _schema_version(database_path) if database_path else 0,
            "rowCountDigest": _json_digest(after_counts),
            "napcatUnchanged": True,
            "historicalTtsUnchanged": True,
            "replayAllowed": False,
            "qqMessagesSentByAcceptance": False,
            "videoCacheMode": oct(Path(str(target["videoCacheHost"])).stat().st_mode & 0o7777),
            "videoCacheOwner": f"{service_user}:{_group_label(napcat_gid)}",
            "applicationOwner": f"{service_user}:{service_group}",
            "ownership": ownership,
            "providerValidation": "deferred_to_post_deploy_acceptance",
        }
        _write_journal(
            journal_path,
            _journal_payload(plan, status="success", phase="complete", backup=backup),
        )
        _write_json(receipt_path, receipt)
        _write_json(backup / "apply-receipt.json", receipt)
        print(f"deployment successful; backup retained at {backup}")
        return 0
    except BaseException as exc:
        rollback_errors: list[str] = []
        if mutation_started and backup is not None:
            rollback_ok = _rollback(
                root=root,
                backup=backup,
                service=service,
                service_was_active=service_was_active,
                database_path=database_path,
                database_backup=database_backup,
                database_state=database_state,
                old_venv=old_venv,
                venv_switch_started=venv_switch_started,
                cache_path=Path(str(target["videoCacheHost"])),
                cache_state=cache_state,
                service_user=service_user,
                service_group=service_group,
                errors=rollback_errors,
            )
        else:
            rollback_ok = _restore_service_state(
                service,
                root,
                was_active=service_was_active,
                errors=rollback_errors,
            )
        if rollback_ok and pre_napcat is not None and pre_tts is not None:
            rollback_ok = _verify_rollback_dependencies(
                service=service,
                root=root,
                napcat_container=str(target["napcatContainer"]),
                expected_napcat=pre_napcat,
                expected_tts=pre_tts,
                errors=rollback_errors,
            )
        if versioned_venv.exists() and not (root / ".venv").resolve() == versioned_venv.resolve():
            shutil.rmtree(versioned_venv, ignore_errors=True)
        receipt = {
            "schemaVersion": 1,
            "result": "rolled_back" if rollback_ok else "rollback_incomplete",
            "planHash": plan_hash,
            "commit": commit,
            "completedAt": datetime.now(UTC).isoformat(),
            "backup": str(backup) if backup else None,
            "errorType": type(exc).__name__,
            "error": _safe_error(exc),
            "rollbackErrors": rollback_errors,
            "replayAllowed": False,
            "qqMessagesSentByAcceptance": False,
        }
        journal_status = "rolled_back" if rollback_ok else "rollback_incomplete"
        try:
            _write_journal(
                journal_path,
                _journal_payload(
                    plan,
                    status=journal_status,
                    phase="complete",
                    backup=backup,
                ),
            )
        except BaseException as journal_exc:
            rollback_errors.append(f"write_journal: {_safe_error(journal_exc)}")
            receipt["result"] = "rollback_incomplete"
            journal_status = "rollback_incomplete"
        _write_json(receipt_path, receipt)
        if backup is not None:
            _write_json(backup / "failure-receipt.json", receipt)
        print(f"deployment failed: {receipt['error']}", file=sys.stderr)
        return 20 if rollback_ok and journal_status == "rolled_back" else 21


def _prestage_and_test(
    *,
    release: Path,
    versioned_venv: Path,
    dependency_lock: Path,
    config: Path,
    profile: Path,
    service_user: str,
    service_group: str,
) -> None:
    _run_as(service_user, service_group, ["python3", "-m", "venv", str(versioned_venv)])
    python = versioned_venv / "bin/python"
    pytest_basetemp = versioned_venv / ".deployment-pytest-tmp"
    if pytest_basetemp.exists() or pytest_basetemp.is_symlink():
        raise DeploymentError("candidate pytest basetemp already exists")
    _run_as(
        service_user,
        service_group,
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "-r",
            str(dependency_lock),
        ],
    )
    environment = {
        "PYTHONPATH": str(release),
        "QQ_BOT_CONFIG_PATH": str(config),
        "QQ_BOT_MODEL_API_KEY": "deployment-import-placeholder",
    }
    staged_profile = release / "config/persona_profile.local.json"
    if staged_profile.exists() or staged_profile.is_symlink():
        raise DeploymentError("release unexpectedly contains a local persona profile")
    shutil.copy2(profile, staged_profile)
    os.chmod(staged_profile, 0o600)
    if hasattr(os, "chown"):
        shutil.chown(staged_profile, service_user, service_group)
    try:
        _run_as(
            service_user,
            service_group,
            [str(python), "-m", "pytest", "-q", "--basetemp", str(pytest_basetemp)],
            cwd=release,
            env=environment,
        )
    finally:
        staged_profile.unlink(missing_ok=True)
        if pytest_basetemp.is_symlink():
            pytest_basetemp.unlink()
        elif pytest_basetemp.exists():
            shutil.rmtree(pytest_basetemp)
    _run_as(
        service_user,
        service_group,
        [str(python), "-m", "compileall", "app", "bot.py", "tests", "tools"],
        cwd=release,
        env=environment,
    )
    _run_as(
        service_user,
        service_group,
        [str(python), "-c", "import bot; import nonebot; print(nonebot.get_driver().type)"],
        cwd=release,
        env=environment,
    )
    _run(["/usr/bin/ffmpeg", "-version"])


def _validate_target_environment(plan: dict[str, Any], target: dict[str, Any], root: Path) -> None:
    _require_authorized_target(target)
    os_release = _read_os_release()
    if target.get("distro") != "Ubuntu-24.04" or os_release.get("VERSION_ID") != "24.04":
        raise DeploymentError("WSL distro identity mismatch")
    if plan.get("scope") != {
        "restartNapCat": False,
        "mutateHistoricalTts": False,
        "sendSyntheticQqMessages": False,
        "publishGithub": False,
    }:
        raise DeploymentError("deployment scope is not fail-closed")
    if not (root / ".venv/bin/python").exists():
        raise DeploymentError("current runtime venv is missing")
    napcat = _napcat_state(str(target["napcatContainer"]))
    if napcat.get("status") != "running" or napcat.get("running") is not True:
        raise DeploymentError("NapCat container must be running before deployment")
    tts = _tts_state()
    adapter_error = historical_tts_adapter_preflight_error(
        service_state=tts.get("state"),
        adapter_kind=_runtime_adapter_kind(root),
    )
    if adapter_error:
        raise DeploymentError(adapter_error)
    _run(["/usr/bin/ffmpeg", "-version"])
    _validate_cache_mount(
        str(target["napcatContainer"]),
        Path(str(target["videoCacheHost"])),
        str(target["videoCacheContainer"]),
        require_leaf=False,
    )


def _require_authorized_target(target: dict[str, Any]) -> None:
    if target != AUTHORIZED_TARGET:
        raise DeploymentError("deployment target is outside the authorized WSL target")


def _runtime_fingerprints(target: dict[str, Any], root: Path) -> dict[str, Any]:
    return normalize_runtime_fingerprint(_runtime_fingerprint_payload(target, root))


def _runtime_fingerprint_payload(target: dict[str, Any], root: Path) -> dict[str, Any]:
    service = str(target["service"])
    config = root / "config/config.json"
    payload = _read_json(config)
    value = str(_object(payload.get("persona")).get("profilePath") or "persona_profile.local.json")
    profile = Path(value)
    if not profile.is_absolute():
        candidate = root / profile
        profile = candidate if candidate.exists() or len(profile.parts) > 1 else config.parent / profile
    profile = profile.resolve()
    if not profile.is_relative_to(root):
        raise DeploymentError("persona profile escaped runtime root")
    fragment = Path(
        _run(["systemctl", "show", service, "--property=FragmentPath", "--value"]).stdout.strip()
    )
    dropins = [
        Path(value)
        for value in _run(
            ["systemctl", "show", service, "--property=DropInPaths", "--value"]
        ).stdout.split()
    ]
    napcat = _napcat_state(str(target["napcatContainer"]))
    tts = _tts_state()
    stat = root.stat()
    return {
        "runtimeConfigSha256": _file_sha(config),
        "runtimePersonaPath": str(profile),
        "runtimePersonaSha256": _file_sha(profile),
        "runtimeRootOwner": f"{_user_name(stat.st_uid)}:{_group_name(stat.st_gid)}",
        "runtimeRootMode": format(stat.st_mode & 0o7777, "o"),
        "serviceState": _systemd_state(service),
        "serviceUser": _run(["systemctl", "show", service, "--property=User", "--value"]).stdout.strip(),
        "serviceGroup": _run(["systemctl", "show", service, "--property=Group", "--value"]).stdout.strip(),
        "serviceFragmentSha256": _file_sha(fragment),
        "serviceUnitSha256": _unit_digest([fragment, *dropins]),
        "napcatContainerId": napcat["id"],
        "napcatStartedAt": napcat["startedAt"],
        "napcatRestartCount": napcat["restartCount"],
        "napcatStatus": napcat["status"],
        "napcatRunning": napcat["running"],
        "napcatRuntimeUid": napcat["runtimeUid"],
        "napcatRuntimeGid": napcat["runtimeGid"],
        "ttsServiceState": tts["state"],
        "ttsServiceFragmentSha256": tts["fragmentSha256"],
        "ttsServiceUnitSha256": tts["unitSha256"],
        "ttsMainPid": tts["mainPid"],
        "ttsNRestarts": tts["nRestarts"],
        "ttsActiveEnterTimestampMonotonic": tts["activeEnterTimestampMonotonic"],
        "ttsRuntimeAdapterSha256": tts["runtimeAdapterSha256"],
        "ttsRuntimeAdapterKind": _runtime_adapter_kind(root),
        "ffmpegVersion": _run(["/usr/bin/ffmpeg", "-version"]).stdout.splitlines()[0],
    }


def _runtime_adapter_kind(root: Path) -> str:
    current = root
    for component in HISTORICAL_TTS_ADAPTER_PATH.parts:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return "missing"
        if stat.S_ISLNK(mode):
            return "symlink"
        if component != HISTORICAL_TTS_ADAPTER_PATH.parts[-1]:
            if not stat.S_ISDIR(mode):
                return "non_regular"
        elif stat.S_ISREG(mode):
            return "regular"
        else:
            return "non_regular"
    return "missing"


def _napcat_state(container: str) -> dict[str, Any]:
    payload = json.loads(
        _run(["docker", "inspect", container], quiet=True).stdout
    )[0]
    runtime_uid, runtime_gid = _napcat_runtime_identity(container)
    return {
        "id": payload.get("Id"),
        "startedAt": _object(payload.get("State")).get("StartedAt"),
        "restartCount": payload.get("RestartCount"),
        "status": _object(payload.get("State")).get("Status"),
        "running": _object(payload.get("State")).get("Running"),
        "runtimeUid": runtime_uid,
        "runtimeGid": runtime_gid,
    }


def _napcat_runtime_identity(container: str) -> tuple[int, int]:
    output = _run(
        ["docker", "exec", container, "ps", "-eo", "uid=,gid=,comm=,args="],
        quiet=True,
    ).stdout
    return _parse_napcat_runtime_identity(output)


def _parse_napcat_runtime_identity(output: str) -> tuple[int, int]:
    identities = {
        (int(parts[0]), int(parts[1]))
        for line in output.splitlines()
        if len(parts := line.split(None, 3)) == 4
        and parts[2] == "qq"
        and _is_napcat_main_qq_command(parts[3])
    }
    if len(identities) != 1:
        raise DeploymentError("NapCat QQ runtime identity is unavailable or ambiguous")
    return next(iter(identities))


def _is_napcat_main_qq_command(command: str) -> bool:
    argv = command.split()
    return (
        bool(argv)
        and argv[0].endswith("/opt/QQ/qq")
        and not any(item.startswith("--type=") for item in argv[1:])
        and any(
            argv[index] == "-q" and index + 1 < len(argv) and argv[index + 1].isdigit()
            for index in range(len(argv))
        )
    )


def _tts_state() -> dict[str, Any]:
    state = normalize_systemd_state(
        _run(["systemctl", "is-active", "qq-bot-tts.service"], check=False).stdout
    )
    fragment_value = _run(
        ["systemctl", "show", "qq-bot-tts.service", "--property=FragmentPath", "--value"],
        check=False,
    ).stdout.strip()
    fragment = Path(fragment_value) if fragment_value else None
    dropins = [
        Path(value)
        for value in _run(
            ["systemctl", "show", "qq-bot-tts.service", "--property=DropInPaths", "--value"],
            check=False,
        ).stdout.split()
    ]
    runtime_adapter = (
        Path(str(AUTHORIZED_TARGET["root"])) / HISTORICAL_TTS_ADAPTER_PATH
    )
    runtime_adapter_kind = _runtime_adapter_kind(Path(str(AUTHORIZED_TARGET["root"])))
    return {
        "state": state,
        "fragmentSha256": _file_sha(fragment) if fragment else None,
        "unitSha256": _unit_digest([fragment, *dropins] if fragment else dropins),
        "mainPid": int(
            _run(
                ["systemctl", "show", "qq-bot-tts.service", "--property=MainPID", "--value"],
                check=False,
            ).stdout.strip()
            or 0
        ),
        "nRestarts": int(
            _run(
                ["systemctl", "show", "qq-bot-tts.service", "--property=NRestarts", "--value"],
                check=False,
            ).stdout.strip()
            or 0
        ),
        "activeEnterTimestampMonotonic": int(
            _run(
                [
                    "systemctl",
                    "show",
                    "qq-bot-tts.service",
                    "--property=ActiveEnterTimestampMonotonic",
                    "--value",
                ],
                check=False,
            ).stdout.strip()
            or 0
        ),
        "runtimeAdapterSha256": (
            _file_sha(runtime_adapter) if runtime_adapter_kind == "regular" else None
        ),
    }


def _validate_cache_mount(
    container: str,
    host_path: Path,
    container_path: str,
    *,
    require_leaf: bool = True,
    runtime_uid: int | None = None,
    runtime_gid: int | None = None,
) -> None:
    if host_path.is_symlink():
        raise DeploymentError("video cache path must not be a symlink")
    host_parent = str(host_path.parent)
    container_parent = str(Path(container_path).parent).replace("\\", "/")
    payload = json.loads(
        _run(["docker", "inspect", container], quiet=True).stdout
    )[0]
    mounts = payload.get("Mounts") if isinstance(payload.get("Mounts"), list) else []
    matched = any(
        item.get("Source") == host_parent
        and item.get("Destination") == container_parent
        and item.get("RW") is True
        for item in mounts
    )
    if not matched:
        raise DeploymentError("NapCat cache mount does not match the plan")
    if require_leaf:
        if runtime_uid is None or runtime_gid is None:
            raise DeploymentError("NapCat runtime identity is required for cache validation")
        prefix = [
            "docker",
            "exec",
            "--user",
            f"{runtime_uid}:{runtime_gid}",
            container,
            "test",
        ]
        _run([*prefix, "-r", container_path])
        _run([*prefix, "-x", container_path])


def _backup_code(root: Path, output: Path) -> None:
    with tarfile.open(output, "w") as bundle:
        for item in sorted(root.iterdir(), key=lambda value: value.name):
            if item.name in PRESERVE_TOP:
                continue
            if item.name == "config":
                for child in sorted(item.iterdir(), key=lambda value: value.name):
                    if child.name in PRESERVE_CONFIG or child.name.endswith(".local.json"):
                        continue
                    bundle.add(child, arcname=f"config/{child.name}", recursive=True)
                continue
            bundle.add(item, arcname=item.name, recursive=True)


def _backup_private(root: Path, backup: Path) -> None:
    private = backup / "private"
    private.mkdir()
    for name in (".env", "config/config.json", "config/persona_profile.local.json"):
        source = root / name
        if source.exists():
            target = private / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _backup_systemd(service: str, backup: Path) -> None:
    fragment = _run(["systemctl", "show", service, "--property=FragmentPath", "--value"]).stdout.strip()
    dropins = _run(["systemctl", "show", service, "--property=DropInPaths", "--value"]).stdout.split()
    with tarfile.open(backup / "systemd.tar", "w") as bundle:
        for value in [fragment, *dropins]:
            path = Path(value)
            if path.is_file():
                bundle.add(path, arcname=str(path).lstrip("/"), recursive=False)


def _capture_venv(root: Path, backup: Path) -> dict[str, Any]:
    current = root / ".venv"
    if current.is_symlink():
        return {"kind": "symlink", "target": os.readlink(current)}
    if current.is_dir():
        venv_backup = backup / "venv.tar"
        digest = _tree_digest(current)
        with tarfile.open(venv_backup, "w") as bundle:
            bundle.add(current, arcname=".venv", recursive=True)
        return {
            "kind": "directory",
            "treeDigest": digest,
            "backup": venv_backup.name,
        }
    raise DeploymentError("current .venv is neither a directory nor a symlink")


def _switch_venv(root: Path, versioned: Path, backup: Path, old: dict[str, Any]) -> None:
    current = root / ".venv"
    if old["kind"] == "directory":
        current.rename(backup / "venv-live")
    else:
        current.unlink()
    temporary = root / f".venv.link-{os.getpid()}"
    temporary.symlink_to(versioned.relative_to(root), target_is_directory=True)
    current_stat = versioned.stat()
    if hasattr(os, "lchown"):
        os.lchown(temporary, current_stat.st_uid, current_stat.st_gid)
    temporary.replace(current)


def _verify_application_ownership(
    *,
    root: Path,
    release: Path,
    config: Path,
    profile: Path,
    database: Path | None,
    versioned_venv: Path,
    cache: Path,
    user: str,
    group: str,
    cache_group: int,
) -> dict[str, Any]:
    uid = _user_id(user)
    gid = _group_id(group)
    code_paths = [root]
    code_paths.extend(
        root / item.name
        for item in release.iterdir()
        if item.name not in PRESERVE_TOP and item.name != ".env"
    )
    code_entries = sum(
        _require_owned_path(path, uid, gid, recursive=path != root)
        for path in code_paths
    )
    config_entries = _require_owned_path(config, uid, gid)
    config_entries += _require_owned_path(profile, uid, gid)
    database_entries = (
        _require_owned_path(database, uid, gid)
        if database is not None and database.exists()
        else 0
    )
    venv_entries = _require_owned_path(versioned_venv, uid, gid, recursive=True)
    cache_entries = _require_owned_path(cache, uid, cache_group)
    return {
        "codeEntries": code_entries,
        "privateConfigEntries": config_entries,
        "databaseEntries": database_entries,
        "versionedVenvEntries": venv_entries,
        "videoCacheEntries": cache_entries,
    }


def _require_owned_path(
    path: Path,
    uid: int,
    gid: int,
    *,
    recursive: bool = False,
) -> int:
    if not path.exists() and not path.is_symlink():
        raise DeploymentError(f"ownership target is missing: {path}")
    paths = [path]
    if recursive and path.is_dir() and not path.is_symlink():
        paths.extend(path.rglob("*"))
    for item in paths:
        stat = item.lstat()
        if stat.st_uid != uid or stat.st_gid != gid:
            raise DeploymentError(f"application ownership mismatch: {item}")
    return len(paths)


def _backup_database(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise DeploymentError("database backup destination already exists")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    published = False
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination_connection = sqlite3.connect(temporary)
        source_connection.backup(destination_connection)
        destination_connection.commit()
        destination_connection.close()
        destination_connection = None
        source_connection.close()
        source_connection = None
        os.chmod(temporary, 0o600)
        _require_quick_check(temporary)
        if os.name != "nt":
            descriptor = os.open(temporary, os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        temporary.replace(destination)
        published = True
        if os.name != "nt":
            descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except BaseException:
        if published:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        temporary.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            Path(f"{temporary}{suffix}").unlink(missing_ok=True)


def _capture_database_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"existed": False, "createdByDeployment": False}
    info = path.stat()
    return {
        "existed": True,
        "createdByDeployment": False,
        "mode": info.st_mode & 0o777,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "device": info.st_dev,
        "inode": info.st_ino,
    }


def _restore_database(
    database: Path,
    backup: Path | None,
    state: dict[str, Any],
) -> None:
    sidecars = [Path(f"{database}{suffix}") for suffix in ("-wal", "-shm")]
    if not state.get("existed"):
        if not state.get("createdByDeployment"):
            return
        for path in [*sidecars, database]:
            path.unlink(missing_ok=True)
        return
    if backup is None or not backup.is_file():
        raise DeploymentError("existing database backup is missing")
    for path in sidecars:
        path.unlink(missing_ok=True)
    shutil.copy2(backup, database)
    os.chmod(database, int(state["mode"]))
    if hasattr(os, "chown"):
        os.chown(database, int(state["uid"]), int(state["gid"]))
    _require_quick_check(database)


def _database_path(root: Path, config_path: Path) -> Path:
    config = _read_json(config_path)
    value = str(_object(config.get("storage")).get("databasePath") or "data/bot.db")
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise DeploymentError("database path escaped runtime root")
    return resolved


def _initialize_database(
    venv: Path,
    source_root: Path,
    database: Path,
    user: str,
    group: str,
    *,
    prepare_parent: bool = False,
) -> None:
    if prepare_parent:
        _prepare_service_directory(database.parent, user, group)
    backup_dir = database.parent / "backups"
    _prepare_service_directory(backup_dir, user, group)
    code = (
        "import asyncio,sys; "
        "from app.storage.database import init_database; "
        "asyncio.run(init_database(sys.argv[1], backup_dir=sys.argv[2]))"
    )
    _run_as(
        user,
        group,
        [
            str(venv / "bin/python"),
            "-c",
            code,
            str(database),
            str(backup_dir),
        ],
        cwd=source_root,
        env={"PYTHONPATH": str(source_root)},
    )


def _prepare_service_directory(path: Path, user: str, group: str) -> None:
    if path.is_symlink():
        raise DeploymentError("service-writable directory must not be a symlink")
    path.mkdir(mode=0o750, exist_ok=True)
    os.chmod(path, 0o750)
    shutil.chown(path, user, group)


def _write_backup_manifest(**values) -> dict[str, Any]:
    backup: Path = values["backup"]
    files = []
    for path in sorted(item for item in backup.rglob("*") if item.is_file()):
        files.append(
            {
                "path": path.relative_to(backup).as_posix(),
                "sizeBytes": path.stat().st_size,
                "sha256": _file_sha(path),
            }
        )
    manifest = {
        "schemaVersion": 1,
        "createdAt": datetime.now(UTC).isoformat(),
        "planHash": values["plan"]["planHash"],
        "commit": values["plan"]["source"]["commit"],
        "serviceUser": values["service_user"],
        "serviceGroup": values["service_group"],
        "databasePath": str(values["database_path"]) if values["database_path"] else None,
        "databaseBackup": str(values["database_backup"]) if values["database_backup"] else None,
        "databaseState": values["database_state"],
        "oldVenv": values["old_venv"],
        "videoCache": values["cache_state"],
        "napcat": values["pre_napcat"],
        "historicalTts": values["pre_tts"],
        "files": files,
    }
    _write_json(backup / "manifest.json", manifest)
    return manifest


def _verify_backup_manifest(backup: Path, manifest: dict[str, Any]) -> None:
    for item in manifest["files"]:
        path = backup / item["path"]
        if not path.is_file() or path.stat().st_size != item["sizeBytes"] or _file_sha(path) != item["sha256"]:
            raise DeploymentError(f"backup manifest verification failed: {item['path']}")


def _sync_release(release: Path, root: Path, user: str, group: str) -> None:
    script = r'''
import json
import shutil
import stat
import sys
from pathlib import Path

release = Path(sys.argv[1])
root = Path(sys.argv[2])
preserve_top = set(json.loads(sys.argv[3]))
preserve_config = set(json.loads(sys.argv[4]))
preserve_runtime_files = set(json.loads(sys.argv[5]))
if preserve_runtime_files != {"scripts/server/moss_tts_adapter.py"}:
    raise SystemExit("unexpected preserved runtime file policy")
adapter_relative = Path("server/moss_tts_adapter.py")
adapter_path = Path("scripts") / adapter_relative

def lstat(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None

def is_directory(info):
    return info is not None and stat.S_ISDIR(info.st_mode)

def require_directory(path, label, *, create=False):
    info = lstat(path)
    if info is None:
        if not create:
            return False
        path.mkdir()
        return True
    if stat.S_ISLNK(info.st_mode):
        raise SystemExit(f"{label} is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise SystemExit(f"{label} is not a directory")
    return True

def remove_path(path):
    info = lstat(path)
    if info is None:
        return
    if stat.S_ISDIR(info.st_mode):
        for child in path.iterdir():
            remove_path(child)
        path.rmdir()
    else:
        path.unlink()

def contains_adapter(relative):
    return tuple(relative.parts) == tuple(adapter_relative.parts[:len(relative.parts)])

def adapter_snapshot():
    current = root
    for component in adapter_path.parts[:-1]:
        current = current / component
        info = lstat(current)
        if info is None:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SystemExit("historical TTS runtime adapter parent is unsafe")
    adapter = root / adapter_path
    info = lstat(adapter)
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise SystemExit("historical TTS runtime adapter is a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit("historical TTS runtime adapter is not a regular file")
    return (info.st_dev, info.st_ino, adapter.read_bytes())

def prune_scripts(directory, relative=Path()):
    require_directory(directory, "runtime scripts directory")
    for child in directory.iterdir():
        child_relative = relative / child.name
        if child_relative == adapter_relative:
            if adapter_snapshot() is None:
                raise SystemExit("historical TTS runtime adapter disappeared")
        elif contains_adapter(child_relative):
            require_directory(child, "historical TTS runtime adapter parent")
            prune_scripts(child, child_relative)
        else:
            remove_path(child)

def copy_release_tree(source, destination, relative):
    info = lstat(source)
    if info is None:
        raise SystemExit(f"release path disappeared: {relative}")
    if stat.S_ISLNK(info.st_mode):
        raise SystemExit(f"release path is a symlink: {relative}")
    if stat.S_ISDIR(info.st_mode):
        destination_info = lstat(destination)
        if destination_info is None:
            destination.mkdir()
        elif stat.S_ISLNK(destination_info.st_mode):
            raise SystemExit(f"runtime destination is a symlink: {relative}")
        elif not stat.S_ISDIR(destination_info.st_mode):
            if contains_adapter(relative):
                raise SystemExit("release conflicts with the historical TTS runtime adapter")
            remove_path(destination)
            destination.mkdir()
        for child in source.iterdir():
            child_relative = relative / child.name
            if child_relative == adapter_relative:
                continue
            copy_release_tree(child, destination / child.name, child_relative)
        return
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"release path is not a regular file: {relative}")
    if contains_adapter(relative):
        raise SystemExit("release conflicts with the historical TTS runtime adapter")
    destination_info = lstat(destination)
    if destination_info is not None:
        if stat.S_ISLNK(destination_info.st_mode):
            raise SystemExit(f"runtime destination is a symlink: {relative}")
        remove_path(destination)
    shutil.copy2(source, destination, follow_symlinks=False)

root_info = lstat(root)
release_info = lstat(release)
if not is_directory(root_info) or stat.S_ISLNK(root_info.st_mode):
    raise SystemExit("runtime root is unsafe")
if not is_directory(release_info) or stat.S_ISLNK(release_info.st_mode):
    raise SystemExit("release root is unsafe")
before_adapter = adapter_snapshot()
for item in root.iterdir():
    if item.name in preserve_top:
        continue
    if item.name == "scripts":
        continue
    if item.name == "config" and item.is_dir():
        for child in item.iterdir():
            if child.name in preserve_config or child.name.endswith(".local.json"):
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        continue
    shutil.rmtree(item) if item.is_dir() else item.unlink()
for item in release.iterdir():
    if item.name in preserve_top or item.name == ".env":
        continue
    if item.name == "scripts":
        continue
    target = root / item.name
    if item.name == "config" and item.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for child in item.iterdir():
            if child.name in preserve_config or child.name.endswith(".local.json"):
                continue
            destination = target / child.name
            shutil.copytree(child, destination) if child.is_dir() else shutil.copy2(child, destination)
        continue
    shutil.copytree(item, target) if item.is_dir() else shutil.copy2(item, target)

runtime_scripts = root / "scripts"
release_scripts = release / "scripts"
if lstat(runtime_scripts) is not None:
    require_directory(runtime_scripts, "runtime scripts directory")
    prune_scripts(runtime_scripts)
if lstat(release_scripts) is not None:
    require_directory(release_scripts, "release scripts directory")
    if lstat(runtime_scripts) is None:
        runtime_scripts.mkdir()
    copy_release_tree(release_scripts, runtime_scripts, Path())

after_adapter = adapter_snapshot()
if after_adapter != before_adapter:
    raise SystemExit("historical TTS runtime adapter identity or content changed")
'''
    _run_as(
        user,
        group,
        [
            "python3",
            "-c",
            script,
            str(release),
            str(root),
            json.dumps(sorted(PRESERVE_TOP), separators=(",", ":")),
            json.dumps(sorted(PRESERVE_CONFIG), separators=(",", ":")),
            json.dumps(sorted(PRESERVE_RUNTIME_FILES), separators=(",", ":")),
        ],
    )


def _install_private_file(source: Path, destination: Path, user: str, group: str) -> None:
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    _run(["install", "-m", "0600", "-o", user, "-g", group, str(source), str(temporary)])
    os.replace(temporary, destination)


def _capture_cache(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise DeploymentError("video cache path must not be a symlink")
    if not path.exists():
        return {"existed": False, "createdByDeployment": False}
    stat = path.stat()
    return {
        "existed": True,
        "createdByDeployment": False,
        "mode": stat.st_mode & 0o7777,
        "uid": stat.st_uid,
        "gid": stat.st_gid,
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def _prepare_cache(
    path: Path,
    user: str,
    group: str,
    plan_hash: str,
    state: dict[str, Any],
    *,
    napcat_gid: int,
) -> None:
    if path.is_symlink():
        raise DeploymentError("video cache path became a symlink")
    if not state.get("existed"):
        if path.exists():
            raise DeploymentError("video cache appeared after pre-deploy capture")
        try:
            _run(
                [
                    "install",
                    "-d",
                    "-m",
                    "2750",
                    "-o",
                    user,
                    "-g",
                    str(napcat_gid),
                    str(path),
                ]
            )
        finally:
            if path.is_dir() and not path.is_symlink():
                state["createdByDeployment"] = True
                info = path.stat()
                state["device"] = info.st_dev
                state["inode"] = info.st_ino
        (path / f".qq-bot-deploy-{plan_hash}").touch(mode=0o600)
    else:
        if not path.is_dir():
            raise DeploymentError("existing video cache is no longer a directory")
        _require_cache_identity(path, state)
        shutil.chown(path, user, napcat_gid)
        os.chmod(path, 0o2750)
    _run_as(user, group, ["test", "-w", str(path)])


def _finish_cache(path: Path, plan_hash: str, state: dict[str, Any]) -> None:
    _require_cache_identity(path, state)
    (path / f".qq-bot-deploy-{plan_hash}").unlink(missing_ok=True)


def _require_cache_identity(path: Path, state: dict[str, Any]) -> None:
    if path.is_symlink() or not path.is_dir():
        raise DeploymentError("video cache identity changed")
    info = path.stat()
    if (
        info.st_dev != state.get("device")
        or info.st_ino != state.get("inode")
    ):
        raise DeploymentError("video cache identity changed")


def _stable_readiness(service: str, root: Path, started_at: datetime) -> dict[str, Any]:
    stable_pid = None
    stable_restarts = None
    consecutive = 0
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        time.sleep(5)
        active = _is_active(service)
        pid = int(_run(["systemctl", "show", service, "--property=MainPID", "--value"]).stdout.strip() or 0)
        restarts = int(_run(["systemctl", "show", service, "--property=NRestarts", "--value"]).stdout.strip() or 0)
        ready = _runtime_probe_ready(
            root=root,
            user=_run(
                ["systemctl", "show", service, "--property=User", "--value"]
            ).stdout.strip(),
            group=_run(
                ["systemctl", "show", service, "--property=Group", "--value"]
            ).stdout.strip()
            or None,
        )
        if active and pid > 0 and ready:
            if stable_pid in {None, pid} and stable_restarts in {None, restarts}:
                stable_pid = pid
                stable_restarts = restarts
                consecutive += 1
                if consecutive >= 3:
                    return {"active": True, "mainPid": pid, "nRestarts": restarts, "consecutiveReady": consecutive}
                continue
        stable_pid = pid if active else None
        stable_restarts = restarts if active else None
        consecutive = 0
    raise DeploymentError("stable runtime readiness gate timed out")


def _runtime_probe_ready(*, root: Path, user: str, group: str | None) -> bool:
    python = str(root / ".venv/bin/python")
    status_tool = "tools/inspect_runtime_status.py"
    modern = _run_as(
        user,
        group,
        [python, status_tool, "--limit", "5", "--summary", "--require-ready"],
        cwd=root,
        check=False,
        quiet=True,
    )
    if modern.returncode == 0:
        return True
    error = modern.stderr or ""
    if not (
        "unrecognized arguments" in error
        and "--summary" in error
        and "--require-ready" in error
    ):
        return False
    legacy = _run_as(
        user,
        group,
        [python, status_tool, "--limit", "5"],
        cwd=root,
        check=False,
        quiet=True,
    )
    if legacy.returncode != 0:
        return False
    try:
        payload = json.loads(legacy.stdout)
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        payload.get("qq_bot_listening") is True
        and payload.get("napcat_to_bot_established") is True
    )


def _journal_has_traceback(service: str, started_at: datetime) -> bool:
    output = _run(
        ["journalctl", "-u", service, "--since", started_at.isoformat(), "--no-pager", "-o", "cat"],
        check=False,
    ).stdout
    return "Traceback (most recent call last)" in output


def _rollback(**values) -> bool:
    root: Path = values["root"]
    backup: Path = values["backup"]
    service = values["service"]
    errors: list[str] | None = values.get("errors")
    if errors is None:
        errors = []
    if not _quiesce_service(service, root, errors):
        return False

    _run_rollback_step(
        "restore_code",
        lambda: _restore_code(root, backup / "code.tar"),
        errors,
    )

    def restore_private() -> None:
        private = backup / "private"
        for name in (".env", "config/config.json", "config/persona_profile.local.json"):
            source = private / name
            destination = root / name
            if source.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                shutil.chown(
                    destination,
                    values["service_user"],
                    values["service_group"],
                )

    _run_rollback_step("restore_private", restore_private, errors)

    def restore_database() -> None:
        database = values["database_path"]
        if database is None:
            return
        _restore_database(
            database,
            values["database_backup"],
            values["database_state"],
        )

    _run_rollback_step("restore_database", restore_database, errors)
    _run_rollback_step(
        "restore_venv",
        lambda: _restore_venv(
            root,
            backup,
            values["old_venv"],
            switch_started=bool(values.get("venv_switch_started")),
        ),
        errors,
    )
    _run_rollback_step(
        "restore_cache",
        lambda: _restore_cache(values["cache_path"], values["cache_state"]),
        errors,
    )
    if errors:
        _quiesce_service(service, root, errors)
        return False
    return _restore_service_state(
        service,
        root,
        was_active=bool(values["service_was_active"]),
        errors=errors,
    )


def _quiesce_service(service: str, root: Path, errors: list[str]) -> bool:
    result = _run(["systemctl", "stop", service], check=False)
    if result.returncode != 0:
        errors.append(f"quiesce: systemctl stop exited {result.returncode}")
    if _is_active(service):
        errors.append("quiesce: service remained active")
    try:
        listener_host, listener_port = _onebot_listener(root / "config/config.json")
    except BaseException:
        # Rollback must still be able to replace a damaged runtime config after
        # systemd has successfully stopped the service.
        pass
    else:
        if _tcp_listening(listener_host, listener_port):
            errors.append("quiesce: OneBot listener remained reachable")
    return not errors


def _run_rollback_step(
    label: str,
    action,
    errors: list[str],
) -> None:
    try:
        action()
    except BaseException as exc:
        errors.append(f"{label}: {_safe_error(exc)}")


def _restore_service_state(
    service: str,
    root: Path,
    *,
    was_active: bool,
    errors: list[str],
) -> bool:
    initial_error_count = len(errors)
    if not was_active:
        if _is_active(service):
            _quiesce_service(service, root, errors)
        return len(errors) == initial_error_count

    started_at = datetime.now(UTC)
    if not _is_active(service):
        result = _run(["systemctl", "start", service], check=False)
        if result.returncode != 0:
            errors.append(f"restart: systemctl start exited {result.returncode}")
    if len(errors) == initial_error_count:
        try:
            _stable_readiness(service, root, started_at)
        except BaseException as exc:
            errors.append(f"readiness: {_safe_error(exc)}")
    if len(errors) != initial_error_count:
        _run(["systemctl", "stop", service], check=False)
        if _is_active(service):
            errors.append("fail_closed_stop: service remained active")
    return len(errors) == initial_error_count


def _verify_rollback_dependencies(
    *,
    service: str,
    root: Path,
    napcat_container: str,
    expected_napcat: dict[str, Any],
    expected_tts: dict[str, Any],
    errors: list[str],
) -> bool:
    initial_error_count = len(errors)
    try:
        if _napcat_state(napcat_container) != expected_napcat:
            raise DeploymentError("NapCat state changed during rollback")
    except BaseException as exc:
        errors.append(f"rollback_napcat: {_safe_error(exc)}")
    try:
        if _tts_state() != expected_tts:
            raise DeploymentError("historical TTS state changed during rollback")
    except BaseException as exc:
        errors.append(f"rollback_historical_tts: {_safe_error(exc)}")
    if len(errors) != initial_error_count:
        _quiesce_service(service, root, errors)
        return False
    return True


def _restore_code(root: Path, code_tar: Path) -> None:
    for item in root.iterdir():
        if item.name in PRESERVE_TOP:
            continue
        if item.name == "config" and item.is_dir():
            for child in item.iterdir():
                if child.name in PRESERVE_CONFIG or child.name.endswith(".local.json"):
                    continue
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            continue
        shutil.rmtree(item) if item.is_dir() else item.unlink()
    with tarfile.open(code_tar) as bundle:
        bundle.extractall(root, filter="fully_trusted", numeric_owner=True)


def _restore_venv(
    root: Path,
    backup: Path,
    old: dict[str, Any],
    *,
    switch_started: bool,
) -> None:
    current = root / ".venv"
    if not switch_started and _venv_matches(current, old):
        return
    if current.is_symlink() or current.is_file():
        current.unlink()
    elif current.is_dir():
        shutil.rmtree(current)
    if old.get("kind") == "directory":
        archive = backup / str(old.get("backup") or "venv.tar")
        try:
            with tarfile.open(archive) as bundle:
                bundle.extractall(root, filter="fully_trusted", numeric_owner=True)
        except BaseException:
            if current.exists():
                shutil.rmtree(current)
            live = backup / "venv-live"
            if not live.is_dir():
                raise
            live.rename(current)
    elif old.get("kind") == "symlink":
        current.symlink_to(str(old["target"]), target_is_directory=True)
    else:
        raise DeploymentError("unknown previous venv kind")
    if not _venv_matches(current, old):
        raise DeploymentError("restored venv does not match the pre-deploy state")


def _venv_matches(current: Path, old: dict[str, Any]) -> bool:
    if old.get("kind") == "symlink":
        return current.is_symlink() and os.readlink(current) == old.get("target")
    if old.get("kind") == "directory":
        return (
            current.is_dir()
            and not current.is_symlink()
            and _tree_digest(current) == old.get("treeDigest")
        )
    return False


def _restore_cache(path: Path, state: dict[str, Any]) -> None:
    if not state.get("existed"):
        if not state.get("createdByDeployment"):
            return
        _require_cache_identity(path, state)
        entries = list(path.iterdir())
        if entries and not (
            len(entries) == 1 and entries[0].name.startswith(".qq-bot-deploy-")
        ):
            raise DeploymentError(
                "deployment-created video cache contains files; preserving it"
            )
        path.rmdir() if not entries else shutil.rmtree(path)
        return
    _require_cache_identity(path, state)
    if hasattr(os, "chown"):
        os.chown(path, int(state["uid"]), int(state["gid"]))
    os.chmod(path, int(state["mode"]))


def _extract_release(archive: Path, release: Path) -> None:
    with tarfile.open(archive) as bundle:
        unsupported = [
            member.name
            for member in bundle.getmembers()
            if not (member.isfile() or member.isdir())
        ]
        if unsupported:
            raise DeploymentError(
                f"release archive contains unsupported entries: {unsupported[0]}"
            )
        bundle.extractall(release, filter="data")
    for required in ("pyproject.toml", "bot.py", "tools/inspect_runtime_status.py"):
        if not (release / required).is_file():
            raise DeploymentError(f"release archive missing {required}")


def _validate_plan_hash(plan: dict[str, Any], expected: str) -> None:
    payload = {key: value for key, value in plan.items() if key != "planHash"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    actual = hashlib.sha256(encoded).hexdigest()
    if plan.get("planHash") != actual or expected != actual:
        raise DeploymentError("plan hash mismatch inside WSL")


def _validate_plan_fresh(
    plan: dict[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    try:
        created_at = datetime.fromisoformat(str(plan.get("createdAt") or ""))
        expires_at = datetime.fromisoformat(str(plan.get("expiresAt") or ""))
    except ValueError as exc:
        raise DeploymentError("plan validity is invalid inside WSL") from exc
    if created_at.tzinfo is None or expires_at.tzinfo is None:
        raise DeploymentError("plan validity is invalid inside WSL")
    created = created_at.astimezone(UTC)
    expires = expires_at.astimezone(UTC)
    if expires - created != timedelta(minutes=PLAN_VALIDITY_MINUTES):
        raise DeploymentError("plan must have exactly 30-minute validity inside WSL")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if current < created:
        raise DeploymentError("plan creation time is in the future inside WSL")
    if current > expires:
        raise DeploymentError("plan expired inside WSL")


def _wrapper_digest(paths: tuple[tuple[str, Path], ...]) -> str:
    digest = hashlib.sha256()
    for label, path in paths:
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _require_wrapper_sha(
    paths: tuple[tuple[str, Path], ...],
    expected: str,
) -> None:
    if not expected or any(not path.is_file() for _, path in paths):
        raise DeploymentError("deployment wrapper hash inputs are missing")
    if _wrapper_digest(paths) != expected:
        raise DeploymentError("deployment wrapper hash mismatch inside WSL")


def _require_sha(path: Path, expected: str, label: str) -> None:
    if not path.is_file() or _file_sha(path) != expected:
        raise DeploymentError(f"{label} hash mismatch")


def _require_quick_check(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise DeploymentError(f"SQLite quick_check failed: {path}")
    finally:
        connection.close()


def _table_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in names}
    finally:
        connection.close()


def _changed_existing_counts(before: dict[str, int], after: dict[str, int]) -> bool:
    return any(
        after.get(name) != count
        for name, count in before.items()
        if name != "schema_migrations"
    )


def _schema_version(path: Path) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        return int(row[0])
    finally:
        connection.close()


def _run_as(
    user: str,
    group: str | None,
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    prefix = ["runuser", "-u", user]
    if group:
        prefix.extend(["-g", group])
    prefix.extend(["--", "env"])
    if env:
        prefix.extend(f"{key}={value}" for key, value in env.items())
    prefix.extend(command)
    return _run(prefix, cwd=cwd, check=check, quiet=quiet)


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if result.stdout and not quiet:
        print(result.stdout, end="")
    if result.stderr and not quiet:
        print(result.stderr, end="", file=sys.stderr)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
    return result


def _is_active(service: str) -> bool:
    return _run(["systemctl", "is-active", "--quiet", service], check=False).returncode == 0


def _systemd_state(service: str) -> str:
    return normalize_systemd_state(
        _run(["systemctl", "is-active", service], check=False).stdout
    )


def _tcp_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _onebot_listener(config_path: Path) -> tuple[str, int]:
    payload = _read_json(config_path)
    onebot = _object(payload.get("onebot"))
    host = str(onebot.get("host") or "").strip()
    port = onebot.get("port")
    if not host or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise DeploymentError("runtime OneBot listener is invalid")
    return host, port


def _read_os_release() -> dict[str, str]:
    values = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DeploymentError(f"expected JSON object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _file_sha(path: Path | None) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None


def _unit_digest(paths: list[Path]) -> str | None:
    if not paths:
        return None
    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file():
            raise DeploymentError(f"systemd unit file is missing: {path}")
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        stat = path.lstat()
        digest.update(str(stat.st_mode & 0o7777).encode())
        digest.update(str(stat.st_uid).encode())
        digest.update(str(stat.st_gid).encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(str(path.stat().st_size).encode())
            digest.update((_file_sha(path) or "").encode())
    return digest.hexdigest()


def _json_digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text[:500] or type(exc).__name__


def _user_name(uid: int) -> str:
    import pwd

    return pwd.getpwuid(uid).pw_name


def _user_id(name: str) -> int:
    import pwd

    return pwd.getpwnam(name).pw_uid


def _group_name(gid: int) -> str:
    import grp

    return grp.getgrgid(gid).gr_name


def _group_label(gid: int) -> str:
    try:
        return _group_name(gid)
    except KeyError:
        return str(gid)


def _group_id(name: str) -> int:
    import grp

    return grp.getgrnam(name).gr_gid


def _chown_tree(root: Path, user: str, group: str) -> None:
    shutil.chown(root, user, group)
    for path in root.rglob("*"):
        shutil.chown(path, user, group)


if __name__ == "__main__":
    raise SystemExit(main())

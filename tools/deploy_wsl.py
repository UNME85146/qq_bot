from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

if __package__:
    from .migrate_wsl_config import validate_history_profile, write_json_atomic
else:  # The immutable transaction bundle keeps wrappers in one directory.
    from migrate_wsl_config import validate_history_profile, write_json_atomic


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLAN_SCHEMA_VERSION = 1
PLAN_VALIDITY_MINUTES = 30
DEFAULT_DISTRO = "Ubuntu-24.04"
DEFAULT_ROOT = "/opt/qq_bot"
DEFAULT_SERVICE = "qq-bot.service"
DEFAULT_NAPCAT_CONTAINER = "napcat"
DEFAULT_VIDEO_CACHE_HOST = "/opt/napcat/cache/qq-bot-media"
DEFAULT_VIDEO_CACHE_CONTAINER = "/app/napcat/cache/qq-bot-media"
WSL_JOURNAL_PATH = "/var/lib/qq-bot-deploy/current.json"
WRAPPER_PATHS = (
    "tools/deploy_wsl.py",
    "tools/migrate_wsl_config.py",
    "tools/wsl_deploy_transaction.py",
    "scripts/deploy_wsl.ps1",
)
AUTHORIZED_TARGET = {
    "distro": DEFAULT_DISTRO,
    "root": DEFAULT_ROOT,
    "service": DEFAULT_SERVICE,
    "napcatContainer": DEFAULT_NAPCAT_CONTAINER,
    "videoCacheHost": DEFAULT_VIDEO_CACHE_HOST,
    "videoCacheContainer": DEFAULT_VIDEO_CACHE_CONTAINER,
}


def normalize_systemd_state(value: Any) -> str:
    return str(value or "").strip() or "inactive"


def normalize_runtime_root_mode(value: Any) -> str:
    try:
        mode = value if isinstance(value, int) else int(str(value).strip(), 8)
    except (TypeError, ValueError) as exc:
        raise ValueError("runtime root mode is not octal") from exc
    return format(mode & 0o7777, "o")


def normalize_runtime_fingerprint(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    if "serviceState" in normalized:
        normalized["serviceState"] = (
            "active"
            if normalize_systemd_state(normalized["serviceState"]) == "active"
            else "inactive"
        )
    if "ttsServiceState" in normalized:
        normalized["ttsServiceState"] = normalize_systemd_state(
            normalized["ttsServiceState"]
        )
    if "runtimeRootMode" in normalized:
        normalized["runtimeRootMode"] = normalize_runtime_root_mode(
            normalized["runtimeRootMode"]
        )
    return normalized


def historical_tts_adapter_preflight_error(
    *,
    service_state: Any,
    adapter_kind: Any,
) -> str | None:
    if adapter_kind == "symlink":
        return "historical TTS runtime adapter must not be a symlink"
    if adapter_kind not in {"regular", "missing"}:
        return "historical TTS runtime adapter must be a regular file"
    if normalize_systemd_state(service_state) == "active" and adapter_kind != "regular":
        return "active historical TTS runtime adapter is missing"
    return None


class PlanDriftError(RuntimeError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if parsed.action == "apply" and not parsed.plan_hash:
            self.error("apply requires --plan-hash")
        return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description="Plan or apply a transaction-safe WSL deployment.")
    parser.add_argument("action", nargs="?", choices=("plan", "apply"), default="plan")
    parser.add_argument("--plan", default=".deploy/wsl-plan.json")
    parser.add_argument("--plan-hash", default="")
    parser.add_argument("--distro", default=DEFAULT_DISTRO)
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--napcat-container", default=DEFAULT_NAPCAT_CONTAINER)
    parser.add_argument("--video-cache-host", default=DEFAULT_VIDEO_CACHE_HOST)
    parser.add_argument("--video-cache-container", default=DEFAULT_VIDEO_CACHE_CONTAINER)
    parser.add_argument(
        "--persona-profile",
        default="config/persona_profile.local.json",
    )
    return parser


def canonical_plan_hash(plan: dict[str, Any]) -> str:
    payload = {key: value for key, value in plan.items() if key != "planHash"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_plan(
    *,
    commit: str,
    tree: str,
    archive_sha256: str,
    wrapper_sha256: str,
    dependency_lock_sha256: str,
    replacement_persona_sha256: str,
    runtime_fingerprints: dict[str, Any],
    created_at: datetime | None = None,
    expires_minutes: int = PLAN_VALIDITY_MINUTES,
    distro: str = DEFAULT_DISTRO,
    root: str = DEFAULT_ROOT,
    service: str = DEFAULT_SERVICE,
    napcat_container: str = DEFAULT_NAPCAT_CONTAINER,
    video_cache_host: str = DEFAULT_VIDEO_CACHE_HOST,
    video_cache_container: str = DEFAULT_VIDEO_CACHE_CONTAINER,
) -> dict[str, Any]:
    if expires_minutes != PLAN_VALIDITY_MINUTES:
        raise ValueError("deployment plans must have exactly 30 minutes of validity")
    created = (created_at or datetime.now(UTC)).astimezone(UTC)
    target = {
        "distro": distro,
        "root": root,
        "service": service,
        "napcatContainer": napcat_container,
        "videoCacheHost": video_cache_host,
        "videoCacheContainer": video_cache_container,
    }
    if target != AUTHORIZED_TARGET:
        raise ValueError("deployment target is outside the authorized WSL target")
    plan: dict[str, Any] = {
        "schemaVersion": PLAN_SCHEMA_VERSION,
        "createdAt": created.isoformat(),
        "expiresAt": (created + timedelta(minutes=expires_minutes)).isoformat(),
        "source": {
            "commit": commit,
            "tree": tree,
            "archiveSha256": archive_sha256,
            "wrapperSha256": wrapper_sha256,
            "dependencyLockSha256": dependency_lock_sha256,
            "replacementPersonaSha256": replacement_persona_sha256,
        },
        "target": target,
        "runtimeFingerprints": runtime_fingerprints,
        "scope": {
            "restartNapCat": False,
            "mutateHistoricalTts": False,
            "sendSyntheticQqMessages": False,
            "publishGithub": False,
        },
    }
    plan["planHash"] = canonical_plan_hash(plan)
    return plan


def validate_plan(
    plan: dict[str, Any],
    *,
    expected_hash: str,
    current_commit: str,
    current_tree: str,
    current_archive_sha256: str,
    current_wrapper_sha256: str,
    current_dependency_lock_sha256: str,
    current_replacement_persona_sha256: str,
    current_runtime_fingerprints: dict[str, Any],
    now: datetime | None = None,
) -> None:
    if plan.get("schemaVersion") != PLAN_SCHEMA_VERSION:
        raise PlanDriftError("unsupported plan schema")
    actual_hash = canonical_plan_hash(plan)
    if plan.get("planHash") != actual_hash or expected_hash != actual_hash:
        raise PlanDriftError("plan hash mismatch")
    _validate_plan_window(plan, now=now)
    source = plan.get("source") if isinstance(plan.get("source"), dict) else {}
    expected_source = {
        "commit": current_commit,
        "tree": current_tree,
        "archiveSha256": current_archive_sha256,
        "wrapperSha256": current_wrapper_sha256,
        "dependencyLockSha256": current_dependency_lock_sha256,
        "replacementPersonaSha256": current_replacement_persona_sha256,
    }
    if source != expected_source:
        raise PlanDriftError("source artifact drift")
    if plan.get("target") != AUTHORIZED_TARGET:
        raise PlanDriftError("deployment target is outside the authorized WSL target")
    if plan.get("runtimeFingerprints") != current_runtime_fingerprints:
        raise PlanDriftError("runtime fingerprint drift")
    if plan.get("scope") != {
        "restartNapCat": False,
        "mutateHistoricalTts": False,
        "sendSyntheticQqMessages": False,
        "publishGithub": False,
    }:
        raise PlanDriftError("deployment scope drift")


def main() -> int:
    args = build_parser().parse_args()
    _ensure_clean_worktree()
    plan_path = _project_path(args.plan)
    profile_path = _project_path(args.persona_profile)
    profile_bytes = profile_path.read_bytes()
    validate_history_profile(json.loads(profile_bytes.decode("utf-8")))

    if args.action == "plan":
        inputs = _current_inputs(args, profile_bytes=profile_bytes)
        plan_inputs = {
            key: value
            for key, value in inputs.items()
            if key
            not in {"archive_bytes", "wrapper_bytes", "dependency_lock_bytes"}
        }
        plan = build_plan(
            **plan_inputs,
            distro=args.distro,
            root=args.root,
            service=args.service,
            napcat_container=args.napcat_container,
            video_cache_host=args.video_cache_host,
            video_cache_container=args.video_cache_container,
        )
        write_json_atomic(plan_path, plan)
        print(f"plan={plan_path}")
        print(f"planHash={plan['planHash']}")
        print(f"commit={plan['source']['commit']}")
        print(f"expiresAt={plan['expiresAt']}")
        print(f"target.distro={plan['target']['distro']}")
        print(f"target.root={plan['target']['root']}")
        print(f"target.service={plan['target']['service']}")
        print(f"target.napcatContainer={plan['target']['napcatContainer']}")
        print(f"target.videoCacheHost={plan['target']['videoCacheHost']}")
        print(f"target.videoCacheContainer={plan['target']['videoCacheContainer']}")
        print("serviceTransition=active-or-inactive->validated-restart-or-original-state")
        print("scope=restartNapCat:false,mutateHistoricalTts:false,sendSyntheticQqMessages:false,publishGithub:false")
        return 0

    plan_bytes = plan_path.read_bytes()
    plan = json.loads(plan_bytes.decode("utf-8"))
    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    apply_args = argparse.Namespace(
        distro=str(target.get("distro") or ""),
        root=str(target.get("root") or ""),
        service=str(target.get("service") or ""),
        napcat_container=str(target.get("napcatContainer") or ""),
        video_cache_host=str(target.get("videoCacheHost") or ""),
        video_cache_container=str(target.get("videoCacheContainer") or ""),
    )
    inputs = _current_inputs(apply_args, profile_bytes=profile_bytes)
    validate_plan(
        plan,
        expected_hash=args.plan_hash,
        current_commit=inputs["commit"],
        current_tree=inputs["tree"],
        current_archive_sha256=inputs["archive_sha256"],
        current_wrapper_sha256=inputs["wrapper_sha256"],
        current_dependency_lock_sha256=inputs["dependency_lock_sha256"],
        current_replacement_persona_sha256=inputs["replacement_persona_sha256"],
        current_runtime_fingerprints=inputs["runtime_fingerprints"],
    )
    receipt, apply_status = _apply_plan(
        plan,
        plan_bytes=plan_bytes,
        profile_bytes=profile_bytes,
        archive_bytes=inputs["archive_bytes"],
        wrapper_bytes=inputs["wrapper_bytes"],
        dependency_lock_bytes=inputs["dependency_lock_bytes"],
    )
    receipt_path = PROJECT_ROOT / ".deploy" / "wsl-apply-receipt.json"
    write_json_atomic(receipt_path, receipt)
    print(f"receipt={receipt_path}")
    print(f"result={receipt.get('result', 'unknown')}")
    return apply_status


def _validate_plan_window(
    plan: dict[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    try:
        created_at = datetime.fromisoformat(str(plan.get("createdAt") or ""))
        expires_at = datetime.fromisoformat(str(plan.get("expiresAt") or ""))
    except ValueError as exc:
        raise PlanDriftError("plan validity timestamps are invalid") from exc
    if created_at.tzinfo is None or expires_at.tzinfo is None:
        raise PlanDriftError("plan validity timestamps are invalid")
    created = created_at.astimezone(UTC)
    expires = expires_at.astimezone(UTC)
    if expires - created != timedelta(minutes=PLAN_VALIDITY_MINUTES):
        raise PlanDriftError("plan must have exactly 30-minute validity")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if current < created:
        raise PlanDriftError("plan creation time is in the future")
    if current > expires:
        raise PlanDriftError("plan expired")


def _current_inputs(args, *, profile_bytes: bytes) -> dict[str, Any]:
    commit = _git("rev-parse", "HEAD").decode().strip()
    tree = _git("rev-parse", f"{commit}^{{tree}}").decode().strip()
    archive_bytes = _git("archive", "--format=tar", commit)
    wrapper_bytes = {
        path: _git("show", f"{commit}:{path}") for path in WRAPPER_PATHS
    }
    dependency_lock_bytes = _git("show", f"{commit}:requirements-wsl.lock")
    return {
        "commit": commit,
        "tree": tree,
        "archive_sha256": _sha256(archive_bytes),
        "wrapper_sha256": _wrapper_sha256(wrapper_bytes),
        "dependency_lock_sha256": _sha256(dependency_lock_bytes),
        "replacement_persona_sha256": _sha256(profile_bytes),
        "runtime_fingerprints": probe_wsl(
            distro=args.distro,
            root=args.root,
            service=args.service,
            napcat_container=args.napcat_container,
        ),
        "archive_bytes": archive_bytes,
        "wrapper_bytes": wrapper_bytes,
        "dependency_lock_bytes": dependency_lock_bytes,
    }


def probe_wsl(*, distro: str, root: str, service: str, napcat_container: str) -> dict[str, Any]:
    script = r'''
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

def run(*args, required=True):
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if required and result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"command failed: {args[0]}")
    return result.stdout.strip()

def sha(path):
    item = Path(path)
    return hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else None

def unit_sha(fragment, dropins):
    digest = hashlib.sha256()
    paths = [Path(value) for value in [fragment, *dropins] if value]
    if not paths:
        return None
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"systemd unit file is missing: {path}")
        digest.update(str(path).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()

def adapter_kind(path):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "regular" if stat.S_ISREG(mode) else "non_regular"

def napcat_identity(container):
    identities = {
        (int(parts[0]), int(parts[1]))
        for line in run("docker", "exec", container, "ps", "-eo", "uid=,gid=,comm=").splitlines()
        if len(parts := line.split(None, 2)) == 3 and parts[2] == "qq"
    }
    if len(identities) != 1:
        raise SystemExit("NapCat QQ runtime identity is unavailable or ambiguous")
    return next(iter(identities))

root = Path(os.environ["QQ_BOT_ROOT"]).resolve()
if not root.is_dir() or str(root) != os.environ["QQ_BOT_ROOT"]:
    raise SystemExit("runtime root identity mismatch")
config_path = root / "config" / "config.json"
config = json.loads(config_path.read_text(encoding="utf-8"))
profile_value = str(config.get("persona", {}).get("profilePath") or "persona_profile.local.json")
profile = Path(profile_value)
if not profile.is_absolute():
    cwd_candidate = root / profile
    profile = cwd_candidate if cwd_candidate.exists() or len(profile.parts) > 1 else config_path.parent / profile
profile = profile.resolve()
if not profile.is_relative_to(root):
    raise SystemExit("persona profile must remain inside runtime root")
fragment = run("systemctl", "show", os.environ["QQ_BOT_SERVICE"], "--property=FragmentPath", "--value")
dropins = run("systemctl", "show", os.environ["QQ_BOT_SERVICE"], "--property=DropInPaths", "--value").split()
napcat = json.loads(run("docker", "inspect", os.environ["QQ_BOT_NAPCAT"]))[0]
napcat_uid, napcat_gid = napcat_identity(os.environ["QQ_BOT_NAPCAT"])
tts_fragment = run("systemctl", "show", "qq-bot-tts.service", "--property=FragmentPath", "--value", required=False)
tts_dropins = run("systemctl", "show", "qq-bot-tts.service", "--property=DropInPaths", "--value", required=False).split()
tts_runtime_adapter = root / "scripts/server/moss_tts_adapter.py"
tts_runtime_adapter_kind = adapter_kind(tts_runtime_adapter)
tts_service_state = run("systemctl", "is-active", "qq-bot-tts.service", required=False) or "inactive"
payload = {
    "runtimeConfigSha256": sha(config_path),
    "runtimePersonaPath": str(profile),
    "runtimePersonaSha256": sha(profile),
    "runtimeRootOwner": run("stat", "-c", "%U:%G", str(root)),
    "runtimeRootMode": run("stat", "-c", "%a", str(root)),
    "serviceState": run("systemctl", "is-active", os.environ["QQ_BOT_SERVICE"], required=False) or "inactive",
    "serviceUser": run("systemctl", "show", os.environ["QQ_BOT_SERVICE"], "--property=User", "--value"),
    "serviceGroup": run("systemctl", "show", os.environ["QQ_BOT_SERVICE"], "--property=Group", "--value"),
    "serviceFragmentSha256": sha(fragment),
    "serviceUnitSha256": unit_sha(fragment, dropins),
    "napcatContainerId": napcat.get("Id"),
    "napcatStartedAt": napcat.get("State", {}).get("StartedAt"),
    "napcatRestartCount": napcat.get("RestartCount"),
    "napcatStatus": napcat.get("State", {}).get("Status"),
    "napcatRunning": napcat.get("State", {}).get("Running"),
    "napcatRuntimeUid": napcat_uid,
    "napcatRuntimeGid": napcat_gid,
    "ttsServiceState": tts_service_state,
    "ttsServiceFragmentSha256": sha(tts_fragment),
    "ttsServiceUnitSha256": unit_sha(tts_fragment, tts_dropins),
    "ttsMainPid": int(run("systemctl", "show", "qq-bot-tts.service", "--property=MainPID", "--value", required=False) or 0),
    "ttsNRestarts": int(run("systemctl", "show", "qq-bot-tts.service", "--property=NRestarts", "--value", required=False) or 0),
    "ttsActiveEnterTimestampMonotonic": int(run("systemctl", "show", "qq-bot-tts.service", "--property=ActiveEnterTimestampMonotonic", "--value", required=False) or 0),
    "ttsRuntimeAdapterSha256": sha(tts_runtime_adapter) if tts_runtime_adapter_kind == "regular" else None,
    "ttsRuntimeAdapterKind": tts_runtime_adapter_kind,
    "ffmpegVersion": run("/usr/bin/ffmpeg", "-version").splitlines()[0],
}
print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
'''
    result = _wsl(
        distro,
        [
            "env",
            f"QQ_BOT_ROOT={root}",
            f"QQ_BOT_SERVICE={service}",
            f"QQ_BOT_NAPCAT={napcat_container}",
            "python3",
            "-c",
            script,
        ],
    )
    fingerprint = json.loads(result.stdout.decode("utf-8").strip())
    if "ttsRuntimeAdapterKind" in fingerprint:
        error = historical_tts_adapter_preflight_error(
            service_state=fingerprint.get("ttsServiceState"),
            adapter_kind=fingerprint["ttsRuntimeAdapterKind"],
        )
        if error:
            raise RuntimeError(error)
    return normalize_runtime_fingerprint(fingerprint)


def _apply_plan(
    plan: dict[str, Any],
    *,
    plan_bytes: bytes,
    profile_bytes: bytes,
    archive_bytes: bytes,
    wrapper_bytes: dict[str, bytes],
    dependency_lock_bytes: bytes,
) -> tuple[dict[str, Any], int]:
    target = plan["target"]
    distro = target["distro"]
    staging = _wsl(distro, ["mktemp", "-d", "/tmp/qq-bot-wsl.XXXXXXXX"]).stdout.decode().strip()
    if not staging.startswith("/tmp/qq-bot-wsl."):
        raise RuntimeError("unexpected WSL staging path")
    try:
        bundle = _build_bundle(
            plan_bytes=plan_bytes,
            profile_bytes=profile_bytes,
            archive_bytes=archive_bytes,
            wrapper_bytes=wrapper_bytes,
            dependency_lock_bytes=dependency_lock_bytes,
        )
        bundle_path = f"{staging}/bundle.tar"
        _wsl(
            distro,
            ["install", "-m", "600", "/dev/stdin", bundle_path],
            input_bytes=bundle,
        )
        remote_bundle_sha = _wsl(
            distro,
            ["sha256sum", "--", bundle_path],
        ).stdout.decode("ascii", errors="strict").split()[0]
        if remote_bundle_sha != _sha256(bundle):
            raise RuntimeError("WSL deployment bundle hash mismatch")
        _wsl(distro, ["tar", "-xf", bundle_path, "-C", staging])
        transaction = _wsl(
            distro,
            [
                "python3",
                f"{staging}/wsl_deploy_transaction.py",
                "--plan",
                f"{staging}/plan.json",
                "--plan-hash",
                str(plan["planHash"]),
                "--archive",
                f"{staging}/release.tar",
                "--profile",
                f"{staging}/persona_profile.local.json",
                "--migration-tool",
                f"{staging}/migrate_wsl_config.py",
                "--orchestrator",
                f"{staging}/deploy_wsl.py",
                "--powershell-wrapper",
                f"{staging}/deploy_wsl.ps1",
                "--lock",
                f"{staging}/requirements-wsl.lock",
                "--receipt",
                f"{staging}/receipt.json",
                "--journal",
                WSL_JOURNAL_PATH,
            ],
            check=False,
            passthrough=True,
        )
        receipt_result = _wsl(
            distro,
            ["cat", f"{staging}/receipt.json"],
            check=False,
        )
        if receipt_result.returncode != 0:
            return _state_unknown_receipt(
                distro=distro,
                plan_hash=str(plan["planHash"]),
                transaction_return_code=transaction.returncode,
                reason="transaction receipt missing",
            ), 22
        try:
            receipt = json.loads(receipt_result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _state_unknown_receipt(
                distro=distro,
                plan_hash=str(plan["planHash"]),
                transaction_return_code=transaction.returncode,
                reason="transaction receipt invalid",
            ), 22
        if not _receipt_matches_transaction(
            receipt,
            plan_hash=str(plan["planHash"]),
            return_code=transaction.returncode,
        ):
            return _state_unknown_receipt(
                distro=distro,
                plan_hash=str(plan["planHash"]),
                transaction_return_code=transaction.returncode,
                reason="transaction receipt mismatch",
            ), 22
        return receipt, transaction.returncode
    finally:
        _wsl(distro, ["rm", "-rf", "--", staging], check=False)


def _state_unknown_receipt(
    *,
    distro: str,
    plan_hash: str,
    transaction_return_code: int,
    reason: str,
) -> dict[str, Any]:
    journal_result = _wsl(
        distro,
        ["cat", WSL_JOURNAL_PATH],
        check=False,
    )
    journal: dict[str, Any] | None = None
    if journal_result.returncode == 0:
        try:
            value = json.loads(journal_result.stdout.decode("utf-8"))
            if isinstance(value, dict) and value.get("planHash") == plan_hash:
                journal = {
                    key: value.get(key)
                    for key in (
                        "schemaVersion",
                        "status",
                        "phase",
                        "planHash",
                        "commit",
                        "backup",
                        "updatedAt",
                    )
                }
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return {
        "schemaVersion": 1,
        "result": "state_unknown",
        "planHash": plan_hash,
        "completedAt": datetime.now(UTC).isoformat(),
        "transactionReturnCode": transaction_return_code,
        "reason": reason,
        "journal": journal,
        "replayAllowed": False,
        "qqMessagesSentByAcceptance": False,
    }


def _receipt_matches_transaction(
    receipt: Any,
    *,
    plan_hash: str,
    return_code: int,
) -> bool:
    if (
        not isinstance(receipt, dict)
        or receipt.get("schemaVersion") != 1
        or receipt.get("planHash") != plan_hash
    ):
        return False
    result = receipt.get("result")
    if return_code == 0:
        return result == "success" and receipt.get("replayAllowed") is not True
    if return_code == 19:
        return (
            result == "rejected_before_mutation"
            and receipt.get("replayAllowed") is True
        ) or (
            result == "state_unknown"
            and receipt.get("replayAllowed") is False
        )
    if return_code == 20:
        return result == "rolled_back" and receipt.get("replayAllowed") is False
    if return_code == 21:
        return (
            result == "rollback_incomplete"
            and receipt.get("replayAllowed") is False
        )
    return False


def _build_bundle(
    *,
    plan_bytes: bytes,
    profile_bytes: bytes,
    archive_bytes: bytes,
    wrapper_bytes: dict[str, bytes],
    dependency_lock_bytes: bytes,
) -> bytes:
    wrapper_names = {
        "tools/deploy_wsl.py": "deploy_wsl.py",
        "tools/migrate_wsl_config.py": "migrate_wsl_config.py",
        "tools/wsl_deploy_transaction.py": "wsl_deploy_transaction.py",
        "scripts/deploy_wsl.ps1": "deploy_wsl.ps1",
    }
    if set(wrapper_bytes) != set(WRAPPER_PATHS):
        raise RuntimeError("incomplete immutable wrapper snapshot")
    files = {
        "release.tar": archive_bytes,
        "plan.json": plan_bytes,
        "persona_profile.local.json": profile_bytes,
        "requirements-wsl.lock": dependency_lock_bytes,
        **{
            wrapper_names[path]: wrapper_bytes[path]
            for path in WRAPPER_PATHS
        },
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            info.mtime = 0
            bundle.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _wrapper_sha256(wrapper_bytes: dict[str, bytes]) -> str:
    if set(wrapper_bytes) != set(WRAPPER_PATHS):
        raise RuntimeError("incomplete immutable wrapper snapshot")
    digest = hashlib.sha256()
    for path in WRAPPER_PATHS:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(wrapper_bytes[path])
        digest.update(b"\0")
    return digest.hexdigest()


def _ensure_clean_worktree() -> None:
    status = _git("status", "--porcelain").decode("utf-8", errors="replace")
    if status.strip():
        raise SystemExit("working tree is dirty; commit the exact source before planning or applying")


def _git(*args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            result.stdout,
            result.stderr,
        )
    return result.stdout


def _wsl(
    distro: str,
    command: list[str],
    *,
    input_bytes: bytes | None = None,
    check: bool = True,
    passthrough: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    full_command = ["wsl.exe", "-d", distro, "-u", "root", "--", *command]
    result = subprocess.run(
        full_command,
        cwd=PROJECT_ROOT,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if passthrough:
        if result.stdout:
            sys.stdout.buffer.write(result.stdout)
        if result.stderr:
            sys.stderr.buffer.write(result.stderr)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            result.stdout,
            result.stderr,
        )
    return result


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

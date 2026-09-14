"""Scheduled storage checks, hourly backups, retention, and weekly restore drills."""

import fcntl
import importlib.util
import json
import os
import time
from pathlib import Path

from sync import clients

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".dispatcher"
spec = importlib.util.spec_from_file_location(
    "installed_storage", STATE / "storage-service/storage_control.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
StorageError = module.StorageError


def storage_client():
    from receiver import read_secret

    credentials = json.loads(read_secret(STATE / "secrets/storage.json"))
    os.environ["FLYCTL"] = "/private/tmp/mtg-deployment-prs/fly-cli/flyctl"
    return module.Storage(credentials)


def publish_status(config, previous, report, error):
    linear, _ = clients()
    status = "blocked" if error else "healthy"
    if previous.get("reported_status") == status and (
        time.time() - previous.get("reported_at", 0) < (600 if error else 3600)
    ):
        return
    body = "**Production storage**\n\n"
    if error:
        body += "Needs attention: " + error
    else:
        body += (
            "Healthy. DigitalOcean encrypted backups run every 30 minutes; full restore checks run weekly."
            if previous.get("provider") == "digitalocean"
            else "Healthy. Backups are verified in private object storage; local copies are limited to two. Capacity checks run every minute, backups hourly, and isolated restore checks weekly."
        )
    if report:
        body += f"\n\nAvailable: {report['free'] / 1e9:.2f} GB. SQLite log: {report['wal_bytes'] / 1e6:.1f} MB."
    body += "\n\n" + time.strftime("Checked %Y-%m-%d %H:%M UTC", time.gmtime())
    linear.ensure_comment(config["issue_id"], config["comment_id"], body, mutable=True)
    state_id = config["blocked_state"] if error else config["healthy_state"]
    if linear.issue(config["issue_id"])["state"]["id"] != state_id:
        linear.update(config["issue_id"], state_id)
    previous.update(reported_status=status, reported_at=time.time())


def run():
    os.umask(0o077)
    with (STATE / "storage-monitor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        path = STATE / "storage-monitor.json"
        saved = json.loads(path.read_text()) if path.exists() else {}
        config = json.loads((STATE / "storage-config.json").read_text())
        from sdlc_dispatcher import digitalocean

        if digitalocean.enabled():
            report, error = None, ""
            try:
                report = digitalocean.status()["health"]
                if not report["healthy"]:
                    error = "DigitalOcean health, capacity or backup verification needs attention"
            except Exception:
                error = "DigitalOcean host health could not be confirmed"
            saved.update(checked_at=time.time(), error=error, provider="digitalocean")
            try:
                publish_status(config, saved, report, error)
            except Exception:
                saved["linear_delivery_error"] = "Storage status delivery will retry"
            path.write_text(json.dumps(saved, indent=2) + "\n")
            return
        storage = storage_client()
        report, error = None, ""
        try:
            report = storage.remote("preflight")
            if time.time() - saved.get("checkpoint_at", 0) >= 300:
                result = storage.remote("checkpoint")
                saved.update(checkpoint_at=time.time(), checkpoint=result["passive"])
                report = result["storage"]
            if report["wal_bytes"] > 128 * 1024 * 1024:
                raise StorageError(
                    "SQLite log remains large after bounded checkpoint maintenance; inspect active transactions"
                )
            if time.time() - saved.get("backup_at", 0) >= 3600:
                receipts = storage.receipts()
                known = {r["local_path"] for r in receipts}
                for old in report["backups"]:
                    if old["path"] not in known:
                        storage.backup("legacy", legacy=old["path"])
                latest = max(
                    (r["created"] for r in receipts if r.get("kind") == "hourly"), default=0
                )
                if time.time() - latest >= 3600:
                    result = storage.backup()
                    saved["backup_id"] = result["id"]
                saved["retention"] = storage.retention()
                saved["backup_at"] = time.time()
            if time.time() - saved.get("restore_at", 0) >= 7 * 86400:
                saved["restore"] = storage.restore_test()
                saved["restore_at"] = time.time()
            saved.update(checked_at=time.time(), storage=report, error="")
        except Exception as exc:
            if isinstance(exc, StorageError) and "maintenance is busy" in str(exc):
                print(
                    json.dumps({"storage": "deferred", "reason": "Maintenance in progress"}),
                    flush=True,
                )
                return
            error = (
                str(exc)
                if isinstance(exc, StorageError)
                else "Storage provider or maintenance check failed; no completion inferred"
            )
            saved.update(checked_at=time.time(), error=error)
        try:
            publish_status(config, saved, report, error)
        except Exception:
            saved["linear_delivery_error"] = "Storage status delivery will retry"
        else:
            saved.pop("linear_delivery_error", None)
        temporary = path.with_suffix(".pending")
        temporary.write_text(json.dumps(saved, indent=2) + "\n")
        temporary.replace(path)
        print(
            json.dumps(
                {"storage": "blocked" if error else "healthy", "checked_at": saved["checked_at"]}
            ),
            flush=True,
        )


if __name__ == "__main__":
    run()

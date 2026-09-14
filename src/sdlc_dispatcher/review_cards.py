"""Publish durable browser proof into Linear before requesting human acceptance."""

import json
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .config import DispatchError
from .http import NoRedirect
from .privacy import model_report
from .review_gate import sha, validate_receipt


def upload(linear, path):
    raw = path.read_bytes()
    kind = {".png": "image/png", ".webm": "video/webm"}.get(path.suffix)
    if not kind or not 0 < len(raw) <= 25_000_000:
        raise DispatchError("Review media is missing or exceeds the upload limit")
    result = linear.call(
        """mutation($type:String!,$name:String!,$size:Int!){
        fileUpload(contentType:$type,filename:$name,size:$size){success
        uploadFile{uploadUrl assetUrl headers{key value}}}}""",
        {"type": kind, "name": path.name, "size": len(raw)},
        write=True,
    )["fileUpload"]
    if not result.get("success"):
        raise DispatchError("Linear could not prepare the review media upload")
    f = result["uploadFile"]
    target, asset = urlsplit(f["uploadUrl"]), urlsplit(f["assetUrl"])
    if (
        target.scheme != "https"
        or target.username
        or not target.hostname
        or not (
            target.hostname in {"storage.googleapis.com", "uploads.linear.app"}
            or target.hostname.endswith((".storage.googleapis.com", ".amazonaws.com"))
        )
        or asset.scheme != "https"
        or asset.hostname != "uploads.linear.app"
    ):
        raise DispatchError("Unexpected Linear media storage origin")
    headers = {"Content-Type": kind, "Cache-Control": "public, max-age=31536000"}
    headers.update({h["key"]: h["value"] for h in f["headers"]})
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            urllib.request.Request(f["uploadUrl"], data=raw, headers=headers, method="PUT"),
            timeout=90,
        ) as response:
            if not 200 <= response.status < 300:
                raise DispatchError("Linear media upload was not confirmed")
    except (OSError, ValueError) as exc:
        raise DispatchError("Linear media upload failed; retry before review") from exc
    return f["assetUrl"]


def original_proof(store, project, job):
    artifact_path = Path(job["artifact"])
    raw = artifact_path.read_bytes()
    if sha(raw) != job["artifact_digest"]:
        raise DispatchError("Candidate artifact changed after verification")
    artifact = json.loads(raw)
    receipt = store.review(job["id"], job["artifact_digest"])
    if not receipt:
        raise DispatchError("No verified Astra receipt is available")
    metadata_path = Path(receipt["metadata_path"])
    if sha(metadata_path.read_bytes()) != receipt["metadata_digest"]:
        raise DispatchError("Astra receipt changed after verification")
    folder = metadata_path.parent
    review = validate_receipt(folder, project, artifact, job["artifact_digest"])
    if review["verdict"] != "pass":
        raise DispatchError("Astra must pass before human review")
    packet = json.loads((folder / "input/packet.json").read_text())
    if packet["issue"]["revision"] != job["revision"]:
        raise DispatchError("Review belongs to a different issue revision")
    return artifact, folder, review


def media(folder):
    packet = json.loads((folder / "input/packet.json").read_text())
    images = packet.get("images", [])
    after = [n for n in images if n.startswith("after-chromium-")]
    if not after:
        after = [n for n in images if n.startswith("after-")]
    pairs = []
    for name in after:
        if name.endswith("-moving.png"):
            continue
        before = "before-" + name[len("after-") :]
        if before in images:
            pairs.extend([before, name])
    names = pairs[:8]
    names += [
        n
        for n in packet.get("evidence", [])
        if n.startswith("after-chromium") and n.endswith(".webm")
    ][:1]
    return [folder / "input/evidence" / name for name in names]


def publish_card(store, project, job, head, folder, review, linear):
    paths = media(folder)
    m = json.loads((folder / "metadata.json").read_text())
    manifest = {
        "head": head,
        "revision": job["revision"],
        "review_digest": m["review_digest"],
        "input_digest": m["input_digest"],
        "folder": str(folder.resolve()),
        "media": {str(p.resolve()): sha(p.read_bytes()) for p in paths},
    }
    digest = sha(json.dumps(manifest, sort_keys=True).encode())
    with store.transaction() as db:
        db.execute(
            "INSERT OR IGNORE INTO review_cards(job,head,digest,manifest,comment_id) VALUES(?,?,?,?,?)",
            (job["id"], head, digest, json.dumps(manifest, sort_keys=True), str(uuid.uuid4())),
        )
        row = dict(
            db.execute(
                "SELECT * FROM review_cards WHERE job=? AND head=?", (job["id"], head)
            ).fetchone()
        )
    if row["digest"] != digest:
        raise DispatchError("Review card changed after registration")
    if row["delivered"]:
        return row
    if not row["body"]:
        links = []
        for path in paths:
            media_digest = sha(path.read_bytes())
            with store.connect() as db:
                cached = db.execute(
                    "SELECT url FROM release_uploads WHERE digest=?", (media_digest,)
                ).fetchone()
            url = cached[0] if cached else upload(linear, path)
            with store.transaction() as db:
                db.execute("INSERT OR IGNORE INTO release_uploads VALUES(?,?)", (media_digest, url))
            label = path.stem.replace("-", " ").capitalize()
            links.append(f"**{label}**\n\n![{label}]({url})")
        summary = model_report({"summary": review["summary"]})["summary"]
        body = "**Ready for your review**\n\n" + summary + "\n\n" + "\n\n".join(links)
        body += "\n\n**What to do:** review the summary and evidence limits, then move this issue to **Ready to Deploy** to authorize merging and production deployment. Move it back to review to withdraw a queued approval."
        body += "\n\nChecks and independent Astra review passed."
        body += (
            " Captures use synthetic preview data."
            if paths
            else " Screenshots are not required; this card contains no visual captures."
        )
        if review.get("limitations"):
            body += (
                "\n\nEvidence limits: "
                + model_report({"text": " ".join(review["limitations"])})["text"][:1800]
            )
        body += f"\n\n[Optional GitHub details]({job['pr_url']}) · Candidate `{head[:12]}` · Evidence `{digest[:12]}`"
        with store.transaction() as db:
            db.execute(
                "UPDATE review_cards SET body=? WHERE job=? AND head=?", (body, job["id"], head)
            )
        row["body"] = body
    linear.ensure_comment(job["external_id"], row["comment_id"], row["body"])
    with store.transaction() as db:
        db.execute(
            "UPDATE review_cards SET delivered=1,error='' WHERE job=? AND head=?", (job["id"], head)
        )
    row["delivered"] = 1
    return row


def ready(store, job, head):
    with store.transaction() as db:
        db.execute(
            "UPDATE review_cards SET ready_at=CASE WHEN ready_at=0 THEN ? ELSE ready_at END,error='' WHERE job=? AND head=? AND delivered=1",
            (time.time(), job["id"], head),
        )
        store._stage(db, job["id"], "awaiting_approval")

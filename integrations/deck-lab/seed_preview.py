"""Create synthetic preview data only. Executed inside the preview container."""

import os
import runpy
from pathlib import Path


def main():
    if (
        os.environ.get("DISPATCHER_PREVIEW") != "1"
        or os.environ.get("SABER_DB_PATH") != "/tmp/deck-lab-preview.db"
    ):
        raise RuntimeError("This seed script only supports the disposable dispatcher preview")
    path = Path("/tmp/deck-lab-preview.db")
    if path.exists():
        raise RuntimeError("Preview database already exists; restart the disposable container")
    source = Path(os.environ.get("DISPATCHER_SOURCE", "/app"))
    runpy.run_path(str(source / "scripts/setup_db.py"))["setup_database"](path)
    from sabermetrics import db

    db.UsersRepo(path).create(
        email="preview@example.test",
        display_name="Preview User",
        role="admin",
        status="active",
        password_hash=db.hash_password("local-preview-only"),
    )
    with db.connect(path) as conn:
        for values in [
            (
                "preview-commander",
                "Test Commander",
                "Legendary Creature",
                "Partner",
                2,
                1,
            ),
            ("preview-partner", "Test Partner", "Legendary Creature", "Partner", 2, 1),
            ("preview-ring", "Sol Ring", "Artifact", "{T}: Add {C}{C}.", 1, 0),
        ]:
            identifier, name, card_type, text, cmc, commander = values
            conn.execute(
                """INSERT INTO cards(id,oracle_id,name,type_line,oracle_text,cmc,
                           color_identity,is_legal_commander,is_legal_in_99)
                           VALUES(?,?,?,?,?,?,'[]',?,1)""",
                (identifier, identifier, name, card_type, text, cmc, commander),
            )
        conn.commit()
    os.execvp("sabermetrics", ["sabermetrics", "serve", "--host", "0.0.0.0", "--port", "8080"])


if __name__ == "__main__":
    main()

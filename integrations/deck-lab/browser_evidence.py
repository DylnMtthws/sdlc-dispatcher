"""Trusted browser scenarios; runs inside the isolated preview network."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ORIGIN = "http://app:8080"


def main():
    phase = os.environ["EVIDENCE_PHASE"]
    output = Path("/evidence")
    measurements, errors, images = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.route(
            "**/*",
            lambda route: (
                route.continue_()
                if urlsplit(route.request.url).netloc == "app:8080"
                else route.abort()
            ),
        )
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(ORIGIN + "/login")
        page.locator("input[name=email]").fill("preview@example.test")
        page.locator("input[name=password]").fill("local-preview-only")
        page.locator("button[type=submit]").click()
        page.wait_for_url(lambda url: "/login" not in url)
        for count in (1, 2):
            ids = ["preview-commander", "preview-partner"][:count]
            created = page.evaluate(
                """async ids => {
                const r=await fetch('/build/new',{method:'POST',headers:{'Content-Type':'application/json',
                'X-CSRFToken':document.querySelector('meta[name="csrf-token"]').content},
                body:JSON.stringify({title:'Synthetic review deck',commander_card_ids:ids})});
                return {status:r.status,body:await r.json()};}""",
                ids,
            )
            assert created["status"] == 201, "Synthetic deck creation failed"
            page.goto(ORIGIN + "/build/deck/" + created["body"]["id"])
            page.locator(".dl-mat-command").wait_for()
            result = page.locator(".dl-mat-command").evaluate("""el => {
                const r=el.getBoundingClientRect();return {width:r.width,height:r.height,
                cards:Array.from(el.querySelectorAll('.dl-mat-card')).map(c=>{
                const b=c.getBoundingClientRect();return {left:b.left-r.left,right:b.right-r.left,
                width:b.width,fits:b.left>=r.left&&b.right<=r.right};})};}""")
            result["commander_count"] = count
            measurements.append(result)
            name = f"{phase}-desktop-{count}.png"
            page.screenshot(path=str(output / name), full_page=True)
            images.append(name)
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(200)
            assert page.locator(".dl-builder").is_visible()
            name = f"{phase}-mobile-{count}.png"
            page.screenshot(path=str(output / name), full_page=True)
            images.append(name)
            page.set_viewport_size({"width": 1440, "height": 900})
        browser.close()
    (output / f"{phase}.json").write_text(
        json.dumps(
            {
                "measurements": measurements,
                "page_errors": errors,
                "images": images,
                "viewports": [{"width": 1440, "height": 900}, {"width": 390, "height": 844}],
                "method": "Actual candidate/base application in isolated Docker with synthetic data; external requests blocked.",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()

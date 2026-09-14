"""Trusted behavioral browser scenarios, inside the isolated preview network."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ORIGIN = "http://app.test:8080"
OUTPUT = Path("/evidence")
CAPTURE_MEDIA = os.environ.get("EVIDENCE_CAPTURE_MEDIA") == "1"


def mutate(page, commands):
    result = page.evaluate(
        """async commands => {
        const id=location.pathname.split('/').pop();
        const state=await (await fetch('/api/decks/'+id)).json();
        const r=await fetch('/api/decks/'+id+'/commands',{method:'POST',
        headers:{'Content-Type':'application/json',
        'X-CSRFToken':document.querySelector('meta[name="csrf-token"]').content},
        body:JSON.stringify({mutation_id:Date.now().toString(36)+Math.random().toString(36).slice(2),expected_revision:state.revision,commands})});
        return {status:r.status,body:await r.json()};}""",
        commands,
    )
    assert result["status"] == 200, str(result)
    page.reload()
    return result["body"]


def state(page):
    return page.evaluate(
        "async () => (await fetch('/api/decks/'+location.pathname.split('/').pop())).json()"
    )


def capture(page, name, images, *, full_page=True):
    if not CAPTURE_MEDIA:
        return
    options = {"full_page": True} if full_page else {}
    page.screenshot(path=str(OUTPUT / name), **options)
    images.append(name)


def home(page, prefix, images):
    """Capture home typography and resume-card layout and interactions."""
    rows = []
    for width, height, scale in [(1920, 945, 1), (390, 844, 1), (390, 844, 2)]:
        page.set_viewport_size({"width": width, "height": height})
        page.goto(ORIGIN + "/")
        how = page.locator(".dl-how")
        how.wait_for()
        assert how.locator(".dl-step").count() == 3, "Expected all three How it goes boxes"
        resume = page.locator("section").filter(has_text="Pick up where you left off")
        resume_cards = resume.locator(".dl-deck-card")
        assert resume_cards.count() == 2, "Expected two resume tiles"
        if scale == 2:
            resume.locator("h2, p, .dl-deck-recency").evaluate_all(
                """elements => elements.forEach(el => {
                el.style.fontSize = (parseFloat(getComputedStyle(el).fontSize) * 2) + 'px';
            })"""
            )
        page.evaluate("document.fonts.ready")
        how_geometry = how.locator(".dl-step").evaluate_all("""elements => {
            const rect = el => { const r=el.getBoundingClientRect();
                return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height}; };
            const baseline = el => {
                const probe=document.createElement('span');
                probe.style.cssText='display:inline-block;width:0;height:0;padding:0;margin:0;border:0;vertical-align:baseline';
                el.prepend(probe); const y=probe.getBoundingClientRect().bottom; probe.remove(); return y;
            };
            return elements.map(el => {
                const n=el.querySelector('b'), h=el.querySelector('h3'), p=el.querySelector('p');
                const nb=baseline(n), hb=baseline(h);
                const range=document.createRange(); range.selectNodeContents(h);
                return {number:n.textContent,heading:h.textContent,box:rect(el),number_rect:rect(n),
                    heading_rect:rect(h),paragraph_rect:rect(p),number_baseline:nb,heading_baseline:hb,
                    first_baseline_delta:hb-nb,heading_line_count:range.getClientRects().length,
                    number_align_self:getComputedStyle(n).alignSelf,heading_align_self:getComputedStyle(h).alignSelf,
                    overflow:el.scrollWidth>el.clientWidth,within_viewport:rect(el).left>=0&&rect(el).right<=innerWidth};
            });
        }""")
        resume_geometry = resume_cards.evaluate_all("""elements => elements.map(el => {
            const r=el.getBoundingClientRect(), menu=el.querySelector('.dl-deck-action-menu');
            const m=menu&&menu.getBoundingClientRect();
            return {left:r.left,right:r.right,width:r.width,height:r.height,
                overflow:el.scrollWidth>el.clientWidth,
                within_viewport:r.left>=0&&r.right<=innerWidth,
                action_menu:!!menu,menu_within_viewport:!m||(m.left>=0&&m.right<=innerWidth)};
        })""")
        rows.append(
            {
                "viewport": [width, height],
                "resume_text_scale": scale,
                "steps": how_geometry,
                "resume_cards": resume_geometry,
            }
        )
        assert all(card["within_viewport"] and not card["overflow"] for card in resume_geometry)
        name = f"{prefix}-home-{width}-text-{scale}x.png"
        capture(resume, name, images, full_page=False)
        if width == 1920:
            page.evaluate("window.scrollTo(0,0)")
            capture(page, f"{prefix}-home-context-{width}.png", images)

            page.goto(ORIGIN + "/build")
            build = page.locator(".dl-library-grid")
            build.wait_for()
            build_name = f"{prefix}-build-reference-{width}.png"
            capture(build, build_name, images, full_page=False)

    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(ORIGIN + "/")
    first = (
        page.locator("section")
        .filter(has_text="Pick up where you left off")
        .locator(".dl-deck-card")
        .first
    )
    menu = first.locator("[data-deck-actions]")
    interactions = {"available": menu.count() == 1}
    if interactions["available"]:
        summary = menu.locator("summary")
        summary.focus()
        summary.press("ArrowDown")
        first_item = menu.locator("[role=menuitem]").first
        interactions["keyboard_opened"] = menu.evaluate("el => el.open")
        interactions["first_item_focused"] = first_item.evaluate(
            "el => el === document.activeElement"
        )
        menu_box = menu.locator(".dl-deck-action-menu").bounding_box()
        interactions["menu_within_viewport"] = bool(
            menu_box and menu_box["x"] >= 0 and menu_box["x"] + menu_box["width"] <= 390
        )
        menu_name = f"{prefix}-home-390-menu.png"
        capture(first, menu_name, images, full_page=False)
        first_item.press("Escape")
        interactions["escape_closed"] = not menu.evaluate("el => el.open")
        interactions["summary_refocused"] = summary.evaluate("el => el === document.activeElement")

        page.evaluate(
            "Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.__copied=value}}})"
        )
        summary.click()
        share = menu.locator("[data-share-deck]")
        share.click()
        page.wait_for_function("() => !!window.__copied")
        interactions["share_copied"] = page.evaluate(
            "() => window.__copied.startsWith(location.origin)"
        )

        page.once("dialog", lambda dialog: dialog.dismiss())
        menu.evaluate("el => el.open = true")
        menu.locator(".dl-deck-delete-form button").click()
        interactions["delete_cancel_preserved"] = (
            page.locator(".dl-deck-card").count() == 2 and page.url == ORIGIN + "/"
        )
    return {"layouts": rows, "interactions": interactions}


def spoiler(page, prefix, images):
    mutate(page, [{"type": "update_view", "view_mode": "table", "display_mode": "spoiler"}])
    rows = []
    for width, height in [(1440, 900), (390, 844)]:
        page.set_viewport_size({"width": width, "height": height})
        target = page.locator(".dl-spoiler-card").filter(has_text="Sol Ring")
        target.wait_for()
        rows.append(
            {
                "viewport": [width, height],
                "rendered": target.evaluate("""el => {
                const p=el.querySelector('p'), r=el.getBoundingClientRect();
                return {text:p.textContent,html:p.innerHTML,paragraph_label:p.getAttribute('aria-label'),
                symbols:[...p.querySelectorAll('.dl-mana-symbol')].map(x=>({text:x.textContent,
                label:x.getAttribute('aria-label'),hidden:x.getAttribute('aria-hidden'),
                width:x.getBoundingClientRect().width})),
                width:r.width,within_viewport:r.left>=0&&r.right<=innerWidth,
                white_space:getComputedStyle(p).whiteSpace};}"""),
                "accessibility_snapshot": target.aria_snapshot(),
            }
        )
        capture(page, f"{prefix}-spoiler-{width}.png", images)
    page.set_viewport_size({"width": 1440, "height": 900})
    return rows


# Observe browser calls and events without replacing their behavior. The native
# drag-image surface is outside page screenshots, so do not claim its pixels were captured.
DRAG_OBSERVER = """() => {
window.__dragEvidence={snapshots:[],events:[]};
const original=DataTransfer.prototype.setDragImage;
DataTransfer.prototype.setDragImage=function(el,x,y){
 const image=el.tagName==='IMG'?el:el.querySelector('img'),r=el.getBoundingClientRect();
 window.__dragEvidence.snapshots.push({tag:el.tagName,className:el.className,x,y,
 width:r.width,height:r.height,decoded:!!image&&image.complete&&image.naturalWidth>0,
 opacity:getComputedStyle(el).opacity});
 return original.call(this,el,x,y);
};
for(const type of ['dragstart','dragover','drop','dragend'])
 document.addEventListener(type,e=>{
  if(window.__dragEvidence.events.length<300)window.__dragEvidence.events.push({type,
  x:e.clientX,y:e.clientY,trusted:e.isTrusted,effect:e.dataTransfer&&e.dataTransfer.dropEffect});
 },true);
}"""

IMAGE_GEOMETRY = """() => [...document.images].map(img => {
 const r=img.getBoundingClientRect();
 return {left:r.left,top:r.top,width:r.width,height:r.height,
 decoded:img.complete&&img.naturalWidth>0,className:img.className,
 parentClass:img.parentElement.className,opacity:getComputedStyle(img).opacity,
 inCard:!!img.closest('[data-entry-id],.dl-search-result'),
 inViewport:r.right>0&&r.bottom>0&&r.left<innerWidth&&r.top<innerHeight};
}).filter(r=>r.width>0&&r.height>0)"""


def drag(page, prefix, images):
    doc = mutate(page, [{"type": "update_view", "view_mode": "playmat"}])
    zones = [z for z in doc["zones"] if z["name"].lower() != "commander"]
    if len(zones) < 2:
        doc = mutate(page, [{"type": "create_zone", "name": "Drag destination"}])
        zones = doc["zones"]
    source_zone = next(e["zone_id"] for e in doc["entries"] if e["card_id"] == "preview-ring")
    destination = next(z for z in zones if z["id"] != source_zone)
    # Put the drop target in view; this only prepares the disposable fixture.
    mutate(page, [{"type": "move_zone", "zone_id": destination["id"], "x": 400, "y": 300}])
    page.evaluate(DRAG_OBSERVER)
    rows = []
    for origin, cancel in [("playmat", False), ("sidebar", False), ("playmat", True)]:
        doc = state(page)
        entry = next(e for e in doc["entries"] if e["card_id"] == "preview-ring")
        if origin == "playmat":
            source = page.locator(f'.dl-mat-card[data-entry-id="{entry["id"]}"]')
            target_zone = next(z for z in zones if z["id"] != entry["zone_id"])
        else:
            if not page.locator("[data-card-search]").is_visible():
                page.locator('[data-toggle-rail="left"]').click()
            with page.expect_response(
                lambda response: "/api/cards?" in response.url and "Sol" in response.url
            ):
                page.locator("[data-card-search]").fill("Sol Ring")
            page.wait_for_timeout(100)
            source = page.locator(".dl-search-result").filter(has_text="Sol Ring")
            source.wait_for()
            target_zone = destination
        target = page.locator(f'.dl-mat-zone[data-zone-id="{target_zone["id"]}"]')
        source.scroll_into_view_if_needed()
        page.wait_for_function(
            "el=>el.querySelector('img').complete&&el.querySelector('img').naturalWidth>0",
            arg=source.element_handle(),
        )
        a, b = source.bounding_box(), target.bounding_box()
        assert a and b, "Drag endpoints are missing"
        start = {"x": a["x"] + min(a["width"] / 2, 25), "y": a["y"] + min(a["height"] / 2, 25)}
        end = {"x": b["x"] + b["width"] - 25, "y": b["y"] + b["height"] - 25}
        page.evaluate("window.__dragEvidence={snapshots:[],events:[]}")
        page.mouse.move(**start)
        page.mouse.down()
        page.mouse.move(start["x"] + 15, start["y"] + 15, steps=6)
        page.mouse.move(end["x"] - 30, end["y"] - 30, steps=15)
        page.wait_for_timeout(50)
        frames = [
            {
                "pointer": {"x": end["x"] - 30, "y": end["y"] - 30},
                "images": page.evaluate(IMAGE_GEOMETRY),
            }
        ]
        if prefix.startswith("after"):
            capture(
                page, f"{prefix}-drag-{origin}-{'cancel' if cancel else 'drop'}-moving.png", images
            )
        page.mouse.move(**end, steps=8)
        page.wait_for_timeout(150)
        frames.append({"pointer": end, "images": page.evaluate(IMAGE_GEOMETRY)})
        capture(page, f"{prefix}-drag-{origin}-{'cancel' if cancel else 'drop'}.png", images)
        if cancel:
            page.keyboard.press("Escape")
        page.mouse.up()
        page.wait_for_timeout(400)
        evidence = page.evaluate("window.__dragEvidence")
        updated = state(page)
        updated_entry = next(e for e in updated["entries"] if e["card_id"] == "preview-ring")
        rows.append(
            {
                "origin": origin,
                "cancel": cancel,
                "start": start,
                "end": end,
                "before": {"zone": entry["zone_id"], "quantity": entry["quantity"]},
                "after": {"zone": updated_entry["zone_id"], "quantity": updated_entry["quantity"]},
                "target_zone": target_zone["id"],
                "browser_events": evidence,
                "moving_frames": frames,
                "images_after_end": page.evaluate(IMAGE_GEOMETRY),
                "remaining_drag_sources": page.locator(".drag-source").count(),
                "remaining_preview_elements": page.locator(
                    ".dl-drag-ghost,.dl-drag-preview"
                ).count(),
            }
        )
    return rows


def main():
    phase = os.environ["EVIDENCE_PHASE"]
    scenarios = set(os.environ.get("EVIDENCE_SCENARIOS", "").split(","))
    measurements, errors, images, behavioral, videos = [], [], [], [], []
    with sync_playwright() as playwright:
        for engine in ["chromium", "webkit"]:
            browser = getattr(playwright, engine).launch(headless=True)
            recording = (
                {
                    "record_video_dir": str(OUTPUT / "video"),
                    "record_video_size": {"width": 960, "height": 600},
                }
                if CAPTURE_MEDIA and "drag" in scenarios and engine == "chromium"
                else {}
            )
            context = browser.new_context(viewport={"width": 1440, "height": 900}, **recording)
            context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if urlsplit(route.request.url).netloc == "app.test:8080"
                    else route.abort()
                ),
            )
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(ORIGIN + "/login")
            page.locator("input[name=email]").fill("preview@example.test")
            page.locator("input[name=password]").fill("local-preview-only")
            page.locator("button[type=submit]").click()
            try:
                page.wait_for_url(lambda url: "/login" not in url, timeout=10000)
            except Exception:
                print(engine, page.url, page.locator("body").inner_text(), flush=True)
                raise
            prefix = f"{phase}-{engine}"
            for count in (1, 2):
                created = page.evaluate(
                    """async ids => {
                    const r=await fetch('/build/new',{method:'POST',headers:{'Content-Type':'application/json',
                    'X-CSRFToken':document.querySelector('meta[name="csrf-token"]').content},
                    body:JSON.stringify({title:'Synthetic review deck',commander_card_ids:ids})});
                    return {status:r.status,body:await r.json()};}""",
                    ["preview-commander", "preview-partner"][:count],
                )
                assert created["status"] == 201, "Synthetic deck creation failed"
                page.goto(ORIGIN + "/build/deck/" + created["body"]["id"])
                page.locator(".dl-mat-command").wait_for()
                result = page.locator(".dl-mat-command").evaluate("""el => {
                    const r=el.getBoundingClientRect();return {width:r.width,height:r.height,
                    cards:Array.from(el.querySelectorAll('.dl-mat-card')).map(c=>{
                    const b=c.getBoundingClientRect();return {left:b.left-r.left,right:b.right-r.left,
                    width:b.width,fits:b.left>=r.left&&b.right<=r.right};})};}""")
                result.update(commander_count=count, browser=engine)
                measurements.append(result)
                if not scenarios & {"spoiler", "drag", "home"}:
                    capture(page, f"{prefix}-desktop-{count}.png", images)
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_timeout(200)
                # Record the rendered layout; baseline visibility is evidence,
                # not a reason for an unrelated harness assertion to abort.
                if not scenarios & {"spoiler", "drag", "home"}:
                    capture(page, f"{prefix}-mobile-{count}.png", images)
                page.set_viewport_size({"width": 1440, "height": 900})
            if scenarios & {"spoiler", "drag"}:
                doc = state(page)
                zone = next(z for z in doc["zones"] if z["name"].lower() == "unsorted")
                mutate(
                    page, [{"type": "add_card", "card_id": "preview-ring", "zone_id": zone["id"]}]
                )
                evidence = {"browser": engine}
                if "spoiler" in scenarios:
                    evidence["spoiler"] = spoiler(page, prefix, images)
                if "drag" in scenarios:
                    evidence["drag"] = drag(page, prefix, images)
                behavioral.append(evidence)
            if "home" in scenarios:
                behavioral.append({"browser": engine, "home": home(page, prefix, images)})
            video = page.video if recording else None
            context.close()
            if video:
                name = f"{prefix}-interaction.webm"
                video.save_as(str(OUTPUT / name))
                video.delete()
                videos.append(name)
                (OUTPUT / "video").rmdir()
            browser.close()
    (OUTPUT / f"{phase}.json").write_text(
        json.dumps(
            {
                "measurements": measurements,
                "behavioral": behavioral,
                "page_errors": errors,
                "images": images,
                "videos": videos,
                "viewports": [[1440, 900], [390, 844]]
                + ([[1920, 945]] if "home" in scenarios else []),
                "method": "Actual base/candidate application, isolated Docker, synthetic decoded artwork, Chromium and WebKit. Real mouse drags; native compositor pixels are not part of page screenshots.",
                "home_method": (
                    "Home typography and resume tiles at reported desktop/mobile sizes, plus synthetic 200% resume-card text to exercise wrapping (not browser zoom). Includes Build-page comparison, mobile menu placement, keyboard menu navigation, share handling, and cancelled deletion. First-line baselines use temporary zero-size inline probes removed before screenshots."
                    if "home" in scenarios
                    else None
                ),
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()

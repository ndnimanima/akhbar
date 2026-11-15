import asyncio, os
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8080"
OUT = "/home/user/rozaneh/docs/shots"
os.makedirs(OUT, exist_ok=True)

PAGES = ["dashboard", "feeds", "topics", "channels", "jobs", "archive", "logs", "settings", "admin", "creators"]

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--no-sandbox"])
        ctx = await b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        pg = await ctx.new_page()
        errors = []
        pg.on("console", lambda m: errors.append(f"[{m.type}] {m.text}") if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        # login page
        await pg.goto(BASE + "/login", wait_until="networkidle")
        await pg.wait_for_timeout(500)
        await pg.screenshot(path=f"{OUT}/00-login-fa-slate.png")

        # sign in
        await pg.fill("#password", "rozaneh")
        await pg.click("#submitBtn")
        await pg.wait_for_url(BASE + "/", timeout=15000)
        await pg.wait_for_selector(".stats", timeout=15000)
        await pg.wait_for_timeout(900)

        async def shot(name, full=False):
            await pg.wait_for_timeout(650)
            await pg.screenshot(path=f"{OUT}/{name}.png", full_page=full)

        await shot("01-dashboard-fa-slate", full=True)

        # nav dropdown open
        await pg.hover(".mitem[data-group='feeds'] > .mlink")
        await pg.wait_for_timeout(500)
        await pg.screenshot(path=f"{OUT}/02-nav-dropdown.png", clip={"x": 0, "y": 0, "width": 1440, "height": 430})

        for page in PAGES:
            await pg.evaluate(f"window.Rozaneh.go('{page}')")
            await pg.wait_for_timeout(700)
            await shot(f"03-{page}-fa-slate", full=(page in ("jobs", "channels", "creators")))

        # admin tabs
        for tab in ("links", "appearance", "data", "access"):
            await pg.evaluate(f"window.Rozaneh.go('admin','{tab}')")
            await pg.wait_for_timeout(600)
            await shot(f"04-admin-{tab}", full=True)

        # modals
        await pg.evaluate("window.Rozaneh.go('feeds')")
        await pg.wait_for_timeout(500)
        await pg.click(".page-actions [data-act='feed.new']")
        await pg.wait_for_timeout(500)
        await shot("05-modal-feed")
        await pg.keyboard.press("Escape")

        await pg.evaluate("window.Rozaneh.go('jobs')")
        await pg.wait_for_timeout(600)
        await pg.click(".jcard [data-act='job.dry']")
        await pg.wait_for_timeout(2500)
        await shot("06-modal-dryrun")
        await pg.keyboard.press("Escape")

        # amber theme
        await pg.click("#themeBtn")
        await pg.wait_for_timeout(700)
        await pg.evaluate("window.Rozaneh.go('dashboard')")
        await pg.wait_for_timeout(800)
        await shot("07-dashboard-fa-amber", full=True)
        await pg.evaluate("window.Rozaneh.go('admin','links')")
        await pg.wait_for_timeout(700)
        await shot("08-admin-links-amber", full=True)

        # english + amber
        await pg.click("#langBtn")
        await pg.wait_for_timeout(800)
        await pg.evaluate("window.Rozaneh.go('dashboard')")
        await pg.wait_for_timeout(800)
        await shot("09-dashboard-en-amber", full=True)
        await pg.evaluate("window.Rozaneh.go('jobs')")
        await pg.wait_for_timeout(700)
        await shot("10-jobs-en-amber", full=True)

        # back to fa + slate for the remaining checks
        await pg.click("#langBtn")
        await pg.wait_for_timeout(500)
        await pg.click("#themeBtn")
        await pg.wait_for_timeout(500)

        # overflow sweep
        overflow = {}
        for w in (1440, 1366, 1120, 900, 768, 430, 390):
            await pg.set_viewport_size({"width": w, "height": 900})
            await pg.wait_for_timeout(250)
            bad = []
            for page in PAGES:
                await pg.evaluate(f"window.Rozaneh.go('{page}')")
                await pg.wait_for_timeout(280)
                sw = await pg.evaluate("() => document.documentElement.scrollWidth")
                if sw > w + 1:
                    bad.append((page, sw))
            overflow[w] = bad
            print(f"w={w}: overflow ->", bad or "none")

        # mobile: hamburger + menu
        await pg.set_viewport_size({"width": 390, "height": 844})
        await pg.evaluate("window.Rozaneh.go('dashboard')")
        await pg.wait_for_timeout(500)
        await shot("11-mobile-dashboard")
        await pg.click("#burger")
        await pg.wait_for_timeout(600)
        await pg.click(".mgroup[data-group='feeds'] .mhead")
        await pg.wait_for_timeout(600)
        await shot("12-mobile-menu")

        print("\nCONSOLE ISSUES:", len(errors))
        for e in errors[:25]:
            print("  ", e[:220])
        await b.close()

asyncio.run(main())

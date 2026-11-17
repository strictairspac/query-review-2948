import asyncio,json
from playwright.async_api import async_playwright

BASE='http://127.0.0.1:18002'
ART='/workspace/test-artifacts/gb-4101'

async def session_token(ctx,email):
    r=await ctx.request.post(BASE+'/api/auth/login',data={'email':email,'password':'ColdChain!17'})
    return await r.json()

async def open_as(browser,email):
    ctx=await browser.new_context()
    data=await session_token(ctx,email)
    await ctx.add_init_script(f"localStorage.setItem('qr-session',{json.dumps(json.dumps(data))})")
    page=await ctx.new_page()
    return page

async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch()
        admin=await open_as(b,'admin@chenggang.test')
        errors=[]
        admin.on('pageerror',lambda e:errors.append(str(e)))
        admin.on('console',lambda m:errors.append('console:'+m.text) if m.type=='error' else None)
        await admin.goto(BASE+'/#index-trials'); await admin.wait_for_selector('#trial-form',timeout=10000)
        print('nav items:',(await admin.locator('.sidebar nav').inner_text()).replace('\n',' | '))
        print('column checkboxes:',await admin.locator('input[name=columns]').count())
        await admin.screenshot(path=ART+'/screen-admin-form.png',full_page=True)
        await admin.check('input[name=columns][value=customer]')
        await admin.check('input[name=columns][value=amount]')
        await admin.fill('input[name=reason]','浏览器实测：客户与金额组合索引')
        await admin.click('#trial-form button[type=submit]')
        await admin.wait_for_timeout(1200)
        print('after submit url:',admin.url)
        txt=''
        for _ in range(25):
            await admin.goto(BASE+'/#index-trials'); await admin.wait_for_timeout(1200)
            txt=await admin.locator('#page').inner_text()
            if '待复核' in txt:break
        print('verified visible:', '待复核' in txt)
        print('plan before/after:', '建前实际计划' in txt, '建后实际计划' in txt)
        print('ownership shown:', 'index-trial:#' in txt)
        print('reason shown:', '浏览器实测' in txt)
        await admin.screenshot(path=ART+'/screen-admin-verified.png',full_page=True)
        if await admin.locator('form[id^=decide-]').count():
            await admin.select_option('select[name=decision]','retain')
            await admin.fill('form[id^=decide-] input[name=reason]','浏览器实测：确认走索引，保留')
            await admin.click('form[id^=decide-] button[type=submit]')
            await admin.wait_for_timeout(1600)
            txt=await admin.locator('#page').inner_text()
            print('retained:', '已保留' in txt, '| decision block:', '复核决定' in txt, '| evidence bound:', '与本次验证证据一致' in txt)
        await admin.screenshot(path=ART+'/screen-admin-retained.png',full_page=True)
        print('admin page errors:',errors[:5])

        viewer=await open_as(b,'viewer@chenggang.test')
        await viewer.goto(BASE+'/#index-trials'); await viewer.wait_for_selector('.sidebar nav',timeout=10000)
        await viewer.wait_for_timeout(800)
        vtxt=await viewer.locator('#page').inner_text()
        print('viewer sees form:',await viewer.locator('#trial-form').count(),'| sees read-only note:', '仅管理员可发起' in vtxt)
        print('viewer export button:',await viewer.locator('button[id^=export-]').count())
        await viewer.screenshot(path=ART+'/screen-viewer.png',full_page=True)
        await b.close()

asyncio.run(main())

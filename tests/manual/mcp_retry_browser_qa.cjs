// Run only against mcp_retry_qa_app.py's temporary, synthetic port 9903.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

(async () => {
  const artifactRoot = process.env.QA_ARTIFACTS || os.tmpdir();
  fs.mkdirSync(artifactRoot, { recursive: true });
  const output = fs.mkdtempSync(path.join(artifactRoot, 'studio-retry-ui-screens-'));
  const browser = await chromium.launch({ headless: true,
    ...(process.env.PLAYWRIGHT_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE } : {}) });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const pageErrors = [], mutations = [];
  page.on('pageerror', e => pageErrors.push(e.message));
  page.on('request', r => { if (!['GET', 'HEAD'].includes(r.method())) mutations.push(r.method() + ' ' + r.url()); });
  await page.route('**/*', r => r.request().url().startsWith('http://127.0.0.1:9903/') ? r.continue() : r.abort());
  await page.addInitScript(() => { window.__coStudioSkipSplash = true; });
  try {
    await page.goto('http://127.0.0.1:9903/');
    await page.locator('#open-history').click();
    await page.locator('#history-sessions button').first().click();
    await page.locator('.history-turn').first().getByRole('button', { name: 'View execution' }).click();
    await page.getByText('Retry scheduled, not completed', { exact: false }).waitFor();
    const steps = page.locator('.history-turn').first().locator('.execution-step');
    assert.equal(await steps.count(), 5);
    assert.match(await steps.nth(2).textContent(), /MCP attempt record 1\/2 · studio_read_doc/);
    assert.match(await steps.nth(2).textContent(), /Recorded transport error · Retry scheduled, not completed · 12.5 ms/);
    assert.match(await steps.nth(3).textContent(), /MCP attempt record 2\/2 · studio_read_doc/);
    assert.match(await steps.nth(3).textContent(), /Recorded success · 12.5 ms/);
    assert.equal(await steps.nth(2).evaluate(el => el.classList.contains('is-error')), true);
    assert.equal(await steps.nth(3).evaluate(el => el.classList.contains('is-error')), false);
    await steps.nth(2).locator('summary').click();
    assert.match(await steps.nth(2).locator('pre').textContent(), /"call_id": "call-1"/);
    const overflows = async () => page.evaluate(() => [...document.querySelectorAll('#history-view, .history-detail, .history-master, .execution-timeline')]
      .filter(el => el.scrollWidth > el.clientWidth + 2).map(el => el.id || el.className));
    assert.deepEqual(await overflows(), []);
    await page.screenshot({ path: path.join(output, 'retry-observations-1440.png'), animations: 'disabled' });
    await steps.nth(2).locator('summary').click();
    await page.setViewportSize({ width: 1280, height: 900 });
    // Let the existing shell's 500 ms resize transition settle before visual QA.
    await page.waitForTimeout(550);
    assert.deepEqual(await overflows(), []);
    await page.screenshot({ path: path.join(output, 'retry-observations-1280.png'), animations: 'disabled' });
    await page.locator('.history-turn').nth(2).getByRole('button', { name: 'View execution' }).click();
    const cancellation = page.locator('.history-turn').nth(2).locator('.execution-step').nth(2);
    await cancellation.waitFor();
    assert.match(await cancellation.textContent(), /Recorded cancellation/);
    assert.doesNotMatch(await cancellation.textContent(), /Retry scheduled/);
    assert.deepEqual(pageErrors, []);
    assert.deepEqual(mutations, []);
    console.log(JSON.stringify({ ok: true, output, pageErrors, mutations }));
  } catch (e) {
    await page.screenshot({ path: path.join(output, 'failure.png') });
    console.error(JSON.stringify({ ok: false, error: e.stack, output, pageErrors, mutations }));
    process.exitCode = 1;
  } finally { await browser.close(); }
})();

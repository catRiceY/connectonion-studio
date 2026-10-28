// Run while history_qa_app.py is serving synthetic data on port 9902.
// PLAYWRIGHT_PATH may point to a locally installed Playwright module.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

(async () => {
  const output = fs.mkdtempSync(path.join(os.tmpdir(), 'history-browser-qa-'));
  const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE } : {}) });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  const mutations = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (!['GET', 'HEAD'].includes(request.method())) mutations.push(`${request.method()} ${request.url()}`);
  });
  await page.route('**/*', route => route.request().url().startsWith('http://127.0.0.1:9902/') ? route.continue() : route.abort());
  await page.addInitScript(() => { window.__coStudioSkipSplash = true; });
  try {
    await page.goto('http://127.0.0.1:9902/');
    await page.locator('#open-history').waitFor({ state: 'visible' });
    await page.locator('#open-history').click();
    await page.waitForFunction(() => document.querySelectorAll('#history-sessions button').length === 20);
    // All synthetic agents are stopped; the compact selector shows names only.
    assert.equal(await page.locator('#history-agent option[value="recorded"]').count(), 1);
    assert.equal(await page.locator('#history-agent option[value="missing"]').count(), 1);
    assert.equal(await page.locator('#history-count').textContent(), '(20/24)');
    assert.match(await page.locator('#history-warning').textContent(), /incomplete/);
    await page.locator('#history-sessions button').first().click();
    await page.waitForFunction(() => document.querySelectorAll('.history-turn').length === 20);
    assert.equal(await page.locator('#history-turn-count').textContent(), '(20/23)');
    assert.match(await page.locator('#history-turns').textContent(), /<img src=x/);
    assert.equal(await page.locator('#history-view img, #history-view script').count(), 0);
    assert.equal(await page.evaluate(() => window.__HISTORY_XSS__), undefined);
    assert.match(await page.locator('#history-turns').textContent(), /Not recorded/);
    assert.match(await page.locator('#history-events').textContent(), /Unassigned turn/);
    assert.equal(await page.locator('#history-event-count').textContent(), '(50/139)');
    const overflows = await page.evaluate(() => [...document.querySelectorAll('#history-view, .history-detail, .history-scroll, .history-master')].map(el => ({name: el.id || el.className, scrollWidth: el.scrollWidth, clientWidth: el.clientWidth})).filter(el => el.scrollWidth > el.clientWidth + 2));
    assert.deepEqual(overflows, []);
    await page.screenshot({ path: path.join(output, 'history-conversation.png') });
    await page.evaluate(() => { document.documentElement.dataset.appearance = 'lavender'; });
    await page.screenshot({ path: path.join(output, 'history-lavender.png') });
    await page.evaluate(() => { delete document.documentElement.dataset.appearance; });
    await page.locator('#history-more-sessions').click();
    await page.waitForFunction(() => document.querySelectorAll('#history-sessions button').length === 24);
    assert.equal(await page.locator('#history-count').textContent(), '(24)');
    await page.locator('#history-more-turns').click();
    await page.waitForFunction(() => document.querySelectorAll('.history-turn').length === 23);
    await page.locator('#history-more-events').click();
    await page.waitForFunction(() => document.querySelectorAll('.history-event').length === 100);
    await page.locator('#history-more-events').click();
    await page.waitForFunction(() => document.querySelectorAll('.history-event').length === 139);
    await page.locator('.history-turn').nth(1).getByRole('button', { name: 'View events' }).click();
    await page.waitForFunction(() => document.querySelector('#history-event-count').textContent === '(6/6)');
    assert.equal(await page.locator('#history-event-filter').inputValue(), '2');
    await page.locator('#history-event-filter').selectOption('2');
    await page.waitForFunction(() => document.querySelector('#history-event-count').textContent === '(6/6)');
    const errorEvent = page.locator('.history-event').filter({ hasText: 'tool_result' });
    await errorEvent.locator('summary').click();
    assert.match(await errorEvent.textContent(), /Synthetic timeout/);
    assert.match(await errorEvent.textContent(), /"status": "error"/);
    await page.screenshot({ path: path.join(output, 'history-event.png') });
    await page.locator('#history-event-filter').selectOption('all');
    await page.waitForFunction(() => document.querySelector('#history-event-count').textContent === '(50/139)');
    assert.match(await page.locator('#history-events').textContent(), /Unassigned turn/);
    await page.locator('#history-agent').selectOption('missing');
    await page.waitForFunction(() => document.querySelector('#history-list-message').textContent === 'No saved sessions.');
    assert.equal(await page.locator('#history-detail').isVisible(), false);
    assert.equal(await page.locator('#history-empty').textContent(), 'Select a session to view its history.');
    assert.equal(await page.locator('#history-list-message').evaluate(el => el.classList.contains('logs-empty') && el.classList.contains('history-empty-message')), true);
    assert.deepEqual(await page.locator('#history-list-message').evaluate(el => {
      const css = getComputedStyle(el);
      return { display: css.display, align: css.alignItems, justify: css.justifyContent, wrap: css.whiteSpace };
    }), { display: 'flex', align: 'center', justify: 'center', wrap: 'nowrap' });
    await page.locator('#history-agent').selectOption('empty');
    await page.waitForFunction(() => document.querySelector('#history-list-message').textContent.includes('No readable sessions'));
    await page.locator('#history-agent').selectOption('broken');
    await page.waitForFunction(() => document.querySelector('#history-warning').textContent.includes('incomplete'));
    await page.screenshot({ path: path.join(output, 'history-empty-warning.png') });
    await page.route('**/api/agents/recorded/sessions?*', route => route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Synthetic failure' })}));
    await page.locator('#history-agent').selectOption('recorded');
    await page.waitForFunction(() => document.querySelector('#history-list-message').textContent.includes('Could not load sessions'));
    assert.equal(await page.locator('#history-list-message').evaluate(el => el.classList.contains('logs-empty') || el.classList.contains('history-empty-message')), false);
    await page.unroute('**/api/agents/recorded/sessions?*');
    await page.locator('#history-refresh').click();
    await page.waitForFunction(() => document.querySelectorAll('#history-sessions button').length === 20);
    await page.locator('#history-more-sessions').click();
    await page.waitForFunction(() => document.querySelectorAll('#history-sessions button').length === 24);
    await page.locator('#history-sessions button').last().click();
    await page.waitForFunction(() => document.querySelector('#history-session-id').textContent === 'session-00');
    await page.locator('#history-refresh').click();
    await page.waitForFunction(() => !document.querySelector('#history-detail').hidden && document.querySelector('#history-session-id').textContent === 'session-00');
    assert.match(await page.locator('#history-list-message').textContent(), /outside this page/);
    for (const [id, className] of [['open-logs', 'is-logs'], ['open-market', 'is-market'], ['open-settings', 'is-settings']]) {
      await page.locator(`#${id}`).click();
      assert.equal(await page.locator('#app').evaluate((el, cls) => el.classList.contains(cls), className), true);
      assert.equal(await page.locator('#app').evaluate(el => el.classList.contains('is-history')), false);
      await page.locator('#open-history').click();
      assert.equal(await page.locator('#app').evaluate(el => el.classList.contains('is-history')), true);
      assert.equal(await page.locator('#app').evaluate((el, cls) => el.classList.contains(cls), className), false);
    }
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#app').evaluate(el => el.classList.contains('is-history')), false);
    await page.locator('#open-history').click();
    await page.locator('[data-view="agents"]').click();
    assert.equal(await page.locator('#app').evaluate(el => el.classList.contains('is-history')), false);
    await page.locator('#open-history').click();
    await page.locator('#btn-new-agent').click();
    await page.waitForFunction(() => document.querySelector('#app').classList.contains('is-creating'));
    assert.equal(await page.locator('#app').evaluate(el => el.classList.contains('is-history')), false);
    await page.keyboard.press('Escape');
    await page.locator('#open-history').click();
    await page.waitForFunction(() => document.querySelectorAll('#history-sessions button').length === 20);
    assert.deepEqual(errors, []);
    assert.deepEqual(mutations, []);
    console.log(JSON.stringify({ ok: true, screenshots: output, pageErrors: errors, mutations }, null, 2));
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png') });
    console.error(JSON.stringify({ ok: false, error: error.stack, screenshots: output, pageErrors: errors, mutations }, null, 2));
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();

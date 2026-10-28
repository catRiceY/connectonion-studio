// Only run against the isolated, synthetic diagnosis_qa_app.py fixture on 9905.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

(async () => {
  const artifacts = process.env.QA_ARTIFACTS || os.tmpdir(); fs.mkdirSync(artifacts, { recursive: true });
  const output = fs.mkdtempSync(path.join(artifacts, 'studio-diagnosis-ui-'));
  const browser = await chromium.launch({ headless: true,
    ...(process.env.PLAYWRIGHT_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE } : {}) });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = []; let generationCalls = 0;
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => { if (r.url().endsWith('/knowledge/generate')) generationCalls++; });
  await page.route('**/*', route => route.request().url().startsWith('http://127.0.0.1:9905/') ? route.continue() : route.abort());
  await page.addInitScript(() => { window.__coStudioSkipSplash = true; });
  const noOverflow = async () => assert.deepEqual(await page.evaluate(() => [...document.querySelectorAll('#knowledge-view, #knowledge-view .workspace-detail, #knowledge-results')]
    .filter(el => el.scrollWidth > el.clientWidth + 2).map(el => el.id || el.className)), []);
  async function confirm() {
    await page.locator('#knowledge-generate').click();
    await page.locator('#knowledge-generation-model').fill('synthetic-qa-no-model-call');
    await page.locator('#knowledge-generation-consent').check();
    await page.locator('#knowledge-generation-start').click();
  }
  try {
    await page.goto('http://127.0.0.1:9905/'); await page.locator('#open-knowledge').click();
    await page.locator('#knowledge-query').fill('断线后，怎么恢复之前的聊天记录并继续对话？');
    await page.locator('#knowledge-search').click(); await page.getByText('Synthetic reconnect guide', { exact: true }).last().waitFor();
    assert.equal(generationCalls, 0);
    await page.locator('#knowledge-generate').click();
    assert.equal(await page.locator('#knowledge-generation-start').isDisabled(), true);
    await noOverflow(); await page.screenshot({ path: path.join(output, 'confirmation-1440.png') });
    await page.locator('#knowledge-generation-model').fill('synthetic-qa-no-model-call');
    await page.locator('#knowledge-generation-consent').check(); await page.locator('#knowledge-generation-start').click();
    await page.getByText('诊断假设 · 待核实', { exact: true }).waitFor();
    assert.equal(generationCalls, 1);
    await noOverflow(); await page.screenshot({ path: path.join(output, 'diagnosis-1440.png') });
    await page.setViewportSize({ width: 1280, height: 900 }); await page.waitForTimeout(550);
    await noOverflow(); await page.screenshot({ path: path.join(output, 'diagnosis-1280.png') });
    await page.locator('#knowledge-query').fill('冲突：不同资料描述相反'); await confirm();
    await page.getByText('证据存在冲突', { exact: true }).waitFor();
    assert.equal(await page.locator('.knowledge-hypothesis').count(), 0);
    await page.locator('#knowledge-query').fill('取消：测试取消请求'); await confirm();
    await page.locator('#knowledge-generation-cancel').click();
    await page.getByText('已请求取消；已发送给服务商的请求仍可能计费。', { exact: true }).waitFor();
    await page.waitForTimeout(200);
    assert.equal(await page.locator('#knowledge-generate').isDisabled(), false);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, output, generationCalls, errors }));
  } catch (e) {
    await page.screenshot({ path: path.join(output, 'failure.png') });
    console.error(JSON.stringify({ ok: false, error: e.stack, output, errors })); process.exitCode = 1;
  } finally { await browser.close(); }
})();

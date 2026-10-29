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
  const page = await browser.newPage({ viewport: { width: 1265, height: 712 } });
  const errors = []; let generationCalls = 0;
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => { if (r.url().endsWith('/knowledge/generate')) generationCalls++; });
  await page.route('**/*', route => route.request().url().startsWith('http://127.0.0.1:9905/') ? route.continue() : route.abort());
  await page.addInitScript(() => { window.__coStudioSkipSplash = true; });
  const noOverflow = async () => {
    assert.deepEqual(await page.evaluate(() => [...document.querySelectorAll('#knowledge-view, #knowledge-view .workspace-detail, #knowledge-results')]
      .filter(el => el.scrollWidth > el.clientWidth + 2).map(el => el.id || el.className)), []);
    const shell = await page.evaluate(() => {
      const app = document.querySelector('#app'); const sidebar = document.querySelector('.sidebar');
      return { scrollLeft: app.scrollLeft, inset: sidebar.getBoundingClientRect().left - app.getBoundingClientRect().left };
    });
    assert.equal(shell.scrollLeft, 0, 'focus must not scroll the application shell horizontally');
    assert.ok(shell.inset >= 0 && shell.inset <= 2, `sidebar moved outside shell: ${shell.inset}`);
  };
  async function confirm(budget = '4096') {
    await page.locator('#knowledge-generate').click();
    await page.locator('#knowledge-generation-model').fill('synthetic-qa-no-model-call');
    await page.locator('#knowledge-generation-budget').selectOption(budget);
    await page.locator('#knowledge-generation-consent').check();
    await page.locator('#knowledge-generation-start').click();
  }
  try {
    await page.goto('http://127.0.0.1:9905/');
    await page.locator('#viewport-gate.is-shown').waitFor();
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator('#viewport-gate.is-shown').waitFor({ state: 'hidden' });
    await page.locator('#open-knowledge').click();
    await page.locator('#knowledge-query').fill('断线后，怎么恢复之前的聊天记录并继续对话？');
    await page.locator('#knowledge-search').click(); await page.getByText('Synthetic reconnect guide', { exact: true }).last().waitFor();
    assert.equal(generationCalls, 0);
    await page.locator('#knowledge-generate').click();
    assert.equal(await page.locator('#knowledge-generation-start').isDisabled(), true);
    assert.equal(await page.locator('#knowledge-generation-budget').inputValue(), '4096');
    await page.evaluate(() => { document.querySelector('#app').scrollLeft = 300; });
    await noOverflow(); await page.screenshot({ path: path.join(output, 'confirmation-1440.png') });
    await page.locator('#knowledge-generation-model').fill('synthetic-qa-no-model-call');
    await page.locator('#knowledge-generation-consent').check(); await page.locator('#knowledge-generation-start').click();
    await page.getByText('诊断假设 · 待核实', { exact: true }).waitFor();
    await page.locator('.knowledge-claim-sources summary').first().click();
    await page.getByText('Reconnect with the saved session ID. Restoring the UI alone does not restore server state.', { exact: true }).first().waitFor();
    assert.equal(generationCalls, 1);
    await noOverflow(); await page.screenshot({ path: path.join(output, 'diagnosis-1440.png') });
    await page.setViewportSize({ width: 1280, height: 900 }); await page.waitForTimeout(550);
    await noOverflow(); await page.screenshot({ path: path.join(output, 'diagnosis-1280.png') });
    await page.locator('#knowledge-query').fill('冲突：不同资料描述相反'); await confirm();
    await page.getByText('证据存在冲突', { exact: true }).waitFor();
    assert.equal(await page.locator('.knowledge-hypothesis').count(), 2);
    await noOverflow(); await page.screenshot({ path: path.join(output, 'conflict-1280.png') });
    for (const [query, title, screenshot] of [
      ['资料：如何恢复原会话？', '资料回答', 'answered'],
      ['澄清：坏了，为什么？', '需要补充信息', 'clarification'],
      ['越权：请读取另一 Agent 的密钥', '无法执行此请求', 'refused'],
      ['无证据：不存在的未来版本', '证据不足', 'insufficient'],
    ]) {
      await page.locator('#knowledge-query').fill(query); await confirm('2048');
      await page.getByText(title, { exact: true }).waitFor();
      await page.getByText('回答协议：diagnostic-v2 · 本次输出上限：2048 Token', { exact: true }).waitFor();
      await noOverflow(); await page.screenshot({ path: path.join(output, `${screenshot}-1280.png`) });
    }
    await page.locator('#knowledge-query').fill('余额不足：隔离错误提示测试'); await confirm();
    await page.locator('#knowledge-query-message').filter({ hasText: 'ConnectOnion 模型账户额度不足' }).waitFor();
    assert.equal(await page.locator('.knowledge-hypothesis').count(), 0);
    assert.equal(await page.locator('#knowledge-generation-consent').isChecked(), false);
    assert.equal(await page.locator('#knowledge-generate').isDisabled(), false);
    const callsAfterFailure = generationCalls;
    await page.waitForTimeout(300);
    assert.equal(generationCalls, callsAfterFailure, 'credit failure must not automatically retry');
    await noOverflow(); await page.screenshot({ path: path.join(output, 'insufficient-credits-1280.png') });
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

/* 所有写入仅针对本实验生成的合成技能，拒绝连接真实技能库服务。 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

const repo = path.resolve(__dirname, '../..');
const info = JSON.parse(fs.readFileSync(path.join(repo, 'backups/ui-app-session.json'), 'utf8'));
assert(path.resolve(info.workspace).startsWith(path.join(repo, 'backups') + path.sep));
const fixture = JSON.parse(fs.readFileSync(path.join(info.workspace, 'data/roots.json'), 'utf8'));
assert(fixture.roots.every(root => path.resolve(root.path).startsWith(info.workspace + path.sep)));
const report = { date: '2026-10-28', environment: 'Chromium，Windows，合成技能隔离目录', checks: [], metrics: {} };
let browser, page;
const errors = [];

async function ready(target) {
  await target.goto(info.url);
  await target.waitForFunction(() => document.querySelector('#quickSummaryText').textContent.startsWith('已就绪'));
}
async function check(name, callback) {
  try {
    await callback();
    report.checks.push({ name, passed: true });
    console.log('PASS ' + name);
  } catch (error) {
    report.checks.push({ name, passed: false, error: error.message });
    console.error('FAIL ' + name + ': ' + error.message);
    await page.screenshot({ path: path.join(info.workspace, 'failure-' + report.checks.length + '.png') }).catch(() => {});
    await ready(page).catch(() => {});
  }
}
async function measurements(target) {
  return target.evaluate(() => ({
    viewport: innerWidth,
    documentWidth: document.documentElement.scrollWidth,
    unlabelledControls: [...document.querySelectorAll('input,select,textarea')]
      .filter(el => !el.labels?.length && !el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby')).map(el => el.id),
    categoryButtons: document.querySelectorAll('button.category-pill').length,
    dialogRole: document.querySelector('#genericModal .modal').getAttribute('role'),
    serviceText: document.querySelector('#daemonText').textContent,
    primaryColor: getComputedStyle(document.querySelector('#btnScan')).color,
    primaryBackground: getComputedStyle(document.querySelector('#btnScan')).backgroundColor,
    primaryGradient: getComputedStyle(document.querySelector('#btnScan')).backgroundImage,
  }));
}
function contrast(foreground, background) {
  const luminance = value => {
    const channels = value.match(/[\d.]+/g).slice(0, 3).map(Number).map(n => n / 255)
      .map(n => n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  };
  const a = luminance(foreground), b = luminance(background);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

(async () => {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, deviceScaleFactor: 1 });
  const baseline = await context.newPage();
  const original = fs.readFileSync(path.join(info.workspace, 'index-before.html'), 'utf8');
  await baseline.route(info.url + '/', route => route.fulfill({ contentType: 'text/html; charset=utf-8', body: original }));
  await ready(baseline);
  report.metrics.beforeDesktop = await measurements(baseline);
  await baseline.screenshot({ path: path.join(repo, 'docs/images/ui-evaluation-before.png') });
  await baseline.setViewportSize({ width: 390, height: 844 });
  report.metrics.beforeMobile = await measurements(baseline);
  await baseline.screenshot({ path: path.join(info.workspace, 'before-mobile.png') });
  await baseline.close();

  page = await context.newPage();
  page.setDefaultTimeout(10000);
  page.on('pageerror', error => errors.push(error.message));
  await ready(page);
  report.metrics.afterDesktop = await measurements(page);
  await page.screenshot({ path: path.join(repo, 'docs/images/ui-evaluation-after.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  report.metrics.afterMobile = await measurements(page);
  await page.screenshot({ path: path.join(repo, 'docs/images/ui-evaluation-mobile.png') });
  await page.setViewportSize({ width: 1440, height: 960 });

  await check('控件名称、标签和对话框语义', async () => {
    assert.deepEqual(report.metrics.afterDesktop.unlabelledControls, []);
    assert.equal(report.metrics.afterDesktop.categoryButtons, 8);
    assert.equal(report.metrics.afterDesktop.dialogRole, 'dialog');
  });
  await check('关闭的抽屉不暴露给焦点或辅助技术，标签栏不产生竖向滚动', async () => {
    const state = await page.evaluate(() => ({
      inert: document.querySelector('#drawerOverlay').inert,
      hidden: document.querySelector('#drawerOverlay').getAttribute('aria-hidden'),
      visibility: getComputedStyle(document.querySelector('#drawerOverlay')).visibility,
      overflowY: getComputedStyle(document.querySelector('.nav-tabs')).overflowY,
    }));
    assert.equal(state.inert, true);
    assert.equal(state.hidden, 'true');
    assert.equal(state.visibility, 'hidden');
    assert.equal(state.overflowY, 'hidden');
  });
  await check('主按钮文字对比度', async () => {
    const m = report.metrics.afterDesktop;
    report.metrics.primaryContrast = Number(contrast(m.primaryColor, m.primaryBackground).toFixed(2));
    assert(report.metrics.primaryContrast >= 4.5);
    const stops = report.metrics.beforeDesktop.primaryGradient.match(/rgba?\([^)]+\)/g);
    report.metrics.beforePrimaryContrast = Number(Math.min(...stops.map(bg => contrast(report.metrics.beforeDesktop.primaryColor, bg))).toFixed(2));
  });
  await check('分类可用键盘选择并同步筛选', async () => {
    const button = page.locator('.category-pill[data-cat="前端与动效"]');
    await button.focus();
    await page.keyboard.press('Enter');
    assert.equal(await button.getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('#filterCategorySelect').inputValue(), '前端与动效');
    assert(Number(await page.locator('#filteredCount').textContent()) > 0);
    await page.locator('#btnClearFilters').click();
  });
  await check('搜索、空结果提示和清除筛选', async () => {
    await page.locator('#nav-catalog').focus();
    await page.keyboard.press('/');
    assert(await page.locator('#searchInput').evaluate(el => el === document.activeElement));
    await page.locator('#searchInput').fill('ui-design');
    assert.equal(await page.locator('#filteredCount').textContent(), '1');
    await page.locator('#searchInput').fill('no-such-skill-qa');
    assert.equal(await page.locator('#filteredCount').textContent(), '0');
    assert(await page.locator('#skillsTableBody').innerText().then(text => text.includes('清除筛选')));
    await page.locator('#btnClearFilters').click();
    assert.equal(await page.locator('#filterCategorySelect').inputValue(), 'all');
    assert.equal(await page.locator('#filteredCount').textContent(), await page.locator('#totalSkillsCount').textContent());
  });
  await check('GitHub 来源与 Agent 筛选', async () => {
    await page.locator('#filterStatus').selectOption('source_github');
    assert.equal(await page.locator('#filteredCount').textContent(), '1');
    assert((await page.locator('#skillsTableBody').innerText()).includes('api-review'));
    await page.locator('#btnClearFilters').click();
    await page.locator('#filterAgent').selectOption('Codex');
    assert(Number(await page.locator('#filteredCount').textContent()) > 0);
    await page.locator('#btnClearFilters').click();
  });
  await check('标签页方向键与选中状态', async () => {
    await page.locator('#nav-catalog').focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#nav-deploy').getAttribute('aria-selected'), 'true');
    assert(await page.locator('#tabDeploy').isVisible());
    await page.keyboard.press('Home');
    assert(await page.locator('#tabCatalog').isVisible());
  });
  await check('抽屉焦点循环、嵌套确认和 Escape 返回', async () => {
    const name = page.locator('#skillsTableBody .skill-title').filter({ hasText: /^ui-design$/ });
    await name.focus();
    await page.keyboard.press('Enter');
    assert(await page.locator('#btnDrawerClose').evaluate(el => el === document.activeElement));
    await page.keyboard.press('Shift+Tab');
    assert(await page.locator('#btnDrawerCloseBottom').evaluate(el => el === document.activeElement));
    await page.keyboard.press('Tab');
    assert(await page.locator('#btnDrawerClose').evaluate(el => el === document.activeElement));
    await page.locator('#btnDeleteSkill').click();
    assert(await page.locator('#btnModalCancel').evaluate(el => el === document.activeElement));
    assert(await page.locator('#drawerOverlay').evaluate(el => el.inert));
    await page.keyboard.press('Escape');
    assert(await page.locator('#drawerOverlay').evaluate(el => el.classList.contains('open')));
    assert(await page.locator('#btnDeleteSkill').evaluate(el => el === document.activeElement));
    await page.keyboard.press('Escape');
    assert(await name.evaluate(el => el === document.activeElement));
  });
  await check('备注和分类通过真实 API 保存，文本安全显示', async () => {
    const row = page.locator('#skillsTableBody tr').filter({ has: page.locator('.skill-title', { hasText: /^ui-design$/ }) });
    await row.locator('.btn-edit-note').click();
    await page.locator('#quickRemarkText').fill('回归校验：<b>仅作为文本</b>');
    await page.locator('#quickRemarkCatSelect').selectOption('前端与动效');
    await page.locator('#btnQuickRemarkSave').click();
    await page.waitForFunction(() => !document.querySelector('#quickRemarkModal').classList.contains('open'));
    const notes = await (await context.request.get(info.url + '/api/skills/notes')).json();
    assert.equal(notes.notes['ui-design'], '回归校验：<b>仅作为文本</b>');
    assert((await row.innerHTML()).includes('&lt;b&gt;仅作为文本&lt;/b&gt;'));
  });
  await check('外部同名版本禁止删除或重新部署统一库内容', async () => {
    const external = page.locator('#skillsTableBody tr').filter({ hasText: '不同版本的表单布局与校验指南' });
    await external.locator('.skill-title').click();
    assert(await page.locator('#btnDeleteSkill').isDisabled());
    assert(await page.locator('#btnRefreshSingle').isDisabled());
    assert(await page.locator('#drawerReadOnlyHint').isVisible());
    await page.keyboard.press('Escape');
  });
  await check('后端拒绝已变化版本的删除，源文件保持存在', async () => {
    const response = await context.request.post(info.url + '/api/skills/delete', {
      data: { directory: 'ui-design', confirm: 'MOVE_SKILL_TO_RECYCLE_BIN', expected_hash: '0'.repeat(64) },
    });
    assert.equal(response.status(), 409);
    assert(fs.existsSync(path.join(info.workspace, 'skills/ui-design/SKILL.md')));
  });
  await check('冲突保留和融合预览可取消', async () => {
    await page.locator('#nav-conflicts').click();
    await page.locator('[data-conflict-keep]').first().click();
    assert(await page.locator('#genericModal').evaluate(el => el.classList.contains('open')));
    await page.keyboard.press('Escape');
    await page.locator('[data-conflict-merge]').first().click();
    await page.waitForFunction(() => document.querySelector('#genericModal').classList.contains('open'));
    assert((await page.locator('#modalBody').innerText()).length > 20);
    await page.keyboard.press('Escape');
  });
  await check('部署预览与真实部署完成数量一致', async () => {
    await page.locator('#nav-deploy').click();
    await page.locator('#btnDeployPlanPreview').click();
    await page.waitForFunction(() => document.querySelector('#genericModal').classList.contains('open'));
    await page.keyboard.press('Escape');
    await page.locator('#btnDeployApply').click();
    const response = page.waitForResponse(res => res.url().endsWith('/api/deploy/apply'), { timeout: 90000 });
    await page.locator('#btnModalConfirm').click();
    const result = await (await response).json();
    assert(result.summary.linked > 0);
    await page.waitForFunction(() => !document.querySelector('#genericModal').classList.contains('open'), { timeout: 90000 });
    assert((await page.locator('#toastContainer').innerText()).includes(`已新建 ${result.summary.linked} 个联接`));
  });
  await check('旧异步部署回调不关闭后来打开的确认框（受控响应）', async () => {
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    await page.route('**/api/deploy/apply', async route => {
      await gate;
      await route.fulfill({ json: { summary: { linked: 2, already_linked: 5, failed: 0 } } });
    });
    try {
      await page.locator('#nav-deploy').click();
      await page.locator('#btnDeployApply').click();
      await page.locator('#btnModalConfirm').click();
      assert.equal(await page.locator('#btnModalCancel').textContent(), '关闭窗口');
      await page.locator('#btnModalCancel').click();
      await page.locator('#btnDeployPlanPreview').click();
      await page.waitForFunction(() => document.querySelector('#genericModal').classList.contains('open'));
      const loaded = page.waitForResponse(response => response.url().endsWith('/api/import-plan'));
      release();
      await loaded;
      await page.waitForFunction(() => document.querySelector('#tabCatalog').getAttribute('aria-busy') === 'false');
      assert(await page.locator('#genericModal').evaluate(el => el.classList.contains('open')));
      assert((await page.locator('#modalTitle').textContent()).includes('预览'));
      assert(await page.locator('#btnModalConfirm').isEnabled());
      await page.keyboard.press('Escape');
    } finally {
      release();
      await page.unroute('**/api/deploy/apply');
    }
  });
  await check('本地创建使用真实隔离后端', async () => {
    await page.locator('#nav-import').click();
    await page.locator('#localSkillName').fill('qa-created-skill');
    await page.locator('#localSkillDescription').fill('合成技能，用于验证本地创建流程');
    await page.locator('#localSkillInstructions').fill('只读取合成测试目录，不操作真实资料。');
    const response = page.waitForResponse(res => res.url().endsWith('/api/sources/create'), { timeout: 60000 });
    await page.locator('#localSkillForm button[type="submit"]').click();
    assert((await response).ok());
    await page.waitForFunction(() => document.querySelector('#localSkillName').value === '');
    const sources = await (await context.request.get(info.url + '/api/sources')).json();
    assert.equal(sources.sources['qa-created-skill'].type, 'local');
  });
  await check('压缩包预览、选择与导入', async () => {
    await page.locator('#nav-import').click();
    await page.locator('#archiveFile').setInputFiles(info.archive);
    await page.locator('#btnArchivePreview').click();
    await page.locator('.source-candidate-select').waitFor();
    assert.equal(await page.locator('.source-candidate-select').count(), 1);
    await page.locator('#btnApplySourcePreview').click();
    const response = page.waitForResponse(res => res.url().endsWith('/api/sources/apply'), { timeout: 60000 });
    await page.locator('#btnModalConfirm').click();
    assert((await response).ok());
    await page.waitForFunction(() => !document.querySelector('#genericModal').classList.contains('open'));
    const sources = await (await context.request.get(info.url + '/api/sources')).json();
    assert.equal(sources.sources['archive-example'].type, 'archive');
  });
  await check('320 至 1440px 四个工作区均无页面横向溢出', async () => {
    report.metrics.viewportChecks = [];
    for (const width of [1440, 1024, 768, 390, 320]) {
      await page.setViewportSize({ width, height: 960 });
      for (const tab of ['catalog', 'deploy', 'import', 'conflicts']) {
        await page.locator('#nav-' + tab).click();
        const size = await page.evaluate(() => ({ width: innerWidth, documentWidth: document.documentElement.scrollWidth }));
        report.metrics.viewportChecks.push({ ...size, tab });
        assert(size.documentWidth <= width + 1, JSON.stringify({ ...size, tab }));
      }
    }
  });
  await check('移动端主要操作触控尺寸', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#nav-catalog').click();
    const small = await page.locator('#btnScan, #btnReconcile, .nav-tab, #filterCategorySelect, #filterAgent, #filterStatus, .row-actions button, .skill-title').evaluateAll(items => items.filter(el => el.getClientRects().length && el.getBoundingClientRect().height < 43.5).map(el => el.id || el.className));
    assert.deepEqual(small, []);
  });
  await check('减少动态效果设置生效', async () => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    const duration = await page.locator('#btnScan').evaluate(el => getComputedStyle(el).transitionDuration);
    assert(duration.split(',').every(value => parseFloat(value) <= 0.001));
    await page.emulateMedia({ reducedMotion: 'no-preference' });
  });
  await check('服务异常有持久提示并可重新连接', async () => {
    await page.route('**/api/health', route => route.abort('failed'));
    await page.reload();
    await page.locator('#serviceNotice').waitFor({ state: 'visible' });
    await page.unroute('**/api/health');
    await page.locator('#btnReconnect').click();
    await page.waitForFunction(() => document.querySelector('#quickSummaryText').textContent.startsWith('已就绪'));
    assert(await page.locator('#serviceNotice').isHidden());
    assert.equal(await page.locator('#daemonText').textContent(), '本地服务在线');
  });
  await check('扫描时间不变时轮询恢复也清除失败状态', async () => {
    await page.route('**/api/skills', route => route.abort('failed'));
    await page.evaluate(() => pollForScanUpdates());
    assert(await page.locator('#serviceNotice').isVisible());
    await page.unroute('**/api/skills');
    await page.evaluate(() => pollForScanUpdates());
    assert(await page.locator('#serviceNotice').isHidden());
    assert.equal(await page.locator('#daemonText').textContent(), '本地服务在线');
  });
  await check('无未处理的页面 JavaScript 异常', async () => assert.deepEqual(errors, []));
  report.summary = { passed: report.checks.filter(item => item.passed).length, total: report.checks.length };
  fs.writeFileSync(path.join(__dirname, 'app-ui-results.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report.summary));
  process.exitCode = report.summary.passed === report.summary.total ? 0 : 1;
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => { if (browser) await browser.close(); });

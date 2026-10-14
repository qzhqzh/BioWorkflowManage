(() => {
  const menu = document.querySelector('.menu-toggle');
  const nav = document.querySelector('.site-nav');
  function closeMenu() { if (menu && nav) { menu.setAttribute('aria-expanded', 'false'); nav.dataset.open = 'false'; } }
  if (menu && nav) {
    menu.addEventListener('click', () => { const open = menu.getAttribute('aria-expanded') !== 'true'; menu.setAttribute('aria-expanded', String(open)); nav.dataset.open = String(open); });
    document.addEventListener('keydown', event => { if (event.key === 'Escape' && nav.dataset.open === 'true') { closeMenu(); menu.focus(); } });
    nav.addEventListener('click', event => { if (event.target.closest('a')) closeMenu(); });
  }
  const setupTabs = [...document.querySelectorAll('.setup-tabs [role="tab"]')];
  function selectTab(selected, focus = false) {
    setupTabs.forEach(tab => {
      const active = tab === selected;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(tab.getAttribute('aria-controls')).hidden = !active;
    });
    if (focus) selected.focus();
  }
  setupTabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectTab(tab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % setupTabs.length;
      if (event.key === 'ArrowLeft') next = (index + setupTabs.length - 1) % setupTabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = setupTabs.length - 1;
      if (next !== undefined) { event.preventDefault(); selectTab(setupTabs[next], true); }
    });
  });
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    const input = document.createElement('textarea');
    input.value = text; input.setAttribute('readonly', ''); input.className = 'clipboard-fallback';
    document.body.append(input); input.select();
    const ok = document.execCommand('copy'); input.remove();
    if (!ok) throw new Error('Copy unavailable');
  }
  document.querySelectorAll('[data-copy-target], [data-copy-url]').forEach(button => {
    button.addEventListener('click', async () => {
      const original = button.innerHTML;
      const status = button.parentElement.querySelector('.code-status');
      button.disabled = true;
      try {
        let text;
        if (button.dataset.copyUrl) {
          const response = await fetch(button.dataset.copyUrl, {credentials: 'omit'});
          if (!response.ok || (response.headers.get('Content-Type') || '').includes('text/html')) throw new Error('Document unavailable');
          text = await response.text();
        } else { text = document.querySelector(button.dataset.copyTarget).textContent; }
        await copyText(text);
        if (status) status.textContent = '已复制';
        else button.textContent = '已复制接入说明';
        button.setAttribute('aria-label', '已复制');
      } catch (_) {
        if (status) status.textContent = '请选中文字复制';
        else button.textContent = '复制失败，请打开 Markdown';
      } finally {
        button.disabled = false;
        setTimeout(() => { button.innerHTML = original; if (status) status.textContent = ''; button.setAttribute('aria-label', button.dataset.copyLabel || '复制'); }, 2600);
      }
    });
  });
  function download(filename, text) {
    const url = URL.createObjectURL(new Blob([text], {type:'text/markdown;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = filename; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const checks = [...document.querySelectorAll('[data-evaluation-check]')];
  const storageKey = 'bwm-evaluation-v1.1.0-20261013';
  if (checks.length) {
    let stored = {}; try { const saved = JSON.parse(localStorage.getItem(storageKey) || '{}'); if (saved && typeof saved === 'object' && !Array.isArray(saved)) stored = saved; } catch (_) { stored = {}; }
    checks.forEach(box => { box.checked = stored[box.id] === true; });
    const count = document.querySelector('[data-evaluation-count]');
    function update() { if (count) count.textContent = checks.filter(box => box.checked).length + ' / ' + checks.length + ' 项已确认'; }
    checks.forEach(box => box.addEventListener('change', () => {
      update(); try { localStorage.setItem(storageKey, JSON.stringify(Object.fromEntries(checks.map(item => [item.id, item.checked])))); } catch (_) { const warning = document.querySelector('[data-evaluation-storage]'); if (warning) warning.textContent = '浏览器未允许保存，请下载当前记录。'; }
    }));
    update();
    document.querySelector('[data-export-evaluation]')?.addEventListener('click', () => {
      const lines = ['# BioWorkflowManage 试部署测评', '', '- 底座版本：v1.1.0', '- 记录时间：' + new Date().toISOString(), '- 流程产品与版本：待填写', '- 环境、资源与数据规模：待填写', '- 测评人：待填写', '', '> 本表是使用者自评记录；勾选不替代测试证据或生产准入评审。', ''];
      checks.forEach(box => lines.push('- [' + (box.checked ? 'x' : ' ') + '] ' + box.closest('label').textContent.trim()));
      lines.push('', '## 证据与问题', '', '填写运行 ID、脱敏日志、预期与实际结果；不粘贴 Token、密码或真实业务数据。', '', '## 下一步', '', '待确认 / 继续试用 / 修复后复测 / 有条件进入生产评审', '');
      download('bwm-v1.1.0-evaluation.md', lines.join('\n'));
    });
  }
  const form = document.querySelector('#feedback-form');
  if (form) form.addEventListener('submit', event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const data = new FormData(form);
    const lines = ['# ' + data.get('title'), '', '- 底座版本：' + data.get('version'), '- 流程产品 / 契约版本：' + data.get('product'), '- 业务应用版本：' + data.get('application'), '- 运行环境：' + data.get('environment'), '- 影响：' + data.get('impact'), '', '## 复现步骤', '', data.get('steps'), '', '## 预期结果', '', data.get('expected'), '', '## 实际结果与证据', '', data.get('actual'), '', '## 脱敏确认', '', '- [ ] 已移除凭据、内部地址和真实业务数据。', ''];
    download('bwm-feedback.md', lines.join('\n'));
    document.querySelector('#feedback-status').textContent = '反馈文件已生成，可检查后交给维护者。';
  });
})();

/* Enhance WebUI's native status toggle without changing stored messages or jobs. */
(() => {
  if (window.companyStatusHistoryInstalled) return;
  window.companyStatusHistoryInstalled = true;

  const initialized = new WeakSet();
  const completionState = new WeakMap();
  const nativeLabels = new WeakMap();
  const pending = new Set();
  let scheduled = false;
  const text = node => (node?.textContent || '').replace(/\s+/g, ' ').trim();
  const plain = node => !node.querySelector('a, button, img, [role="button"]');

  function enhance(panel, button, completed) {
    panel.dataset.companyStatusHistory = '';
    panel.dataset.companyStatusCompleted = String(completed);
    button.classList.add('company-status-toggle');
    const first = !initialized.has(button);
    const finishing = completed && completionState.get(button) !== true;
    if (first) {
      initialized.add(button);
      nativeLabels.set(button, button.getAttribute('aria-label') || '展開/收合歷史狀態');
    }
    // Collapse once on completion; later clicks still belong to the user.
    if ((first || finishing) && button.getAttribute('aria-expanded') === 'true') button.click();
    completionState.set(button, completed);
    const expanded = button.getAttribute('aria-expanded') === 'true';
    button.title = expanded ? '收合處理過程' : '點擊展開處理過程';
    button.setAttribute('aria-label', completed ? (expanded ? '收合處理過程' : '展開處理過程') : nativeLabels.get(button));
    const current = button.querySelector('.status-description');
    current?.setAttribute('aria-live', 'polite');
    current?.setAttribute('aria-atomic', 'true');

    const history = [...panel.querySelectorAll('.status-description')]
      .filter(node => !button.contains(node))
      .map(node => ({node, row: node.parentElement, value: text(node)}));
    let previous = null;
    for (const item of history) {
      delete item.row.dataset.companyStatusDuplicate;
      delete item.row.dataset.companyHistoryTail;
      if (plain(item.node) && item.value === previous) {
        item.row.dataset.companyStatusDuplicate = '';
      }
      previous = plain(item.node) ? item.value : null;
    }
    // The latest status remains above the history, so omit its repeated tail.
    for (let index = history.length - 1; !completed && index >= 0; index--) {
      const item = history[index];
      if (!plain(item.node) || !plain(current) || item.value !== text(current)) break;
      item.row.dataset.companyStatusDuplicate = '';
    }
    const visible = history.filter(item => !item.row.hasAttribute('data-company-status-duplicate'));
    visible.at(-1)?.row.setAttribute('data-company-history-tail', '');
    panel.dataset.companyHistoryEmpty = String(expanded && visible.length === 0);
  }

  function refresh(message) {
    if (!message.isConnected) return;
    const panels = new Map();
    for (const status of message.querySelectorAll('.status-description')) {
      const button = status.closest('button[aria-expanded]');
      if (button && !button.closest('#response-content-container') && message.contains(button) &&
          button.parentElement.classList.contains('flex-col')) {
        panels.set(button.parentElement, button);
      }
    }
    const ownedJob = message.querySelector('a[href^="/agents?job="]');
    const current = [...panels.values()].map(button => button.querySelector('.status-description'));
    const completed = Boolean(ownedJob && current.length &&
      current.every(status => !status.querySelector('.shimmer')) &&
      text(message.querySelector('#response-content-container')));
    for (const [panel, button] of panels) enhance(panel, button, completed);

    // Old receipts contain the same pending status in both the native header and body.
    // Keep the work/cancel link, and reveal normal answer paragraphs after completion.
    for (const paragraph of message.querySelectorAll('[data-company-redundant-status]')) {
      delete paragraph.dataset.companyRedundantStatus;
    }
    if (!message.querySelector('a[href^="/agents?job="]')) return;
    const latest = [...panels.values()]
      .map(button => button.querySelector('.status-description'))
      .filter(status => status.querySelector('.shimmer'));
    const paragraphs = [...message.querySelectorAll('#response-content-container p')];
    // The terminal outcome is available in the process history; hide its entire
    // redundant body heading while keeping the result details and links.
    const result = paragraphs[0];
    if (completed && result && plain(result) && /^工作狀態\s*[:：]/.test(text(result))) {
      result.dataset.companyRedundantStatus = '';
    }
    // Omit the generated footer, retaining downloads embedded in the actual result.
    const footer = paragraphs.findIndex(paragraph =>
      text(paragraph) === '查看工作、下載與確認操作' &&
      paragraph.querySelector('a[href^="/agents?job="]'));
    if (completed && footer >= 0) {
      for (let index = footer; index < paragraphs.length; index++) {
        const paragraph = paragraphs[index];
        const links = paragraph.querySelectorAll('a');
        if (links.length !== 1 || text(paragraph) !== text(links[0]) ||
            (index > footer && !/^下載檔案\s+\d+$/.test(text(links[0])))) break;
        paragraph.dataset.companyRedundantStatus = '';
      }
    }
    const receipt = latest.length > 0 && paragraphs.length === 2 &&
      paragraphs[1].querySelector('a[href^="/agents?job="]') &&
      text(paragraphs[1]) === '查看工作或取消';
    for (const paragraph of paragraphs) {
      if (plain(paragraph) && ((receipt && paragraph === paragraphs[0]) ||
          latest.some(status => text(status) === text(paragraph)))) {
        paragraph.dataset.companyRedundantStatus = '';
      }
    }
  }

  function collect(node) {
    const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    if (!element) return;
    const owner = element.closest('[id^="message-"]');
    if (owner) pending.add(owner);
    for (const message of element.querySelectorAll('[id^="message-"]')) pending.add(message);
  }

  function flush() {
    scheduled = false;
    const messages = [...pending];
    pending.clear();
    for (const message of messages) refresh(message);
  }

  const observer = new MutationObserver(records => {
    for (const record of records) {
      collect(record.target);
      for (const node of record.addedNodes) collect(node);
    }
    if (pending.size && !scheduled) {
      scheduled = true;
      requestAnimationFrame(flush);
    }
  });
  observer.observe(document.documentElement, {
    subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ['aria-expanded'],
  });
  collect(document.documentElement);
  flush();
})();

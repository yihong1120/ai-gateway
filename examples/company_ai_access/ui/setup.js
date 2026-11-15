'use strict';
const statusElement = document.getElementById('status');
const button = document.getElementById('approve');
const toolChoice = document.getElementById('tool-choice');
function showInstallCommand() {
  const tool = toolChoice.value;
  const switches = tool === 'Both' ? '' : ' -Tools ' + tool;
  document.getElementById('install-command').textContent = '.\\Setup-CompanyAI.ps1 -Portal ' + location.origin + switches;
}
toolChoice.addEventListener('change', showInstallCommand);
showInstallCommand();
const query = new URLSearchParams(location.search);
const requestId = query.get('request_id');
let enrollment;
let expiryTimer;
const apiPrefix = document.querySelector('main').dataset.apiPrefix || '/api/company-ai';
const loginLink = document.getElementById('login');
const loginHelp = document.getElementById('login-help');
const loginAttempt = 'company-ai-login:' + requestId;
if (apiPrefix.startsWith('/bff/')) {
  loginLink.href = '/bff/auth/oidc/login?return_to=' + encodeURIComponent(location.pathname + location.search);
}
function showError(error) {
  statusElement.textContent = error.message;
  if (error.status !== 401) return;
  button.disabled = true;
  loginHelp.hidden = false;
  // A single automatic login per request avoids a redirect loop if SSO fails.
  // Session storage contains only a request identifier, never a device credential.
  if (apiPrefix.startsWith('/bff/') && /^[0-9a-f-]{36}$/.test(requestId || '')) {
    try {
      if (!sessionStorage.getItem(loginAttempt)) {
        sessionStorage.setItem(loginAttempt, '1');
        statusElement.textContent = '正在前往公司登入；登入後會返回此驗證碼頁，請核對後按「核准此電腦」。';
        location.assign(loginLink.href);
      }
    } catch (_) { /* If storage/navigation is unavailable, use the login link. */ }
  }
}
function responseError(response) {
  const message = response.status === 401 ? '尚未登入。請按「登入公司網站」，登入後在此頁核對驗證碼並核准。' :
    response.status === 410 ? '此綁定請求已超過 10 分鐘期限。請回 PowerShell 重新執行設定工具，再開啟新的綁定網址。' :
    '無法完成綁定（HTTP ' + response.status + '）。請重新執行設定工具或聯絡管理員。';
  return Object.assign(new Error(message), { status: response.status });
}
async function request(path, options = {}) {
  if (options.method === 'POST' && apiPrefix.startsWith('/bff/')) {
    const csrfResponse = await fetch('/bff/auth/csrf', { credentials: 'same-origin' });
    if (!csrfResponse.ok) throw responseError(csrfResponse);
    const csrf = await csrfResponse.json();
    options.headers = { ...options.headers, 'X-CSRF-Token': csrf.csrf_token };
  }
  const response = await fetch(apiPrefix + path, { credentials: 'same-origin', ...options });
  if (!response.ok) throw responseError(response);
  return response.json();
}
async function load() {
  clearInterval(expiryTimer);
  button.disabled = true;
  try {
    const policy = await request('/policy');
    for (const option of toolChoice.options) {
      if (option.value !== 'Claude') {
        option.disabled = !policy.codex_enabled;
        option.textContent = (option.value === 'Codex' ? 'Codex CLI' : 'Claude Code 與 Codex CLI') + (policy.codex_enabled ? '' : '（公司尚未開放）');
      }
    }
    if (!policy.codex_enabled && toolChoice.value !== 'Claude') {
      toolChoice.value = 'Claude';
      showInstallCommand();
    }
  } catch (error) { showError(error); return; }
  if (!/^[0-9a-f-]{36}$/.test(requestId || '')) {
    statusElement.textContent = '請先執行公司提供的 Windows 設定工具，再從工具開啟此頁。';
    return;
  }
  try {
    enrollment = await request('/enrollments/' + requestId);
    loginHelp.hidden = true;
    try { sessionStorage.removeItem(loginAttempt); } catch (_) { }
    document.getElementById('device').textContent = enrollment.label;
    document.getElementById('code').textContent = enrollment.code;
    statusElement.textContent = enrollment.replace_device_id ? '完成設定後將撤銷這台電腦的舊金鑰。' : '請核對以下資訊。';
    button.disabled = enrollment.status !== 'pending';
    if (enrollment.status !== 'pending') {
      statusElement.textContent = '此請求已核准。請回到 PowerShell 等待設定完成；若已失敗，請重新執行設定工具。';
    } else {
      const expire = () => {
        if (Date.now() / 1000 >= enrollment.expires_at) {
          button.disabled = true;
          statusElement.textContent = responseError({ status: 410 }).message;
          clearInterval(expiryTimer);
        }
      };
      expiryTimer = setInterval(expire, 1000);
      expire();
    }
  } catch (error) { showError(error); }
}
document.getElementById('retry').addEventListener('click', load);
button.addEventListener('click', async () => {
  button.disabled = true;
  try {
    await request('/enrollments/' + requestId + '/approve', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Company-AI-Action': '1' },
      body: JSON.stringify({ user_code: enrollment.code, confirm: true }),
    });
    clearInterval(expiryTimer);
    statusElement.textContent = '已核准。請回 Windows 工具等待設定完成。';
  } catch (error) { showError(error); button.disabled = error.status === 401 || error.status === 410; }
});
load();

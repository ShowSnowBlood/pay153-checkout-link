const $ = (id) => document.getElementById(id);
const form = $('checkoutForm');
let jobId = '';
let pollTimer = 0;
let countdownTimer = 0;
let displayedProgress = 0;
let targetProgress = 0;
let progressStatus = 'idle';
let progressFrame = 0;
let progressLastTick = 0;
let proxySaveTimer = 0;
let logAutoFollow = true;
let renderedLogKey = '';

const PROXY_STORAGE_KEYS = {
  entry: 'pay153.proxy_pool_1',
  exit: 'pay153.proxy_pool_2'
};

const providerDefaults = {
  hosted: {country: 'US', currency: 'USD'}, paypal: {country: 'US', currency: 'USD'},
  ideal: {country: 'NL', currency: 'EUR'}, upi: {country: 'IN', currency: 'INR'},
  pix: {country: 'BR', currency: 'BRL'}
};
const countryCurrency = {US:'USD',DE:'EUR',FR:'EUR',NL:'EUR',IN:'INR',BR:'BRL',GB:'GBP',JP:'JPY',AU:'AUD',CA:'CAD'};

function proxyLines(node){
  return node.value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
}
function updateProxyCount(node, counter){
  const count = proxyLines(node).length;
  counter.textContent = `${count} / 500`;
  counter.classList.toggle('over-limit', count > 500);
  node.setCustomValidity(count > 500 ? '每个代理池最多填写 500 条' : '');
  return count;
}
function setProxySaveState(text, failed=false){
  const node = $('proxySaveState');
  node.textContent = text;
  node.classList.toggle('save-failed', failed);
}
function saveProxyPools(){
  clearTimeout(proxySaveTimer);
  proxySaveTimer = setTimeout(() => {
    try {
      localStorage.setItem(PROXY_STORAGE_KEYS.entry, $('entryProxy').value);
      localStorage.setItem(PROXY_STORAGE_KEYS.exit, $('exitProxy').value);
      setProxySaveState('已保存到本机');
    } catch (error) {
      setProxySaveState('本地保存失败', true);
    }
  }, 220);
}
function restoreProxyPools(){
  try {
    const entry = localStorage.getItem(PROXY_STORAGE_KEYS.entry);
    const exit = localStorage.getItem(PROXY_STORAGE_KEYS.exit);
    if (entry !== null) $('entryProxy').value = entry;
    if (exit !== null) $('exitProxy').value = exit;
    setProxySaveState(entry !== null || exit !== null ? '已恢复本地代理' : '本地自动保存');
  } catch (error) {
    setProxySaveState('本地保存不可用', true);
  }
}

function selected(name){ return form.querySelector(`input[name="${name}"]:checked`)?.value || ''; }
function bindChoices(group, onChange){
  group.querySelectorAll('label').forEach(label => label.addEventListener('click', () => {
    group.querySelectorAll('label').forEach(x => x.classList.remove('active'));
    label.classList.add('active');
    setTimeout(onChange, 0);
  }));
}
bindChoices($('planGrid'), () => syncFields(false));
bindChoices($('railGrid'), () => syncFields(true));

function syncFields(applyRailDefault=false){
  const plan = selected('plan'), rail = selected('link_type');
  $('teamFields').hidden = plan !== 'team';
  $('codexFields').hidden = plan !== 'codex_low';
  $('idealOptions').hidden = rail !== 'ideal';
  $('paypalOptions').hidden = rail !== 'paypal';
  $('pixOptions').hidden = rail !== 'pix';
  $('regionFields').hidden = rail === 'paypal';
  $('regionAutoHint').hidden = rail !== 'paypal';
  $('pixTaxId').required = false;
  const promoSupported = plan === 'plus';
  $('promoLine').style.display = promoSupported ? 'flex' : 'none';
  $('plusPromoFields').hidden = !promoSupported || !$('usePromo').checked;
  const needsExit = rail !== 'hosted' && rail !== 'pix';
  $('proxyGrid').classList.toggle('single', !needsExit);
  $('exitProxyField').hidden = !needsExit;
  $('exitProxy').required = needsExit;
  $('copyEntryProxy').hidden = !needsExit;
  const recommendations = {
    hosted: '推荐代理：使用账号常用地区。',
    paypal: '\u63a8\u8350\u4ee3\u7406\uff1a\u7cfb\u7edf\u4f18\u5148\u4f7f\u7528\u4ee3\u7406\u6c60 2 \u5f53\u524d\u56fd\u5bb6\u7684 PayPal \u8d26\u5355\uff1b\u82e5\u8be5\u56fd\u5bb6 Checkout \u672a\u5f00\u653e PayPal\uff0c\u5219\u81ea\u52a8\u56de\u9000\u5fb7\u56fd DE/EUR \u8d26\u5355\u3002',
    ideal: '推荐代理：两个代理池均使用 NL。',
    upi: '推荐代理：代理池 1 使用可获得优惠资格的国家或地区（如 TR、JP、BR），代理池 2 使用 IN 创建并处理 UPI。',
    pix: '推荐代理：代理池 1 使用 BR。'
  };
  const pool2Hints = {paypal:'巴西 PayPal 推荐 BR',ideal:'推荐 NL',upi:'推荐 IN'};
  const recommendation = `${recommendations[rail] || '推荐代理：使用与所选地区一致的代理。'} 动态网关可用 __rotate__，任务会先深度探测再固定 session。`;
  $('proxyRecommendation').textContent = recommendation;
  $('proxyFootHint').textContent = recommendation;
  $('exitProxyHint').textContent = pool2Hints[rail] || '推荐同地区';
  if (applyRailDefault && providerDefaults[rail]) {
    $('country').value = providerDefaults[rail].country;
    $('currency').value = providerDefaults[rail].currency;
  }
}
$('country').addEventListener('change', () => $('currency').value = countryCurrency[$('country').value] || 'USD');
$('usePromo').addEventListener('change', () => syncFields(false));
$('entryProxy').addEventListener('input', () => { updateProxyCount($('entryProxy'), $('entryProxyCount')); saveProxyPools(); });
$('exitProxy').addEventListener('input', () => { updateProxyCount($('exitProxy'), $('exitProxyCount')); saveProxyPools(); });
$('copyEntryProxy').addEventListener('click', () => {
  $('exitProxy').value = $('entryProxy').value.trim();
  updateProxyCount($('exitProxy'), $('exitProxyCount'));
  saveProxyPools();
  $('exitProxy').focus();
});

function paintProgress(value){
  const p = Math.max(0, Math.min(100, value));
  $('progressValue').textContent = `${Math.round(p)}%`;
  $('orbitValue').style.strokeDashoffset = String(320.44 * (1 - p / 100));
  $('progressBar').style.width = `${p}%`;
}
function animateProgress(timestamp){
  const dt = Math.min(.08, Math.max(.001, (timestamp - (progressLastTick || timestamp)) / 1000));
  progressLastTick = timestamp;
  if (progressStatus === 'running' && targetProgress < 96) {
    targetProgress = Math.min(96, targetProgress + dt * .28);
  }
  const diff = targetProgress - displayedProgress;
  if (Math.abs(diff) > .02) {
    const rate = progressStatus === 'done' ? 42 : Math.max(7, Math.abs(diff) * 1.35);
    displayedProgress += Math.sign(diff) * Math.min(Math.abs(diff), rate * dt);
    paintProgress(displayedProgress);
  } else {
    displayedProgress = targetProgress;
    paintProgress(displayedProgress);
  }
  if (progressStatus === 'running' || Math.abs(targetProgress - displayedProgress) > .02) {
    progressFrame = requestAnimationFrame(animateProgress);
  } else {
    progressFrame = 0;
    progressLastTick = 0;
  }
}
function resetProgress(){
  if (progressFrame) cancelAnimationFrame(progressFrame);
  displayedProgress = 0;
  targetProgress = 0;
  progressStatus = 'idle';
  progressFrame = 0;
  progressLastTick = 0;
  paintProgress(0);
}
function setProgress(percent, text, status='running'){
  const p = Math.max(0, Math.min(100, Number(percent)||0));
  const retryReset = status === 'running' && p <= 10 && Math.max(displayedProgress, targetProgress) >= 20;
  if (retryReset) {
    displayedProgress = p;
    targetProgress = p;
    paintProgress(p);
  } else if (status === 'running') {
    targetProgress = Math.max(targetProgress, p);
  } else {
    targetProgress = p;
  }
  progressStatus = status;
  $('progressText').textContent = text || '处理中';
  const badge = $('statusBadge'); badge.className = `status-badge ${status}`;
  badge.textContent = status === 'done' ? '完成' : status === 'error' ? '异常' : status === 'cancelled' ? '已停止' : status === 'queued' ? '排队中' : status === 'running' ? '运行中' : '等待';
  $('progressStage').textContent = status === 'done' ? '任务完成' : status === 'error' ? '任务异常' : status === 'cancelled' ? '任务已停止' : status === 'queued' ? '等待执行' : status === 'running' ? '正在处理' : '等待开始';
  if (!progressFrame) progressFrame = requestAnimationFrame(animateProgress);
}
function renderLogs(logs){
  const box = $('logBox');
  if (!logs?.length) return;
  const nextKey = logs.map(x => `${x.time}|${x.message}`).join('\n');
  if (nextKey === renderedLogKey) return;
  const previousTop = box.scrollTop;
  const wasFollowing = logAutoFollow;
  box.innerHTML = logs.map(x => `<div class="log-row"><time>${escapeHtml(x.time)}</time><span>${escapeHtml(x.message)}</span></div>`).join('');
  renderedLogKey = nextKey;
  if (wasFollowing) box.scrollTop = box.scrollHeight;
  else box.scrollTop = previousTop;
}
function escapeHtml(v){ return String(v ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function setRunning(running){ $('submitButton').disabled = running; $('cancelButton').hidden = !running; }
$('logBox').addEventListener('scroll', () => {
  const box = $('logBox');
  logAutoFollow = box.scrollHeight - box.clientHeight - box.scrollTop < 28;
});

function showResult(result){
  $('resultPanel').hidden = false;
  $('resultType').textContent = `${String(result.plan||'').toUpperCase()} · ${String(result.link_type||'').toUpperCase()}`;
  $('resultEmail').textContent = result.account_email || '—';
  $('resultRegion').textContent = `${result.country || '—'} / ${result.currency || '—'}`;
  $('resultPromo').textContent = !result.promo_requested ? '未请求' : result.promo_applied === true ? '已生效 · 今日应付 0' : result.promo_applied === false ? '未生效' : '打开结账页确认';
  $('resultSession').textContent = result.checkout_session_id || '—';
  const finalValue = result.qr_data || result.provider_redirect_url || result.checkout_url || '';
  $('resultValue').value = finalValue;
  const openUrl = result.provider_redirect_url || result.checkout_url || '';
  $('openResult').href = openUrl || '#';
  $('openResult').style.display = openUrl ? 'inline-flex' : 'none';
  const qr = result.qr_image_png || result.qr_image_svg || '';
  $('qrWrap').hidden = !qr;
  if (qr) $('qrImage').src = qr;
  startCountdown(result.expires_at);
  $('resultPanel').scrollIntoView({behavior:'smooth',block:'nearest'});
}
function startCountdown(expiresAt){
  clearInterval(countdownTimer); const node = $('qrCountdown');
  if (!expiresAt) { node.textContent = ''; return; }
  const render = () => { const remain = Math.max(0, Number(expiresAt)*1000-Date.now()); const m=Math.floor(remain/60000),s=Math.floor(remain%60000/1000); node.textContent=remain?`二维码剩余 ${m}:${String(s).padStart(2,'0')}`:'二维码已到期'; };
  render(); countdownTimer=setInterval(render,1000);
}

async function poll(){
  if (!jobId) return;
  try{
    const r = await fetch(`/api/checkout-progress?job_id=${encodeURIComponent(jobId)}`, {cache:'no-store'});
    const data = await r.json(); if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    setProgress(data.percent, data.text, data.status);
    renderLogs(data.logs);
    if (data.status === 'done') { clearInterval(pollTimer); setRunning(false); showResult(data.result || {}); }
    if (data.status === 'error' || data.status === 'cancelled') { clearInterval(pollTimer); setRunning(false); if(data.error) renderLogs([...(data.logs||[]),{time:'ERROR',message:data.error}]); }
  }catch(e){ clearInterval(pollTimer); setRunning(false); setProgress(100, e.message || String(e), 'error'); }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault(); $('resultPanel').hidden = true; $('logBox').innerHTML = '<div class="empty-log">正在创建任务…</div>';
  renderedLogKey = '';
  logAutoFollow = true;
  resetProgress();
  setRunning(true); setProgress(3, '提交任务', 'running');
  const plan = selected('plan');
  const body = {
    token: $('token').value, plan, link_type: selected('link_type'), country: $('country').value,
    currency: $('currency').value, entry_proxies: proxyLines($('entryProxy')), exit_proxies: proxyLines($('exitProxy')),
    retry_count: Math.max(1, Math.min(50, Number($('retryCount').value || 10))),
    use_promo: plan === 'plus' && $('usePromo').checked,
    promo_campaign: plan === 'plus' ? $('promoCampaign').value.trim() : '',
    promo_code: plan === 'team' ? $('promoCode').value.trim() : '',
    workspace_name: plan === 'codex_low' ? $('codexWorkspaceName').value.trim() : $('workspaceName').value.trim(),
    workspace_id: $('workspaceId').value.trim(), seat_quantity: Number($('seatQuantity').value || 5),
    price_interval: $('priceInterval').value, credit_quantity: Number($('creditQuantity').value || 13),
    ideal_bank: '',
    pix_tax_id: selected('link_type') === 'pix' ? $('pixTaxId').value.trim() : '',
    pix_auto_kind: selected('link_type') === 'pix' ? $('pixAutoKind').value : 'cpf'
  };
  try{
    const r = await fetch('/api/checkout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data = await r.json(); if(!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    jobId = data.job_id;
    if (data.queue_position > 0) setProgress(2, `任务已进入队列，当前第 ${data.queue_position} 位`, 'queued');
    clearInterval(pollTimer); await poll(); pollTimer=setInterval(poll,1200);
  }catch(e){ setRunning(false); setProgress(100,e.message||String(e),'error'); }
});

$('cancelButton').addEventListener('click', async () => {
  if(!jobId) return;
  setRunning(false);
  setProgress(100,'任务已停止','cancelled');
  await fetch('/api/checkout-cancel',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:jobId})
  });
});
$('copyResult').addEventListener('click', async () => { await navigator.clipboard.writeText($('resultValue').value || ''); const old=$('copyResult').textContent; $('copyResult').textContent='已复制'; setTimeout(()=>$('copyResult').textContent=old,1200); });

function applyTheme(dark){
  document.documentElement.classList.toggle('dark',dark);
  localStorage.setItem('pay153-theme',dark?'dark':'light');
  $('themeToggle').textContent = dark ? '☀' : '☾';
  $('themeToggle').setAttribute('aria-label', dark ? '切换到浅色模式' : '切换到深色模式');
}
const requestedTheme = new URLSearchParams(location.search).get('theme');
const saved=localStorage.getItem('pay153-theme');
applyTheme(requestedTheme ? requestedTheme === 'dark' : (saved ? saved==='dark' : matchMedia('(prefers-color-scheme: dark)').matches));
$('themeToggle').addEventListener('click',()=>applyTheme(!document.documentElement.classList.contains('dark')));
syncFields(true);
restoreProxyPools();
updateProxyCount($('entryProxy'), $('entryProxyCount'));
updateProxyCount($('exitProxy'), $('exitProxyCount'));

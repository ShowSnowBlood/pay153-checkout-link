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
let logAutoFollow = true;
let renderedLogKey = '';

const providerDefaults = {
  hosted: {country: 'US', currency: 'USD'}, paypal: {country: 'US', currency: 'USD'},
  ideal: {country: 'NL', currency: 'EUR'}, upi: {country: 'IN', currency: 'INR'},
  pix: {country: 'BR', currency: 'BRL'}
};
const countryCurrency = {US:'USD',DE:'EUR',FR:'EUR',NL:'EUR',IN:'INR',BR:'BRL',GB:'GBP',JP:'JPY',AU:'AUD',CA:'CAD'};

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
  if (applyRailDefault && providerDefaults[rail]) {
    $('country').value = providerDefaults[rail].country;
    $('currency').value = providerDefaults[rail].currency;
  }
}
$('country').addEventListener('change', () => $('currency').value = countryCurrency[$('country').value] || 'USD');
$('usePromo').addEventListener('change', () => syncFields(false));

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
function formatLeaseRemaining(seconds){
  const value = Math.max(0, Number(seconds) || 0);
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  return hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分钟`;
}
function formatLeaseTime(timestamp){
  if (!timestamp) return '—';
  return new Date(Number(timestamp) * 1000).toLocaleString('zh-CN', {hour12:false});
}
async function refreshProxyLeases(){
  const body = $('leaseTableBody');
  try {
    const response = await fetch('/api/proxy-leases', {cache:'no-store'});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    const leases = payload.leases || [];
    body.innerHTML = leases.length ? leases.map(row => `<tr>
      <td><code>${escapeHtml(row.at_fingerprint)}</code></td>
      <td>${escapeHtml(String(row.provider || '').toUpperCase())}</td>
      <td>${escapeHtml(row.country || '—')}</td>
      <td><code>${escapeHtml(row.exit_ip || '—')}</code></td>
      <td><code>${escapeHtml(row.session_id || '—')}</code></td>
      <td>${escapeHtml(formatLeaseRemaining(row.remaining_seconds))}</td>
      <td><span class="lease-status">${escapeHtml(row.status === 'active' ? '活动' : row.status)}</span></td>
      <td>${escapeHtml(formatLeaseTime(row.last_used_at))}</td>
    </tr>`).join('') : '<tr><td colspan="8" class="lease-empty">暂无活动租约</td></tr>';
  } catch (error) {
    body.innerHTML = '<tr><td colspan="8" class="lease-empty">租约记录暂时不可用</td></tr>';
  }
}
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
  let promoText = !result.promo_requested
    ? '未请求'
    : result.promo_applied === true
      ? '已生效 · 今日应付 0'
      : result.promo_applied === false
        ? '未生效'
        : '打开结账页确认';
  if (String(result.link_type || '').toLowerCase() === 'upi') {
    const mandateSource = String(result.upi_mandate_source || '').toLowerCase();
    if (mandateSource === 'local') promoText += ' · AutoPay 本地补全';
    else if (mandateSource === 'server') promoText += ' · AutoPay 服务端';
    if (result.fallback_reason) promoText += ' · 官方页兜底';
  }
  $('resultPromo').textContent = promoText;
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
    if (data.status === 'done') { clearInterval(pollTimer); setRunning(false); showResult(data.result || {}); refreshProxyLeases(); }
    if (data.status === 'error' || data.status === 'cancelled') { clearInterval(pollTimer); setRunning(false); refreshProxyLeases(); if(data.error) renderLogs([...(data.logs||[]),{time:'ERROR',message:data.error}]); }
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
    currency: $('currency').value,
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
$('refreshLeases').addEventListener('click', refreshProxyLeases);
syncFields(true);
refreshProxyLeases();
setInterval(refreshProxyLeases, 60000);

export function batteryView(data, now = Date.now() / 1000) {
  const p = data?.percentage;
  const valid = typeof p === 'number' && Number.isFinite(p) && p >= 0 && p <= 100;
  const updated = typeof data?.updated_at === 'number' && Number.isFinite(data.updated_at) ? data.updated_at : null;
  const age = updated === null ? null : Math.max(0, now - updated);
  const stale = data?.status === 'stale' || (age !== null && age > (data.stale_after_s || 30));
  const time = updated === null ? '尚无有效读数' : '更新 ' + new Date(updated * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
  if (data?.status === 'mock') return {value:'电量 —', status:'模拟模式 · 未连接真实电池', time:'', state:'mock', percentage:null};
  if (!valid) return {value:'电量 —', status:data?.status === 'waiting' ? '等待底盘电量数据' : '电量暂不可用', time, state:'unavailable', percentage:null};
  const unverified = data.quality !== 'reported';
  const percent = Number(p.toFixed(1)) + '%';
  return {
    value: (stale ? '上次上报 ' : unverified ? '底盘上报 ' : '电量 ') + percent,
    status: stale ? '数据已过期' : unverified ? '电池状态未确认' : '底盘上报值',
    time, percentage:p,
    state: stale ? 'stale' : unverified ? 'unverified' : p <= 20 ? 'low' : 'live'
  };
}

export function powerView(data) {
  if (data?.status === 'mock') return {text:'模拟模式 · 无真实供电读数', detail:''};
  const p = data?.power || {};
  const adapter = p.adapter_connected === true ? '已连接' : p.adapter_connected === false ? '未连接' : '未确认';
  const labels = {charging:'充电中', discharging:'放电中', not_charging:'未充电', full:'已满电', unknown:'未知'};
  const charging = data?.status === 'stale' ? '未知（数据过期）' : labels[p.charging_status] || '未知';
  let detail = p.note || '当前尚无已验证的适配器直连信号。';
  if (Number.isInteger(p.raw_charge_state)) detail += ' 底盘充电原始状态：' + p.raw_charge_state + '（枚举未确认' + (p.raw_charge_fresh ? '' : '，数据过期') + '）。';
  return {text:'外接电源：' + adapter + ' · 充电：' + charging, detail};
}

export function mountBatteryWidget({endpoint, intervalMs = 5000, root = document}) {
  const card = root.querySelector('#batteryWidget');
  if (!card) return () => {};
  const value = card.querySelector('#batteryValue'), status = card.querySelector('#batteryStatus');
  const updated = card.querySelector('#batteryUpdated'), gauge = card.querySelector('#batteryGauge');
  const fill = card.querySelector('#batteryFill'), button = card.querySelector('#batteryRefresh');
  const power = card.querySelector('#batteryPower');
  let busy = false, last = null, disposed = false;
  function paint(data) {
    const view = batteryView(data);
    value.textContent = view.value; status.textContent = view.status; updated.textContent = view.time;
    card.dataset.state = view.state;
    card.title = [data.note, data.error].filter(Boolean).join(' ');
    if (power) {const v = powerView(data); power.textContent = v.text; power.title = v.detail;}
    gauge.hidden = view.percentage === null;
    gauge.setAttribute('aria-label', view.value + '，' + view.status);
    fill.style.width = (view.percentage ?? 0) + '%';
  }
  async function refresh() {
    if (busy || disposed) return;
    busy = true; button.disabled = true;
    const abort = new AbortController(), timeout = setTimeout(() => abort.abort(), 4000);
    try {
      const response = await fetch(endpoint, {cache:'no-store', signal:abort.signal});
      if (!response.ok) throw Error('HTTP ' + response.status);
      const data = await response.json();
      if (!data || typeof data !== 'object' || typeof data.status !== 'string') throw Error('电量回执格式无效');
      if (data.status !== 'mock' && data.percentage === null && typeof last?.percentage === 'number') {
        paint({...last, status:'stale', error:data.error});
      } else {last = data; paint(data);}
    } catch (error) {
      paint({...last, status:last && typeof last.percentage === 'number' ? 'stale' : 'unavailable', error:error.message});
    } finally {
      clearTimeout(timeout); busy = false; button.disabled = false;
    }
  }
  button.onclick = refresh;
  const timer = setInterval(() => {if (!document.hidden) refresh();}, intervalMs);
  document.addEventListener('visibilitychange', refreshVisible);
  function refreshVisible() {if (!document.hidden) refresh();}
  refresh();
  return () => {disposed = true; clearInterval(timer); document.removeEventListener('visibilitychange', refreshVisible);};
}

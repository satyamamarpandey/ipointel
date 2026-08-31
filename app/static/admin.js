(()=>{const $=s=>document.querySelector(s);
let TOKEN=sessionStorage.getItem('adminToken')||'';
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function call(url,opt={}){
  const headers=Object.assign({'X-Admin-Token':TOKEN},opt.headers||{});
  const r=await fetch(url,Object.assign({},opt,{headers}));
  if(r.status===403){sessionStorage.removeItem('adminToken');TOKEN='';showGate('Invalid admin token.');throw new Error('forbidden')}
  if(!r.ok)throw new Error((await r.json().catch(()=>({}))).detail||`HTTP ${r.status}`);
  return r.json();
}
function showGate(msg){$('#tokenGate').style.display='block';$('#adminBody').style.display='none';if(msg)$('#tokMsg').textContent=msg}
function showBody(){$('#tokenGate').style.display='none';$('#adminBody').style.display='block'}

async function loadUsers(){
  const users=await call('/api/admin/users');
  const counts={WAITLISTED:0,INVITED:0,ACTIVE:0,DISABLED:0};
  users.forEach(u=>counts[u.access_status]=(counts[u.access_status]||0)+1);
  $('#mWait').textContent=counts.WAITLISTED;$('#mInvited').textContent=counts.INVITED;
  $('#mActive').textContent=counts.ACTIVE;$('#mDisabled').textContent=counts.DISABLED;
  $('#userCount').textContent=users.length+' users';
  $('#userRows').innerHTML=users.map(u=>{
    const actions=[];
    if(u.access_status==='WAITLISTED'||u.access_status==='DISABLED')actions.push(`<button class="btn ghost" data-act="invite" data-id="${u.id}" type="button">Invite</button>`);
    if(u.access_status!=='DISABLED')actions.push(`<button class="btn ghost" data-act="disable" data-id="${u.id}" type="button">Disable</button>`);
    if(u.access_status==='DISABLED')actions.push(`<button class="btn ghost" data-act="enable" data-id="${u.id}" type="button">Re-enable</button>`);
    return `<tr><td>${esc(u.email)}</td><td>${esc(u.name)}</td><td>${esc(u.markets)}</td><td>${esc(u.access_status)}</td><td>${new Date(u.created_at).toLocaleDateString()}</td><td>${u.last_login_at?new Date(u.last_login_at).toLocaleDateString():'never'}</td><td>${actions.join(' ')}</td></tr>`;
  }).join('');
}

async function loadOps(){
  const ops=await call('/api/admin/ops-summary');
  $('#mForward').textContent=ops.predictions.total_forward;
  $('#srcHealth').innerHTML=ops.source_health.map(s=>`<div style="padding:6px 0;border-bottom:1px solid #eef1f3;"><b>${esc(s.source)}</b> — ${esc(s.status)}${s.error?` <span class="muted">(${esc(s.error.slice(0,120))})</span>`:''}</div>`).join('')
    +`<div style="padding:6px 0;"><b>Worker</b> — ${esc(ops.worker.status)}, current job: ${esc(ops.worker.current_job)}</div>`;
}

async function loadSheets(){
  const s=await call('/api/admin/sheets-status');
  const state=s.configured?'CONFIGURED — LIVE SYNC':'PENDING CONFIGURATION';
  $('#sheetsStatus').innerHTML=`<div style="padding:6px 0;"><b>${esc(state)}</b></div>
    <div style="padding:6px 0;">Total ${s.total} · Synced ${s.synced} · Pending ${s.pending} · Failed ${s.failed}</div>
    <div style="padding:6px 0;" class="muted">Last successful sync: ${s.last_synced_at?new Date(s.last_synced_at).toLocaleString():'never'}</div>
    ${s.failed?'<button class="btn ghost" id="sheetsRetryBtn" type="button">Retry failed rows</button>':''}`;
  const btn=document.getElementById('sheetsRetryBtn');
  if(btn)btn.addEventListener('click',async()=>{btn.disabled=true;try{await call('/api/admin/sheets-retry',{method:'POST'});await loadSheets()}catch(e){alert(e.message||'Retry failed')}});
}

async function loadAudit(){
  const rows=await call('/api/admin/audit-log?limit=20');
  $('#auditRows').innerHTML=rows.length?rows.map(r=>`<div style="padding:6px 0;border-bottom:1px solid #eef1f3;font-size:12px;">${new Date(r.created_at).toLocaleString()} — <b>${esc(r.action)}</b> ${esc(r.target)}</div>`).join(''):'<p class="muted">No admin actions yet.</p>';
}

async function refreshAll(){await Promise.all([loadUsers(),loadOps(),loadSheets(),loadAudit()])}

document.getElementById('userRows').addEventListener('click',async e=>{
  const btn=e.target.closest('button[data-act]');if(!btn)return;
  const id=btn.dataset.id,act=btn.dataset.act;btn.disabled=true;
  try{await call(`/api/admin/users/${id}/${act}`,{method:'POST'});await refreshAll()}
  catch(err){alert(err.message||'Action failed')}
});

document.getElementById('tokSave').addEventListener('click',async()=>{
  TOKEN=document.getElementById('tok').value.trim();
  try{await call('/api/admin/users');sessionStorage.setItem('adminToken',TOKEN);showBody();await refreshAll()}
  catch(err){showGate('Invalid admin token.')}
});

if(TOKEN){showBody();refreshAll().catch(()=>showGate('Invalid admin token.'))}else{showGate()}
})();

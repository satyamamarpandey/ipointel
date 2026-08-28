(()=>{const token=new URLSearchParams(location.search).get('token');const status=document.getElementById('prefStatus'),form=document.getElementById('prefForm'),msg=document.getElementById('prefMessage');
if(!token){status.textContent='Missing preferences link token.';return}
const boolFields=['alert_score_change','alert_recommendation_change','alert_red_flag','alert_new_ipo','digest_weekly'];
fetch(`/api/preferences?token=${encodeURIComponent(token)}`).then(r=>{if(!r.ok)throw new Error('Invalid or expired link');return r.json()}).then(p=>{
  document.getElementById('markets').value=p.markets;
  boolFields.forEach(f=>document.getElementById(f).checked=!!p[f]);
  status.style.display='none';form.style.display='block';
}).catch(e=>{status.textContent=e.message||'Could not load preferences.'});
form.addEventListener('submit',async e=>{e.preventDefault();msg.textContent='Saving…';
  const body={markets:document.getElementById('markets').value};
  boolFields.forEach(f=>body[f]=document.getElementById(f).checked);
  try{const r=await fetch(`/api/preferences?token=${encodeURIComponent(token)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!r.ok)throw new Error('Save failed');msg.textContent='Saved.'}
  catch(err){msg.textContent=err.message||'Could not save preferences.'}
})})();

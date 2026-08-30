(()=>{const form=document.getElementById('loginForm'),msg=document.getElementById('loginMessage');
const err=new URLSearchParams(location.search).get('error');
if(err){const label={invalid_token:'That sign-in link is invalid.',already_used:'That sign-in link was already used.',expired:'That sign-in link expired - request a new one below.',disabled:'This account has been disabled.',revoked:'That sign-in link was revoked.'}[err]||'Could not sign you in with that link.';msg.textContent=label}
form.addEventListener('submit',async e=>{e.preventDefault();msg.textContent='Sending…';
  try{
    const email=document.getElementById('email').value.trim();
    const r=await fetch('/api/auth/request-login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});
    const body=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(body.detail||'Could not send sign-in link.');
    msg.textContent=body.message||'If that email has beta access, a sign-in link is on its way.';
    form.querySelector('button').disabled=true;
  }catch(er){msg.textContent=er.message||'Could not send sign-in link.'}
})})();

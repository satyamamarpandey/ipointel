import httpx
from ..config import get_settings

def send_welcome(email:str,name:str,referral_code:str,unsubscribe_token:str=""):
    s=get_settings()
    if not (s.enable_email and s.resend_api_key):return False,"email disabled"
    unsub=f"{s.public_base_url}/unsubscribe?token={unsubscribe_token}" if unsubscribe_token else ""
    html=f"<h2>You're on the IPO Intelligence early-access list.</h2><p>We'll send material IPO score changes and launch updates. Referral code: <b>{referral_code}</b>.</p>"+(f"<p><a href=\"{unsub}\">Unsubscribe</a></p>" if unsub else "")
    try:
        r=httpx.post("https://api.resend.com/emails",headers={"Authorization":f"Bearer {s.resend_api_key}","Content-Type":"application/json"},json={"from":s.resend_from,"to":[email],"subject":"IPO Intelligence early access confirmed","html":html},timeout=15)
        r.raise_for_status();return True,"sent"
    except Exception as e:return False,str(e)

def send_score_alert(email:str,company:str,recommendation:str,overall:float,listing:float,long_term:float,confidence:float,unsubscribe_token:str=""):
    s=get_settings()
    if not (s.enable_email and s.resend_api_key):return False,"email disabled"
    unsub=f"{s.public_base_url}/unsubscribe?token={unsubscribe_token}" if unsubscribe_token else ""
    html=(f"<h2>{company}: material IPO score update</h2><p><b>{recommendation}</b></p>"
          f"<p>Overall {overall:.0f}/100 · Listing {listing:.0f}/100 · Long term {long_term:.0f}/100 · Confidence {confidence:.0f}%.</p>"
          f"<p><a href=\"{s.public_base_url}/app\">Open the evidence-first dashboard</a></p>"+(f"<p><a href=\"{unsub}\">Unsubscribe</a></p>" if unsub else ""))
    try:
        r=httpx.post("https://api.resend.com/emails",headers={"Authorization":f"Bearer {s.resend_api_key}","Content-Type":"application/json"},json={"from":s.resend_from,"to":[email],"subject":f"IPO score update: {company}","html":html},timeout=15);r.raise_for_status();return True,"sent"
    except Exception as e:return False,str(e)

from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import WaitlistLead,IPO,ScoreSnapshot,NotificationLog
from ..config import get_settings
from .emailer import send_score_alert

def material(score:ScoreSnapshot,previous:ScoreSnapshot|None):
    if "NO RECOMMENDATION" in score.recommendation:return False
    if previous is None:return score.confidence>=get_settings().min_recommendation_confidence and score.overall_score>=60
    return score.recommendation!=previous.recommendation or abs(score.overall_score-previous.overall_score)>=5 or abs(score.listing_score-previous.listing_score)>=7 or abs(score.long_term_score-previous.long_term_score)>=7

def send_pending(db:Session,limit_scores=30):
    s=get_settings()
    if not (s.enable_email and s.resend_api_key):return {"sent":0,"skipped":"email disabled"}
    scores=db.scalars(select(ScoreSnapshot).order_by(ScoreSnapshot.created_at.desc()).limit(limit_scores)).all();sent=0
    leads=db.scalars(select(WaitlistLead).where(WaitlistLead.consent==True)).all()
    for sc in scores:
        ipo=db.get(IPO,sc.ipo_id)
        history=db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id==ipo.id,ScoreSnapshot.id!=sc.id).order_by(ScoreSnapshot.created_at.desc()).limit(1)).all();prev=history[0] if history else None
        if not material(sc,prev):continue
        for lead in leads:
            if lead.markets=="india" and ipo.country!="India":continue
            if lead.markets=="us" and ipo.country!="United States":continue
            exists=db.scalar(select(NotificationLog).where(NotificationLog.lead_id==lead.id,NotificationLog.score_snapshot_id==sc.id))
            if exists:continue
            ok,msg=send_score_alert(lead.email,ipo.company,sc.recommendation,sc.overall_score,sc.listing_score,sc.long_term_score,sc.confidence,lead.unsubscribe_token)
            db.add(NotificationLog(lead_id=lead.id,ipo_id=ipo.id,score_snapshot_id=sc.id,status="sent" if ok else "error",error="" if ok else msg));sent+=1 if ok else 0
    db.commit();return {"sent":sent}

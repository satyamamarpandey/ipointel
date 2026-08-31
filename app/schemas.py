from pydantic import BaseModel, EmailStr, Field

class WaitlistIn(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=160)
    investor_type: str = Field(default="retail", pattern="^(retail|professional|advisor|founder|other)$")
    markets: str = Field(default="both", pattern="^(india|us|both)$")
    consent: bool = True
    referred_by: str = Field(default="", max_length=40)
    source: str = Field(default="direct", max_length=80)
    campaign: str = Field(default="", max_length=80)
    page_path: str = Field(default="", max_length=160)
    website: str = Field(default="", max_length=120)  # honeypot

class WaitlistOut(BaseModel):
    ok: bool
    message: str
    referral_code: str = ""

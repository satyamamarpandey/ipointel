FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m -u 10001 appuser && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
# --forwarded-allow-ips is what makes --proxy-headers actually take effect.
# Uvicorn only rewrites the client address from X-Forwarded-For when the
# immediate peer is in this list, and the default is 127.0.0.1 - but behind
# Caddy the peer is the caddy container's (dynamic) network address, so the
# header was being ignored and every request reported Caddy's own IP. That
# collapsed all three rate limiters (sign-in 5/10min, signup 6/min, events
# 60/min) into a single global bucket shared by every visitor.
# "*" is safe here only because `web` publishes no ports in either compose
# file - Caddy is the sole reachable path to it, so X-Forwarded-For cannot
# be spoofed by an outside client. Keep it that way, or narrow this value.
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--proxy-headers","--forwarded-allow-ips","*"]

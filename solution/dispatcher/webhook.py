"""FastAPI webhook server and observability console."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import AppConfig
from .store import StateStore

logger = logging.getLogger("dispatcher.webhook")


def create_webhook_app(
    store: StateStore,
    config: AppConfig,
    wake_event: Optional[asyncio.Event] = None,
) -> FastAPI:
    app = FastAPI(title="Agentic Universe Dispatcher Service", version="1.0.0")

    @app.post("/webhooks/telephony")
    async def telephony_webhook(request: Request):
        # 1. Read raw body bytes for HMAC verification
        raw_body = await request.body()
        sig_header = request.headers.get("X-AU-Signature")

        if not sig_header:
            logger.warning("Rejected webhook without X-AU-Signature header")
            raise HTTPException(status_code=401, detail="missing_signature")

        expected_sig = hmac.new(
            config.webhook_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(sig_header, expected_sig):
            logger.warning("Invalid webhook signature: %s", sig_header)
            raise HTTPException(status_code=401, detail="invalid_signature")

        # 2. Parse payload
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="invalid_json")

        for req_field in ("request_id", "campaign_id", "lead_id", "phone"):
            if not payload.get(req_field):
                raise HTTPException(status_code=400, detail=f"missing_{req_field}")

        # 3. Apply result idempotently
        changed = store.apply_result(payload, config)
        if changed:
            logger.info("Webhook recorded result for %s (disp=%s, status=%s)",
                        payload.get("request_id"), payload.get("disposition"), payload.get("status"))
            if wake_event is not None:
                wake_event.set()

        return JSONResponse({"status": "ok", "recorded": changed}, status_code=200)

    @app.get("/status")
    async def get_status():
        summary = store.get_dashboard_summary()
        summary["is_finished"] = store.is_all_finished()
        summary["tenants_config"] = {
            t.tenant_id: {"max_concurrent": t.max_concurrent_calls}
            for t in config.tenants.values()
        }
        return JSONResponse(summary)

    @app.get("/", response_class=HTMLResponse)
    async def get_dashboard():
        """Sleek real-time observability console designed with modern, clean UI standards."""
        html_content = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Agentic Universe | Dispatcher Console</title>
  <style>
    :root {
      --bg: #090d16;
      --card-bg: #111827;
      --border: #1f2937;
      --text: #f3f4f6;
      --text-muted: #9ca3af;
      --accent: #3b82f6;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --purple: #8b5cf6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      padding: 2.5rem 2rem;
      max-width: 1200px;
      margin: 0 auto;
      line-height: 1.5;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 2rem;
      border-bottom: 1px solid var(--border);
      padding-bottom: 1.25rem;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }
    .badge {
      font-size: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.05em;
      padding: 0.25rem 0.6rem;
      border-radius: 9999px;
      text-transform: uppercase;
      background: rgba(59, 130, 246, 0.15);
      color: var(--accent);
      border: 1px solid rgba(59, 130, 246, 0.3);
    }
    .live-indicator {
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--success);
      margin-right: 6px;
      box-shadow: 0 0 10px var(--success);
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.5; transform: scale(0.9); }
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 1.25rem;
      margin-bottom: 2rem;
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1.25rem;
    }
    .card-title {
      font-size: 0.825rem;
      font-weight: 500;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 0.5rem;
    }
    .card-value {
      font-size: 2rem;
      font-weight: 700;
      color: var(--text);
    }
    .tenant-card {
      margin-bottom: 1rem;
    }
    .tenant-header {
      display: flex;
      justify-content: space-between;
      font-size: 0.9rem;
      font-weight: 600;
      margin-bottom: 0.4rem;
    }
    .meter-bg {
      width: 100%;
      height: 8px;
      background: #1f2937;
      border-radius: 4px;
      overflow: hidden;
    }
    .meter-fill {
      height: 100%;
      background: var(--accent);
      border-radius: 4px;
      transition: width 0.3s ease;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.875rem;
    }
    th, td {
      padding: 0.75rem 1rem;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }
    th {
      color: var(--text-muted);
      font-weight: 600;
      background: rgba(255, 255, 255, 0.02);
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <h1 style="font-size: 1.35rem; font-weight: 600;">Agentic Universe Dispatcher</h1>
      <span class="badge"><span class="live-indicator"></span>Active</span>
    </div>
    <div style="font-size: 0.85rem; color: var(--text-muted);">
      Refreshes live (1s) &bull; Port 8200
    </div>
  </header>

  <div class="grid">
    <div class="card">
      <div class="card-title">Completed Leads</div>
      <div class="card-value" id="val-completed" style="color: var(--success);">-</div>
    </div>
    <div class="card">
      <div class="card-title">Exhausted Leads</div>
      <div class="card-value" id="val-exhausted" style="color: var(--warning);">-</div>
    </div>
    <div class="card">
      <div class="card-title">DND Blocked</div>
      <div class="card-value" id="val-dnd" style="color: var(--danger);">-</div>
    </div>
    <div class="card">
      <div class="card-title">Pending & In-Flight</div>
      <div class="card-value" id="val-active" style="color: var(--accent);">-</div>
    </div>
  </div>

  <div class="grid" style="grid-template-columns: 1fr 1fr;">
    <div class="card">
      <div class="card-title" style="margin-bottom: 1rem;">Tenant Concurrency (R2 Invariant)</div>
      <div id="tenants-container">Loading...</div>
    </div>
    <div class="card">
      <div class="card-title" style="margin-bottom: 1rem;">Call State Ledger</div>
      <table>
        <thead>
          <tr><th>State</th><th>Active Count</th></tr>
        </thead>
        <tbody id="calls-tbody">
          <tr><td colspan="2">Loading...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <script>
    async function updateDashboard() {
      try {
        const res = await fetch('/status');
        const data = await res.json();
        
        const leads = data.leads_by_state || {};
        document.getElementById('val-completed').innerText = leads['COMPLETED'] || 0;
        document.getElementById('val-exhausted').innerText = leads['EXHAUSTED'] || 0;
        document.getElementById('val-dnd').innerText = leads['DND_BLOCKED'] || 0;
        
        const activeLeads = (leads['PENDING'] || 0) + (leads['DIALING'] || 0) + (leads['ACTIVE'] || 0);
        document.getElementById('val-active').innerText = activeLeads;

        // Tenants
        const tenantsDiv = document.getElementById('tenants-container');
        let tHtml = '';
        const tConfigs = data.tenants_config || {};
        const activeT = data.active_by_tenant || {};
        for (const [tid, tcfg] of Object.entries(tConfigs)) {
          const act = activeT[tid] || 0;
          const max = tcfg.max_concurrent || 1;
          const pct = Math.min(100, Math.round((act / max) * 100));
          tHtml += `
            <div class="tenant-card">
              <div class="tenant-header">
                <span>${tid}</span>
                <span>${act} / ${max} active (${pct}%)</span>
              </div>
              <div class="meter-bg">
                <div class="meter-fill" style="width: ${pct}%;"></div>
              </div>
            </div>
          `;
        }
        tenantsDiv.innerHTML = tHtml;

        // Calls table
        const calls = data.calls_by_state || {};
        const tbody = document.getElementById('calls-tbody');
        let cHtml = '';
        for (const [st, cnt] of Object.entries(calls)) {
          cHtml += `<tr><td><strong>${st}</strong></td><td>${cnt}</td></tr>`;
        }
        tbody.innerHTML = cHtml || '<tr><td colspan="2">No calls logged yet</td></tr>';
      } catch (err) {
        console.error('Update failed', err);
      }
    }
    setInterval(updateDashboard, 1000);
    updateDashboard();
  </script>
</body>
</html>
"""
        return HTMLResponse(content=html_content)

    return app


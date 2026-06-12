"""FastAPI backend for the OutboundAI dashboard."""

import asyncio
import json
import logging
import os
import random
import ssl
import certifi
import aiohttp
from pathlib import Path
from typing import Optional
import hashlib
import hmac
import secrets
import time

from dotenv import load_dotenv
load_dotenv(".env")

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl


from db import (
    SENSITIVE_KEYS, cancel_appointment, clear_errors, create_campaign, delete_campaign,
    get_all_appointments, get_all_calls, get_all_campaigns, get_all_settings,
    get_all_agent_profiles, get_agent_profile, create_agent_profile, update_agent_profile,
    delete_agent_profile, set_default_agent_profile, get_calls_by_phone, get_campaign,
    get_contacts, get_errors, get_logs, get_setting, get_stats, init_db, log_error,
    save_settings, set_setting, update_call_notes, update_campaign_run_stats, update_campaign_status,
)
from prompts import DEFAULT_SYSTEM_PROMPT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")


# -- Authentication and Security --

SECRET_KEY = os.getenv("SECRET_KEY", "outbound-ai-super-secret-key-change-me")

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return f"{salt}:{key.hex()}"

def verify_password(password: str, hashed: str) -> bool:
    try:
        salt, key_hex = hashed.split(":")
        key = bytes.fromhex(key_hex)
        new_key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
        return hmac.compare_digest(key, new_key)
    except Exception:
        return False

def generate_session_token(email: str, password_hash: str) -> str:
    expiry = int(time.time()) + 86400 * 7
    payload = f"{email}:{expiry}"
    sig_key = f"{SECRET_KEY}:{password_hash}"
    sig = hmac.new(sig_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"

async def verify_session_token_and_get_email(token: str) -> Optional[str]:
    if not token:
        return None
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return None
        email, expiry_str, sig = parts
        expiry = int(expiry_str)
        if expiry < time.time():
            return None
        
        user_data = await get_user_data(email)
        if not user_data:
            return None
        password_hash = user_data.get("password_hash")
        
        expected_payload = f"{email}:{expiry_str}"
        sig_key = f"{SECRET_KEY}:{password_hash}"
        expected_sig = hmac.new(sig_key.encode(), expected_payload.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected_sig):
            return email
    except Exception:
        pass
    return None

async def verify_session_token(token: str) -> bool:
    email = await verify_session_token_and_get_email(token)
    return email is not None

async def get_users() -> dict:
    raw = await get_setting("ADMIN_USERS", "{}")
    try:
        return json.loads(raw)
    except Exception:
        return {}

async def save_users(users: dict) -> None:
    await set_setting("ADMIN_USERS", json.dumps(users))

async def get_user_data(email: str) -> Optional[dict]:
    users = await get_users()
    email = email.strip().lower()
    if email not in users:
        return None
    data = users[email]
    if isinstance(data, str):
        return {"password_hash": data, "role": "admin"}
    return data

async def init_default_user():
    admin_email = os.getenv("DEFAULT_ADMIN_EMAIL", "").strip().lower()
    admin_password = os.getenv("DEFAULT_ADMIN_PASSWORD", "")
    
    user_email = os.getenv("DEFAULT_USER_EMAIL", "").strip().lower()
    user_password = os.getenv("DEFAULT_USER_PASSWORD", "")

    recovery_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    recovery_password = os.getenv("ADMIN_PASSWORD", "")

    users = await get_users()

    # Initialize demo hackathon account
    test_email = "hackathontest@gmail.com"
    test_password = "hackathon"
    if test_email not in users:
        users[test_email] = {
            "password_hash": hash_password(test_password),
            "role": "user"
        }
        await save_users(users)
        logger.info(f"Initialized demo hackathon account: {test_email}")

    if recovery_email and recovery_password:
        users[recovery_email] = {
            "password_hash": hash_password(recovery_password),
            "role": "admin"
        }
        await save_users(users)
        logger.info(f"Updated recovery/override admin user account: {recovery_email}")

    if admin_email and admin_password and admin_email not in users:
        users[admin_email] = {
            "password_hash": hash_password(admin_password),
            "role": "admin"
        }
        await save_users(users)
        logger.info(f"Initialized default admin account: {admin_email}")

    if user_email and user_password and user_email not in users:
        users[user_email] = {
            "password_hash": hash_password(user_password),
            "role": "user"
        }
        await save_users(users)
        logger.info(f"Initialized default normal user account: {user_email}")


init_db()

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _scheduler = AsyncIOScheduler()
except ImportError:
    _scheduler = None
    logger.warning("APScheduler not installed - campaign scheduling disabled")

app = FastAPI(title="OutboundAI Dashboard", version="1.0.0")


@app.on_event("startup")
async def _startup():
    await init_default_user()
    if _scheduler:
        _scheduler.start()
        await _reschedule_all_campaigns()


@app.on_event("shutdown")
async def _shutdown():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


async def eff(key: str) -> str:
    val = await get_setting(key, "")
    return val if val else os.getenv(key, "")


# -- Request models --

class CallRequest(BaseModel):
    phone: str
    lead_name: str = "there"
    business_name: str = "our company"
    service_type: str = "our service"
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class AgentProfileRequest(BaseModel):
    name: str
    voice: str = "Aoede"
    model: str = "gemini-3.1-flash-live-preview"
    system_prompt: Optional[str] = None
    enabled_tools: str = "[]"
    is_default: bool = False


class PromptRequest(BaseModel):
    prompt: str


class SettingsRequest(BaseModel):
    settings: dict


class NotesRequest(BaseModel):
    notes: str


class CampaignRequest(BaseModel):
    name: str
    contacts: list
    schedule_type: str = "once"
    schedule_time: str = "09:00"
    call_delay_seconds: int = 3
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class StatusRequest(BaseModel):
    status: str


# -- Security & Abuse Prevention Stores --
failed_login_attempts = {}  # IP -> list of timestamps
dispatch_timestamps = []    # List of timestamps of successful dispatches

def check_login_rate_limit(ip: str) -> bool:
    now = time.time()
    if ip not in failed_login_attempts:
        failed_login_attempts[ip] = []
    # Keep attempts in the last 15 minutes (900 seconds)
    failed_login_attempts[ip] = [t for t in failed_login_attempts[ip] if now - t < 900]
    if len(failed_login_attempts[ip]) >= 5:
        return False
    return True

def record_failed_login(ip: str):
    now = time.time()
    if ip not in failed_login_attempts:
        failed_login_attempts[ip] = []
    failed_login_attempts[ip].append(now)

async def is_emergency_suspended() -> bool:
    env_val = os.getenv("EMERGENCY_STOP", "").lower()
    if env_val == "true":
        return True
    db_val = await get_setting("EMERGENCY_STOP", "false")
    return db_val.lower() == "true"

async def get_security_limits() -> tuple[int, int]:
    min_limit_raw = await get_setting("MAX_CALLS_PER_MINUTE", "15")
    daily_limit_raw = await get_setting("MAX_DAILY_CALLS", "500")
    try:
        min_limit = int(os.getenv("MAX_CALLS_PER_MINUTE", min_limit_raw))
    except Exception:
        min_limit = 15
    try:
        daily_limit = int(os.getenv("MAX_DAILY_CALLS", daily_limit_raw))
    except Exception:
        daily_limit = 500
    return min_limit, daily_limit

async def increment_and_check_dispatches() -> bool:
    global dispatch_timestamps
    now = time.time()
    min_limit, daily_limit = await get_security_limits()
    # 1. Clean timestamps older than 24 hours
    dispatch_timestamps = [t for t in dispatch_timestamps if now - t < 86400]
    # 2. Rate limit: check calls in last 60 seconds
    last_minute_calls = [t for t in dispatch_timestamps if now - t < 60]
    if len(last_minute_calls) >= min_limit:
        logger.warning(f"Abuse prevention: rate limit of {min_limit} calls per minute exceeded.")
        return False
    # 3. Daily Cap: max daily limit
    if len(dispatch_timestamps) >= daily_limit:
        logger.error(f"Abuse prevention: daily cap of {daily_limit} calls reached.")
        return False
    dispatch_timestamps.append(now)
    return True


# -- Authentication Request Models --

class LoginRequest(BaseModel):
    email: str
    password: str

class UserCreateRequest(BaseModel):
    email: str
    password: str
    role: str = "user"

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# -- Dashboard & Authentication --

@app.get("/login", response_class=HTMLResponse)
async def serve_login(request: Request):
    token = request.cookies.get("session_token")
    if await verify_session_token(token):
        return RedirectResponse(url="/", status_code=303)
    login_path = Path(__file__).parent / "ui" / "login.html"
    if login_path.exists():
        return HTMLResponse(content=login_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Login page not found - place login.html in ui/</h1>", status_code=404)

@app.post("/login")
async def api_login(req: LoginRequest, response: Response, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not check_login_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Please try again in 15 minutes.")
        
    users = await get_users()
    email = req.email.strip().lower()
    if email in users:
        user_data = users[email]
        password_hash = user_data if isinstance(user_data, str) else user_data.get("password_hash")
        if verify_password(req.password, password_hash):
            token = generate_session_token(email, password_hash)
            response.set_cookie(
                key="session_token",
                value=token,
                httponly=True,
                max_age=86400 * 7,
                samesite="lax",
                secure=False
            )
            return {"status": "success", "message": "Logged in successfully"}
            
    record_failed_login(ip)
    raise HTTPException(status_code=401, detail="Invalid email or password")

@app.get("/logout")
async def api_logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session_token")
    return response

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    token = request.cookies.get("session_token")
    if not await verify_session_token(token):
        return RedirectResponse(url="/login", status_code=303)
    html_path = Path(__file__).parent / "ui" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard not found - place index.html in ui/</h1>", status_code=404)


# -- User Info & Management --

@app.get("/api/me")
async def api_me(request: Request):
    token = request.cookies.get("session_token")
    email = await verify_session_token_and_get_email(token)
    if not email:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_data = await get_user_data(email)
    role = user_data.get("role", "user") if user_data else "user"
    return {"email": email, "role": role}

@app.get("/api/settings/users")
async def api_get_users(request: Request):
    users = await get_users()
    out = []
    for email, data in users.items():
        role = "admin"
        if isinstance(data, dict):
            role = data.get("role", "user")
        out.append({"email": email, "role": role})
    return {"users": out}

@app.post("/api/settings/users")
async def api_add_user(req: UserCreateRequest):
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email format")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters long")
    if req.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="Invalid role type")
    users = await get_users()
    if email in users:
        raise HTTPException(status_code=400, detail="User already exists")
    users[email] = {
        "password_hash": hash_password(req.password),
        "role": req.role
    }
    await save_users(users)
    return {"status": "success", "message": f"User {email} added successfully"}

@app.post("/api/settings/change-password")
async def api_change_password(req: ChangePasswordRequest, request: Request, response: Response):
    token = request.cookies.get("session_token")
    email = await verify_session_token_and_get_email(token)
    if not email:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    users = await get_users()
    if email not in users:
        raise HTTPException(status_code=404, detail="User not found")
        
    user_data = users[email]
    password_hash = user_data if isinstance(user_data, str) else user_data.get("password_hash")
    
    if not verify_password(req.current_password, password_hash):
        raise HTTPException(status_code=400, detail="Invalid current password")
        
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters long")
        
    new_hash = hash_password(req.new_password)
    if isinstance(user_data, str):
        users[email] = new_hash
    else:
        users[email]["password_hash"] = new_hash
        
    await save_users(users)
    response.delete_cookie("session_token")
    return {"status": "success", "message": "Password updated successfully. Logging out."}

@app.delete("/api/settings/users/{email}")
async def api_delete_user(email: str):
    email = email.strip().lower()
    users = await get_users()
    if email not in users:
        raise HTTPException(status_code=404, detail="User not found")
    if len(users) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last remaining user")
    del users[email]
    await save_users(users)
    return {"status": "success", "message": f"User {email} deleted successfully"}


# -- Authentication & Authorization Middleware --

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api"):
        if path not in ("/login", "/logout", "/api/me"):
            token = request.cookies.get("session_token")
            email = await verify_session_token_and_get_email(token)
            if not email:
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
            
            # Enforce admin-only access on configuration, setup, prompt, logs, and user management APIs
            admin_only_prefixes = [
                "/api/settings",
                "/api/setup",
                "/api/prompt",
                "/api/logs",
            ]
            is_admin_endpoint = any(path.startswith(prefix) for prefix in admin_only_prefixes)
            if is_admin_endpoint:
                user_data = await get_user_data(email)
                if not user_data or user_data.get("role") != "admin":
                    return JSONResponse(status_code=403, content={"detail": "Forbidden: Admin access required"})
    
    response = await call_next(request)
    return response


# -- Call dispatch --

@app.post("/api/call")
async def api_dispatch_call(req: CallRequest):
    if await is_emergency_suspended():
        raise HTTPException(503, "Outbound calls are suspended by the administrator (Emergency Stop active).")
        
    if not await increment_and_check_dispatches():
        raise HTTPException(429, "Rate limit or daily dispatch cap exceeded. Please try again later.")

    url    = await eff("LIVEKIT_URL")
    key    = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")

    if not all([url, key, secret]):
        raise HTTPException(400, "LiveKit credentials not configured. Go to Settings.")

    phone = req.phone.strip()
    if not phone.startswith("+"):
        raise HTTPException(400, "Phone must be in E.164 format: +919876543210")

    effective_prompt = req.system_prompt
    effective_voice = None
    effective_model = None
    effective_tools = None

    if req.agent_profile_id:
        profile = await get_agent_profile(req.agent_profile_id)
        if profile:
            if not effective_prompt and profile.get("system_prompt"):
                effective_prompt = profile["system_prompt"]
            effective_voice = profile.get("voice")
            effective_model = profile.get("model")
            effective_tools = profile.get("enabled_tools")

    if not effective_prompt:
        effective_prompt = await get_setting("system_prompt", "") or None

    room_name = f"call-{phone.replace('+', '')}-{random.randint(1000, 9999)}"
    metadata: dict = {
        "phone_number": phone,
        "lead_name": req.lead_name,
        "business_name": req.business_name,
        "service_type": req.service_type,
        "system_prompt": effective_prompt,
    }
    if effective_voice:  metadata["voice_override"] = effective_voice
    if effective_model:  metadata["model_override"] = effective_model
    if effective_tools:  metadata["tools_override"] = effective_tools

    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=5))
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(
                agent_name="outbound-caller", room=room_name, metadata=json.dumps(metadata)
            )
        )
        await lk.aclose()
        await session.close()
        await log_error("server", f"Call dispatched to {phone}", f"room={room_name}", "info")
        return {"status": "dispatched", "room": room_name, "phone": phone}
    except Exception as exc:
        logger.error("Dispatch error: %s", exc)
        raise HTTPException(500, f"Dispatch failed: {exc}")


# -- Calls --

@app.get("/api/calls")
async def api_get_calls(page: int = 1, limit: int = 20):
    return await get_all_calls(page=page, limit=limit)


@app.patch("/api/calls/{call_id}/notes")
async def api_update_notes(call_id: str, req: NotesRequest):
    ok = await update_call_notes(call_id, req.notes)
    if not ok:
        raise HTTPException(404, "Call not found")
    return {"status": "updated"}


# -- Stats --

@app.get("/api/stats")
async def api_get_stats():
    return await get_stats()


# -- Appointments --

@app.get("/api/appointments")
async def api_get_appointments(date: Optional[str] = None):
    return await get_all_appointments(date_filter=date)


@app.delete("/api/appointments/{appointment_id}")
async def api_cancel_appointment(appointment_id: str):
    ok = await cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(404, "Appointment not found or already cancelled")
    return {"status": "cancelled"}


# -- Prompt --

@app.get("/api/prompt")
async def api_get_prompt():
    saved = await get_setting("system_prompt", "")
    return {"prompt": saved or DEFAULT_SYSTEM_PROMPT, "is_custom": bool(saved)}


@app.post("/api/prompt")
async def api_save_prompt(req: PromptRequest):
    await set_setting("system_prompt", req.prompt)
    return {"status": "saved"}


@app.delete("/api/prompt")
async def api_reset_prompt():
    await set_setting("system_prompt", "")
    return {"status": "reset", "prompt": DEFAULT_SYSTEM_PROMPT}


# -- Settings --

@app.get("/api/settings")
async def api_get_settings():
    return await get_all_settings()


@app.post("/api/settings")
async def api_save_settings(req: SettingsRequest):
    filtered = {k: v for k, v in req.settings.items() if v is not None and v != ""}
    await save_settings(filtered)
    for k, v in filtered.items():
        os.environ[k] = str(v)
    return {"status": "saved", "count": len(filtered)}


# -- SIP trunk setup --

@app.post("/api/setup/trunk")
async def api_setup_trunk():
    url    = await eff("LIVEKIT_URL")
    key    = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    sip_domain = (await eff("VOBIZ_SIP_DOMAIN")) or (await eff("TWILIO_SIP_DOMAIN"))
    username   = (await eff("VOBIZ_USERNAME")) or (await eff("TWILIO_SIP_USERNAME")) or (await eff("TWILIO_ACCOUNT_SID"))
    password   = (await eff("VOBIZ_PASSWORD")) or (await eff("TWILIO_SIP_PASSWORD")) or (await eff("TWILIO_AUTH_TOKEN"))
    phone      = (await eff("VOBIZ_OUTBOUND_NUMBER")) or (await eff("TWILIO_OUTBOUND_NUMBER"))

    if not all([url, key, secret, sip_domain, username, password, phone]):
        raise HTTPException(400, "Configure LiveKit and VoBiz/SIP credentials in Settings first.")

    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        trunk = await lk.sip.create_sip_outbound_trunk(
            lk_api.CreateSIPOutboundTrunkRequest(
                trunk=lk_api.SIPOutboundTrunkInfo(
                    name="Vobiz Outbound Trunk",
                    address=sip_domain,
                    auth_username=username,
                    auth_password=password,
                    numbers=[phone],
                )
            )
        )
        trunk_id = trunk.sip_trunk_id
        await set_setting("OUTBOUND_TRUNK_ID", trunk_id)
        os.environ["OUTBOUND_TRUNK_ID"] = trunk_id
        await lk.aclose()
        await session.close()
        return {"status": "created", "trunk_id": trunk_id}
    except Exception as exc:
        raise HTTPException(500, f"Trunk creation failed: {exc}")


# -- Logs --

@app.get("/api/logs")
async def api_get_logs(limit: int = 200, level: Optional[str] = None, source: Optional[str] = None):
    return await get_logs(level=level, source=source, limit=limit)


@app.delete("/api/logs")
async def api_clear_logs():
    await clear_errors()
    return {"status": "cleared"}


# -- CRM --

@app.get("/api/crm")
async def api_get_contacts():
    return {"data": await get_contacts()}


@app.get("/api/crm/calls")
async def api_get_contact_calls(phone: str = Query(...)):
    return {"data": await get_calls_by_phone(phone)}


# -- Agent Profiles --

@app.get("/api/agent-profiles")
async def api_list_agent_profiles():
    try:
        return await get_all_agent_profiles()
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/agent-profiles")
async def api_create_agent_profile(req: AgentProfileRequest):
    try:
        profile_id = await create_agent_profile(
            name=req.name, voice=req.voice, model=req.model,
            system_prompt=req.system_prompt, enabled_tools=req.enabled_tools, is_default=req.is_default,
        )
        return {"status": "created", "id": profile_id}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/agent-profiles/{profile_id}")
async def api_get_agent_profile(profile_id: str):
    profile = await get_agent_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile


@app.put("/api/agent-profiles/{profile_id}")
async def api_update_agent_profile(profile_id: str, req: AgentProfileRequest):
    ok = await update_agent_profile(profile_id, {
        "name": req.name, "voice": req.voice, "model": req.model,
        "system_prompt": req.system_prompt, "enabled_tools": req.enabled_tools,
        "is_default": 1 if req.is_default else 0,
    })
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "updated"}


@app.delete("/api/agent-profiles/{profile_id}")
async def api_delete_agent_profile(profile_id: str):
    ok = await delete_agent_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "deleted"}


@app.post("/api/agent-profiles/{profile_id}/set-default")
async def api_set_default_profile(profile_id: str):
    try:
        await set_default_agent_profile(profile_id)
        return {"status": "default set"}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# -- Campaigns --

async def _dispatch_one(lk, lk_api, contact: dict, room_name: str,
                         prompt: Optional[str], profile: Optional[dict] = None) -> bool:
    try:
        saved_prompt = prompt or (await get_setting("system_prompt", "")) or None
        metadata: dict = {
            "phone_number": contact["phone"],
            "lead_name": contact.get("lead_name", "there"),
            "business_name": contact.get("business_name", "our company"),
            "service_type": contact.get("service_type", "our service"),
            "system_prompt": saved_prompt,
        }
        if profile:
            if not metadata["system_prompt"] and profile.get("system_prompt"):
                metadata["system_prompt"] = profile["system_prompt"]
            if profile.get("voice"):   metadata["voice_override"] = profile["voice"]
            if profile.get("model"):   metadata["model_override"] = profile["model"]
            if profile.get("enabled_tools"): metadata["tools_override"] = profile["enabled_tools"]
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(agent_name="outbound-caller", room=room_name, metadata=json.dumps(metadata))
        )
        return True
    except Exception as exc:
        logger.error("Campaign dispatch error for %s: %s", contact.get("phone"), exc)
        return False


async def _run_campaign(campaign_id: str) -> None:
    if await is_emergency_suspended():
        logger.warning("Campaign run aborted: Emergency Stop active.")
        return
        
    campaign = await get_campaign(campaign_id)
    if not campaign:
        return
    contacts = json.loads(campaign.get("contacts_json") or "[]")
    if not contacts:
        return
    delay = int(campaign.get("call_delay_seconds") or 3)
    prompt = campaign.get("system_prompt")
    agent_profile_id = campaign.get("agent_profile_id")
    profile = None
    if agent_profile_id:
        profile = await get_agent_profile(agent_profile_id)

    url    = await eff("LIVEKIT_URL")
    key    = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        logger.error("Campaign %s: LiveKit not configured", campaign_id)
        return

    from livekit import api as lk_api_module
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))

    ok_count = fail_count = 0
    try:
        lk = lk_api_module.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        for i, contact in enumerate(contacts):
            if await is_emergency_suspended():
                logger.warning("Campaign run aborted during execution: Emergency Stop active.")
                fail_count += (len(contacts) - i)
                break
            if not await increment_and_check_dispatches():
                logger.warning("Campaign run paused/aborted: Rate limit or daily cap reached.")
                fail_count += (len(contacts) - i)
                break
            phone = contact.get("phone", "")
            if not phone.startswith("+"):
                fail_count += 1
                continue
            room_name = f"camp-{campaign_id[:8]}-{phone.replace('+','')}-{random.randint(100,999)}"
            success = await _dispatch_one(lk, lk_api_module, contact, room_name, prompt, profile)
            if success:
                ok_count += 1
            else:
                fail_count += 1
            if i < len(contacts) - 1:
                await asyncio.sleep(delay)
        await lk.aclose()
    except Exception as exc:
        logger.error("Campaign run error: %s", exc)
    finally:
        await session.close()

    await update_campaign_run_stats(campaign_id, ok_count, fail_count)
    logger.info("Campaign %s done - %d dispatched, %d failed", campaign_id, ok_count, fail_count)


async def _reschedule_all_campaigns() -> None:
    if not _scheduler:
        return
    try:
        campaigns = await get_all_campaigns()
        for c in campaigns:
            if c.get("status") == "active" and c.get("schedule_type") in ("daily", "weekdays"):
                _schedule_campaign(c["id"], c["schedule_type"], c.get("schedule_time", "09:00"))
    except Exception as exc:
        logger.warning("Could not reschedule campaigns: %s", exc)


def _schedule_campaign(campaign_id: str, schedule_type: str, schedule_time: str) -> None:
    if not _scheduler:
        return
    job_id = f"campaign_{campaign_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    try:
        hour, minute = map(int, schedule_time.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 9, 0
    if schedule_type == "daily":
        trigger = CronTrigger(hour=hour, minute=minute)
    else:
        trigger = CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute)
    _scheduler.add_job(_run_campaign, trigger=trigger, args=[campaign_id], id=job_id, replace_existing=True)
    logger.info("Scheduled campaign %s (%s at %02d:%02d)", campaign_id, schedule_type, hour, minute)


@app.post("/api/campaigns")
async def api_create_campaign(req: CampaignRequest):
    if await is_emergency_suspended():
        raise HTTPException(503, "Campaign dispatches are suspended by the administrator (Emergency Stop active).")
    if not req.contacts:
        raise HTTPException(400, "contacts list cannot be empty")
    if req.schedule_type not in ("once", "daily", "weekdays"):
        raise HTTPException(400, "schedule_type must be: once | daily | weekdays")

    campaign_id = await create_campaign(
        name=req.name, contacts_json=json.dumps(req.contacts),
        schedule_type=req.schedule_type, schedule_time=req.schedule_time,
        call_delay_seconds=req.call_delay_seconds, system_prompt=req.system_prompt,
        agent_profile_id=req.agent_profile_id,
    )
    campaign = await get_campaign(campaign_id)

    if req.schedule_type == "once":
        asyncio.create_task(_run_campaign(campaign_id))
    else:
        _schedule_campaign(campaign_id, req.schedule_type, req.schedule_time)

    return {"status": "created", "campaign_id": campaign_id, "campaign": campaign}


@app.get("/api/campaigns")
async def api_list_campaigns():
    return await get_all_campaigns()


@app.delete("/api/campaigns/{campaign_id}")
async def api_delete_campaign(campaign_id: str):
    ok = await delete_campaign(campaign_id)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    return {"status": "deleted"}


@app.post("/api/campaigns/{campaign_id}/run")
async def api_run_campaign_now(campaign_id: str):
    if await is_emergency_suspended():
        raise HTTPException(503, "Campaign dispatches are suspended by the administrator (Emergency Stop active).")
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    asyncio.create_task(_run_campaign(campaign_id))
    return {"status": "dispatching", "campaign_id": campaign_id}


@app.patch("/api/campaigns/{campaign_id}/status")
async def api_update_campaign_status(campaign_id: str, req: StatusRequest):
    if req.status not in ("active", "paused", "completed"):
        raise HTTPException(400, "status must be: active | paused | completed")
    ok = await update_campaign_status(campaign_id, req.status)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if req.status == "paused" and _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    elif req.status == "active":
        campaign = await get_campaign(campaign_id)
        if campaign and campaign.get("schedule_type") in ("daily", "weekdays"):
            _schedule_campaign(campaign_id, campaign["schedule_type"], campaign.get("schedule_time", "09:00"))
    return {"status": req.status}

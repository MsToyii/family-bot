"""管理后台 API 路由"""
import hmac
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import set_key
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from jinja2 import Environment, FileSystemLoader

from config import config
from storage import models as storage
from daily_report import is_report_enabled, set_report_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")
_tmpl_dir = str(Path(__file__).parent / "templates")
_jinja_env = Environment(loader=FileSystemLoader(_tmpl_dir))

def _render(name: str, **ctx) -> HTMLResponse:
    tmpl = _jinja_env.get_template(name)
    return HTMLResponse(tmpl.render(**ctx))


def _to_beijing_time(ts: str | None) -> str:
    """SQLite UTC 时间 → 北京时间 (UTC+8)"""
    if not ts:
        return ""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        return (dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def _display_name(user_id: str) -> str:
    """open_id → 显示别名"""
    return config.user_aliases.get(user_id, user_id)

SECRET_KEY = os.getenv("ADMIN_PASSWORD", "")
serializer = URLSafeTimedSerializer(SECRET_KEY, salt="admin-session")
SESSION_MAX_AGE = 86400

ENV_PATH = str(Path(__file__).parent / ".env")


def _create_session(response, username: str = "admin"):
    token = serializer.dumps({"user": username})
    response.set_cookie(
        "admin_session", token,
        httponly=True, max_age=SESSION_MAX_AGE, samesite="lax",
    )
    return response


def _verify_session(request: Request) -> str | None:
    token = request.cookies.get("admin_session")
    if not token:
        return None
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE)
        return data.get("user")
    except (BadSignature, SignatureExpired):
        return None


async def _auth_dependency(request: Request):
    user = _verify_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


# ==================== 页面路由 ====================

@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = _verify_session(request)
    return _render("admin.html", authenticated=user is not None)


@router.post("/login")
async def admin_login(request: Request):
    body = await request.json()
    password = body.get("password", "")
    if not config.admin_password:
        return JSONResponse({"success": False, "error": "未配置 ADMIN_PASSWORD"}, status_code=500)
    if hmac.compare_digest(password, config.admin_password):
        resp = JSONResponse({"success": True})
        _create_session(resp)
        return resp
    return JSONResponse({"success": False, "error": "密码错误"}, status_code=401)


@router.post("/logout")
async def admin_logout():
    resp = JSONResponse({"success": True})
    resp.set_cookie("admin_session", "", max_age=0)
    return resp


# ==================== API 路由 ====================

async def _api_auth(request: Request):
    return await _auth_dependency(request)


@router.get("/api/dashboard")
async def dashboard(request: Request):
    await _api_auth(request)
    stats = await storage.get_daily_stats(date_offset=0)
    return {
        "total": stats["total"],
        "active_users": len(stats["users"]),
        "users": [{
            "user_id": u["user_id"],
            "display_name": _display_name(u["user_id"]),
            "role": u["role"],
            "count": u["count"],
        } for u in stats["users"]],
        "error_count": stats["error_count"],
        "trend": stats["trend"],
        "first_date": stats["first_date"],
    }


@router.get("/api/conversations")
async def conversations(
    request: Request,
    user_id: str = "",
    date_from: str = "",
    date_to: str = "",
    search: str = "",
    page: int = 1,
    page_size: int = 20,
):
    await _api_auth(request)
    result = await storage.get_conversations_paginated(
        user_id=user_id or None,
        date_from=date_from or None,
        date_to=date_to or None,
        search=search or None,
        page=page,
        page_size=page_size,
    )
    for item in result["items"]:
        item["display_name"] = _display_name(item["user_id"])
        item["created_at"] = _to_beijing_time(item.get("created_at"))
    return result


@router.get("/api/users")
async def get_users(request: Request):
    await _api_auth(request)
    return {
        "admin_users": config.admin_users,
        "child_users": config.child_users,
        "tutor_users": config.tutor_users,
    }


@router.post("/api/users")
async def update_users(request: Request):
    await _api_auth(request)
    body = await request.json()
    updates = {
        "ADMIN_USERS": body.get("admin_users", ""),
        "CHILD_USERS": body.get("child_users", ""),
        "TUTOR_USERS": body.get("tutor_users", ""),
    }
    for key, value in updates.items():
        set_key(ENV_PATH, key, value)
    config.reload_users()
    logger.info("用户列表已更新")
    return {"success": True}


@router.get("/api/settings")
async def get_settings(request: Request):
    await _api_auth(request)
    def mask(s: str) -> str:
        if len(s) <= 8:
            return s[:4] + "****" if len(s) > 4 else "****"
        return s[:4] + "****" + s[-4:]

    return {
        "deepseek_key": mask(config.deepseek_api_key),
        "kimi_key": mask(config.kimi_api_key),
        "feishu_app_id": config.feishu_app_id,
        "report_enabled": is_report_enabled(),
        "report_time": config.report_time,
        "context_rounds": config.context_rounds,
        "host": config.host,
        "port": config.port,
    }


@router.post("/api/settings")
async def update_settings(request: Request):
    await _api_auth(request)
    body = await request.json()
    if "report_enabled" in body:
        set_report_enabled(body["report_enabled"])
    if "report_time" in body:
        set_key(ENV_PATH, "REPORT_TIME", body["report_time"])
        config.report_time = body["report_time"]
    logger.info("系统设置已更新")
    return {"success": True, "note": "推送时间修改将在下次服务重启后生效"}


@router.get("/api/progress")
async def progress(request: Request, child_id: str = "", subject: str = ""):
    await _api_auth(request)
    if child_id:
        items = await storage.get_progress(child_id, subject or None)
    else:
        items = await storage.get_all_progress(subject or None)
    return {"items": items}


@router.get("/api/progress/filters")
async def progress_filters(request: Request):
    await _api_auth(request)
    return {
        "children": await storage.get_distinct_children(),
        "subjects": await storage.get_distinct_subjects(),
    }


@router.post("/api/change-password")
async def change_password(request: Request):
    await _api_auth(request)
    body = await request.json()
    current = body.get("current_password", "")
    new = body.get("new_password", "")
    if not new or len(new) < 4:
        return JSONResponse({"success": False, "error": "新密码至少4位"}, status_code=400)
    if not hmac.compare_digest(current, config.admin_password):
        return JSONResponse({"success": False, "error": "当前密码错误"}, status_code=401)
    set_key(ENV_PATH, "ADMIN_PASSWORD", new)
    config.admin_password = new
    # 使所有现有 session 失效
    global serializer
    serializer = URLSafeTimedSerializer(new, salt="admin-session")
    logger.info("管理员密码已更新")
    return {"success": True}



"""
Session认证模块
提供基于Session的登录认证功能
"""
import secrets
import os
from functools import wraps
from typing import Optional
from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse


def generate_session_secret() -> str:
    """生成随机的session密钥"""
    return secrets.token_hex(32)


def is_logged_in(request: Request) -> bool:
    """检查用户是否已登录"""
    return request.session.get("authenticated", False)


def login_user(request: Request):
    """标记用户为已登录状态"""
    request.session["authenticated"] = True


def logout_user(request: Request):
    """清除用户登录状态"""
    request.session.clear()


def require_login(redirect_to_login: bool = True):
    """
    要求用户登录的装饰器

    Args:
        redirect_to_login: 未登录时是否重定向到登录页面（默认True）
                          False时返回404错误
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, request: Request, **kwargs):
            if not is_logged_in(request):
                if redirect_to_login:
                    accept_header = (request.headers.get("accept") or "").lower()
                    wants_html = "text/html" in accept_header or request.url.path.endswith("/html")

                    if wants_html:
                        # For SPA deployments (Vue hash router), redirect to the frontend login route.
                        # Prefer ASGI scope root_path (supports reverse proxies), then fall back to env.
                        root_path = str(request.scope.get("root_path") or "")
                        if not root_path:
                            prefix = (os.getenv("PATH_PREFIX") or "").strip().strip("/")
                            if prefix:
                                root_path = f"/{prefix}"

                        login_url = f"{root_path}/#/login" if root_path else "/#/login"

                        return RedirectResponse(url=login_url, status_code=302)

                raise HTTPException(401, "Unauthorized")

            return await func(*args, request=request, **kwargs)
        return wrapper
    return decorator

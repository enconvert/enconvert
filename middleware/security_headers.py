from starlette.middleware.base import BaseHTTPMiddleware
from config import GATEWAY_DOMAIN

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        cf = "https://challenges.cloudflare.com"
        frame_ancestors = getattr(request.state, "frame_ancestors", None)
        if frame_ancestors:
            ancestors = " ".join(frame_ancestors)
            response.headers["Content-Security-Policy"] = (
                f"frame-ancestors 'self' {ancestors}; "
                f"default-src 'self'; "
                f"script-src 'self' {cf} 'unsafe-inline'; "
                f"frame-src {cf}; "
                f"style-src 'self' 'unsafe-inline'; "
                f"connect-src 'self' {GATEWAY_DOMAIN}"
            )
        else:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                f"default-src 'self'; "
                f"script-src 'self' {cf} 'unsafe-inline'; "
                f"frame-src {cf}; "
                f"style-src 'self' 'unsafe-inline'; "
                f"connect-src 'self' {GATEWAY_DOMAIN}"
            )

        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"

        return response
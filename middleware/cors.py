from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from utils.validators import is_domain_allowed
from config import WIDGET_ORIGIN, DASHBOARD_ORIGIN, BACKEND_ORIGIN
import logging

logger = logging.getLogger("conversion-api-gateway")

class DynamicCORSMiddleware(BaseHTTPMiddleware):
    """
    Dynamic CORS middleware that checks allowed_domains per API key
    
    For preflight requests (OPTIONS):
    - Allows all origins temporarily (can't validate API key yet)
    - Actual validation happens in the real request
    
    For actual requests:
    - Checks if request.state.allowed_domains exists (set by auth)
    - Validates origin against allowed_domains
    - Adds CORS headers only if domain is allowed
    """
    
    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("Origin")
        
        #Handle preflight OPTIONS requests
        if request.method == "OPTIONS":
            # For preflight, we can't validate API key (not sent in OPTIONS)
            # So we allow the preflight and validate in the actual request
            
            if origin:
                return Response(
                    status_code=200,
                    headers={
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                        "Access-Control-Allow-Headers": "X-API-Key, Authorization, Content-Type, Content-Length, X-Parent-Origin",
                        "Access-Control-Allow-Credentials": "true",
                        "Access-Control-Max-Age": "3600", 
                    }
                )
                
            else:
                # No origin, just return 200 OK for preflight
                return Response(status_code=200)
            
        #Process the actual reqeuest
        response = await call_next(request)
        
        # Add CORS headers to response if:
        # 1. Request has an Origin header (it's a browser request)
        # 2. Request state has allowed_domains (set by auth middleware)
        # 3. Origin is in the allowed domains
        
        if origin:
            # Always allow the widget/playground, dashboard, and backend SSR origins
            is_widget_origin = origin == WIDGET_ORIGIN
            is_dashboard_origin = origin == DASHBOARD_ORIGIN
            is_backend_origin = origin == BACKEND_ORIGIN
            # Browser extensions have chrome-extension:// origins
            is_extension_origin = origin.startswith("chrome-extension://")

            allowed_domains = getattr(request.state, "allowed_domains", [])
            domain_allowed = allowed_domains and is_domain_allowed(origin, allowed_domains)

            if is_widget_origin or is_dashboard_origin or is_backend_origin or is_extension_origin or domain_allowed:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = "X-API-Key, Authorization, Content-Type, Content-Length, X-Parent-Origin"
                response.headers["Access-Control-Expose-Headers"] = "RateLimit-Limit, RateLimit-Remaining, RateLimit-Reset, Retry-After, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset, X-Filename, X-File-Size, X-Conversion-Time, X-GCS-URI, Content-Disposition"
            else:
                logger.warning(f"CORS rejected for origin: {origin}, allowed: {allowed_domains}")
                
        return response
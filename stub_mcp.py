import json
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware

kwargs = {}
for mod in ("mcp.server.transport_security", "mcp.server.streamable_http"):
    try:
        TS = __import__(mod, fromlist=["TransportSecuritySettings"]).TransportSecuritySettings
        kwargs["transport_security"] = TS(enable_dns_rebinding_protection=False)
        break
    except Exception:
        continue

mcp = FastMCP("insightfulpipe-stub", **kwargs)

@mcp.tool()
def get_campaigns() -> str:
    """Return the workspace's ad campaigns with spend and conversions."""
    return json.dumps([
        {"campaign": "Summer Sale", "spend": 1200.50, "conversions": 120},
        {"campaign": "Retargeting", "spend": 890.00, "conversions": 64},
    ])

app = mcp.streamable_http_app()

class LogAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        a = request.headers.get("authorization", "")
        if a:
            open("/tmp/mcp_auth.txt", "w").write(a)
        return await call_next(request)
app.add_middleware(LogAuth)

import base64, hashlib, hmac, os, secrets, time
from gateway.token_store import TokenStore
from pathlib import Path
from urllib.parse import urlencode
import anyio
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse, RedirectResponse, PlainTextResponse
from starlette.routing import Route
from starlette.requests import Request

UPSTREAM=os.getenv("GATEWAY_UPSTREAM","http://127.0.0.1:8000")
EXT=Path(os.getenv("GATEWAY_TOKEN_FILE","/home/test/.config/ssh-mcp/gateway-token"))
INT=Path(os.getenv("GATEWAY_INTERNAL_TOKEN_FILE","/home/test/.config/ssh-mcp/http-token"))
codes={}
# Persist opaque random credentials as hashes, never plaintext.
# The file stays outside the Git worktree and survives gateway restarts.
TOKEN_STORE=TokenStore(Path(os.getenv(
    "GATEWAY_TOKEN_DB", str(Path.home() / ".config/ssh-mcp/oauth-tokens.sqlite3")
)))

def read(p): return p.read_text().strip()
def b64u(x): return base64.urlsafe_b64encode(x).rstrip(b"=").decode()
def challenge(verifier): return b64u(hashlib.sha256(verifier.encode()).digest())

def valid_gateway_auth(authorization: str) -> bool:
    if not authorization.startswith("Bearer "):
        return False
    supplied = authorization[7:]
    if not supplied:
        return False
    if EXT.is_file() and hmac.compare_digest(supplied, read(EXT)):
        return True
    return TOKEN_STORE.authorized(supplied)


async def health(request): return JSONResponse({"status":"ok","upstream":UPSTREAM})
async def metadata(request):
    base=os.getenv("GATEWAY_PUBLIC_BASE",str(request.base_url).rstrip("/"))
    return JSONResponse({"issuer":base,"authorization_endpoint":base+"/oauth/authorize","token_endpoint":base+"/oauth/token","code_challenge_methods_supported":["S256"],"response_types_supported":["code"],"grant_types_supported":["authorization_code","refresh_token"],"scopes_supported":["mcp","offline_access"],"token_endpoint_auth_methods_supported":["none"]})
async def resource(request):
    base=os.getenv("GATEWAY_PUBLIC_BASE",str(request.base_url).rstrip("/"))
    return JSONResponse({"resource":base+"/mcp","authorization_servers":[base]})
async def authorize(request):
    q=request.query_params
    required=("client_id","redirect_uri","response_type","state","code_challenge")
    if any(not q.get(k) for k in required) or q["response_type"]!="code":
        return JSONResponse({"error":"invalid_request"},status_code=400)
    code=secrets.token_urlsafe(32)
    codes[code]={"client_id":q["client_id"],"redirect_uri":q["redirect_uri"],"challenge":q["code_challenge"],"chatgpt_state":q.get("state"),"expires":time.time()+300}
    params={"code":code,"state":q["state"]}
    return RedirectResponse(q["redirect_uri"]+"?"+urlencode(params),status_code=302)
async def token_endpoint(request):
    form=await request.form()
    grant=form.get("grant_type")
    if grant == "refresh_token":
        # This is a public OAuth client using PKCE, not a client secret.
        if form.get("client_id") != CHATGPT_CLIENT:
            return JSONResponse({"error":"invalid_client"}, status_code=401)
        result=TOKEN_STORE.refresh(form.get("refresh_token", ""), CHATGPT_CLIENT)
        if not result:
            return JSONResponse({"error":"invalid_grant"}, status_code=400)
        return JSONResponse(result, headers={"Cache-Control":"no-store","Pragma":"no-cache"})
    if grant != "authorization_code":
        return JSONResponse({"error":"unsupported_grant_type"}, status_code=400)
    code=form.get("code")
    verifier=form.get("code_verifier")
    client=form.get("client_id")
    item=codes.get(code) if code else None
    if (not item or item["expires"]<time.time()
            or client != item["client_id"] or client != CHATGPT_CLIENT
            or form.get("redirect_uri") != item["redirect_uri"]
            or not verifier
            or not hmac.compare_digest(challenge(verifier),item["challenge"])):
        return JSONResponse({"error":"invalid_grant"},status_code=400)
    # Consume authorization codes only after validating all parameters.
    codes.pop(code, None)
    return JSONResponse(
        TOKEN_STORE.issue(client),
        headers={"Cache-Control":"no-store","Pragma":"no-cache"},
    )
routes=[Route("/healthz",health),Route("/.well-known/oauth-authorization-server",metadata),Route("/.well-known/oauth-protected-resource",resource),Route("/oauth/authorize",authorize),Route("/oauth/token",token_endpoint,methods=["POST"])]
app=Starlette(routes=routes)

async def register(request):
    try:
        data=await request.json()
    except Exception:
        data={}
    client_id="chatgpt-"+secrets.token_urlsafe(12)
    return JSONResponse({"client_id":client_id,"client_id_issued_at":int(time.time()),"redirect_uris":data.get("redirect_uris",[]),"grant_types":["authorization_code"],"response_types":["code"],"token_endpoint_auth_method":"none"})
app.routes.append(Route("/register",register,methods=["POST"]))

CHATGPT_CLIENT=os.getenv("CHATGPT_OAUTH_CLIENT_ID", "chatgpt-mcp-bridge")
CHATGPT_REDIRECT=os.getenv("CHATGPT_OAUTH_REDIRECT_URI", "https://chatgpt.com/connector/oauth/_m0Xcx2r0jW3")
CHATGPT_REDIRECT_PREFIX="https://chatgpt.com/connector/oauth/"
CHATGPT_STABLE_REDIRECT="https://chatgpt.com/connector_platform_oauth_redirect"
def valid_chatgpt_redirect(value):
    if value in {CHATGPT_REDIRECT, CHATGPT_STABLE_REDIRECT}:
        return True
    if not value.startswith(CHATGPT_REDIRECT_PREFIX):
        return False
    suffix=value[len(CHATGPT_REDIRECT_PREFIX):]
    return bool(suffix) and all(ch.isalnum() or ch in "_-" for ch in suffix)
oauth_states={}
async def chatgpt_authorize(request):
    q=request.query_params
    redirect_uri=q.get("redirect_uri", "")
    if q.get("client_id")!=CHATGPT_CLIENT or not valid_chatgpt_redirect(redirect_uri) or q.get("response_type")!="code" or q.get("code_challenge_method")!="S256" or not q.get("code_challenge") or not q.get("state"):
        return JSONResponse({"error":"invalid_client_or_redirect"},status_code=400)
    state=secrets.token_urlsafe(24)
    oauth_states[state]={"client_id":q["client_id"],"redirect_uri":q["redirect_uri"],"challenge":q["code_challenge"],"chatgpt_state":q.get("state"),"expires":time.time()+300}
    github="https://github.com/login/oauth/authorize?"+urlencode({"client_id":os.getenv("GITHUB_CLIENT_ID",""),"redirect_uri":os.getenv("GATEWAY_PUBLIC_BASE",str(request.base_url).rstrip("/"))+"/oauth/github/callback","scope":"read:user","state":state})
    return RedirectResponse(github,status_code=302)
async def github_callback_v2(request):
    code=request.query_params.get("code"); state=request.query_params.get("state")
    item=oauth_states.pop(state,None) if state else None
    if not code or not item or item["expires"]<time.time(): return PlainTextResponse("invalid OAuth state",status_code=400)
    cid=os.getenv("GITHUB_CLIENT_ID"); secret=os.getenv("GITHUB_CLIENT_SECRET")
    async with httpx.AsyncClient(timeout=60) as c:
        r=await c.post("https://github.com/login/oauth/access_token",data={"client_id":cid,"client_secret":secret,"code":code},headers={"Accept":"application/json"})
        access=r.json().get("access_token") if r.status_code==200 else None
        u=await c.get("https://api.github.com/user",headers={"Authorization":"Bearer "+(access or ""), "Accept":"application/vnd.github+json"})
    if u.status_code!=200: return PlainTextResponse("GitHub identity failed",status_code=403)
    user=u.json(); allowed={x.strip() for x in os.getenv("GITHUB_ALLOWED_IDS","").split(",") if x.strip()}
    if not allowed or str(user.get("id")) not in allowed: return PlainTextResponse("GitHub user not allowed",status_code=403)
    verifier_challenge=item["challenge"]
    code2=secrets.token_urlsafe(32)
    codes[code2]={"client_id":item["client_id"],"redirect_uri":item["redirect_uri"],"challenge":verifier_challenge,"expires":time.time()+300,"github_id":user.get("id")}
    return RedirectResponse(item["redirect_uri"]+"?"+urlencode({"code":code2,"state":item.get("chatgpt_state")}),status_code=302)
app.router.routes=[r for r in app.routes if getattr(r,"path",None) != "/oauth/authorize"]
app.routes.append(Route("/oauth/authorize",chatgpt_authorize,methods=["GET"]))
app.routes.append(Route("/oauth/github/callback",github_callback_v2,methods=["GET"]))

async def oidc_metadata(request): return await metadata(request)
async def resource_alias(request): return await resource(request)
app.routes.append(Route("/.well-known/openid-configuration",oidc_metadata,methods=["GET"]))
app.routes.append(Route("/.well-known/oauth-protected-resource/mcp",resource_alias,methods=["GET"]))
app.routes.append(Route("/mcp/.well-known/oauth-protected-resource",resource_alias,methods=["GET"]))

async def _close_upstream(upstream, client):
    """Close both sides even if the downstream request was cancelled."""
    with anyio.CancelScope(shield=True):
        try:
            await upstream.aclose()
        finally:
            await client.aclose()


class MCPProxyResponse(StreamingResponse):
    """Stream the upstream response while owning its HTTP connection."""

    def __init__(self, upstream, client, headers):
        self._upstream = upstream
        self._upstream_client = client
        super().__init__(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=headers,
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Includes SSE disconnects, cancellation and send failures.
            await _close_upstream(self._upstream, self._upstream_client)


async def mcp_proxy_fixed(request):
    if not valid_gateway_auth(request.headers.get("authorization", "")):
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    body = await request.body()
    headers = {
        key: request.headers[key]
        for key in ("content-type", "accept", "mcp-session-id", "last-event-id")
        if key in request.headers
    }
    # Never forward the external client token to the internal MCP service.
    headers["authorization"] = "Bearer " + read(INT)

    # Read timeout is intentionally unlimited for long-lived MCP/SSE sessions.
    # The ASGI response owns the upstream connection and closes it on exit.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
    )
    try:
        outgoing = client.build_request(
            request.method, UPSTREAM + "/mcp", content=body, headers=headers
        )
        upstream = await client.send(outgoing, stream=True)
    except BaseException as exc:
        with anyio.CancelScope(shield=True):
            await client.aclose()
        if isinstance(exc, httpx.TimeoutException):
            return JSONResponse({"error": "upstream_timeout"}, status_code=504)
        if isinstance(exc, httpx.HTTPError):
            return JSONResponse({"error": "upstream_unavailable"}, status_code=502)
        raise

    # MCP session and event headers must survive the proxy unchanged.
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in {
            "content-type", "mcp-session-id", "last-event-id",
            "cache-control", "content-encoding",
        }
    }
    return MCPProxyResponse(upstream, client, headers=response_headers)


app.router.routes.append(
    Route("/mcp", mcp_proxy_fixed, methods=["GET", "POST", "DELETE"])
)

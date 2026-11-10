"""Warehouse Stock Manager - FastAPI server.

Local:  APP_PASSWORD=your-password uvicorn main:app --host 0.0.0.0 --port 8000
Cloud:  set APP_PASSWORD, SESSION_SECRET and DATABASE_URL (a postgres:// URL), see README.md.
"""
import hmac
import os
import secrets
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from auth import LoginThrottle, make_token, verify_token
from store import Store, StoreError

BASE = Path(__file__).resolve().parent

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
if len(APP_PASSWORD) < 8:
    raise RuntimeError(
        "Set the APP_PASSWORD environment variable (at least 8 characters) before starting the server."
    )

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
if DATABASE_URL and len(SESSION_SECRET) < 32:
    raise RuntimeError(
        "Set SESSION_SECRET to a long random text (at least 32 characters) so logins survive restarts."
    )
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)

store = Store(DATABASE_URL or os.environ.get("DB_PATH", str(BASE / "warehouse.db")))

app = FastAPI(title="Warehouse Stock Manager", docs_url=None, redoc_url=None, openapi_url=None)

SESSION_TTL = 12 * 3600
throttle = LoginThrottle(max_fails=8, window=600)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(StoreError)
async def store_error_handler(request: Request, exc: StoreError):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


def require_auth(authorization: str = Header(default="")):
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not verify_token(SESSION_SECRET, token):
        raise HTTPException(status_code=401, detail="Please log in")


class LoginIn(BaseModel):
    password: str = Field(max_length=200)


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    sku: str = Field(default="", max_length=40)
    cat: str = Field(default="", max_length=40)
    unit: str = Field(default="pcs", max_length=12)
    min: int = Field(default=10, ge=0, le=1_000_000)
    qty: int = Field(default=0, ge=0, le=1_000_000)
    date: Optional[str] = Field(default=None, max_length=10)
    expiry_date: Optional[str] = Field(default=None, max_length=10)


class MoveIn(BaseModel):
    type: Literal["purchase", "issue"]
    qty: int = Field(ge=1, le=1_000_000)
    date: Optional[str] = Field(default=None, max_length=10)
    supplier: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=120)


class SettingsIn(BaseModel):
    phone: str = Field(default="", max_length=25)
    apikey: Optional[str] = Field(default=None, max_length=40)
    low: bool = False
    expiry_days: int = Field(default=7, ge=0, le=365)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ip = request.client.host if request.client else "unknown"
    if throttle.blocked(ip):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in a few minutes.")
    if not hmac.compare_digest(body.password.encode(), APP_PASSWORD.encode()):
        throttle.fail(ip)
        raise HTTPException(status_code=401, detail="Wrong password")
    return {"token": make_token(SESSION_SECRET, SESSION_TTL)}


api = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


@api.get("/products")
def list_products():
    return store.list_products()


@api.post("/products")
def create_product(body: ProductIn):
    return store.create_product(
        body.name, body.sku, body.cat, body.unit, body.min, body.qty, body.date, body.expiry_date
    )


@api.put("/products/{pid}")
def update_product(pid: int, body: ProductIn):
    return store.update_product(pid, body.name, body.sku, body.cat, body.unit, body.min, body.expiry_date)


@api.delete("/products/{pid}")
def delete_product(pid: int):
    store.delete_product(pid)
    return {"ok": True}


@api.post("/products/{pid}/move")
def move_stock(pid: int, body: MoveIn, background: BackgroundTasks):
    result = store.move(pid, body.type, body.qty, body.date, body.supplier, body.note)
    alert = None
    if result["alert_message"]:
        alert = "queued" if store.alerts_configured() else "not_configured"
        background.add_task(store.dispatch_alert, result["alert_message"])
    return {"product": result["product"], "alert": alert}


@api.get("/log")
def stock_log():
    return store.list_log()


@api.get("/backup")
def backup():
    return store.export_all()


@api.get("/settings")
def get_settings():
    return store.get_settings()


@api.put("/settings")
def put_settings(body: SettingsIn):
    return store.update_settings(body.phone, body.apikey, body.low, body.expiry_days)


@api.get("/alerts")
def alert_log():
    return store.list_alerts()


@api.post("/alerts/expiry-check")
def expiry_check():
    return store.dispatch_expiry_alerts()


@api.post("/alerts/test")
def test_alert():
    return store.send_test()


app.include_router(api)

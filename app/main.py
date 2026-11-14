from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.modules.accounting.bridge_router import router as bridge_router
from app.modules.accounting.router import router as accounting_router
from app.modules.auth.router import router as auth_router

app = FastAPI(
    title="Hamads Accounting",
    version="1.0.0",
    docs_url="/docs", # Force enable this for debugging
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/admin/auth")
app.include_router(accounting_router)
app.include_router(bridge_router)


@app.get("/health")
async def health():
    return {"status": "ok"}

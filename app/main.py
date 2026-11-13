from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.modules.auth.router import router as auth_router
from app.modules.catalog.router import router as catalog_router
from app.modules.cart.router import router as cart_router
from app.modules.delivery.router import router as delivery_router
from app.modules.orders.router import router as orders_router
from app.modules.reviews.router import router as reviews_router
from app.modules.wishlist.router import router as wishlist_router
from app.modules.fulfillment.router import router as fulfillment_router
from app.modules.admin.router import router as admin_router
from app.modules.accounting.router import router as accounting_router

app = FastAPI(
    title="Hamads API",
    version="1.0.0",
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
)

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=settings.CORS_ORIGINS,
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )


app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in settings.CORS_ORIGINS if o != "*"],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



@app.get("/health")
async def health_check():
    return {"status": "ok"}

app.include_router(auth_router)
app.include_router(catalog_router)
app.include_router(cart_router)
app.include_router(delivery_router)
app.include_router(orders_router)
app.include_router(reviews_router)
app.include_router(wishlist_router)
app.include_router(fulfillment_router)
app.include_router(admin_router)
app.include_router(accounting_router)


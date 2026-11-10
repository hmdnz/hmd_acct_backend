import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.modules.admin import schemas, service
from app.modules.auth.dependencies import get_current_admin
from app.modules.auth.models import Admin
from app.modules.auth.schemas import TokenResponse
from app.modules.catalog import schemas as catalog_schemas

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------- auth ----------

@router.post("/auth/login", response_model=TokenResponse)
async def admin_login(
    payload: schemas.AdminLoginRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    client_ip = request.client.host if request.client else "unknown"
    return await service.login(db, payload.email, payload.password, client_ip)


@router.get("/me", response_model=schemas.AdminMe)
async def admin_me(admin: Admin = Depends(get_current_admin)):
    return schemas.AdminMe(
        id=admin.id, first_name=admin.first_name, last_name=admin.last_name, email=admin.email
    )


# ---------- sellers ----------

@router.get("/sellers", response_model=schemas.Page[schemas.AdminSellerOut])
async def list_sellers(
    is_approved: bool | None = None,
    is_active: bool | None = None,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.list_sellers(db, is_approved, is_active, q, limit, offset)


@router.patch("/sellers/{seller_id}", response_model=schemas.AdminSellerOut)
async def update_seller(
    seller_id: uuid.UUID,
    payload: schemas.AdminSellerUpdate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.update_seller(db, admin, seller_id, payload)


# ---------- customers ----------

@router.get("/users", response_model=schemas.Page[schemas.AdminUserOut])
async def list_users(
    is_active: bool | None = None,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.list_users(db, is_active, q, limit, offset)


@router.patch("/users/{user_id}", response_model=schemas.AdminUserOut)
async def update_user(
    user_id: uuid.UUID,
    payload: schemas.AdminUserUpdate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.update_user(db, admin, user_id, payload)


# ---------- products ----------

@router.get("/products", response_model=schemas.Page[schemas.AdminProductOut])
async def list_products(
    is_approved: bool | None = None,
    is_active: bool | None = None,
    seller_id: uuid.UUID | None = None,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    """Moderation queue: ?is_approved=false&is_active=true"""
    return await service.list_products(db, is_approved, is_active, seller_id, q, limit, offset)


@router.patch("/products/{product_id}", response_model=schemas.AdminProductOut)
async def update_product(
    product_id: uuid.UUID,
    payload: schemas.AdminProductUpdate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.update_product(db, admin, product_id, payload)


# ---------- reviews ----------

@router.get("/reviews", response_model=schemas.Page[schemas.AdminReviewOut])
async def list_reviews(
    product_id: uuid.UUID | None = None,
    is_visible: bool | None = None,
    rating: int | None = Query(default=None, ge=1, le=5),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.list_reviews(db, product_id, is_visible, rating, limit, offset)


@router.patch("/reviews/{review_id}", response_model=schemas.AdminReviewOut)
async def set_review_visibility(
    review_id: uuid.UUID,
    payload: schemas.AdminReviewUpdate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.set_review_visibility(db, admin, review_id, payload)


# ---------- categories / brands ----------

@router.post("/categories", response_model=catalog_schemas.CategoryResponse, status_code=201)
async def create_category(
    payload: schemas.CategoryCreate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.create_category(db, admin, payload)


@router.patch("/categories/{category_id}", response_model=catalog_schemas.CategoryResponse)
async def rename_category(
    category_id: uuid.UUID,
    payload: schemas.NameRequest,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.rename_category(db, admin, category_id, payload)


@router.post("/brands", response_model=catalog_schemas.BrandResponse, status_code=201)
async def create_brand(
    payload: schemas.BrandCreate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.create_brand(db, admin, payload)


@router.patch("/brands/{brand_id}", response_model=catalog_schemas.BrandResponse)
async def rename_brand(
    brand_id: uuid.UUID,
    payload: schemas.NameRequest,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.rename_brand(db, admin, brand_id, payload)


# ---------- audit log ----------

@router.get("/audit-log", response_model=schemas.Page[schemas.AdminActionOut])
async def audit_log(
    target_type: str | None = Query(default=None, max_length=30),
    target_id: uuid.UUID | None = None,
    admin_id: uuid.UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.list_actions(db, target_type, target_id, admin_id, limit, offset)
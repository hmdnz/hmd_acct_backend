import uuid

from fastapi import HTTPException
from slugify import slugify
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)
from app.modules.admin import schemas
from app.modules.auth.models import Admin, AdminAction, Seller, User
from app.modules.auth.schemas import TokenResponse
from app.modules.catalog import schemas as catalog_schemas
from app.modules.catalog.models import Brand, Category, Product
from app.modules.reviews.models import Review
from app.modules.reviews.service import display_name, refresh_product_rating
from app.modules.auth.rate_limit import (
    clear_login_failures, enforce_login_lockout, record_login_failure,
)

# Verified against when the email doesn't exist, so response time doesn't reveal
# which admin emails are real.
_DUMMY_HASH = hash_password("not-a-real-password")


# ---------- helpers ----------

def _set_flags(
    db: AsyncSession, admin_id: uuid.UUID, obj, target_type: str,
    changes: dict, reason: str | None,
) -> bool:
    """Apply only values that actually differ, and write one audit row describing
    what changed. Returns True if anything changed (so repeats are no-ops)."""
    diffs = []
    for field, value in changes.items():
        old = getattr(obj, field)
        if old != value:
            setattr(obj, field, value)
            diffs.append(f"{field}: {old} -> {value}")
    if diffs:
        db.add(AdminAction(
            admin_id=admin_id, action=f"{target_type}.update", target_type=target_type,
            target_id=obj.id, detail="; ".join(diffs), reason=reason,
        ))
    return bool(diffs)


def _log(db: AsyncSession, admin_id, action, target_type, target_id, detail=None, reason=None):
    db.add(AdminAction(
        admin_id=admin_id, action=action, target_type=target_type,
        target_id=target_id, detail=detail, reason=reason,
    ))


def _like(q: str) -> str:
    return f"%{q.strip()}%"


# ---------- auth ----------

async def login(db: AsyncSession, email: str, password: str, client_ip: str) -> TokenResponse:
    email = email.strip().lower()
    try:
        await enforce_login_lockout("admin", email, client_ip)
    except ValueError as e:
        raise HTTPException(429, str(e))

    admin = (await db.execute(select(Admin).where(Admin.email == email))).scalars().first()

    if not admin:
        verify_password(password, _DUMMY_HASH)
        await record_login_failure("admin", email, client_ip)
        raise HTTPException(401, "Invalid email or password")
    if not verify_password(password, admin.hashed_password):
        await record_login_failure("admin", email, client_ip)
        raise HTTPException(401, "Invalid email or password")
    if not admin.is_active:
        raise HTTPException(403, "Account disabled")

    await clear_login_failures("admin", email)
    subject = str(admin.id)
    return TokenResponse(
        access_token=create_access_token(subject, context="admin"),
        refresh_token=create_refresh_token(subject, context="admin"),
    )


# ---------- sellers ----------

async def list_sellers(
    db: AsyncSession, is_approved: bool | None, is_active: bool | None,
    q: str | None, limit: int, offset: int,
) -> schemas.Page[schemas.AdminSellerOut]:
    conds = []
    if is_approved is not None:
        conds.append(Seller.is_approved.is_(is_approved))
    if is_active is not None:
        conds.append(Seller.is_active.is_(is_active))
    if q and q.strip():
        like = _like(q)
        conds.append(or_(
            Seller.brand_name.ilike(like), Seller.email.ilike(like),
            Seller.phone_number.ilike(like),
        ))

    total = (await db.execute(select(func.count(Seller.id)).where(*conds))).scalar_one()
    rows = (
        await db.execute(
            select(Seller).where(*conds)
            .order_by(Seller.created_at.desc(), Seller.id).limit(limit).offset(offset)
        )
    ).scalars().all()
    return schemas.Page[schemas.AdminSellerOut](
        items=[schemas.AdminSellerOut.model_validate(r) for r in rows],
        total=total, limit=limit, offset=offset,
    )


async def update_seller(
    db: AsyncSession, admin: Admin, seller_id: uuid.UUID, data: schemas.AdminSellerUpdate
) -> schemas.AdminSellerOut:
    admin_id = admin.id
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(404, "Seller not found")

    changes = data.model_dump(exclude={"reason"}, exclude_none=True)
    if _set_flags(db, admin_id, seller, "seller", changes, data.reason):
        await db.commit()
        await db.refresh(seller)  # commit expires the object; reload before reading it
    return schemas.AdminSellerOut.model_validate(seller)


# ---------- customers ----------

async def list_users(
    db: AsyncSession, is_active: bool | None, q: str | None, limit: int, offset: int,
) -> schemas.Page[schemas.AdminUserOut]:
    conds = []
    if is_active is not None:
        conds.append(User.is_active.is_(is_active))
    if q and q.strip():
        like = _like(q)
        conds.append(or_(
            User.phone_number.ilike(like), User.email.ilike(like),
            User.first_name.ilike(like), User.last_name.ilike(like),
        ))

    total = (await db.execute(select(func.count(User.id)).where(*conds))).scalar_one()
    rows = (
        await db.execute(
            select(User).where(*conds)
            .order_by(User.created_at.desc(), User.id).limit(limit).offset(offset)
        )
    ).scalars().all()
    return schemas.Page[schemas.AdminUserOut](
        items=[schemas.AdminUserOut.model_validate(r) for r in rows],
        total=total, limit=limit, offset=offset,
    )


async def update_user(
    db: AsyncSession, admin: Admin, user_id: uuid.UUID, data: schemas.AdminUserUpdate
) -> schemas.AdminUserOut:
    admin_id = admin.id
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    if _set_flags(db, admin_id, user, "user", {"is_active": data.is_active}, data.reason):
        await db.commit()
        await db.refresh(user)
    return schemas.AdminUserOut.model_validate(user)


# ---------- products ----------

def _product_out(p: Product, store_name: str) -> schemas.AdminProductOut:
    return schemas.AdminProductOut(
        id=p.id, seller_id=p.seller_id, store_name=store_name, name=p.name, slug=p.slug,
        description=p.description, price=p.price, compare_at_price=p.compare_at_price,
        stock_quantity=p.stock_quantity, category_id=p.category_id, brand_id=p.brand_id,
        image_urls=p.image_urls,  # images must be eager-loaded by the query
        average_rating=p.average_rating, review_count=p.review_count,
        is_active=p.is_active, is_approved=p.is_approved, created_at=p.created_at,
    )


async def list_products(
    db: AsyncSession, is_approved: bool | None, is_active: bool | None,
    seller_id: uuid.UUID | None, q: str | None, limit: int, offset: int,
) -> schemas.Page[schemas.AdminProductOut]:
    conds = []
    if is_approved is not None:
        conds.append(Product.is_approved.is_(is_approved))
    if is_active is not None:
        conds.append(Product.is_active.is_(is_active))
    if seller_id:
        conds.append(Product.seller_id == seller_id)
    if q and q.strip():
        conds.append(Product.name.ilike(_like(q)))

    total = (await db.execute(select(func.count(Product.id)).where(*conds))).scalar_one()
    rows = (
        await db.execute(
            select(Product, Seller.brand_name)
            .join(Seller, Seller.id == Product.seller_id)
            .where(*conds)
            .options(selectinload(Product.images))
            .order_by(Product.created_at.desc(), Product.id).limit(limit).offset(offset)
        )
    ).all()
    return schemas.Page[schemas.AdminProductOut](
        items=[_product_out(p, store) for p, store in rows],
        total=total, limit=limit, offset=offset,
    )


async def _fetch_product_out(db: AsyncSession, product_id: uuid.UUID) -> schemas.AdminProductOut:
    row = (
        await db.execute(
            select(Product, Seller.brand_name)
            .join(Seller, Seller.id == Product.seller_id)
            .where(Product.id == product_id)
            .options(selectinload(Product.images))
            .execution_options(populate_existing=True)
        )
    ).first()
    if not row:
        raise HTTPException(404, "Product not found")
    return _product_out(*row)


async def update_product(
    db: AsyncSession, admin: Admin, product_id: uuid.UUID, data: schemas.AdminProductUpdate
) -> schemas.AdminProductOut:
    admin_id = admin.id
    product = await db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")

    changes = data.model_dump(exclude={"reason"}, exclude_none=True)
    if _set_flags(db, admin_id, product, "product", changes, data.reason):
        await db.commit()
    return await _fetch_product_out(db, product_id)


# ---------- reviews ----------

def _review_out(r: Review) -> schemas.AdminReviewOut:
    return schemas.AdminReviewOut(
        id=r.id, product_id=r.product_id, user_id=r.user_id,
        reviewer_name=display_name(r.user),  # user must be eager-loaded
        rating=r.rating, title=r.title, comment=r.comment,
        is_visible=r.is_visible, created_at=r.created_at,
    )


async def list_reviews(
    db: AsyncSession, product_id: uuid.UUID | None, is_visible: bool | None,
    rating: int | None, limit: int, offset: int,
) -> schemas.Page[schemas.AdminReviewOut]:
    conds = []
    if product_id:
        conds.append(Review.product_id == product_id)
    if is_visible is not None:
        conds.append(Review.is_visible.is_(is_visible))
    if rating:
        conds.append(Review.rating == rating)

    total = (await db.execute(select(func.count(Review.id)).where(*conds))).scalar_one()
    rows = (
        await db.execute(
            select(Review).where(*conds).options(selectinload(Review.user))
            .order_by(Review.created_at.desc(), Review.id).limit(limit).offset(offset)
        )
    ).scalars().all()
    return schemas.Page[schemas.AdminReviewOut](
        items=[_review_out(r) for r in rows], total=total, limit=limit, offset=offset,
    )


async def set_review_visibility(
    db: AsyncSession, admin: Admin, review_id: uuid.UUID, data: schemas.AdminReviewUpdate
) -> schemas.AdminReviewOut:
    admin_id = admin.id
    review = await db.get(Review, review_id)
    if not review:
        raise HTTPException(404, "Review not found")
    product_id = review.product_id

    if _set_flags(db, admin_id, review, "review", {"is_visible": data.is_visible}, data.reason):
        await db.flush()
        # Hidden reviews must stop counting toward the product's rating.
        await refresh_product_rating(db, product_id)
        await db.commit()

    fresh = (
        await db.execute(
            select(Review).where(Review.id == review_id)
            .options(selectinload(Review.user))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return _review_out(fresh)


# ---------- categories / brands ----------

async def create_category(
    db: AsyncSession, admin: Admin, data: schemas.CategoryCreate
) -> catalog_schemas.CategoryResponse:
    admin_id = admin.id
    if data.parent_id and not await db.get(Category, data.parent_id):
        raise HTTPException(404, "Parent category not found")

    slug = slugify(data.name)
    if not slug:
        raise HTTPException(422, "Name must contain letters or numbers")
    if (await db.execute(select(Category.id).where(Category.slug == slug))).first():
        raise HTTPException(409, "A category with this name already exists")

    category = Category(name=data.name, slug=slug, parent_id=data.parent_id)
    db.add(category)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A category with this name already exists")

    out = catalog_schemas.CategoryResponse(
        id=category.id, name=category.name, slug=category.slug, parent_id=category.parent_id
    )
    _log(db, admin_id, "category.create", "category", category.id, detail=f"name: {data.name}")
    await db.commit()
    return out


async def rename_category(
    db: AsyncSession, admin: Admin, category_id: uuid.UUID, data: schemas.NameRequest
) -> catalog_schemas.CategoryResponse:
    admin_id = admin.id
    category = await db.get(Category, category_id)
    if not category:
        raise HTTPException(404, "Category not found")

    old = category.name
    category.name = data.name  # slug is left alone so existing URLs keep working
    _log(db, admin_id, "category.rename", "category", category_id, detail=f"name: {old} -> {data.name}")
    out = catalog_schemas.CategoryResponse(
        id=category.id, name=data.name, slug=category.slug, parent_id=category.parent_id
    )
    await db.commit()
    return out


async def create_brand(
    db: AsyncSession, admin: Admin, data: schemas.BrandCreate
) -> catalog_schemas.BrandResponse:
    admin_id = admin.id
    slug = slugify(data.name)
    if not slug:
        raise HTTPException(422, "Name must contain letters or numbers")
    if (await db.execute(select(Brand.id).where(Brand.slug == slug))).first():
        raise HTTPException(409, "A brand with this name already exists")

    brand = Brand(name=data.name, slug=slug)
    db.add(brand)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A brand with this name already exists")

    out = catalog_schemas.BrandResponse(id=brand.id, name=brand.name, slug=brand.slug)
    _log(db, admin_id, "brand.create", "brand", brand.id, detail=f"name: {data.name}")
    await db.commit()
    return out


async def rename_brand(
    db: AsyncSession, admin: Admin, brand_id: uuid.UUID, data: schemas.NameRequest
) -> catalog_schemas.BrandResponse:
    admin_id = admin.id
    brand = await db.get(Brand, brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")

    old = brand.name
    brand.name = data.name
    _log(db, admin_id, "brand.rename", "brand", brand_id, detail=f"name: {old} -> {data.name}")
    out = catalog_schemas.BrandResponse(id=brand.id, name=data.name, slug=brand.slug)
    await db.commit()
    return out


# ---------- audit log ----------

async def list_actions(
    db: AsyncSession, target_type: str | None, target_id: uuid.UUID | None,
    admin_id: uuid.UUID | None, limit: int, offset: int,
) -> schemas.Page[schemas.AdminActionOut]:
    conds = []
    if target_type:
        conds.append(AdminAction.target_type == target_type)
    if target_id:
        conds.append(AdminAction.target_id == target_id)
    if admin_id:
        conds.append(AdminAction.admin_id == admin_id)

    total = (await db.execute(select(func.count(AdminAction.id)).where(*conds))).scalar_one()
    rows = (
        await db.execute(
            select(AdminAction).where(*conds)
            .order_by(AdminAction.created_at.desc(), AdminAction.id).limit(limit).offset(offset)
        )
    ).scalars().all()
    return schemas.Page[schemas.AdminActionOut](
        items=[schemas.AdminActionOut.model_validate(r) for r in rows],
        total=total, limit=limit, offset=offset,
    )
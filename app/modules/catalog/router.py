from typing import Literal  # NEW
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.modules.auth.dependencies import get_current_seller
from app.modules.auth.models import Seller
from app.modules.catalog import schemas, service
from app.modules.catalog.models import Brand
from app.modules.catalog.s3_service import generate_presigned_upload_urls, get_public_url, seller_folder  # CHANGED
from app.modules.catalog import query_service
import uuid
from decimal import Decimal

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.post("/products/presigned-upload", response_model=schemas.PresignedUploadBatchResponse)
async def request_image_upload_urls(
    payload: schemas.PresignedUploadBatchRequest, current_seller: Seller = Depends(get_current_seller)
):
    try:
        # CHANGED: keys are now scoped to the seller: products/<seller_id>/<uuid>.<ext>
        uploads = generate_presigned_upload_urls(payload.content_types, folder=seller_folder(current_seller.id))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"uploads": uploads}


@router.post("/products", response_model=schemas.ProductResponse)
async def create_product(
    payload: schemas.ProductCreateRequest,
    current_seller: Seller = Depends(get_current_seller),
    db: AsyncSession = Depends(get_db),
):
    if not current_seller.is_approved:
        raise HTTPException(403, "Your seller account must be approved before listing products")

    product = await service.create_product(db, current_seller, payload)
    return schemas.ProductResponse(
        id=product.id, name=product.name, slug=product.slug,
        price=product.price, compare_at_price=product.compare_at_price,
        stock_quantity=product.stock_quantity, is_active=product.is_active, is_approved=product.is_approved,
        image_urls=[get_public_url(img.s3_key) for img in product.images],
    )


@router.get("/products", response_model=schemas.ProductListResponse)
async def list_products(
    q: str | None = None,
    category_id: uuid.UUID | None = None,
    brand_id: uuid.UUID | None = None,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
    tag: str | None = None,  # NEW
    sort: Literal["relevance", "newest", "price_asc", "price_desc", "rating", "best_selling"] | None = None,
    page: int = Query(1, ge=1),  # CHANGED: page=0 or negative used to give a negative OFFSET (500)
    page_size: int = Query(20, ge=1, le=100),  # CHANGED: was unbounded
    db: AsyncSession = Depends(get_db),
):
    products, total = await query_service.search_products(
        db, query=q, category_id=category_id, brand_id=brand_id,
        min_price=min_price, max_price=max_price, tag=tag, sort=sort, page=page, page_size=page_size,
    )
    items = [
        schemas.ProductListItem(
            id=p.id, name=p.name, slug=p.slug, price=p.price, compare_at_price=p.compare_at_price,
            thumbnail_url=get_public_url(p.images[0].s3_key) if p.images else None,
            average_rating=p.average_rating, review_count=p.review_count,  # NEW
        )
        for p in products
    ]
    return schemas.ProductListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/products/{product_id}", response_model=schemas.ProductDetailResponse)
async def get_product(product_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    product = await query_service.get_product_detail(db, product_id)
    if not product:
        raise HTTPException(404, "Product not found")

    seller = await db.get(Seller, product.seller_id)
    brand = await db.get(Brand, product.brand_id) if product.brand_id else None
    return schemas.ProductDetailResponse(
        id=product.id, name=product.name, slug=product.slug, description=product.description,
        price=product.price, compare_at_price=product.compare_at_price, stock_quantity=product.stock_quantity,
        category_id=product.category_id, brand_id=product.brand_id,
        image_urls=[get_public_url(img.s3_key) for img in product.images],
        tag_names=[tag.name for tag in product.tags],
        brand_name=brand.name if brand else None,
        store_name=seller.brand_name,
        average_rating=product.average_rating, review_count=product.review_count,  # NEW
    )


@router.get("/categories", response_model=list[schemas.CategoryResponse])
async def get_categories(db: AsyncSession = Depends(get_db)):
    return await query_service.list_categories(db)


@router.get("/brands", response_model=list[schemas.BrandResponse])
async def get_brands(db: AsyncSession = Depends(get_db)):
    return await query_service.list_brands(db)


@router.get("/sellers/me/products", response_model=list[schemas.ProductListItem])
async def get_my_products(current_seller: Seller = Depends(get_current_seller), db: AsyncSession = Depends(get_db)):
    products = await query_service.list_my_products(db, current_seller.id)
    return [
        schemas.ProductListItem(
            id=p.id, name=p.name, slug=p.slug, price=p.price, compare_at_price=p.compare_at_price,
            thumbnail_url=get_public_url(p.images[0].s3_key) if p.images else None,
            average_rating=p.average_rating, review_count=p.review_count,  # NEW
        )
        for p in products
    ]


@router.get("/seller/products", response_model=list[schemas.ProductResponse])
async def list_my_products(
    is_active: bool | None = None,
    is_approved: bool | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_seller: Seller = Depends(get_current_seller),
):
    """The seller's own products, including unapproved and inactive ones."""
    return await service.list_own_products(db, current_seller, is_active, is_approved, limit, offset)


@router.patch("/products/{product_id}", response_model=schemas.ProductResponse)
async def update_product(
    product_id: uuid.UUID,
    payload: schemas.ProductUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_seller: Seller = Depends(get_current_seller),
):
    return await service.update_product(db, current_seller, product_id, payload)


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_seller: Seller = Depends(get_current_seller),
):
    await service.deactivate_product(db, current_seller, product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
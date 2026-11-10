import uuid
from slugify import slugify
from sqlalchemy import select,delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.modules.catalog.models import Product, ProductImage, Tag, Category, Brand
from app.modules.catalog.schemas import ProductCreateRequest, ProductUpdateRequest
from app.modules.auth.models import Seller
from fastapi import HTTPException
from app.modules.catalog.s3_service import delete_objects_async, is_valid_product_key

from sqlalchemy.orm import selectinload


async def create_product(db: AsyncSession, seller: Seller, data: ProductCreateRequest) -> Product:
    # 1. Validate references up front so bad IDs give a clean 404 instead of a 500.
    if not await db.get(Category, data.category_id):
        raise HTTPException(404, "Category not found")
    if data.brand_id and not await db.get(Brand, data.brand_id):
        raise HTTPException(404, "Brand not found")
    
    if data.compare_at_price is not None and data.compare_at_price <= data.price:
        raise HTTPException(422, "compare_at_price must be higher than price")

    _validate_image_keys(data.image_keys, seller.id)
 
    # 2. Resolve tags BEFORE the product exists. Normalize (trim + lowercase) and
    #    de-duplicate, so "Sale", "sale " and "SALE" become one tag.
    names = list(dict.fromkeys(n.strip().lower() for n in data.tag_names if n.strip()))
    tags: list[Tag] = []
    for name in names:
        tag = (await db.execute(select(Tag).where(Tag.name == name))).scalars().first()
        if not tag:
            tag = Tag(name=name)
            db.add(tag)
            await db.flush()
        tags.append(tag)
 
    # 3. Build the product with its tags in the constructor. A brand-new object has
    #    nothing to lazy-load, so no MissingGreenlet.
    slug = f"{slugify(data.name)}-{uuid.uuid4().hex[:6]}"
    product = Product(
        seller_id=seller.id, category_id=data.category_id, brand_id=data.brand_id,
        name=data.name, slug=slug, description=data.description,
        price=data.price, compare_at_price=data.compare_at_price,
        stock_quantity=data.stock_quantity,
        tags=tags,
    )
    db.add(product)
    await db.flush()
 
    for i, key in enumerate(data.image_keys):
        db.add(ProductImage(product_id=product.id, s3_key=key, display_order=i))
 
    await db.commit()
 
    # 4. Re-fetch with EVERYTHING the response schema reads eager-loaded.
    #    (The old version loaded images only, so a response that includes tags
    #    would have hit the same MissingGreenlet error here.)
    result = await db.execute(
        select(Product)
        .options(selectinload(Product.images), selectinload(Product.tags))
        .where(Product.id == product.id)
        .execution_options(populate_existing=True)
    )
    return result.scalars().one()

REVIEW_FIELDS = ("name", "description", "category_id", "brand_id")
 
# Columns that are NOT NULL: an explicit null from the client is a 422, not a 500.
NOT_NULLABLE = {"name", "category_id", "price", "stock_quantity", "is_active"}

def _validate_image_keys(
    keys: list[str], seller_id: uuid.UUID, already_on_product: set[str] | None = None
) -> None:
    if len(set(keys)) != len(keys):
        raise HTTPException(422, "Duplicate image keys")
    allowed = already_on_product or set()
    for key in keys:
        if key not in allowed and not is_valid_product_key(key, seller_id):
            raise HTTPException(422, "Invalid image key")
 
 
async def _resolve_tags(db: AsyncSession, tag_names: list[str]) -> list[Tag]:
    """Same normalization as create_product: trim, lowercase, de-duplicate.
    (create_product can call this too instead of its inline loop.)"""
    names = list(dict.fromkeys(n.strip().lower() for n in tag_names if n.strip()))
    tags: list[Tag] = []
    for name in names:
        tag = (await db.execute(select(Tag).where(Tag.name == name))).scalars().first()
        if not tag:
            tag = Tag(name=name)
            db.add(tag)
            await db.flush()
        tags.append(tag)
    return tags
 
 
def _product_query(product_id: uuid.UUID, seller_id: uuid.UUID):
    # seller_id in the WHERE clause = ownership check; someone else's id gives 404.
    return (
        select(Product)
        .where(Product.id == product_id, Product.seller_id == seller_id)
        .options(selectinload(Product.images), selectinload(Product.tags))
        .execution_options(populate_existing=True)
    )
 
 
async def list_own_products(
    db: AsyncSession,
    seller: Seller,
    is_active: bool | None,
    is_approved: bool | None,
    limit: int,
    offset: int,
) -> list[Product]:
    stmt = select(Product).where(Product.seller_id == seller.id)
    if is_active is not None:
        stmt = stmt.where(Product.is_active.is_(is_active))
    if is_approved is not None:
        stmt = stmt.where(Product.is_approved.is_(is_approved))
    stmt = (
        stmt.options(selectinload(Product.images), selectinload(Product.tags))
        .order_by(Product.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await db.execute(stmt)).scalars().all())
 
 
async def update_product(
    db: AsyncSession, seller: Seller, product_id: uuid.UUID, data:ProductUpdateRequest
) -> Product:
    product = (await db.execute(_product_query(product_id, seller.id))).scalar_one_or_none()
    if not product:
        raise HTTPException(404, "Product not found")
 
    changes = data.model_dump(exclude_unset=True)
    image_keys = changes.pop("image_keys", None)
    tag_names = changes.pop("tag_names", None)
    current_keys = [i.s3_key for i in sorted(product.images, key=lambda i: i.display_order)]
 
    # --- validation ---
    for field in NOT_NULLABLE:
        if field in changes and changes[field] is None:
            raise HTTPException(422, f"{field} cannot be null")
 
    if "category_id" in changes and not await db.get(Category, changes["category_id"]):
        raise HTTPException(404, "Category not found")
    if changes.get("brand_id") and not await db.get(Brand, changes["brand_id"]):
        raise HTTPException(404, "Brand not found")
 
    new_price = changes.get("price", product.price)
    new_compare = changes["compare_at_price"] if "compare_at_price" in changes else product.compare_at_price
    if new_compare is not None and new_compare <= new_price:
        raise HTTPException(422, "compare_at_price must be higher than price")
    
    if image_keys is not None:
        _validate_image_keys(image_keys, seller.id, already_on_product=set(current_keys))
 
    # --- does this edit need re-approval? (only if something really changed) ---
    needs_review = any(
        f in changes and changes[f] != getattr(product, f) for f in REVIEW_FIELDS
    )
    
    images_changed = image_keys is not None and image_keys != current_keys
    needs_review = needs_review or images_changed
 
    # --- apply ---
    for field, value in changes.items():
        setattr(product, field, value)
 
    removed_keys: list[str] = []
    if images_changed:
        removed_keys = [k for k in current_keys if k not in set(image_keys)]
        await db.execute(delete(ProductImage).where(ProductImage.product_id == product.id))
        for i, key in enumerate(image_keys):
            db.add(ProductImage(product_id=product.id, s3_key=key, display_order=i))
 
    if tag_names is not None:
        product.tags = await _resolve_tags(db, tag_names)  # tags already eager-loaded above
 
    if needs_review:
        product.is_approved = False
 
    await db.commit()
    if removed_keys:
        # Never delete an S3 object another product row still points at.
        still_used = set((await db.execute(
            select(ProductImage.s3_key).where(ProductImage.s3_key.in_(removed_keys))
        )).scalars().all())
        removed_keys = [k for k in removed_keys if k not in still_used]
    await delete_objects_async(removed_keys)
    return (await db.execute(_product_query(product_id, seller.id))).scalar_one()
 
 
async def deactivate_product(db: AsyncSession, seller: Seller, product_id: uuid.UUID) -> None:
    """Soft delete: hard-deleting would break order history (OrderItem -> product)."""
    product = (
        await db.execute(
            select(Product).where(Product.id == product_id, Product.seller_id == seller.id)
        )
    ).scalar_one_or_none()
    if not product:
        raise HTTPException(404, "Product not found")
    product.is_active = False  # idempotent: deleting twice is fine
    await db.commit()
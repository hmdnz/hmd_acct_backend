from pydantic import BaseModel, Field, model_validator
import uuid

class AddressCreateRequest(BaseModel):
    country: str = Field(min_length=2, max_length=100)
    state: str = Field(min_length=2, max_length=100)
    lga: str = Field(min_length=2, max_length=100)
    street: str = Field(min_length=2, max_length=255)
    house_number: str = Field(min_length=1, max_length=50)
    landmark: str | None = Field(default=None, max_length=255)
    google_place_id: str | None = Field(default=None, max_length=255)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    is_default: bool = False

class AddressUpdateRequest(BaseModel):
    country: str | None = Field(default=None, min_length=2, max_length=100)
    state: str | None = Field(default=None, min_length=2, max_length=100)
    lga: str | None = Field(default=None, min_length=2, max_length=100)
    street: str | None = Field(default=None, min_length=2, max_length=255)
    house_number: str | None = Field(default=None, min_length=1, max_length=50)
    landmark: str | None = Field(default=None, max_length=255)
    google_place_id: str | None = Field(default=None, max_length=255)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    is_default: bool | None = None

    @model_validator(mode="after")
    def _no_null_required(self):
        # These columns are NOT NULL; an explicit null would 500 at the DB.
        for f in ("country", "state", "lga", "street", "house_number", "is_default"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self

class AddressResponse(BaseModel):
    id: uuid.UUID
    country: str
    state: str
    lga: str
    street: str
    house_number: str
    landmark: str | None
    is_default: bool

    class Config:
        from_attributes = True
"""Pydantic request/response models for the REST API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MatchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="待匹配的外部地址文本")
    save: bool = Field(True, description="是否把匹配结果写入待复核/日志表")


class BatchMatchRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1)
    save: bool = True


class AddressIn(BaseModel):
    address: str | None = None
    full_text: str | None = None
    line1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    postcode: str | None = None
    zip: str | None = None
    country: str | None = None
    external_id: str | None = None
    id: str | None = None

    def lookup_fields(self) -> dict[str, Any]:
        return {
            "line1": self.line1,
            "city": self.city,
            "state": self.state,
            "postal_code": self.postal_code or self.postcode or self.zip,
            "country": self.country,
            "external_id": self.external_id or self.id,
        }


class AddressBatchIn(BaseModel):
    addresses: list[AddressIn]
    skip_duplicates: bool = True


class ReviewAction(BaseModel):
    action: str = Field(..., pattern="^(confirmed|overridden|rejected)$")
    selected_id: int | None = None

"""Customer auth — register, login, me (JWT scope=customer)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.database import get_session
from app.models.customer import Customer
from app.utils.security import (
    create_customer_token,
    hash_password,
    rate_limit_store,
    rate_limit_write,
    verify_customer_auth,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    email: str = Field(pattern=_EMAIL_PATTERN, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    phone: Optional[str] = Field(default=None, max_length=32)


class LoginRequest(BaseModel):
    email: str = Field(pattern=_EMAIL_PATTERN, max_length=255)
    password: str


class CustomerOut(BaseModel):
    id: int
    name: str
    email: str
    phone: Optional[str]

    model_config = {"from_attributes": True}


class AuthResponse(BaseModel):
    token: str
    customer: CustomerOut


def _normalize_email(email: str) -> str:
    return email.strip().lower()


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit_write)],
)
async def register(body: RegisterRequest) -> AuthResponse:
    """Create a customer account and return a JWT (scope=customer, 24h)."""
    email = _normalize_email(body.email)
    async for session in get_session():
        existing = await session.execute(
            select(Customer).where(Customer.email == email)
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email já cadastrado",
            )
        customer = Customer(
            name=body.name.strip(),
            email=email,
            phone=body.phone,
            password_hash=hash_password(body.password),
        )
        session.add(customer)
        await session.commit()
        token = create_customer_token(customer.id)
        return AuthResponse(
            token=token,
            customer=CustomerOut.model_validate(customer),
        )


@router.post(
    "/login",
    response_model=AuthResponse,
    dependencies=[Depends(rate_limit_write)],
)
async def login(body: LoginRequest) -> AuthResponse:
    """Validate credentials and return a JWT (scope=customer, 24h)."""
    email = _normalize_email(body.email)
    async for session in get_session():
        result = await session.execute(
            select(Customer).where(Customer.email == email)
        )
        customer = result.scalar_one_or_none()
        if customer is None or not verify_password(body.password, customer.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Email ou senha incorretos",
            )
        token = create_customer_token(customer.id)
        return AuthResponse(
            token=token,
            customer=CustomerOut.model_validate(customer),
        )


@router.get(
    "/me",
    response_model=CustomerOut,
    dependencies=[Depends(rate_limit_store)],
)
async def me(customer_id: int = Depends(verify_customer_auth)) -> CustomerOut:
    """Return the authenticated customer's profile."""
    async for session in get_session():
        customer = await session.get(Customer, customer_id)
        if customer is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Cliente não encontrado",
            )
        return CustomerOut.model_validate(customer)
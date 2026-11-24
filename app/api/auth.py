import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db import get_db
from app.models import User
from app.schemas import LoginRequest, TokenResponse, UserOut
from app.services.rate_limit import account_limiter, client_ip, ip_limiter
from app.services.security import create_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["Автентифікація"])
logger = logging.getLogger("cicd.auth")


@router.post("/login", response_model=TokenResponse, summary="Вхід у систему",
             responses={429: {"description": "Забагато невдалих спроб"}})
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    """Вхід за поштою й паролем.

    Кількість невдалих спроб обмежена окремо за адресою джерела й за
    обліковим записом: система доступна з мережі, а пошти користувачів не є
    таємницею, тому без такого обмеження пароль підбирається перебором.
    Перевищення дає відповідь 429 із заголовком `Retry-After`.
    """
    ip = client_ip(request)
    account = body.email.strip().lower()

    wait = max(ip_limiter().retry_after(ip), account_limiter().retry_after(account))
    if wait > 0:
        logger.warning("Вхід заблоковано: адреса %s, запис %s, лишилося %.0f с",
                       ip, account, wait)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed sign-in attempts. Try again later.",
            headers={"Retry-After": str(int(wait) + 1)},
        )

    user = db.scalars(select(User).where(User.email == body.email)).first()
    if user is None or not verify_password(body.password, user.password_hash):
        # Невдачу записуємо лише для спроби, яка була дозволена: інакше
        # сторонній тримав би обліковий запис зачиненим як завгодно довго.
        ip_limiter().record_failure(ip)
        account_limiter().record_failure(account)
        logger.info("Невдалий вхід: адреса %s, запис %s", ip, account)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is disabled")

    # Успішний вхід знімає підозру: інакше кілька описок законного
    # користувача залишали б його замкненим до кінця вікна.
    ip_limiter().reset(ip)
    account_limiter().reset(account)
    return TokenResponse(
        access_token=create_access_token(user.email, user.role.value),
        role=user.role,
        email=user.email,
    )


@router.get("/me", response_model=UserOut, summary="Поточний користувач")
def me(user: User = Depends(current_user)) -> User:
    return user

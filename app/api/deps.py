"""Спільні залежності API: поточний користувач і перевірка ролі."""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Project, Role, User
from app.services.security import decode_token

bearer = HTTPBearer(auto_error=False)


def current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    payload = decode_token(cred.credentials)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = db.scalars(select(User).where(User.email == payload.get("sub"))).first()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user


def require_devops(user: User = Depends(current_user)) -> User:
    """Зміна політик і застосування рекомендацій доступні лише DevOps-інженеру."""
    if user.role != Role.DEVOPS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "DevOps engineer role required")
    return user


def require_lead_or_devops(user: User = Depends(current_user)) -> User:
    """Розсилання зведення назовні доступне керівникові та DevOps-інженеру."""
    if user.role not in (Role.LEAD, Role.DEVOPS):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Engineering lead or DevOps engineer role required")
    return user


def get_project(project_id: int, db: Session = Depends(get_db)) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project {project_id} not found")
    return project

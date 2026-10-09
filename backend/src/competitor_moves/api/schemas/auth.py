from pydantic import BaseModel, Field, field_validator

EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def no_control_chars(v: str | None) -> str | None:
    """NUL and other control characters can't be stored in Postgres text and never belong in credentials."""
    if v is not None and any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise ValueError("must not contain control characters")
    return v


class Credentials(BaseModel):
    email: str = Field(pattern=EMAIL, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    name: str | None = Field(None, max_length=120)
    plan: str | None = None

    _clean = field_validator("email", "password", "name")(no_control_chars)


class Login(BaseModel):  # no length rules, so a bad password is a 401 and not a 422
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)

    _clean = field_validator("email", "password")(no_control_chars)


class UserPatch(BaseModel):
    is_active: bool

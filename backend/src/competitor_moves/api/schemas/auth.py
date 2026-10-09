from pydantic import BaseModel, Field

EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class Credentials(BaseModel):
    email: str = Field(pattern=EMAIL, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class Login(BaseModel):  # no length rules, so a bad password is a 401 and not a 422
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class UserPatch(BaseModel):
    is_active: bool

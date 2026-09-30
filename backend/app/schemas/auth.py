from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserInfo(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    category_ids: list[int] = []
    feishu_open_id: str | None = None

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    username: str
    password: str
    display_name: str
    role: str = "viewer"
    category_ids: list[int] = []
    feishu_open_id: str | None = None


class UserUpdate(BaseModel):
    display_name: str | None = None
    password: str | None = None
    role: str | None = None
    category_ids: list[int] | None = None
    feishu_open_id: str | None = None

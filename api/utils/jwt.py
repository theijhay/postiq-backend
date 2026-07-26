from datetime import datetime, timedelta
from jose import jwt, JWTError
from api.utils.settings import settings
from api.v1.schemas.user import TokenData

def create_access_token(user_id: str, expires_delta: timedelta):
    data = {"sub": str(user_id)}
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(user_id: str, expires_delta: timedelta):
    data = {"sub": str(user_id)}
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def verify_access_token(token: str, credentials_exception):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
        return TokenData(id=user_id)
    except JWTError:
        raise credentials_exception
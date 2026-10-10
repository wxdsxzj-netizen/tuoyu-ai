"""字段级加密（M14-07）：Fernet（TL_CREDENTIALS_KEY），读取兼容历史明文。"""
import json

from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

PREFIX = "enc:"


class EncryptedString(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or str(value).startswith(PREFIX):
            return value
        from app.outreach.crypto import encrypt
        return PREFIX + encrypt(str(value))

    def process_result_value(self, value, dialect):
        if value and value.startswith(PREFIX):
            from app.outreach.crypto import decrypt
            return decrypt(value[len(PREFIX):])
        return value


class EncryptedJSON(TypeDecorator):
    impl = JSONB
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or (isinstance(value, dict) and "_enc" in value):
            return value
        from app.outreach.crypto import encrypt
        return {"_enc": encrypt(json.dumps(value, ensure_ascii=False))}

    def process_result_value(self, value, dialect):
        if isinstance(value, dict) and "_enc" in value:
            from app.outreach.crypto import decrypt
            return json.loads(decrypt(value["_enc"]))
        return value

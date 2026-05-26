"""飞书事件加解密工具"""
import base64
import hashlib
import json
import os

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad


def _sha256(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


def decrypt(encrypt_key: str, encrypted_text: str) -> dict:
    """解密飞书加密的事件数据，返回 JSON dict"""
    key = _sha256(encrypt_key)
    raw = base64.b64decode(encrypted_text)
    iv = raw[:16]
    ciphertext = raw[16:]
    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    plaintext = unpad(cipher.decrypt(ciphertext), AES.block_size)
    return json.loads(plaintext)


def encrypt(encrypt_key: str, data: dict) -> str:
    """加密响应数据，返回 base64 字符串"""
    key = _sha256(encrypt_key)
    iv = os.urandom(16)
    plaintext = json.dumps(data, separators=(",", ":")).encode()
    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    ciphertext = cipher.encrypt(pad(plaintext, AES.block_size))
    return base64.b64encode(iv + ciphertext).decode()

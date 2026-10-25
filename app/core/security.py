import hashlib
import hmac
import secrets


def hash_password(password: str):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310_000).hex()
    return f'pbkdf2_sha256${salt}${digest}'


def verify_password(password: str, encoded: str):
    try:
        _, salt, digest = encoded.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310_000).hex()
        return hmac.compare_digest(actual, digest)
    except ValueError:
        return False


def token_hash(token: str):
    return hashlib.sha256(token.encode()).hexdigest()


async def hash_password_async(password: str):
    from anyio.to_thread import run_sync
    return await run_sync(hash_password, password)


async def verify_password_async(password: str, encoded: str):
    from anyio.to_thread import run_sync
    return await run_sync(verify_password, password, encoded)


def __getattr__(name):
    # Compatibility for callers predating the auth module ownership boundary.
    if name in {'current_user', 'admin_user', 'session_cookie'}:
        from app.modules.auth import dependencies
        return getattr(dependencies, name)
    raise AttributeError(name)

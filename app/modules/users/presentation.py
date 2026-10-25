def user_out(user) -> dict:
    from app.shared.formatters import public
    return public(user, ('password_hash', 'admin_config_password_hash'))

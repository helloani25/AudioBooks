import os
from datetime import timedelta
from pathlib import Path

from cachelib.file import FileSystemCache

try:
    import redis
except ImportError:  # pragma: no cover - optional dependency
    redis = None


def configure_app(app) -> None:
    app.config['SECRET_KEY'] = os.environ['SECRET_KEY']

    # Session
    app.config['SESSION_TYPE'] = 'redis'
    app.config['SESSION_PERMANENT'] = True
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=15)

    def use_filesystem_sessions() -> None:
        session_dir = Path(__file__).resolve().parent / 'flask_session'
        app.config['SESSION_TYPE'] = 'cachelib'
        app.config['SESSION_CACHELIB'] = FileSystemCache(
            cache_dir=str(session_dir),
            threshold=500,
        )
        app.config.pop('SESSION_REDIS', None)

    redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
    if redis is not None:
        try:
            app.config['SESSION_REDIS'] = redis.from_url(redis_url)
            app.config['SESSION_REDIS'].ping()
            print(f"Connected to Redis at {redis_url} for sessions.")
        except Exception as e:
            print(f"WARNING: Redis not available at {redis_url}. Error: {e}")
            print("Falling back to filesystem for sessions. For production, please ensure a Redis server is running.")
            use_filesystem_sessions()
    else:
        print("WARNING: Redis package not installed. Falling back to filesystem sessions.")
        use_filesystem_sessions()

    # Cookies
    app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() == 'true'
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

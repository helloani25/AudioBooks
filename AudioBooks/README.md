# AudioBooks System Design
A modular system for browsing and listening to Gutenberg project books.

## Modules
- **[Authentication](./Authentication)**: User signup, login, and session management.
- **[Catalog](./Catalog)**: Gutenberg metadata ingestion and API for book search/filtering.
- **[Presentation](./Presentation)**: React/Vite frontend for the user interface.

## Getting Started

1. **Install Dependencies**:
   ```bash
   pip install -r ../requirements.txt
   cd Presentation/library && npm install
   sudo port install redis
   cat /opt/local/etc/redis.conf
   redis-server
   ```

2. **Start the Backend**:
   Run from the project root:
   ```bash
   export PYTHONPATH=$PYTHONPATH:.
   python3 AudioBooks/app.py
   ```
   *Note: Using `PYTHONPATH` ensures that absolute imports like `from AudioBooks...` are resolved correctly.*


3. **Install npm**:
```bash
npm install
```
4. **Start the Frontend**:
   ```bash
   cd AudioBooks/Presentation/library
   npm run dev
   ```

## Redis Caching and Sessions
The system uses Redis for both sessions and catalog metadata caching if available. 
- **Sessions**: If Redis is unavailable, it falls back to the local filesystem (`flask_session` folder).
- **Catalog Caching**: If Redis is unavailable, it falls back to in-memory caching for the current process.

To use Redis, ensure a Redis server is running on `localhost:6379` or set the `REDIS_URL` environment variable:
```bash
export REDIS_URL="redis://your-redis-host:6379"
python3 AudioBooks/app.py
```

## API Endpoints
- `GET /api/csrf-token`: Returns a CSRF token for subsequent POST requests.
- `POST /api/signup`: Creates a new user account.
- `POST /api/login`: Authenticates a user and starts a session (supports Basic Auth).
- `POST /api/logout`: Ends the current session.
- `GET /api/me`: Returns the current user's profile information.
- `GET /api/books`: Returns a paginated list of books (supports `subject`, `search`, `limit`, `offset`).
- `GET /api/books/count`: Returns the total number of books matching filters.
- `GET /api/subjects`: Returns a list of available subjects and their book counts.

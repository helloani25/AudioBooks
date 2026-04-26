import os
import sys
from datetime import timedelta

try:
    import redis
except ImportError:  # pragma: no cover - optional dependency
    redis = None

# Ensure the project root is in sys.path so that absolute imports like 'from AudioBooks...' work
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from flask import Flask, request, jsonify, session, make_response
from flask_session import Session
from flask_wtf.csrf import CSRFProtect, generate_csrf
from flask_cors import CORS
from werkzeug.security import check_password_hash
from AudioBooks.Authentication.Repository.UserRepository import UserRepository
from AudioBooks.Catalog.Service.CatalogService import catalog_bp
from AudioBooks.MediaPlayer.Service.MediaHistoryService import media_history_bp

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_key_only_for_internal_use')

# Redis Session Configuration
app.config['SESSION_TYPE'] = 'redis'
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=15)
app.config['SESSION_USE_SIGNER'] = True
redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
if redis is not None:
    try:
        app.config['SESSION_REDIS'] = redis.from_url(redis_url)
        app.config['SESSION_REDIS'].ping()
        print(f"Connected to Redis at {redis_url} for sessions.")
    except Exception as e:
        print(f"WARNING: Redis not available at {redis_url}. Error: {e}")
        print("Falling back to filesystem for sessions. For production, please ensure a Redis server is running.")
        app.config['SESSION_TYPE'] = 'filesystem'
        app.config.pop('SESSION_REDIS', None)
else:
    print("WARNING: Redis package not installed. Falling back to filesystem sessions.")
    app.config['SESSION_TYPE'] = 'filesystem'

# Secure Cookie Configuration
app.config['SESSION_COOKIE_SECURE'] = False # Set to True for HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Initialize Extensions
Session(app)
CSRFProtect(app)
CORS(app, supports_credentials=True)

# Register Blueprints
app.register_blueprint(catalog_bp)
app.register_blueprint(media_history_bp)

user_repo = UserRepository()

@app.route('/api/csrf-token', methods=['GET'])
def get_csrf_token():
    token = generate_csrf()
    response = make_response(jsonify({'csrf_token': token}))
    # Note: Flask-WTF usually sets the CSRF cookie automatically
    return response

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    confirm_password = data.get('confirm_password')
    full_name = data.get('full_name')

    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400
    if password != confirm_password:
        return jsonify({'error': 'Passwords do not match'}), 400

    if user_repo.create_user(email, password, full_name):
        return jsonify({'message': 'User created successfully'}), 201
    else:
        return jsonify({'error': 'User already exists'}), 409

@app.route('/api/login', methods=['POST'])
def login():
    # Supports both Basic Auth and JSON form data for login
    email = None
    password = None

    auth = request.authorization
    if auth:
        email = auth.username
        password = auth.password
    else:
        data = request.get_json()
        if data:
            email = data.get('email')
            password = data.get('password')

    if not email or not password:
        return jsonify({'error': 'Credentials required'}), 401

    user = user_repo.verify_password(email, password)
    if user:
        # Session Rotation: clear old session and create new one
        session.clear()
        session.permanent = True
        session['user_id'] = user['id']
        session['email'] = user['email']
        return jsonify({'message': 'Login successful', 'redirect': '/home.html'}), 200
    else:
        return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'message': 'Logged out'}), 200

@app.route('/api/me', methods=['GET'])
def me():
    if 'user_id' in session:
        return jsonify({'user_id': session['user_id'], 'email': session['email']}), 200
    return jsonify({'error': 'Not authenticated'}), 401

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)

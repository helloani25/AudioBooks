import sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash

class UserRepository:
    def __init__(self, db_path=None):
        if db_path is None:
            # Use absolute path relative to this file
            current_dir = os.path.dirname(os.path.abspath(__file__))
            self.db_path = os.path.join(current_dir, 'users.db')
        else:
            self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                full_name TEXT,
                password_hash TEXT NOT NULL
            )
        ''')
        conn.commit()
        conn.close()

    def create_user(self, email, password, full_name=None):
        password_hash = generate_password_hash(password)
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO users (email, full_name, password_hash) VALUES (?, ?, ?)', (email, full_name, password_hash))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

    def get_user(self, email):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT id, email, password_hash FROM users WHERE email = ?', (email,))
        user = cursor.fetchone()
        conn.close()
        if user:
            return {'id': user[0], 'email': user[1], 'password_hash': user[2]}
        return None

    def verify_password(self, email, password):
        user = self.get_user(email)
        if user and check_password_hash(user['password_hash'], password):
            return user
        return None

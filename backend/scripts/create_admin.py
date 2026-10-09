"""Print the SQL to create an admin. Admins are never created through the API.

    python scripts/create_admin.py admin@example.com 'a-long-password' | psql "$DATABASE_URL"
"""
import sys

from competitor_moves.core.security import hash_password

if len(sys.argv) != 3:
    sys.exit("usage: python scripts/create_admin.py EMAIL PASSWORD")
email = sys.argv[1].replace("'", "''")
print(f"insert into users(email,password_hash,role) values ('{email}','{hash_password(sys.argv[2])}','admin');")

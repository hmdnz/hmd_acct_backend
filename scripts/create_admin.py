"""Create an admin account (there is no public registration).

Run from the project root:   python -m scripts.create_admin
"""
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import get_db
from app.modules.auth.models import Admin

MIN_PASSWORD_LENGTH = 12


async def main() -> None:
    first_name = input("First name: ").strip()
    last_name = input("Last name: ").strip()
    email = input("Email: ").strip().lower()
    if not (first_name and last_name and "@" in email):
        sys.exit("First name, last name and a valid email are required.")
    password = getpass.getpass(f"Password (min {MIN_PASSWORD_LENGTH} characters): ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if getpass.getpass("Confirm password: ") != password:
        sys.exit("Passwords do not match.")

    async for db in get_db():
        if (await db.execute(select(Admin.id).where(Admin.email == email))).first():
            sys.exit("An admin with that email already exists.")
        db.add(Admin(first_name=first_name, last_name=last_name, email=email, hashed_password=hash_password(password)))
        await db.commit()
        print(f"Admin created: {email}")


if __name__ == "__main__":
    asyncio.run(main())

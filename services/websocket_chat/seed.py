"""Seed data for the WebSocket chat example."""

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import ensure_rbac_tables
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from services.websocket_chat.app import ChatMessage, ChatRoom, User


async def run(db=None):
    """Seed the chat database with demo data."""
    if db is None:
        db = get_db()

    existing = await User.objects.filter(username="admin").first()
    if existing:
        logger.info("Chat already seeded, skipping")
        return

    # Create demo user
    admin = User(
        username="admin",
        password_hash=hash_password(seed_password("admin")),
    )
    await admin.save()
    logger.info("Created demo user 'admin' — see seed_password log for actual value")

    # Create starter rooms
    rooms_spec = [
        ("general", "General discussion — say hello!"),
        ("random", "Off-topic chat, memes, and fun"),
        ("tech", "Programming, tools, and technology"),
    ]
    for name, description in rooms_spec:
        room = ChatRoom(
            name=name,
            description=description,
            created_by=admin.id,
        )
        await room.save()
    logger.info("Created {count} starter rooms", count=len(rooms_spec))

    # Welcome message in general
    general = await ChatRoom.objects.filter(name="general").first()
    msg = ChatMessage(
        room_id=general.id,
        user_id=admin.id,
        username="admin",
        content="Welcome to HyperChat! This is a demo of HyperDjango WebSocket + Channels.",
    )
    await msg.save()
    # HyperAdmin panel user (hyper_users table, separate from app users)
    await ensure_rbac_tables(db=db)
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("    hyper_users: admin ensured for HyperAdmin panel")

    logger.info("Seeded welcome message in #general")

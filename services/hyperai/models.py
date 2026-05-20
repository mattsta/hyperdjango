"""
HyperAI — Models for multi-conversation AI chat service.

5 models: User, Conversation, Message, APIKey, UsageLog.
IDMixin on Conversation for opaque external IDs (enumeration-resistant; still authorize every access — not IDOR protection on their own).
SignedAPIKeyMixin on APIKey for signed key generation, HMAC verification, and DB-backed storage.
"""

from enum import Enum

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.public_id import IDMixin, IDMode, KeySlot
from hyperdjango.signing import SignedAPIKeyMixin, SigningKey


class Tier(Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class MessageRole(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class User(TimestampMixin, Model):
    class Meta:
        table = "ai_users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    email: str = Field(unique=True)
    password_hash: str = Field(exclude=True)
    tier: Tier = Field(default=Tier.FREE)
    usage_count: int = Field(default=0)


class Conversation(IDMixin, TimestampMixin, Model):
    class Meta:
        table = "ai_conversations"

    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "gf783jV2vxJW6ChM95wcPqmrRXQpHFG4"
        hmac_keys = [KeySlot(key="ai-conv-key-2026-q1", offset=5000)]

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User, index=True)
    title: str = Field(default="New Chat")
    model_name: str = Field(default="hyper-4")
    system_prompt: str = Field(default="You are a helpful AI assistant.")


class Message(TimestampMixin, Model):
    class Meta:
        table = "ai_messages"

    id: int = Field(primary_key=True, auto=True)
    conversation_id: int = Field(foreign_key=Conversation, index=True)
    role: MessageRole = Field()
    content: str = Field()
    token_count: int = Field(default=0)


class APIKey(SignedAPIKeyMixin, IDMixin, TimestampMixin):
    """API keys with signed token generation and HMAC-first verification.

    SignedAPIKeyMixin provides: key_hash, key_prefix, is_active, expires_at, scopes,
    generate() classmethod, verify() classmethod, verify_signature_only().
    IDMixin provides: get_external_id() for revoke/management routes.
    """

    class Meta:
        table = "ai_api_keys"

    class TokenConfig:
        keys = [SigningKey(secret="ai-apikey-signing-2026-q2", version=1)]
        key_display_prefix = "sk_hyper_"

    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "pgC7QHFR53q2Jh96cM8rGXPvxmjwW4fV"
        hmac_keys = [KeySlot(key="ai-keys-key-2026-q1", offset=8000)]

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User, index=True)
    name: str = Field(default="")
    last_used: str = Field(default="")  # Unix timestamp string or empty


class UsageLog(TimestampMixin, Model):
    class Meta:
        table = "ai_usage_logs"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User, index=True)
    conversation_id: int = Field(foreign_key=Conversation, index=True)
    model_name: str = Field()
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    cost_cents: int = Field(default=0)  # Cost in hundredths of a cent

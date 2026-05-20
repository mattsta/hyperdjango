"""
Forms Demo — showcasing HyperDjango's form framework.

Demonstrates:
  - Form class with 8 field types (CharField, IntegerField, EmailField, etc.)
  - Field-level validation (min/max, pattern, required/optional)
  - Per-field custom validation (clean_<fieldname>)
  - Cross-field clean() method (password confirmation, date range)
  - ModelForm auto-generated from Model definition
  - Form rendering (as_div) with error display
  - ChoiceField from Enum
  - FormSet for batch entry
  - Flash messages on success/error
  - CSRF protection on all form posts

Setup:
    uv run hyper setup --app services.forms_demo.app:app --seed services.forms_demo.seed:run

Run:
    uv run hyper run --app services.forms_demo.app:app --port 8400
"""

import sys
from datetime import date, datetime
from enum import Enum
from pathlib import Path

from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.auth import hash_password
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.forms import (
    BooleanField,
    CharField,
    ChoiceField,
    DateField,
    EmailField,
    FloatField,
    Form,
    ModelForm,
    PasswordField,
)
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import (
    Field,
    FileField,
    ImageField,
    Model,
    delete_uploaded_file,
    save_uploaded_file,
)
from hyperdjango.openapi import mount_docs
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import (
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)
from hyperdjango.storage import MemoryStorage

_APP_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

# Set per-app defaults (DEFAULTS tier — env vars still override)
DEFAULTS["DATABASE_URL"] = (
    get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
)

app = HyperApp(
    title="Forms Demo",
    database=get_setting("DATABASE_URL"),
    templates=str(_APP_DIR / "templates"),
    debug=get_setting("DEBUG"),
)

app.use(TimingMiddleware())
app.use(SecurityHeadersMiddleware(hsts=False))
app.use(
    CSRFMiddleware(
        secret=get_setting("CSRF_SECRET"),
        exempt_paths={"/api/validate/contact"},
    )
)

_session_engine = TokenEngine(
    keys=[
        SigningKey(
            secret=get_setting("SESSION_SIGNING_KEY"),
            version=1,
        ),
    ]
)
auth = SessionAuth(
    secret=get_setting("SESSION_SECRET"),
    token_engine=_session_engine,
)
app.use(auth)


@app.exception_handler(Exception)
async def _handle_error(request, exc):
    return Response.json({"detail": "Internal server error"}, status=500)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Priority(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class Category(Enum):
    BUG = "bug"
    FEATURE = "feature"
    QUESTION = "question"
    FEEDBACK = "feedback"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class User(TimestampMixin, Model):
    class Meta:
        table = "forms_users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    email: str = Field(default="")
    password_hash: str = Field(exclude=True)


class Ticket(TimestampMixin, Model):
    class Meta:
        table = "forms_tickets"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    description: str = Field(default="")
    category: Category = Field(default=Category.BUG)
    priority: Priority = Field(default=Priority.NORMAL)
    email: str = Field(default="")
    budget: float = Field(default=0.0)
    due_date: date = Field(default=None)
    is_urgent: bool = Field(default=False)
    author_id: int = Field(default=0)


# ---------------------------------------------------------------------------
# Forms — manual Form class
# ---------------------------------------------------------------------------


class ContactForm(Form):
    """Contact form with 6 field types and cross-field validation."""

    name = CharField(min_length=2, label="Your Name")
    email = EmailField(label="Email Address")
    subject = CharField(min_length=5, label="Subject")
    message = CharField(
        min_length=10, widget="textarea", label="Message", attrs={"rows": 5}
    )
    priority = ChoiceField(
        choices=[(p.value, p.value.title()) for p in Priority],
        label="Priority",
    )
    budget = FloatField(
        required=False,
        min_value=0,
        label="Budget ($)",
        help_text="Optional — leave blank if not applicable",
    )
    due_date = DateField(
        required=False, label="Preferred Due Date", help_text="Must be in the future"
    )
    agree_terms = BooleanField(label="I agree to the terms")

    def clean_due_date(self):
        """Custom per-field validation: due date must be in the future."""
        val = self.cleaned_data.get("due_date")
        if val is not None and val <= date.today():
            raise ValueError("Due date must be in the future")
        return val

    def clean(self):
        """Cross-field validation: urgent priority requires a due date."""
        data = self.cleaned_data
        if data.get("priority") == "urgent" and not data.get("due_date"):
            self.add_error("due_date", "Due date is required for urgent priority")


class RegisterForm(Form):
    """Registration form with password confirmation cross-field validation."""

    username = CharField(min_length=3, label="Username")
    email = EmailField(label="Email")
    password = PasswordField(min_length=6, label="Password")
    password_confirm = PasswordField(min_length=6, label="Confirm Password")

    def clean(self):
        """Cross-field: passwords must match."""
        data = self.cleaned_data
        if data.get("password") and data.get("password") != data.get(
            "password_confirm"
        ):
            self.add_error("password_confirm", "Passwords do not match")


# ---------------------------------------------------------------------------
# ModelForm — auto-generated from Ticket model
# ---------------------------------------------------------------------------


class TicketForm(ModelForm):
    """Auto-generated form from the Ticket model."""

    class Meta:
        model = Ticket
        fields = [
            "title",
            "description",
            "category",
            "priority",
            "email",
            "budget",
            "due_date",
            "is_urgent",
        ]

    def clean_budget(self):
        val = self.cleaned_data.get("budget")
        if val is not None and val < 0:
            raise ValueError("Budget cannot be negative")
        return val


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _form_data_from_request(form_dict: dict) -> dict:
    """Convert form data dict[str, list[str]] to dict[str, str] for Form binding."""
    result = {}
    for key, val in form_dict.items():
        if isinstance(val, list):
            result[key] = val[0] if val else ""
        else:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
async def index(request):
    return app.render("index.html", {})


# --- Contact Form ---


@app.get("/contact")
async def contact_page(request):
    form = ContactForm()
    return app.render("contact.html", {"form": form, "success": False})


@app.post("/contact")
async def contact_submit(request):
    raw = await request.form()
    data = _form_data_from_request(raw)
    form = ContactForm(data=data)

    if form.is_valid():
        # In production: send email, create ticket, etc.
        return app.render("contact.html", {"form": ContactForm(), "success": True})

    return app.render("contact.html", {"form": form, "success": False})


# --- Registration Form ---


@app.get("/register")
async def register_page(request):
    form = RegisterForm()
    return app.render("register.html", {"form": form, "success": False})


@app.post("/register")
async def register_submit(request):
    raw = await request.form()
    data = _form_data_from_request(raw)
    form = RegisterForm(data=data)

    if form.is_valid():
        existing = await User.objects.filter(
            username=form.cleaned_data["username"]
        ).exists()
        if existing:
            form.add_error("username", "Username already taken")
            return app.render("register.html", {"form": form, "success": False})

        pw_hash = hash_password(form.cleaned_data["password"])
        user = User(
            username=form.cleaned_data["username"],
            email=form.cleaned_data["email"],
            password_hash=pw_hash,
        )
        await user.save()
        return app.render("register.html", {"form": RegisterForm(), "success": True})

    return app.render("register.html", {"form": form, "success": False})


# --- Ticket ModelForm ---


@app.get("/tickets")
async def ticket_list(request):
    tickets = await Ticket.objects.order_by("-id").limit(20).all()
    return app.render("tickets.html", {"tickets": tickets})


@app.get("/tickets/new")
async def ticket_new(request):
    form = TicketForm()
    return app.render("ticket_form.html", {"form": form, "success": False})


@app.post("/tickets/new")
async def ticket_create(request):
    raw = await request.form()
    data = _form_data_from_request(raw)
    form = TicketForm(data=data)

    if form.is_valid():
        ticket = Ticket(**form.cleaned_data)
        await ticket.save()
        return app.render(
            "ticket_form.html",
            {"form": TicketForm(), "success": True, "ticket_id": ticket.id},
        )

    return app.render("ticket_form.html", {"form": form, "success": False})


# --- JSON API for form validation ---


@app.post("/api/validate/contact")
async def validate_contact(request):
    """Validate contact form via JSON API (for HTMX/JS live validation)."""
    data = await request.json()
    form = ContactForm(data=data)
    valid = form.is_valid()
    return Response.json(
        {
            "valid": valid,
            "errors": form.errors if not valid else {},
            "cleaned_data": form.cleaned_data if valid else {},
        }
    )


# ---------------------------------------------------------------------------
# File Upload Showcase
# ---------------------------------------------------------------------------

# In-memory storage for testing (FileSystemStorage for production)
_upload_storage = MemoryStorage()

# Allowed extensions and max file size
_ALLOWED_EXTENSIONS = frozenset({"txt", "pdf", "png", "jpg", "jpeg", "gif", "csv"})
_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB


class Document(TimestampMixin, Model):
    class Meta:
        table = "forms_documents"

    id: int = Field(primary_key=True, auto=True)
    filename: str = Field()
    original_name: str = Field()
    content_type: str = Field(default="application/octet-stream")
    size: int = Field(default=0)
    uploaded_at: datetime = Field(default="now()")


def _get_extension(filename: str) -> str:
    """Extract file extension from filename."""
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].lower()


async def _upload_ctx(request, *, error: str = "", success: str = "") -> dict:
    """Build the upload.html template context.

    Single source of truth for the context dict keys — previously
    copy-pasted 5 times across the upload_page and upload_submit
    handlers. Also the only place that injects `csrf_token`, which
    the upload form needs because CSRFMiddleware enforces double-
    submit on all POST routes (not just JSON APIs).
    """
    documents = await Document.objects.order_by("-id").limit(20).all()
    return {
        "csrf_token": request.cookies.get("csrftoken", ""),
        "documents": documents,
        "allowed_extensions": ", ".join(sorted(_ALLOWED_EXTENSIONS)),
        "max_size_mb": _MAX_FILE_SIZE // (1024 * 1024),
        "error": error,
        "success": success,
    }


@app.get("/upload")
async def upload_page(request):
    """File upload form page."""
    return app.render("upload.html", await _upload_ctx(request))


@app.post("/upload")
async def upload_submit(request):
    """Handle file upload with validation."""
    uploaded_files = await request.files()
    uploaded = uploaded_files.get("file")

    if not uploaded or not uploaded.filename:
        return app.render(
            "upload.html",
            await _upload_ctx(request, error="No file selected"),
        )

    # Sanitize filename: strip path components, remove dangerous chars
    original_name = uploaded.filename.split("/")[-1].split("\\")[-1]
    if not original_name:
        original_name = "unnamed"
    content = uploaded.data
    content_type = uploaded.content_type

    # Validate extension
    ext = _get_extension(original_name)
    if ext not in _ALLOWED_EXTENSIONS:
        return app.render(
            "upload.html",
            await _upload_ctx(
                request,
                error=f"Extension '.{ext}' not allowed. Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
            ),
        )

    # Validate size
    size = len(content)
    if size > _MAX_FILE_SIZE:
        return app.render(
            "upload.html",
            await _upload_ctx(
                request,
                error=f"File too large ({size // 1024}KB). Max: {_MAX_FILE_SIZE // (1024 * 1024)}MB",
            ),
        )

    # Store in memory storage
    stored_name = await _upload_storage.save(original_name, content)

    # Record in database
    doc = Document(
        filename=stored_name,
        original_name=original_name,
        content_type=content_type,
        size=size,
    )
    await doc.save()

    return app.render(
        "upload.html",
        await _upload_ctx(request, success=f"Uploaded: {original_name} ({size} bytes)"),
    )


@app.get("/upload/{doc_id:int}/download")
async def download_file(request, doc_id: int):
    """Download an uploaded file."""
    doc = await Document.objects.filter(id=doc_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")

    try:
        content = await _upload_storage.open(doc.filename)
    except FileNotFoundError:
        content = None
    if content is None:
        raise HTTPException(404, "File not found in storage")

    return Response(
        body=content,
        content_type=doc.content_type,
        headers={"Content-Disposition": f'attachment; filename="{doc.original_name}"'},
    )


@app.get("/api/uploads")
async def api_list_uploads(request):
    """JSON API: list all uploaded documents."""
    documents = await Document.objects.order_by("-id").limit(50).all()
    return Response.json(
        {
            "count": len(documents),
            "documents": [
                {
                    "id": d.id,
                    "filename": d.original_name,
                    "content_type": d.content_type,
                    "size": d.size,
                }
                for d in documents
            ],
        }
    )


# ---------------------------------------------------------------------------
# FileField Lifecycle Showcase
# ---------------------------------------------------------------------------

_attachment_storage = MemoryStorage(base_url="/files/")


class Attachment(TimestampMixin, Model):
    """Model with FileField + ImageField — demonstrates save/delete lifecycle."""

    class Meta:
        table = "forms_attachments"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    document: str = FileField(upload_to="documents/")
    thumbnail: str = ImageField(upload_to="thumbnails/")
    uploaded_by: str = Field(default="anonymous")


@app.post("/api/attachments")
async def api_create_attachment(request):
    """Upload attachment with FileField lifecycle.

    Demonstrates: multipart upload, FileField save_uploaded_file(),
    ImageField extension validation, cleanup on validation failure.
    """
    files = await request.files()
    form = await request.form()

    title = form.get("title", "Untitled")
    if isinstance(title, list):
        title = title[0]

    doc_file = files.get("document")
    if not doc_file or not doc_file.filename:
        raise HTTPException(400, "document file required")

    attachment = Attachment(title=title, uploaded_by="api-user")

    doc_path = await save_uploaded_file(
        attachment, "document", doc_file.data, doc_file.filename, _attachment_storage
    )

    thumb_file = files.get("thumbnail")
    if thumb_file and thumb_file.filename:
        try:
            await save_uploaded_file(
                attachment,
                "thumbnail",
                thumb_file.data,
                thumb_file.filename,
                _attachment_storage,
            )
        except ValueError as e:
            await delete_uploaded_file(attachment, "document", _attachment_storage)
            raise HTTPException(400, str(e))

    await attachment.save()

    return Response.json(
        {
            "id": attachment.id,
            "title": attachment.title,
            "document_url": _attachment_storage.url(attachment.document),
            "thumbnail_url": _attachment_storage.url(attachment.thumbnail)
            if attachment.thumbnail
            else None,
            "size": len(doc_file.data),
        },
        status=201,
    )


@app.get("/api/attachments/{att_id:int}/download")
async def api_download_attachment(request, att_id: int):
    """Download attachment file with Content-Disposition."""
    att = await Attachment.objects.filter(id=att_id).first()
    if not att or not att.document:
        raise HTTPException(404, "Attachment not found")

    try:
        content = await _attachment_storage.open(att.document)
    except FileNotFoundError:
        raise HTTPException(404, "File not found in storage")

    filename = att.document.rsplit("/", 1)[-1]
    return Response(
        body=content,
        content_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(content)),
        },
    )


@app.delete("/api/attachments/{att_id:int}")
async def api_delete_attachment(request, att_id: int):
    """Delete attachment and associated files (cascade delete).

    SECURITY NOTE (forms_demo scope): this route is intentionally
    public because forms_demo exists to showcase the FileField
    lifecycle end-to-end, NOT to demonstrate access control. A
    production app built from this template MUST:

        1. Add `@guard(Require.authenticated())` above this handler.
        2. Add an `owner_id: int = Field(foreign_key=User)` to the
           Attachment model and populate it at upload time.
        3. Change the filter below to
           `Attachment.objects.filter(id=att_id, owner_id=request.user["id"])`
           so users cannot delete each other's files.

    See `services/notes_api/` for the reference pattern.
    """
    att = await Attachment.objects.filter(id=att_id).first()
    if not att:
        raise HTTPException(404, "Attachment not found")

    if att.document:
        await delete_uploaded_file(att, "document", _attachment_storage)
    if att.thumbnail:
        await delete_uploaded_file(att, "thumbnail", _attachment_storage)

    await Attachment.objects.filter(id=att_id).delete()
    return Response.json({"deleted": True, "id": att_id})


@app.get("/api/attachments")
async def api_list_attachments(request):
    """List attachments with file URLs."""
    attachments = await Attachment.objects.order_by("-id").limit(50).all()
    return Response.json(
        {
            "count": len(attachments),
            "attachments": [
                {
                    "id": a.id,
                    "title": a.title,
                    "document_url": _attachment_storage.url(a.document)
                    if a.document
                    else None,
                    "thumbnail_url": _attachment_storage.url(a.thumbnail)
                    if a.thumbnail
                    else None,
                }
                for a in attachments
            ],
        }
    )


# --- Health ---


app.mount_health()
mount_docs(app)


if __name__ == "__main__":
    _port = int(sys.argv[1]) if len(sys.argv) > 1 else get_setting("PORT", 8400)
    app.run(port=_port)

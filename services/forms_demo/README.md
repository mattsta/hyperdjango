# Forms Demo

Form validation showcase with Form classes, ModelForm, cross-field validation, and server-rendered HTML forms.

## Quick Start

```bash
uv run hyper setup --app services.forms_demo.app:app --seed services.forms_demo.seed:run
uv run hyper run --app services.forms_demo.app:app --port 8400
```

## Features

- Form class with 8 field types: CharField, IntegerField, EmailField, PasswordField, FloatField, DateField, ChoiceField, BooleanField
- Field-level validation with min/max length, min/max value, required/optional
- Per-field custom validation via `clean_<fieldname>()` methods
- Cross-field `clean()` method (password confirmation, urgent priority requires due date)
- ModelForm auto-generated from Ticket model definition
- ChoiceField populated from Enum (Priority, Category)
- Form rendering with `as_div` and error display
- JSON API endpoint for live form validation (HTMX/JS compatible)
- CSRF protection on all form submissions
- Registration form with server-side username uniqueness check
- File upload with extension whitelist, size limit (5MB), and MemoryStorage
- Upload list with download links
- Filename sanitization (strips path traversal)

## Platform Features Demonstrated

- **Form** class with declarative field definitions
- **ModelForm** auto-generating fields from Model metadata
- **clean_fieldname()** per-field custom validators
- **clean()** cross-field validation with `add_error()`
- **CSRFMiddleware** protecting all POST routes
- **SessionAuth** for authenticated ticket creation
- **Template rendering** with Zig template engine
- **MemoryStorage** for in-process file uploads (FileSystemStorage for production)
- **request.files()** for multipart file handling with UploadedFile

## Pages

```
GET  /                  Index page
GET  /contact           Contact form (6 field types + cross-field validation)
POST /contact           Submit contact form
GET  /register          Registration form (password confirmation validation)
POST /register          Submit registration
GET  /tickets           Ticket list
GET  /tickets/new       New ticket form (ModelForm from Ticket model)
POST /tickets/new       Submit ticket
POST /api/validate/contact  JSON validation API (for live validation)
GET  /upload               File upload form + document list
POST /upload               Upload file (multipart, validated)
GET  /upload/{id}/download Download uploaded file
GET  /api/uploads          JSON: list uploaded documents
```

## Project Structure

```
forms_demo/
    app.py              Form classes, ModelForm, routes, validation logic
    seed.py             Sample users and tickets
    templates/          HTML templates (index, contact, register, tickets)
```

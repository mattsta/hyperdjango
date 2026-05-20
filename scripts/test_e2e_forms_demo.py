"""
End-to-end tests for Forms Demo example.

# hyper-test: e2e

Tests:
  - Health + readiness
  - Contact form: render, valid submit, field validation errors, cross-field validation
  - Register form: render, valid submit, password mismatch, duplicate username
  - Ticket ModelForm: render, valid submit, field validation
  - JSON validation API
"""

import json
import subprocess
import sys
import time

from e2e_helper import (
    TEST_PORTS,
    AppRunner,
    Session,
    http_get,
    http_post,
)

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name, response, expected_status=200):
    global PASS, FAIL
    if response.status == expected_status:
        PASS += 1
        print(f"  PASS  {name} ({response.status})")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}: expected {expected_status}, got {response.status}"
    print(msg)
    ERRORS.append(msg)
    if response.body:
        print(f"        body: {response.body[:300]}")
    return False


def ok(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
    print(msg)
    ERRORS.append(msg)
    return False


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("Forms Demo E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["forms_demo"]
    ts = str(int(time.time()) % 100000)

    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.forms_demo.app:app",
            "--drop",
            "--seed",
            "services.forms_demo.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        "services.forms_demo.app:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
    ) as runner:
        base = runner.url()
        s = Session(base)
        print(f"\nServer running at {base}\n")

        # ── Health ──
        print("--- Health ---")
        r = http_get(f"{base}/health")
        check("Health 200", r, 200)
        r = http_get(f"{base}/ready")
        check("Ready 200", r, 200)
        if r.status == 200:
            ok("Ready status ok", r.json.get("status") == "ok")
            ok("Ready has checks", "checks" in r.json)

        # ── Home ──
        print("\n--- Home ---")
        r = http_get(f"{base}/")
        check("Home 200", r, 200)
        ok("Home has links", "/contact" in r.body and "/register" in r.body)

        # ── Contact Form: render ──
        print("\n--- Contact Form ---")
        r = s.get("/contact")
        check("Contact page 200", r, 200)
        ok("Has form fields", "name" in r.body.lower() and "email" in r.body.lower())
        ok("Has priority select", "priority" in r.body.lower())
        ok("Has submit button", "Send Message" in r.body)

        # Contact: valid submit
        r = s.post(
            "/contact",
            body=(
                "name=Test+User&email=test@example.com&subject=Hello+World"
                "&message=This+is+a+test+message+with+enough+length"
                "&priority=normal&agree_terms=on"
            ),
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Valid contact submit",
            r.status == 200 and "success" in r.body.lower(),
            f"status={r.status}",
        )

        # Contact: missing required field
        r = s.post(
            "/contact",
            body=(
                "name=&email=test@example.com&subject=Hi&message=Short&priority=normal"
            ),
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Missing name shows error",
            r.status == 200
            and ("error" in r.body.lower() or "required" in r.body.lower()),
            f"status={r.status}",
        )

        # Contact: invalid email
        r = s.post(
            "/contact",
            body=(
                "name=Test&email=notanemail&subject=Hello+Test&message=Long+enough+message+here"
                "&priority=normal&agree_terms=on"
            ),
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Invalid email shows error",
            r.status == 200 and "sent successfully" not in r.body.lower(),
            f"status={r.status} body_start={r.body[:200]!r}",
        )

        # Contact: cross-field (urgent without due_date)
        r = s.post(
            "/contact",
            body=(
                "name=Test+User&email=test@example.com&subject=Urgent+Request"
                "&message=This+is+urgent+please+help+us+quickly"
                "&priority=urgent&agree_terms=on"
            ),
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Urgent without due_date shows error",
            r.status == 200 and "due" in r.body.lower(),
            f"status={r.status}",
        )

        # ── Register Form ──
        print("\n--- Register Form ---")
        r = s.get("/register")
        check("Register page 200", r, 200)
        ok("Has password fields", "password" in r.body.lower())

        # Valid registration
        r = s.post(
            "/register",
            body=(
                f"username=newuser_{ts}&email=new@example.com"
                f"&password=secret123&password_confirm=secret123"
            ),
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Valid register succeeds",
            r.status == 200 and "success" in r.body.lower(),
            f"status={r.status}",
        )

        # Password mismatch
        r = s.post(
            "/register",
            body=(
                f"username=mismatch_{ts}&email=mis@example.com"
                f"&password=secret123&password_confirm=different456"
            ),
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Password mismatch shows error",
            r.status == 200
            and ("match" in r.body.lower() or "error" in r.body.lower()),
        )

        # Duplicate username
        r = s.post(
            "/register",
            body=(
                f"username=newuser_{ts}&email=dup@example.com"
                f"&password=secret123&password_confirm=secret123"
            ),
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Duplicate username shows error",
            r.status == 200
            and ("taken" in r.body.lower() or "error" in r.body.lower()),
        )

        # ── Ticket ModelForm ──
        print("\n--- Ticket ModelForm ---")
        r = s.get("/tickets/new")
        check("Ticket form page 200", r, 200)
        ok(
            "Has ModelForm fields",
            "title" in r.body.lower() and "category" in r.body.lower(),
        )
        ok("Has Enum choices", "bug" in r.body.lower() or "feature" in r.body.lower())

        # Valid ticket
        r = s.post(
            "/tickets/new",
            body=(
                f"title=Test+Ticket+{ts}&description=A+test+ticket"
                f"&category=bug&priority=high&email=test@example.com"
                f"&budget=99.99&is_urgent=on"
            ),
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Valid ticket created",
            r.status == 200 and "success" in r.body.lower(),
            f"status={r.status}",
        )

        # Ticket list shows new ticket
        r = s.get("/tickets")
        check("Ticket list 200", r, 200)
        ok("Ticket list has items", "Test Ticket" in r.body or "Login page" in r.body)

        # Missing required title
        r = s.post(
            "/tickets/new",
            body=("title=&description=No+title&category=bug&priority=normal"),
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Missing title shows error",
            r.status == 200
            and "created successfully" not in r.body.lower()
            and ("error" in r.body.lower() or "required" in r.body.lower()),
            f"status={r.status} body_start={r.body[:200]!r}",
        )

        # ── JSON Validation API ──
        print("\n--- JSON Validation ---")
        r = http_post(
            f"{base}/api/validate/contact",
            body=json.dumps(
                {
                    "name": "Test",
                    "email": "test@example.com",
                    "subject": "Hello World",
                    "message": "A sufficiently long message for validation",
                    "priority": "normal",
                    "agree_terms": "on",
                }
            ),
        )
        check("Validate API 200", r, 200)
        if r.status == 200:
            ok("Valid form returns valid=true", r.json.get("valid") is True)

        r = http_post(
            f"{base}/api/validate/contact",
            body=json.dumps(
                {
                    "name": "",
                    "email": "bad",
                    "subject": "Hi",
                    "message": "Short",
                    "priority": "normal",
                }
            ),
        )
        check("Validate invalid 200", r, 200)
        if r.status == 200:
            ok("Invalid form returns valid=false", r.json.get("valid") is False)
            ok("Has error details", len(r.json.get("errors", {})) > 0)

        # ── File Upload ────────────────────────────────────────
        print("\n--- File Upload ---")

        r = s.get("/upload")
        check("Upload page 200", r, 200)
        ok("Upload page has form", "file" in r.body)
        ok("Upload page shows allowed extensions", "csv" in r.body or "txt" in r.body)

        # Upload API list (should be empty initially)
        r = s.get("/api/uploads")
        check("Upload API 200", r, 200)
        ok("Upload API has count", "count" in r.json)
        initial_count = r.json.get("count", 0)

        # Upload a valid .txt file via multipart
        r = s.post_multipart(
            "/upload",
            fields={
                "file": ("test_doc.txt", b"Hello from the E2E test!", "text/plain"),
            },
        )
        ok(
            "Upload valid file",
            r.status == 200 and "Uploaded" in r.body,
            f"got {r.status}",
        )

        # Verify file appears in API list
        r = s.get("/api/uploads")
        ok(
            "Upload count increased",
            r.json.get("count", 0) > initial_count,
            f"was {initial_count}, now {r.json.get('count', 0)}",
        )
        if r.json.get("documents"):
            doc = r.json["documents"][0]
            ok("Upload has filename", doc.get("filename") == "test_doc.txt")
            ok("Upload has size", doc.get("size") == 24)
            doc_id = doc.get("id")
        else:
            ok("Upload has filename", False, "no documents returned")
            ok("Upload has size", False)
            doc_id = None

        # Download the uploaded file
        if doc_id:
            r = s.get(f"/upload/{doc_id}/download")
            check("Download uploaded file", r, 200)
            ok("Download has content", "Hello from the E2E test!" in r.body)
            ok(
                "Download has disposition header",
                "attachment" in r.headers.get("content-disposition", ""),
            )

        # Upload a .csv file
        r = s.post_multipart(
            "/upload",
            fields={
                "file": ("data.csv", b"name,age\nalice,30\nbob,25", "text/csv"),
            },
        )
        ok("Upload CSV", r.status == 200 and "Uploaded" in r.body, f"got {r.status}")

        # Reject disallowed extension
        r = s.post_multipart(
            "/upload",
            fields={
                "file": ("malware.exe", b"MZ\x90\x00", "application/octet-stream"),
            },
        )
        ok(
            "Reject .exe extension",
            r.status == 200 and "not allowed" in r.body,
            f"got {r.status}, body: {r.body[:200]}",
        )

        # Reject no file
        r = s.post(
            "/upload",
            body="",
            content_type="application/x-www-form-urlencoded",
        )
        ok(
            "Reject empty upload",
            r.status == 200 and ("No file" in r.body or "error" in r.body.lower()),
        )

        # Download non-existent file → 404
        r = s.get("/upload/999999/download")
        check("Download non-existent 404", r, 404)

        # ── FileField Lifecycle (Attachment API) ───────────────
        print("\n--- FileField Lifecycle ---")

        # List attachments (empty)
        r = http_get(f"{base}/api/attachments")
        check("Attachments list 200", r, 200)
        ok("attachments initially empty", r.json.get("count", -1) == 0)

        # Upload attachment with FileField
        r = s.post_multipart(
            "/api/attachments",
            fields={
                "title": "E2E Test Doc",
                "document": ("report.txt", b"E2E FileField test content", "text/plain"),
            },
        )
        check("Create attachment 201", r, 201)
        att_id = None
        if r.status == 201:
            ok("attachment has id", "id" in r.json)
            ok("attachment has document_url", "document_url" in r.json)
            ok("attachment size correct", r.json.get("size") == 26)
            att_id = r.json.get("id")

        # Download the attachment
        if att_id:
            r = http_get(f"{base}/api/attachments/{att_id}/download")
            check("Download attachment 200", r, 200)
            ok("download content matches", "E2E FileField test content" in r.body)
            ok(
                "download has Content-Disposition",
                "attachment" in r.headers.get("content-disposition", ""),
            )

        # List attachments (should have 1)
        r = http_get(f"{base}/api/attachments")
        ok("attachments count 1", r.json.get("count") == 1)

        # Upload with invalid image extension for thumbnail
        r = s.post_multipart(
            "/api/attachments",
            fields={
                "title": "Bad Thumb",
                "document": ("valid.txt", b"ok", "text/plain"),
                "thumbnail": ("bad.exe", b"nope", "application/octet-stream"),
            },
        )
        check("Invalid thumbnail extension 400", r, 400)

        # Delete attachment (cascade — removes files too)
        if att_id:
            r = s.delete(f"/api/attachments/{att_id}")
            check("Delete attachment 200", r, 200)
            ok("delete confirmed", r.json.get("deleted") is True)

            # Verify download now 404
            r = http_get(f"{base}/api/attachments/{att_id}/download")
            check("Deleted attachment 404", r, 404)

        # Verify list now empty
        r = http_get(f"{base}/api/attachments")
        ok("attachments empty after delete", r.json.get("count") == 0)

        # Delete non-existent attachment
        r = s.delete("/api/attachments/99999")
        check("Delete non-existent 404", r, 404)

        # Upload without document → 400
        r = s.post_multipart(
            "/api/attachments",
            fields={"title": "No File"},
        )
        check("No document 400", r, 400)

    # ── Summary ──
    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for err in ERRORS:
            print(f"  {err}")
    print("=" * 60)

    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

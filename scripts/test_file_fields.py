"""
Tests for FileField and ImageField model integration.

- FileField creates string field with upload_to metadata
- ImageField validates file extensions
- save_uploaded_file stores file and sets model attribute
- delete_uploaded_file removes file and clears attribute
- MemoryStorage integration
- FileSystemStorage integration
"""

# hyper-test: unit

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

results = []
test_funcs = []


def test(name):
    def decorator(func):
        test_funcs.append((name, func))
        return func

    return decorator


def check(label, condition):
    results.append((label, condition))
    symbol = "\u2713" if condition else "\u2717"
    print(f"  {symbol} {label}")


# ═══════════════════════════════════════════════════════════════════════════
# FileField / ImageField creation
# ═══════════════════════════════════════════════════════════════════════════


@test("FileField: creates with upload_to metadata")
async def test_file_field_meta():
    from hyperdjango.models import FileField, _get_file_meta

    f = FileField(upload_to="docs/")
    meta = _get_file_meta(f)
    check("has metadata", meta is not None)
    check("upload_to = docs/", meta["upload_to"] == "docs/")
    check("field_type = file", meta["field_type"] == "file")


@test("FileField: default value is empty string")
async def test_file_field_default():
    from hyperdjango.models import FileField

    f = FileField()
    check("default is empty string", f.default == "")


@test("FileField: upload_to normalized with trailing slash")
async def test_file_field_trailing_slash():
    from hyperdjango.models import FileField, _get_file_meta

    f1 = FileField(upload_to="docs")
    f2 = FileField(upload_to="docs/")
    check("docs → docs/", _get_file_meta(f1)["upload_to"] == "docs/")
    check("docs/ → docs/", _get_file_meta(f2)["upload_to"] == "docs/")


@test("ImageField: creates with allowed_extensions")
async def test_image_field_meta():
    from hyperdjango.models import ImageField, _get_file_meta

    f = ImageField(upload_to="photos/")
    meta = _get_file_meta(f)
    check("has metadata", meta is not None)
    check("upload_to = photos/", meta["upload_to"] == "photos/")
    check("field_type = image", meta["field_type"] == "image")
    check("has allowed_extensions", len(meta["allowed_extensions"]) > 0)
    check(".jpg allowed", ".jpg" in meta["allowed_extensions"])
    check(".png allowed", ".png" in meta["allowed_extensions"])
    # SECURITY: .svg is NOT allowed by default (active-content stored-XSS vector).
    check(".svg NOT allowed by default", ".svg" not in meta["allowed_extensions"])


@test("ImageField: custom extensions")
async def test_image_field_custom_ext():
    from hyperdjango.models import ImageField, _get_file_meta

    f = ImageField(upload_to="art/", allowed_extensions=(".tiff", ".bmp"))
    meta = _get_file_meta(f)
    check(".tiff allowed", ".tiff" in meta["allowed_extensions"])
    check(".bmp allowed", ".bmp" in meta["allowed_extensions"])
    check(".jpg not allowed", ".jpg" not in meta["allowed_extensions"])


@test("_is_file_field: detects file fields")
async def test_is_file_field():
    from hyperdjango.models import Field, FileField, ImageField, _is_file_field

    f1 = FileField()
    f2 = ImageField()
    f3 = Field(max_length=100)
    check("FileField detected", _is_file_field(f1))
    check("ImageField detected", _is_file_field(f2))
    check("regular Field not detected", not _is_file_field(f3))


# ═══════════════════════════════════════════════════════════════════════════
# Model with FileField
# ═══════════════════════════════════════════════════════════════════════════


@test("model: FileField on model class")
async def test_model_file_field():
    from hyperdjango.models import Field, FileField, ImageField, Model

    class Product(Model):
        class Meta:
            table = "test_products"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(max_length=200)
        document: str = FileField(upload_to="products/docs/")
        photo: str = ImageField(upload_to="products/photos/")

    p = Product(id=1, name="Widget", document="", photo="")
    check("document default empty", p.document == "")
    check("photo default empty", p.photo == "")


# ═══════════════════════════════════════════════════════════════════════════
# save_uploaded_file with MemoryStorage
# ═══════════════════════════════════════════════════════════════════════════


@test("save: stores file in MemoryStorage")
async def test_save_memory():
    from hyperdjango.models import Field, FileField, Model, save_uploaded_file
    from hyperdjango.storage import MemoryStorage

    class Doc(Model):
        class Meta:
            table = "test_docs"

        id: int = Field(primary_key=True, auto=True)
        file: str = FileField(upload_to="docs/")

    storage = MemoryStorage()
    doc = Doc(id=1, file="")

    path = await save_uploaded_file(doc, "file", b"hello world", "readme.txt", storage)
    check("path includes upload_to", path.startswith("docs/"))
    check("path includes filename", "readme" in path)
    check("model field updated", doc.file == path)

    # Verify file exists in storage
    content = await storage.open(path)
    check("content matches", content == b"hello world")


@test("save: ImageField validates extension")
async def test_save_image_validates():
    from hyperdjango.models import Field, ImageField, Model, save_uploaded_file
    from hyperdjango.storage import MemoryStorage

    class Photo(Model):
        class Meta:
            table = "test_photos"

        id: int = Field(primary_key=True, auto=True)
        image: str = ImageField(upload_to="photos/")

    storage = MemoryStorage()
    photo = Photo(id=1, image="")

    # Valid extension
    path = await save_uploaded_file(photo, "image", b"\x89PNG", "pic.png", storage)
    check("png accepted", "pic.png" in path)

    # Invalid extension
    try:
        await save_uploaded_file(photo, "image", b"data", "report.pdf", storage)
        check("pdf rejected", False)
    except ValueError as e:
        check("pdf rejected with error", ".pdf" in str(e))


@test("save: ImageField allows raster formats, rejects .svg by default")
async def test_save_image_extensions():
    from hyperdjango.models import Field, ImageField, Model, save_uploaded_file
    from hyperdjango.storage import MemoryStorage

    class Img(Model):
        class Meta:
            table = "test_imgs"

        id: int = Field(primary_key=True, auto=True)
        pic: str = ImageField(upload_to="imgs/")

    storage = MemoryStorage()

    for ext in ["jpg", "jpeg", "png", "gif", "webp"]:
        img = Img(id=1, pic="")
        path = await save_uploaded_file(img, "pic", b"data", f"test.{ext}", storage)
        check(f".{ext} accepted", path.endswith(f"test.{ext}"))

    # SECURITY: .svg (active content) is rejected by the secure default.
    svg_rejected = False
    try:
        await save_uploaded_file(Img(id=1, pic=""), "pic", b"<svg/>", "x.svg", storage)
    except ValueError:
        svg_rejected = True
    check(".svg rejected by default", svg_rejected)


@test("save: user filename is sanitized to a safe basename")
async def test_save_filename_sanitized():
    from hyperdjango.models import Field, FileField, Model, save_uploaded_file
    from hyperdjango.storage import MemoryStorage

    class Doc(Model):
        class Meta:
            table = "test_docs"

        id: int = Field(primary_key=True, auto=True)
        f: str = FileField(upload_to="docs/")

    storage = MemoryStorage()
    # A traversal-laden filename is reduced to a basename under upload_to.
    path = await save_uploaded_file(
        Doc(id=1, f=""), "f", b"data", "../../../etc/passwd", storage
    )
    check("no traversal in stored path", ".." not in path)
    check("stored under upload_to", path == "docs/passwd")


# ═══════════════════════════════════════════════════════════════════════════
# delete_uploaded_file
# ═══════════════════════════════════════════════════════════════════════════


@test("delete: removes file and clears field")
async def test_delete():
    from hyperdjango.models import (
        Field,
        FileField,
        Model,
        delete_uploaded_file,
        save_uploaded_file,
    )
    from hyperdjango.storage import MemoryStorage

    class Doc(Model):
        class Meta:
            table = "test_docs"

        id: int = Field(primary_key=True, auto=True)
        file: str = FileField(upload_to="docs/")

    storage = MemoryStorage()
    doc = Doc(id=1, file="")

    await save_uploaded_file(doc, "file", b"content", "file.txt", storage)
    check("file set", doc.file != "")

    path = doc.file
    await delete_uploaded_file(doc, "file", storage)
    check("field cleared", doc.file == "")

    exists = await storage.exists(path)
    check("file deleted from storage", not exists)


@test("delete: handles already-deleted file gracefully")
async def test_delete_missing():
    from hyperdjango.models import Field, FileField, Model, delete_uploaded_file
    from hyperdjango.storage import MemoryStorage

    class Doc(Model):
        class Meta:
            table = "test_docs"

        id: int = Field(primary_key=True, auto=True)
        file: str = FileField(upload_to="docs/")

    storage = MemoryStorage()
    doc = Doc(id=1, file="docs/nonexistent.txt")

    # Should not raise
    await delete_uploaded_file(doc, "file", storage)
    check("no exception on missing file", True)
    check("field cleared", doc.file == "")


@test("delete: empty field is no-op")
async def test_delete_empty():
    from hyperdjango.models import Field, FileField, Model, delete_uploaded_file
    from hyperdjango.storage import MemoryStorage

    class Doc(Model):
        class Meta:
            table = "test_docs"

        id: int = Field(primary_key=True, auto=True)
        file: str = FileField(upload_to="docs/")

    storage = MemoryStorage()
    doc = Doc(id=1, file="")

    await delete_uploaded_file(doc, "file", storage)
    check("still empty", doc.file == "")


# ═══════════════════════════════════════════════════════════════════════════
# FileSystemStorage integration
# ═══════════════════════════════════════════════════════════════════════════


@test("filesystem: save and retrieve")
async def test_filesystem():
    from hyperdjango.models import Field, FileField, Model, save_uploaded_file
    from hyperdjango.storage import FileSystemStorage

    tmp = tempfile.mkdtemp()
    try:
        storage = FileSystemStorage(location=tmp, base_url="/media/")

        class Doc(Model):
            class Meta:
                table = "test_docs"

            id: int = Field(primary_key=True, auto=True)
            file: str = FileField(upload_to="docs/")

        doc = Doc(id=1, file="")
        path = await save_uploaded_file(
            doc, "file", b"real file content", "test.txt", storage
        )
        check("file saved", (Path(tmp) / path).is_file())

        content = await storage.open(path)
        check("content matches", content == b"real file content")

        url = storage.url(path)
        check("url has base_url", url.startswith("/media/"))
    finally:
        shutil.rmtree(tmp)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    print(f"\n{'=' * 60}")
    print("FileField + ImageField Tests")
    print(f"{'=' * 60}\n")

    for name, func in test_funcs:
        print(f"\n[TEST] {name}")
        try:
            await func()
        except Exception as e:
            check(f"EXCEPTION: {e}", False)
            import traceback

            traceback.print_exc()

    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    total = len(results)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    if failed:
        print("\nFailed:")
        for label, ok in results:
            if not ok:
                print(f"  \u2717 {label}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

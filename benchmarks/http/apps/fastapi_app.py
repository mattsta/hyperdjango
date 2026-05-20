"""FastAPI benchmark app (async, served by uvicorn). See apps/__init__.py."""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI()


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/json")
async def json_ep(n: int = 0):
    return {"data": "x" * n}


@app.get("/plaintext", response_class=PlainTextResponse)
async def plaintext():
    return "Hello, World!"

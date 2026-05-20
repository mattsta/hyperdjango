"""Flask benchmark app (sync WSGI, served by gunicorn). See apps/__init__.py."""

from flask import Flask, jsonify, request

app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify(ok=True)


@app.get("/json")
def json_ep():
    n = int(request.args.get("n", 0))
    return jsonify(data="x" * n)


@app.get("/plaintext")
def plaintext():
    return "Hello, World!"

"""Minimal URL configuration for hyperdjango tests."""

from django.http import HttpResponse, JsonResponse
from django.urls import path


def hello_view(request):
    return HttpResponse("Hello from Django!")


def echo_view(request):
    return JsonResponse(
        {
            "method": request.method,
            "path": request.path,
            "query": request.GET.dict(),
            "headers": {k: v for k, v in request.META.items() if k.startswith("HTTP_")},
        }
    )


urlpatterns = [
    path("hello/", hello_view),
    path("echo/", echo_view),
]

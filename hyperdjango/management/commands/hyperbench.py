"""
manage.py hyperbench — Benchmark HyperDjango vs standard Django.

Compares validation speed of HyperForm (dhi) vs Django Form.
"""

import time

from django import forms
from django.core.management.base import BaseCommand

from hyperdjango.validation.forms import HyperForm


class Command(BaseCommand):
    help = "Benchmark HyperDjango validation vs standard Django"

    def add_arguments(self, parser):
        parser.add_argument(
            "--iterations",
            type=int,
            default=10000,
            help="Number of validation iterations (default: 10000)",
        )

    def handle(self, *args, **options):
        iterations = options["iterations"]

        self.stdout.write(
            self.style.MIGRATE_HEADING(f"Benchmarking {iterations} validations")
        )

        self._bench_forms(iterations)

    def _bench_forms(self, n):
        # Define identical forms
        class DjangoUserForm(forms.Form):
            name = forms.CharField(max_length=100)
            age = forms.IntegerField(min_value=0, max_value=150)
            email = forms.EmailField()

        class HyperUserForm(HyperForm):
            name = forms.CharField(max_length=100)
            age = forms.IntegerField(min_value=0, max_value=150)
            email = forms.EmailField()

        valid_data = {"name": "Alice", "age": "25", "email": "alice@example.com"}

        # Warm up
        DjangoUserForm(data=valid_data).is_valid()
        HyperUserForm(data=valid_data).is_valid()

        # Benchmark Django
        start = time.perf_counter()
        for _ in range(n):
            f = DjangoUserForm(data=valid_data)
            f.is_valid()
        django_time = time.perf_counter() - start

        # Benchmark HyperForm
        start = time.perf_counter()
        for _ in range(n):
            f = HyperUserForm(data=valid_data)
            f.is_valid()
        hyper_time = time.perf_counter() - start

        # Results
        self.stdout.write("")
        self.stdout.write(
            f"  Django Form:  {django_time:.3f}s ({n / django_time:.0f} validations/sec)"
        )
        self.stdout.write(
            f"  HyperForm:    {hyper_time:.3f}s ({n / hyper_time:.0f} validations/sec)"
        )

        if hyper_time > 0:
            speedup = django_time / hyper_time
            style = self.style.SUCCESS if speedup > 1 else self.style.WARNING
            self.stdout.write(style(f"  Speedup:      {speedup:.1f}x"))

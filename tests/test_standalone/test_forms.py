"""Regression tests for hyperdjango.forms (standalone Form / FormSet).

Covers:
- Reflected-XSS: rendered error text, labels, help_text and choice errors must
  be HTML-escaped and must never echo raw submitted markup.
- FormSet with extra>0: blank extra forms are valid-and-ignored, not failures.
- FormSet initial: seeds UNBOUND forms (initial, not bound data).
- Form.clean(): runs even when individual fields have errors (Django semantics).
"""

from hyperdjango.forms import (
    CharField,
    ChoiceField,
    Form,
    FormSet,
    IntegerField,
)

XSS = "<script>alert(1)</script>"


class TestFormRenderingEscaping:
    def test_choice_error_does_not_reflect_raw_input(self):
        class F(Form):
            color = ChoiceField(choices=[("r", "Red"), ("b", "Blue")])

        form = F(data={"color": XSS})
        assert not form.is_valid()
        out = form.as_div()
        # Raw <script> must never appear in rendered output.
        assert "<script>" not in out
        # The raw value is not echoed back at all in the choice error message.
        assert "alert(1)" not in out

    def test_error_message_is_escaped(self):
        class F(Form):
            name = CharField(max_length=100)

            def clean_name(self):
                raise ValueError(XSS)

        form = F(data={"name": "x"})
        assert not form.is_valid()
        out = form.as_div()
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_label_is_escaped(self):
        class F(Form):
            name = CharField(label=XSS)

        out = F(data={"name": "x"}).as_div()
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_help_text_is_escaped(self):
        class F(Form):
            name = CharField(help_text=XSS)

        out = F(data={"name": "x"}).as_div()
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_form_level_error_is_escaped(self):
        class F(Form):
            name = CharField()

            def clean(self):
                raise ValueError(XSS)

        form = F(data={"name": "ok"})
        assert not form.is_valid()
        out = form.as_div()
        assert "<script>" not in out
        assert "&lt;script&gt;" in out


class ItemForm(Form):
    name = CharField(max_length=100)
    qty = IntegerField(min_value=0)


class TestFormSetExtra:
    def test_extra_blank_forms_are_valid_and_ignored(self):
        fs = FormSet(ItemForm, data=[{"name": "Widget", "qty": 5}], extra=2)
        assert fs.is_valid(), fs.errors
        # Only the bound form contributes cleaned_data.
        assert len(fs.cleaned_data) == 1

    def test_all_extra_forms_valid(self):
        fs = FormSet(ItemForm, extra=3)
        assert fs.is_valid(), fs.errors

    def test_bad_data_still_fails_with_extra(self):
        fs = FormSet(ItemForm, data=[{"name": "w", "qty": -5}], extra=1)
        assert not fs.is_valid()

    def test_initial_creates_unbound_forms(self):
        fs = FormSet(ItemForm, initial=[{"name": "A", "qty": 1}])
        assert not fs.forms[0]._is_bound
        assert fs.forms[0].initial["name"] == "A"
        # Unbound initial-only formset is valid (nothing submitted yet).
        assert fs.is_valid(), fs.errors


class TestFormCleanAlwaysRuns:
    def test_clean_runs_even_when_field_has_error(self):
        seen = {}

        class F(Form):
            name = CharField(max_length=3)

            def clean(self):
                seen["ran"] = True

        form = F(data={"name": "too long"})
        assert not form.is_valid()  # field error present
        assert seen.get("ran") is True  # clean() still executed

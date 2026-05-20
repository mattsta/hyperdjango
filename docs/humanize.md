# Humanize

Human-readable formatting for numbers, dates, and file sizes. Replaces
`django.contrib.humanize`.

---

## Overview

Nine standalone functions for converting raw values into human-friendly text.
All functions work both as direct Python calls and as template filters via the
`humanize` template library.

```python
from hyperdjango.humanize import ordinal, intcomma, intword, naturaltime
from hyperdjango.humanize import naturalday, naturaldate, filesizeformat, apnumber
from hyperdjango.humanize import HUMANIZE_FILTERS
```

---

## Functions

### ordinal(value: int) -> str

Convert an integer to its ordinal string.

```python
ordinal(1)  # "1st"
ordinal(2)  # "2nd"
ordinal(3)  # "3rd"
ordinal(4)  # "4th"
ordinal(11)  # "11th"
ordinal(12)  # "12th"
ordinal(13)  # "13th"
ordinal(21)  # "21st"
ordinal(102)  # "102nd"
```

### intcomma(value: int | float | str) -> str

Add commas as thousands separators.

```python
intcomma(1000)  # "1,000"
intcomma(1234567)  # "1,234,567"
intcomma(1234567.89)  # "1,234,567.89"
intcomma(-50000)  # "-50,000"
```

### intword(value: int | float) -> str

Convert large integers to friendly text with magnitude words.

```python
intword(1000000)  # "1.0 million"
intword(2500000)  # "2.5 million"
intword(1000000000)  # "1.0 billion"
intword(3000000000000)  # "3.0 trillion"
intword(500000)  # "500000" (below threshold)
intword(-2000000)  # "-2.0 million"
```

Supported scales: million, billion, trillion, quadrillion.

### naturaltime(value: datetime) -> str

Show relative time from now. Past values show "ago", future values show "in ...".

```python
from datetime import datetime, timedelta, timezone

now = datetime.now(timezone.utc)

naturaltime(now - timedelta(seconds=3))  # "just now"
naturaltime(now - timedelta(seconds=30))  # "30 seconds ago"
naturaltime(now - timedelta(minutes=5))  # "5 minutes ago"
naturaltime(now - timedelta(hours=2))  # "2 hours ago"
naturaltime(now - timedelta(days=3))  # "3 days ago"
naturaltime(now - timedelta(weeks=2))  # "2 weeks ago"
naturaltime(now - timedelta(days=60))  # "2 months ago"
naturaltime(now - timedelta(days=400))  # "1 year ago"

naturaltime(now + timedelta(hours=3))  # "in 3 hours"
naturaltime(now + timedelta(days=1))  # "in 1 day"
```

### naturalday(value: date) -> str

Show relative day names for today, yesterday, and tomorrow. Other dates are
formatted as `"Mar 28, 2026"`.

```python
from datetime import date

naturalday(date.today())  # "today"
naturalday(date.today() - timedelta(days=1))  # "yesterday"
naturalday(date.today() + timedelta(days=1))  # "tomorrow"
naturalday(date(2025, 12, 25))  # "Dec 25, 2025"
```

### naturaldate(value: date) -> str

Alias for `naturalday` (Django compatibility).

### filesizeformat(value: int | float) -> str

Format bytes as a human-readable file size.

```python
filesizeformat(0)  # "0 bytes"
filesizeformat(1)  # "1 byte"
filesizeformat(1024)  # "1.0 KB"
filesizeformat(1048576)  # "1.0 MB"
filesizeformat(1073741824)  # "1.0 GB"
filesizeformat(5368709120)  # "5.0 GB"
```

Supported units: bytes, KB, MB, GB, TB, PB.

### apnumber(value: int) -> str

Associated Press style: spell out integers 0-9, use digits for 10+.

```python
apnumber(0)  # "zero"
apnumber(4)  # "four"
apnumber(9)  # "nine"
apnumber(10)  # "10"
apnumber(42)  # "42"
```

---

## Template Filter Usage

Load the humanize library in your template engine, then use filters with the `|`
pipe syntax:

```python
from hyperdjango.templating import TemplateEngine

engine = TemplateEngine("templates")
engine.load_library("humanize")
```

```jinja
{{ 1|ordinal }}                    {# 1st #}
{{ 1000000|intcomma }}             {# 1,000,000 #}
{{ 2500000|intword }}              {# 2.5 million #}
{{ timestamp|naturaltime }}        {# 3 hours ago #}
{{ today|naturalday }}             {# today #}
{{ file_size|filesizeformat }}     {# 4.2 MB #}
{{ count|apnumber }}               {# seven #}
```

---

## Library Registration

The module creates a `Library("humanize")` instance at import time and registers all
seven filter functions on it. This makes the filters available to the template engine
via the standard library loading mechanism:

```python
from hyperdjango.templating import TemplateEngine

engine = TemplateEngine("templates")
engine.load_library("humanize")
```

Once loaded, all filters are available with the `|` pipe syntax in templates:

```jinja
{# Ordinal suffixes #}
{{ position|ordinal }}              {# renders: 1st, 2nd, 3rd, 4th, ... #}

{# Thousands separators #}
{{ user_count|intcomma }}           {# renders: 1,234,567 #}

{# Magnitude words #}
{{ revenue|intword }}               {# renders: 2.5 million #}

{# Relative timestamps #}
{{ post.created_at|naturaltime }}   {# renders: 3 hours ago #}

{# Relative dates #}
{{ event.date|naturalday }}         {# renders: today, yesterday, or Mar 28, 2026 #}

{# Human-readable file sizes #}
{{ attachment.size|filesizeformat }} {# renders: 4.2 MB #}

{# AP-style number words #}
{{ item_count|apnumber }}           {# renders: seven (for 0-9), 42 (for 10+) #}
```

The library auto-discovery means you do not need to manually register individual
filters. A single `engine.load_library("humanize")` call registers all seven.

---

## HUMANIZE_FILTERS Registry

All filters are also exported in a single dict for programmatic access outside of
templates:

```python
from hyperdjango.humanize import HUMANIZE_FILTERS

HUMANIZE_FILTERS
# {
#     "ordinal": ordinal,
#     "intcomma": intcomma,
#     "intword": intword,
#     "naturaltime": naturaltime,
#     "naturalday": naturalday,
#     "filesizeformat": filesizeformat,
#     "apnumber": apnumber,
# }
```

---

## Django Migration Guide

| Django                        | HyperDjango                       |
| ----------------------------- | --------------------------------- |
| `django.contrib.humanize`     | `hyperdjango.humanize`            |
| `{% load humanize %}`         | `engine.load_library("humanize")` |
| `{{ value\|ordinal }}`        | Same syntax                       |
| `{{ value\|intcomma }}`       | Same syntax                       |
| `{{ value\|intword }}`        | Same syntax                       |
| `{{ value\|naturaltime }}`    | Same syntax                       |
| `{{ value\|naturalday }}`     | Same syntax                       |
| `{{ value\|filesizeformat }}` | Same syntax                       |
| `{{ value\|apnumber }}`       | Same syntax                       |

# Sitemaps

XML sitemap generation per the sitemaps.org protocol. Replaces `django.contrib.sitemaps`.

---

## Overview

Generate standards-compliant XML sitemaps and sitemap indexes for search engine
crawlers. Supports pagination (50,000 URLs per page, Google's limit), automatic
`lastmod` detection, ETag-based conditional responses, and a generic sitemap class
for quick setup.

```python
from hyperdjango.sitemaps import Sitemap, GenericSitemap, sitemap_view
```

---

## Sitemap Base Class

Subclass `Sitemap` and override `items()`, `location()`, and other methods to define
your sitemap content.

```python
from hyperdjango.sitemaps import Sitemap


class ArticleSitemap(Sitemap):
    changefreq = "daily"
    priority = 0.8
    protocol = "https"  # default

    def items(self):
        return Article.objects.all()

    def location(self, item):
        return f"/articles/{item.slug}/"

    def lastmod(self, item):
        return item.updated_at
```

### Class Attributes

| Attribute    | Type                | Default   | Description                 |
| ------------ | ------------------- | --------- | --------------------------- |
| `limit`      | `int`               | `50000`   | Max items per sitemap page  |
| `protocol`   | `str`               | `"https"` | URL protocol                |
| `changefreq` | `str` or callable   | `None`    | How often pages change      |
| `priority`   | `float` or callable | `None`    | Crawl priority (0.0 to 1.0) |

### Methods to Override

#### items() -> list

Return the items to include in this sitemap. Each item is passed to `location()`,
`lastmod()`, `changefreq()`, and `priority()`.

```python
def items(self):
    return Article.objects.filter(published=True)
```

#### location(item) -> str

Return the URL path for an item. Default implementation calls
`item.get_absolute_url()`.

```python
def location(self, item):
    return f"/blog/{item.slug}/"
```

#### lastmod(item) -> datetime | date | None

Return the last-modified date for an item. Used in the `<lastmod>` XML element.

```python
def lastmod(self, item):
    return item.updated_at
```

#### changefreq and priority as methods

Both `changefreq` and `priority` can be either class attributes or methods that
accept an item:

```python
class ArticleSitemap(Sitemap):
    def changefreq(self, item):
        if item.is_breaking:
            return "hourly"
        return "weekly"

    def priority(self, item):
        if item.is_featured:
            return 1.0
        return 0.5
```

Valid changefreq values: `always`, `hourly`, `daily`, `weekly`, `monthly`, `yearly`,
`never`.

### Pagination

The `paginator` property returns a `SimplePaginator` that splits items into pages
of `limit` size. The sitemap view handles pagination automatically.

```python
sm = ArticleSitemap()
pag = sm.paginator
print(pag.num_pages)  # number of sitemap pages
page = pag.page(1)  # Page object with .items list
```

### get_latest_lastmod()

Returns the most recent `lastmod` across all items, or `None`. Used in the sitemap
index to show when each section was last updated.

---

## GenericSitemap

A convenience class for building sitemaps from a queryset without subclassing:

```python
from hyperdjango.sitemaps import GenericSitemap

article_sitemap = GenericSitemap(
    queryset=Article.objects.filter(published=True),
    date_field="updated_at",
    priority=0.8,
    changefreq="daily",
)
```

| Parameter    | Type            | Description                  |
| ------------ | --------------- | ---------------------------- |
| `queryset`   | queryset/list   | Items to include             |
| `date_field` | `str` or None   | Attribute name for `lastmod` |
| `priority`   | `float` or None | Override default priority    |
| `changefreq` | `str` or None   | Override default changefreq  |
| `protocol`   | `str` or None   | Override URL protocol        |

---

## Sitemap View

Wire the sitemap view into your router:

```python
from hyperdjango.sitemaps import sitemap_view

sitemaps = {
    "articles": ArticleSitemap(),
    "pages": GenericSitemap(queryset=Page.objects.all(), date_field="updated_at"),
}


# Sitemap index (lists all section sitemaps)
@app.route("/sitemap.xml")
def sitemap_index(request):
    return sitemap_view(request, sitemaps)


# Individual section sitemap
@app.route("/sitemap-{section}.xml")
def sitemap_section(request, section: str):
    page = int(request.query.get("p", "1"))
    return sitemap_view(request, sitemaps, section=section, page=page)
```

### Parameters

| Parameter  | Type                 | Description                          |
| ---------- | -------------------- | ------------------------------------ |
| `request`  | `Request`            | The incoming HTTP request            |
| `sitemaps` | `dict[str, Sitemap]` | Section name to Sitemap instance     |
| `section`  | `str` or None        | Render a specific section (or index) |
| `page`     | `int` or None        | Page number (1-based, default 1)     |

### Response

- Content-Type: `application/xml; charset=utf-8`
- ETag header (MD5 of response body) for conditional GET
- Cache-Control: `public, max-age=3600`

---

## Sitemap Index

When `section` is None, `sitemap_view` renders a sitemap index linking to all
section sitemaps:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://example.com/sitemap-articles.xml</loc>
    <lastmod>2026-03-28T12:00:00+0000</lastmod>
  </sitemap>
  <sitemap>
    <loc>https://example.com/sitemap-pages.xml</loc>
  </sitemap>
</sitemapindex>
```

For sections with more than 50,000 items, the index links to paginated URLs
(`?p=1`, `?p=2`, etc.).

---

## Individual Sitemap XML

```xml
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://example.com/articles/my-post/</loc>
    <lastmod>2026-03-28T12:00:00+0000</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.8</priority>
  </url>
</urlset>
```

---

## ETag Caching

The view computes an MD5 ETag from the XML output. Clients sending
`If-None-Match` with a matching ETag receive a 304 Not Modified response
(handled at the response layer via `resp.set_etag()`).

---

## Date Formatting

- `datetime` with tzinfo: full W3C datetime (`2026-03-28T12:00:00+0000`)
- `datetime` without tzinfo: treated as UTC
- `date`: short format (`2026-03-28`)

---

## Django Migration Guide

| Django                            | HyperDjango                           |
| --------------------------------- | ------------------------------------- |
| `django.contrib.sitemaps.Sitemap` | `hyperdjango.sitemaps.Sitemap`        |
| `GenericSitemap`                  | `hyperdjango.sitemaps.GenericSitemap` |
| Template-based XML rendering      | String-based XML building             |
| `sitemap` URL pattern             | `sitemap_view` function               |
| `x-robots-tag` header             | Manual header via Response            |

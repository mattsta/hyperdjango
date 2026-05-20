# Syndication (RSS & Atom Feeds)

RSS 2.0 and Atom 1.0 feed generation. Replaces `django.contrib.syndication`.

---

## Overview

Generate standards-compliant syndication feeds with per-item metadata, enclosure
support (podcasts), and ETag-based conditional responses. Subclass `Feed`, override
the `title`, `items`, and `item_*` methods, and wire into your router.

```python
from hyperdjango.syndication import Feed, feed_view
```

---

## Feed Base Class

```python
from hyperdjango.syndication import Feed


class LatestArticlesFeed(Feed):
    feed_type = "rss"  # "rss" (default) or "atom"
    language = "en"  # default

    def title(self):
        return "Latest Articles"

    def link(self):
        return "https://example.com/articles/"

    def description(self):
        return "Most recent articles from our blog."

    def items(self):
        return Article.objects.order_by("-published_at")[:20]

    def item_title(self, item):
        return item.title

    def item_description(self, item):
        return item.summary

    def item_link(self, item):
        return f"https://example.com/articles/{item.slug}/"

    def item_pubdate(self, item):
        return item.published_at
```

### Feed-Level Methods

| Method          | Returns | Description                                 |
| --------------- | ------- | ------------------------------------------- |
| `title()`       | `str`   | Feed title                                  |
| `link()`        | `str`   | Feed website URL                            |
| `description()` | `str`   | Feed description / subtitle                 |
| `feed_url()`    | `str`   | URL of the feed itself (atom:link rel=self) |
| `items()`       | `list`  | Items to include in the feed                |

### Per-Item Methods

| Method                           | Returns             | Description                    |
| -------------------------------- | ------------------- | ------------------------------ |
| `item_title(item)`               | `str`               | Item title                     |
| `item_description(item)`         | `str`               | Item description / summary     |
| `item_link(item)`                | `str`               | Item URL                       |
| `item_pubdate(item)`             | `datetime \| None`  | Publication date               |
| `item_author_name(item)`         | `str \| None`       | Author name                    |
| `item_categories(item)`          | `list[str] \| None` | Category tags                  |
| `item_guid(item)`                | `str \| None`       | Globally unique identifier     |
| `item_guid_is_permalink(item)`   | `bool`              | Is the GUID a permalink? (RSS) |
| `item_enclosure_url(item)`       | `str \| None`       | Enclosure URL (media file)     |
| `item_enclosure_length(item)`    | `int \| None`       | Enclosure size in bytes        |
| `item_enclosure_mime_type(item)` | `str \| None`       | Enclosure MIME type            |

### Class Attributes

| Attribute   | Type  | Default | Description         |
| ----------- | ----- | ------- | ------------------- |
| `language`  | `str` | `"en"`  | Feed language code  |
| `feed_type` | `str` | `"rss"` | `"rss"` or `"atom"` |

---

## RSS 2.0 vs Atom 1.0

Set `feed_type` to control the output format:

```python
class MyRSSFeed(Feed):
    feed_type = "rss"  # RSS 2.0 (default)


class MyAtomFeed(Feed):
    feed_type = "atom"  # Atom 1.0
```

### RSS 2.0 Output

```xml
<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
<title>Latest Articles</title>
<link>https://example.com/articles/</link>
<description>Most recent articles from our blog.</description>
<language>en</language>
<atom:link href="/feed/" rel="self" type="application/rss+xml"/>
<lastBuildDate>Sat, 28 Mar 2026 12:00:00 +0000</lastBuildDate>
<item>
<title>My Article</title>
<link>https://example.com/articles/my-article/</link>
<description>Article summary here.</description>
<pubDate>Sat, 28 Mar 2026 10:00:00 +0000</pubDate>
<guid isPermaLink="true">https://example.com/articles/my-article/</guid>
</item>
</channel>
</rss>
```

Content-Type: `application/rss+xml; charset=utf-8`

### Atom 1.0 Output

```xml
<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Articles</title>
<link href="https://example.com/articles/" rel="alternate"/>
<subtitle>Most recent articles from our blog.</subtitle>
<id>https://example.com/articles/</id>
<link href="/feed/" rel="self"/>
<updated>2026-03-28T12:00:00Z</updated>
<entry>
<title>My Article</title>
<link href="https://example.com/articles/my-article/" rel="alternate"/>
<summary>Article summary here.</summary>
<published>2026-03-28T10:00:00Z</published>
<updated>2026-03-28T10:00:00Z</updated>
<id>https://example.com/articles/my-article/</id>
</entry>
</feed>
```

Content-Type: `application/atom+xml; charset=utf-8`

---

## Podcast Support (Enclosures)

Add enclosure methods to your feed for podcast/media feeds:

```python
class PodcastFeed(Feed):
    def title(self):
        return "My Podcast"

    def link(self):
        return "https://example.com/podcast/"

    def description(self):
        return "Weekly technology podcast."

    def items(self):
        return Episode.objects.order_by("-published_at")[:50]

    def item_title(self, item):
        return item.title

    def item_description(self, item):
        return item.description

    def item_link(self, item):
        return f"https://example.com/podcast/{item.slug}/"

    def item_pubdate(self, item):
        return item.published_at

    def item_enclosure_url(self, item):
        return item.audio_url

    def item_enclosure_length(self, item):
        return item.file_size

    def item_enclosure_mime_type(self, item):
        return "audio/mpeg"
```

Generates:

```xml
<enclosure url="https://example.com/audio/ep42.mp3"
           length="48372891"
           type="audio/mpeg"/>
```

---

## Categories and Authors

```python
class CategorizedFeed(Feed):
    def item_author_name(self, item):
        return item.author.name

    def item_categories(self, item):
        return [tag.name for tag in item.tags]
```

RSS output:

```xml
<author>Jane Smith</author>
<category>Technology</category>
<category>Python</category>
```

---

## Custom GUIDs

By default, the item link is used as the GUID. Override for custom behavior:

```python
class MyFeed(Feed):
    def item_guid(self, item):
        return f"urn:uuid:{item.uuid}"

    def item_guid_is_permalink(self, item):
        return False  # GUID is not a URL
```

---

## Feed View

Wire a feed into your router:

```python
from hyperdjango.syndication import feed_view


@app.route("/feed/")
def rss_feed(request):
    return feed_view(request, LatestArticlesFeed)


@app.route("/feed/atom/")
def atom_feed(request):
    return feed_view(request, MyAtomFeed)
```

The `feed_view` function:

1. Instantiates the feed class.
2. Renders RSS 2.0 or Atom 1.0 based on `feed_type`.
3. Computes an MD5 ETag from the XML output.
4. Returns 304 Not Modified if the client's `If-None-Match` header matches.

---

## ETag Caching

Every feed response includes an `ETag` header. Clients that send `If-None-Match`
with a matching ETag receive a 304 response with no body, saving bandwidth.

```
ETag: "a1b2c3d4e5f6..."
```

---

## Django Migration Guide

| Django                                  | HyperDjango                     |
| --------------------------------------- | ------------------------------- |
| `django.contrib.syndication.views.Feed` | `hyperdjango.syndication.Feed`  |
| `Rss201rev2Feed` / `Atom1Feed`          | `feed_type = "rss"` / `"atom"`  |
| URL pattern with feed view              | `feed_view(request, FeedClass)` |
| `item_enclosure_url` etc.               | Same API                        |

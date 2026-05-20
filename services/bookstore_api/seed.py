"""Bookstore API seed data."""

import random

from hyperdjango.auth import seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from services.bookstore_api.app import Author, Book, Category, Review

CATEGORIES = [
    ("programming", "Programming", "Books about software development and coding"),
    ("databases", "Databases", "Database design, optimization, and administration"),
    (
        "web-dev",
        "Web Development",
        "Frontend, backend, and full-stack web technologies",
    ),
    ("devops", "DevOps", "Infrastructure, CI/CD, containers, and cloud"),
    (
        "security",
        "Security",
        "Application security, cryptography, and penetration testing",
    ),
    (
        "ai-ml",
        "AI & Machine Learning",
        "Artificial intelligence, deep learning, and data science",
    ),
    (
        "architecture",
        "Software Architecture",
        "System design, patterns, and distributed systems",
    ),
    ("career", "Career", "Professional growth, leadership, and tech culture"),
]

AUTHORS = [
    (
        "Martin Fowler",
        "Software architect and author of refactoring books",
        "https://martinfowler.com",
    ),
    ("Robert C. Martin", "Clean code advocate and software craftsman", ""),
    (
        "Sandi Metz",
        "OOP design expert and Ruby community leader",
        "https://sandimetz.com",
    ),
    ("Kent Beck", "Extreme programming pioneer and TDD creator", ""),
    ("Eric Evans", "Domain-driven design originator", ""),
    ("Sam Newman", "Microservices and distributed systems author", ""),
    ("Brendan Burns", "Kubernetes co-creator and distributed systems expert", ""),
    ("Alex Xu", "System design interview expert", ""),
    ("Martin Kleppmann", "Distributed data systems researcher", ""),
    ("Charity Majors", "Observability advocate and software engineer", ""),
    ("Liz Rice", "Container security expert and eBPF author", ""),
    ("Kelsey Hightower", "Cloud native advocate and Kubernetes champion", ""),
    (
        "Julia Evans",
        "Systems programming explainer and zine creator",
        "https://jvns.ca",
    ),
    ("Tanya Janca", "Application security educator", "https://shehackspurple.ca"),
    ("Alice Zhao", "Data science communicator and educator", ""),
    ("Aurélien Géron", "Machine learning practitioner and author", ""),
    ("Nicole Forsgren", "DevOps research scientist and DORA co-author", ""),
    ("Gene Kim", "DevOps and IT revolution thought leader", ""),
    ("Camille Fournier", "Engineering management and systems architecture", ""),
    ("Will Larson", "Engineering leadership and staff engineering", ""),
    ("Sanjay Ghemawat", "Distributed systems and storage engineer", ""),
    ("Brian Goetz", "Java language architect and concurrency expert", ""),
    ("Dmitry Jemerov", "Kotlin language designer and JetBrains developer", ""),
    ("Bryan Cantrill", "Systems software engineer and DTrace creator", ""),
    ("Jessie Frazelle", "Container runtime and systems programmer", ""),
]

# (title, description, category_slug, author_idx, isbn_suffix, pages, published, featured)
BOOKS = [
    (
        "Refactoring: Improving the Design of Existing Code",
        "The definitive guide to restructuring code without changing its behavior.",
        "programming",
        0,
        "001",
        448,
        True,
        True,
    ),
    (
        "Clean Code: A Handbook of Agile Software Craftsmanship",
        "Principles, patterns, and practices for writing clean code.",
        "programming",
        1,
        "002",
        464,
        True,
        True,
    ),
    (
        "Practical Object-Oriented Design in Ruby",
        "An elegant approach to OOP design with Ruby examples.",
        "programming",
        2,
        "003",
        272,
        True,
        False,
    ),
    (
        "Test-Driven Development: By Example",
        "The foundational TDD methodology book.",
        "programming",
        3,
        "004",
        240,
        True,
        False,
    ),
    (
        "Domain-Driven Design: Tackling Complexity in the Heart of Software",
        "Strategic and tactical patterns for complex domains.",
        "architecture",
        4,
        "005",
        560,
        True,
        True,
    ),
    (
        "Building Microservices",
        "Designing fine-grained systems with microservice architecture.",
        "architecture",
        5,
        "006",
        280,
        True,
        False,
    ),
    (
        "Designing Distributed Systems",
        "Patterns and paradigms for scalable, reliable services.",
        "architecture",
        6,
        "007",
        166,
        True,
        False,
    ),
    (
        "System Design Interview",
        "Step-by-step framework for large-scale system design.",
        "architecture",
        7,
        "008",
        322,
        True,
        True,
    ),
    (
        "Designing Data-Intensive Applications",
        "The big ideas behind reliable, scalable data systems.",
        "databases",
        8,
        "009",
        616,
        True,
        True,
    ),
    (
        "Observability Engineering",
        "From monitoring to observability in modern distributed systems.",
        "devops",
        9,
        "010",
        318,
        True,
        False,
    ),
    (
        "Container Security",
        "Fundamental technology concepts for securing containers and cloud native.",
        "security",
        10,
        "011",
        198,
        True,
        False,
    ),
    (
        "Kubernetes Up & Running",
        "Dive into the future of infrastructure.",
        "devops",
        11,
        "012",
        278,
        True,
        False,
    ),
    (
        "How DNS Works",
        "A fun and colorful explanation of the DNS protocol.",
        "web-dev",
        12,
        "013",
        28,
        True,
        False,
    ),
    (
        "Alice and Bob Learn Application Security",
        "A comprehensive guide to secure software development.",
        "security",
        13,
        "014",
        320,
        True,
        False,
    ),
    (
        "Data Science from Scratch",
        "Building data science skills from the ground up with Python.",
        "ai-ml",
        14,
        "015",
        406,
        True,
        False,
    ),
    (
        "Hands-On Machine Learning",
        "Practical ML with Scikit-Learn, Keras, and TensorFlow.",
        "ai-ml",
        15,
        "016",
        856,
        True,
        True,
    ),
    (
        "Accelerate: The Science of DevOps",
        "Research-backed insights on high-performing tech organizations.",
        "devops",
        16,
        "017",
        288,
        True,
        False,
    ),
    (
        "The Phoenix Project",
        "A novel about IT, DevOps, and helping your business win.",
        "devops",
        17,
        "018",
        382,
        True,
        True,
    ),
    (
        "The Manager's Path",
        "A guide for tech leaders navigating growth and change.",
        "career",
        18,
        "019",
        244,
        True,
        False,
    ),
    (
        "An Elegant Puzzle: Systems of Engineering Management",
        "Approaches to the hardest parts of engineering management.",
        "career",
        19,
        "020",
        288,
        True,
        False,
    ),
    (
        "I Heart Logs",
        "Event data, stream processing, and data integration.",
        "databases",
        20,
        "021",
        118,
        True,
        False,
    ),
    (
        "Java Concurrency in Practice",
        "The definitive guide to concurrent programming in Java.",
        "programming",
        21,
        "022",
        384,
        True,
        False,
    ),
    (
        "Kotlin in Action",
        "Practical Kotlin for JVM development.",
        "programming",
        22,
        "023",
        360,
        True,
        False,
    ),
    (
        "Oxide and Friends: Systems Software",
        "Essays on building systems software from first principles.",
        "programming",
        23,
        "024",
        280,
        True,
        False,
    ),
    (
        "Containers from Scratch",
        "Understanding Linux containers by building one.",
        "devops",
        24,
        "025",
        190,
        True,
        False,
    ),
    (
        "Advanced Refactoring Patterns",
        "Next-level refactoring for large codebases.",
        "programming",
        0,
        "026",
        320,
        False,
        False,
    ),
    (
        "Clean Architecture: Extended Edition",
        "Architecture principles for the modern era.",
        "architecture",
        1,
        "027",
        400,
        False,
        False,
    ),
    (
        "Beyond Microservices",
        "When microservices aren't the answer.",
        "architecture",
        5,
        "028",
        260,
        False,
        False,
    ),
    (
        "Distributed Transactions Demystified",
        "Consensus, sagas, and eventual consistency.",
        "databases",
        8,
        "029",
        350,
        False,
        False,
    ),
    (
        "Zero-Trust Security in Practice",
        "Implementing zero-trust for real organizations.",
        "security",
        13,
        "030",
        290,
        False,
        False,
    ),
    (
        "The Art of PostgreSQL",
        "Mastering the world's most advanced open-source database.",
        "databases",
        8,
        "031",
        438,
        True,
        False,
    ),
    (
        "SQL Performance Explained",
        "Visual guide to query optimization.",
        "databases",
        8,
        "032",
        204,
        True,
        False,
    ),
    (
        "High Performance Browser Networking",
        "What every developer should know about networking.",
        "web-dev",
        7,
        "033",
        400,
        True,
        False,
    ),
    (
        "Web Scalability for Startup Engineers",
        "Practical scalability patterns.",
        "web-dev",
        7,
        "034",
        310,
        True,
        False,
    ),
    (
        "Full Stack Python",
        "Complete guide to Python web frameworks.",
        "web-dev",
        12,
        "035",
        380,
        True,
        False,
    ),
    (
        "Production-Ready Microservices",
        "Building standardized microservices.",
        "architecture",
        5,
        "036",
        172,
        True,
        False,
    ),
    (
        "Release It!",
        "Design and deploy production-ready software.",
        "devops",
        17,
        "037",
        376,
        True,
        False,
    ),
    (
        "The DevOps Handbook",
        "How to create world-class agility and reliability.",
        "devops",
        17,
        "038",
        480,
        True,
        True,
    ),
    (
        "Site Reliability Engineering",
        "How Google runs production systems.",
        "devops",
        11,
        "039",
        550,
        True,
        False,
    ),
    (
        "Deep Learning with Python",
        "Neural networks with Keras.",
        "ai-ml",
        15,
        "040",
        504,
        True,
        False,
    ),
    (
        "Natural Language Processing",
        "Building NLP systems from scratch.",
        "ai-ml",
        14,
        "041",
        340,
        True,
        False,
    ),
    (
        "Reinforcement Learning: An Introduction",
        "Theory and practice of RL.",
        "ai-ml",
        15,
        "042",
        526,
        True,
        False,
    ),
    (
        "Threat Modeling",
        "Designing for security from the start.",
        "security",
        13,
        "043",
        288,
        True,
        False,
    ),
    (
        "Cryptography Engineering",
        "Design principles for practical cryptography.",
        "security",
        10,
        "044",
        352,
        True,
        False,
    ),
    (
        "Staff Engineer: Leadership Beyond the Management Track",
        "Guides for staff-plus engineers.",
        "career",
        19,
        "045",
        296,
        True,
        False,
    ),
    (
        "The Staff Engineer's Path",
        "Technical leadership and organizational influence.",
        "career",
        18,
        "046",
        320,
        True,
        False,
    ),
    (
        "Team Topologies",
        "Organizing business and technology teams for fast flow.",
        "career",
        16,
        "047",
        240,
        True,
        False,
    ),
    (
        "Effective Java",
        "Best practices for the Java platform.",
        "programming",
        21,
        "048",
        412,
        True,
        False,
    ),
    (
        "Programming Rust",
        "Fast, safe systems development.",
        "programming",
        23,
        "049",
        622,
        True,
        False,
    ),
    (
        "The Rust Programming Language",
        "Comprehensive guide to Rust.",
        "programming",
        24,
        "050",
        560,
        True,
        True,
    ),
    (
        "Advanced Kubernetes Patterns",
        "Beyond the basics of container orchestration.",
        "devops",
        6,
        "051",
        380,
        False,
        False,
    ),
    (
        "ML Ops at Scale",
        "Production machine learning infrastructure.",
        "ai-ml",
        16,
        "052",
        310,
        False,
        False,
    ),
    (
        "Event-Driven Architecture",
        "Designing reactive distributed systems.",
        "architecture",
        20,
        "053",
        340,
        False,
        False,
    ),
    (
        "The Art of Zig",
        "Systems programming with Zig.",
        "programming",
        23,
        "054",
        280,
        False,
        False,
    ),
    (
        "WebAssembly in Action",
        "Running code everywhere with Wasm.",
        "web-dev",
        24,
        "055",
        290,
        False,
        False,
    ),
    (
        "Database Internals",
        "A deep dive into how databases work.",
        "databases",
        20,
        "056",
        350,
        True,
        False,
    ),
    (
        "Streaming Systems",
        "The what, where, when, and how of large-scale data processing.",
        "databases",
        20,
        "057",
        350,
        True,
        False,
    ),
    (
        "Learning HTTP/2",
        "A practical guide to HTTP/2.",
        "web-dev",
        12,
        "058",
        150,
        True,
        False,
    ),
    (
        "GraphQL in Action",
        "Practical GraphQL for full-stack development.",
        "web-dev",
        7,
        "059",
        370,
        True,
        False,
    ),
    (
        "Fundamentals of Software Architecture",
        "Core concepts every architect should know.",
        "architecture",
        0,
        "060",
        422,
        True,
        False,
    ),
    (
        "Software Architecture: The Hard Parts",
        "Trade-off analysis for distributed architectures.",
        "architecture",
        5,
        "061",
        460,
        True,
        False,
    ),
    (
        "Python for Data Analysis",
        "Data wrangling with Pandas.",
        "ai-ml",
        14,
        "062",
        544,
        True,
        False,
    ),
    (
        "Feature Engineering for Machine Learning",
        "Principles and techniques for data scientists.",
        "ai-ml",
        14,
        "063",
        218,
        True,
        False,
    ),
    (
        "The Web Application Hacker's Handbook",
        "Finding and exploiting security flaws.",
        "security",
        10,
        "064",
        912,
        True,
        False,
    ),
    (
        "Practical Cloud Security",
        "A guide for securing cloud infrastructure.",
        "security",
        10,
        "065",
        276,
        True,
        False,
    ),
    (
        "Debugging Teams",
        "Better productivity through collaboration.",
        "career",
        11,
        "066",
        148,
        True,
        False,
    ),
    (
        "Thinking in Systems",
        "A primer on systems thinking.",
        "career",
        9,
        "067",
        240,
        True,
        False,
    ),
    (
        "Modern Java in Action",
        "Lambdas, streams, functional programming.",
        "programming",
        21,
        "068",
        592,
        True,
        False,
    ),
    (
        "Head First Design Patterns",
        "Visual guide to design patterns.",
        "programming",
        2,
        "069",
        694,
        True,
        True,
    ),
    (
        "Fluent Python",
        "Writing idiomatic Python code.",
        "programming",
        12,
        "070",
        792,
        True,
        False,
    ),
    (
        "Learning Go",
        "Idiomatic Go for experienced developers.",
        "programming",
        22,
        "071",
        375,
        True,
        False,
    ),
    (
        "Concurrency in Go",
        "Tools and techniques for Go developers.",
        "programming",
        22,
        "072",
        238,
        True,
        False,
    ),
    (
        "Pro Git",
        "Everything you need to know about Git.",
        "devops",
        6,
        "073",
        456,
        True,
        False,
    ),
    (
        "Infrastructure as Code",
        "Managing servers in the cloud.",
        "devops",
        6,
        "074",
        362,
        True,
        False,
    ),
    (
        "Terraform: Up & Running",
        "Writing infrastructure as code.",
        "devops",
        11,
        "075",
        322,
        True,
        False,
    ),
]

REVIEW_TEMPLATES = [
    (5, "Excellent book! Changed how I think about {topic}. Highly recommended."),
    (5, "One of the best {topic} books I've read. Clear, practical, and thorough."),
    (4, "Very solid {topic} reference. Well-written with great examples."),
    (4, "Good coverage of {topic}. A few chapters could go deeper, but overall great."),
    (4, "Practical and well-organized. The {topic} examples are particularly helpful."),
    (3, "Decent introduction to {topic}. Better suited for beginners."),
    (3, "Covers {topic} well but feels slightly dated in some sections."),
    (3, "Good foundations on {topic}, but I wanted more advanced content."),
    (2, "Somewhat useful for {topic}, but the code examples had errors."),
    (1, "Not what I expected for {topic}. Too basic for experienced developers."),
]

REVIEWER_NAMES = [
    "Alex Chen",
    "Jordan Smith",
    "Sam Williams",
    "Taylor Brown",
    "Morgan Davis",
    "Casey Johnson",
    "Riley Anderson",
    "Quinn Thomas",
    "Avery Garcia",
    "Cameron Martinez",
    "Hayden Robinson",
    "Dakota Lee",
    "Parker Hall",
    "Blake Turner",
    "Reese Cooper",
    "Drew Mitchell",
    "Kai Patel",
    "Sage Nguyen",
    "River Kim",
    "Finley Wang",
]

CATEGORY_TOPICS = {
    "programming": "software development",
    "databases": "database engineering",
    "web-dev": "web development",
    "devops": "DevOps practices",
    "security": "security engineering",
    "ai-ml": "machine learning",
    "architecture": "system architecture",
    "career": "engineering leadership",
}


async def run(db=None):
    """Seed the bookstore database."""
    if db is None:
        db = get_db()

    existing = await Book.objects.count()
    if existing:
        logger.info("  Bookstore already seeded ({n} books). Skipping.", n=existing)
        return

    logger.info("  Seeding bookstore data...")

    # Users + RBAC groups
    checker = PermissionChecker(db)
    await checker.ensure_tables()

    # Ensure RBAC groups: staff (full access) inherits from reader (view-only)
    reader_group = await checker.ensure_group("reader")
    staff_group = await checker.ensure_group("staff")

    # Create model-level permissions for books
    await checker.create_default_permissions("book", "book")
    await checker.create_default_permissions("author", "author")
    await checker.create_default_permissions("category", "category")
    await checker.create_default_permissions("review", "review")

    # Reader group: view-only permissions
    for model in ("book", "author", "category", "review"):
        await checker.grant_group_perm(reader_group.id, f"view_{model}", model)

    # Staff group: full CRUD (inherits reader's view perms, adds add/change/delete)
    for model in ("book", "author", "category", "review"):
        for action in ("add", "change", "delete"):
            await checker.grant_group_perm(staff_group.id, f"{action}_{model}", model)

    # Create users and assign to groups
    admin = await checker.create_user("admin", seed_password("admin"), is_staff=True)
    await checker.add_user_to_group(admin.id, staff_group.id)

    reader = await checker.create_user("reader", seed_password("reader"))
    await checker.add_user_to_group(reader.id, reader_group.id)

    logger.info("    2 users + 2 RBAC groups created (admin→staff, reader→reader)")

    # Categories
    cat_map: dict[str, int] = {}
    for slug, name, desc in CATEGORIES:
        cat = Category(name=name, slug=slug, description=desc)
        await cat.save()
        cat_map[slug] = cat.id
    logger.info("    {n} categories created", n=len(CATEGORIES))

    # Authors
    author_objs: list[Author] = []
    for name, bio, website in AUTHORS:
        a = Author(name=name, bio=bio, website=website)
        await a.save()
        author_objs.append(a)
    logger.info("    {n} authors created", n=len(AUTHORS))

    # Books
    rng = random.Random(42)
    book_cat_map: dict[int, str] = {}  # book_id → cat_slug for reviews
    for (
        title,
        desc,
        cat_slug,
        author_idx,
        isbn_suffix,
        pages,
        published,
        featured,
    ) in BOOKS:
        isbn = f"978-0-{isbn_suffix}-00000-{isbn_suffix[-1]}"
        price = f"{rng.randint(19, 79)}.{rng.choice(['95', '99', '00'])}"
        book = Book(
            title=title,
            isbn=isbn,
            description=desc,
            price=price,
            pages=pages,
            published=published,
            featured=featured,
            author_id=author_objs[author_idx].id,
            category_id=cat_map[cat_slug],
        )
        await book.save()
        if published:
            book_cat_map[book.id] = cat_slug
    logger.info("    {n} books created", n=len(BOOKS))

    # Reviews — only for published books
    review_count = 0
    for book_id, cat_slug in book_cat_map.items():
        num_reviews = rng.randint(1, 5)
        reviewers = rng.sample(REVIEWER_NAMES, min(num_reviews, len(REVIEWER_NAMES)))
        topic = CATEGORY_TOPICS.get(cat_slug, "technology")

        for reviewer_name in reviewers[:num_reviews]:
            rating, comment_tpl = rng.choice(REVIEW_TEMPLATES)
            review = Review(
                book_id=book_id,
                reviewer_name=reviewer_name,
                rating=rating,
                comment=comment_tpl.format(topic=topic),
            )
            await review.save()
            review_count += 1

    logger.info("    {r} reviews created", r=review_count)
    logger.info(
        "  Bookstore seeded: {b} books, {a} authors, {c} categories, {r} reviews",
        b=len(BOOKS),
        a=len(AUTHORS),
        c=len(CATEGORIES),
        r=review_count,
    )

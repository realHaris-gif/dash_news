# 📰 Dash news

> An automated, high-density retro broadsheet newspaper engine built with Python, Flask, and AsyncIO. Delivers curated global tech news, offensive cybersecurity disclosures, Formula 1 telemetry, computer science curricula, and real-time academic course management via the Google Classroom API.

---

## ⚡ Overview

**The Daily Dispatch** transforms distributed RSS feeds, technical blogs, preprint research archives, live sports APIs, and academic coursework into a unified, 19th-century-styled front-page broadsheet. Designed for high information density and zero algorithmic clutter, it renders an authentic morning newspaper layout optimized for desktop reading, mobile browsers, and physical print/PDF exports.

---

## ✨ Key Features

### 🏛️ Vintage Broadsheet Design
* **Authentic Typography:** Uses Google Web Fonts (`Cinzel Decorative`, `Playfair Display`, and `Old Standard TT`) with classic dual-column justified typesetting and prominent drop caps.
* **Period Aesthetic:** Features distressed newsprint paper grain, double-line column borders, editorial ear banners, and physical page-fold separators.
* **Print & Export Ready:** Clean CSS print styles format the broadsheet onto physical single-sheet paper or exported PDF documents with browser controls hidden.

### 🎓 Academic Noticeboard (Google Classroom API)
* **Section-Targeted Filtering:** Scans enrolled courses tailored to specific section identifiers (Section 5E).
* **Coursework Priority Queue:** Parses active, unsubmitted coursework created in the last 30 days and ranks items by deadline urgency (`OVERDUE`, `PRIORITY`, `MEDIUM`, `UPCOMING`).
* **Quiz & Evaluation Tracking:** Automatically identifies announcements containing assessment notices, extracting dates, times, and syllabus scope.
* **Past Date Expiry:** Dynamically prunes past viva and oral evaluation notices so only upcoming sessions remain on the board.

### 💻 The Programmers' Desk & Tech Wire
* **Front-Page Curations:** Asynchronous feed pipelines pulling from Hacker News (Algolia Search API), Lobsters, LWN (Linux Weekly News), and Ars Technica.
* **Silicon & AI Agents:** Real-time updates from TechCrunch, automatically backed by OpenGraph scrapers to extract high-resolution editorial imagery.
* **Cyber Defense & Zero-Days:** Live vulnerabilities, CVE disclosures, and threat actor reporting sourced directly from The Hacker News.

### 🔬 Research Desk (arXiv cs.* Preprints)
* **Background Polling Daemon:** Dedicated worker thread querying arXiv APIs with rate-limit compliance.
* **Four Track Rotation:** Pulls submissions across:
  * Machine Learning (`cs.LG`)
  * Cryptography & Security (`cs.CR`)
  * Distributed & Parallel Computing (`cs.DC`)
  * Programming Languages (`cs.PL`)

### 🧠 Rotating Knowledge Vaults
* **System Design School:** Full architectures (Distributed Caches, Rate Limiters, URL Shorteners, News Feeds) covering core topologies, trade-offs, back-of-the-envelope capacity estimations, and interview questions.
* **Computer Science Deep Dives:** Rotating technical dossiers covering FlashAttention kernel fusion, Memory Management (TLB, Virtual Memory), Exploit Mitigations (ROP, ASLR, Canaries), and Cryptographic Primitives (BLAKE3, Merkle-Damgård).
* **Craft & Practice:** Daily terminal tricks (`strace`, `lsof`, `perf`, `git bisect`), classic seminal papers (Ritchie, Lamport, Vaswani), interview brain teasers, and historical etymological lexicons.

### 🏎️ The Sports Desk
* **Formula 1 Telemetry:** Integrates the Jolpica F1 API (Ergast-compatible) to display active Driver Championship standings, circuit metadata, session breakdowns, and a live client-side countdown timer to the next Grand Prix start.
* **Football & Tennis:** Sourced from BBC Sport, The Guardian, ESPN, and ATP Tour wires.

---

## 🛠️ Architecture & Tech Stack

* **Backend Framework:** Python 3.10+, Flask
* **Concurrency:** `asyncio`, `aiohttp` (concurrent feed and image resolution)
* **Feed & Data Parsing:** `feedparser`, `beautifulsoup4`, `urllib`
* **APIs:** Google Classroom REST API v1, Jolpica / Ergast F1 API, Algolia HN Search API, arXiv Query API
* **Authentication:** OAuth 2.0 with offline access tokens (`google-auth-oauthlib`, `google-api-python-client`)
* **Persistence & Cache:** SQLite3 archive layer, memory TTL caching (30-minute revalidation)
* **Frontend:** Jinja2 server-side rendering, CSS Grid & Flexbox, SVG procedural noise filters

---

## 📁 Project Structure

```text
daily-broadsheet/
├── app.py                      # Core Flask application, scrapers, API pipelines, and HTML template
├── credentials.json            # Google Cloud OAuth 2.0 Client credentials (User provided)
├── token.json                  # Generated OAuth 2.0 user session token (Auto-generated)
├── broadsheet_archive.db       # SQLite article cache & deduplication store
├── requirements.txt            # Python production dependencies
├── Procfile                    # Deployment entry point for PaaS (Render / Railway)
└── README.md                   # Project documentation

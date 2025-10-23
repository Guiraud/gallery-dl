#!/usr/bin/env python3
"""Génération de pages HTML locales pour les exports Twitter/X de gallery-dl.

Ce module parcourt les fichiers de métadonnées produits par l'option
``--write-metadata`` et reconstruit un fil consultable hors-ligne.

Il peut être utilisé de deux façons :

* via l'API Python : :func:`build_indexes` ou :func:`build_index`;
* en ligne de commande par le script ``scripts/build_twitter_html.py``.
"""

import argparse
import html
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

URL_PATTERN = re.compile(r"(https?://[^\s]+)")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".jfif"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".m4v"}


@dataclass
class Attachment:
    rel_path: str
    media_type: str  # "image" | "video" | "file"
    alt_text: Optional[str] = None


@dataclass
class Tweet:
    tweet_id: str
    author_screen_name: str
    author_display_name: str
    content: str
    date_raw: Optional[str]
    lang: Optional[str]
    permalink: str
    hashtags: List[str] = field(default_factory=list)
    mentions: List[str] = field(default_factory=list)
    stats: Dict[str, Optional[int]] = field(default_factory=dict)
    attachments: List[Attachment] = field(default_factory=list)

    @property
    def date(self) -> Optional[datetime]:
        if not self.date_raw:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return datetime.strptime(self.date_raw, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(self.date_raw)
        except ValueError:
            return None


def build_indexes(
    root: Path,
    *,
    recursive: bool = True,
    overwrite: bool = True,
) -> List[Path]:
    """Build index.html files for all twitter export folders under *root*."""
    root = root.expanduser().resolve()
    if recursive:
        metadata_iter = root.rglob("*.json")
    else:
        metadata_iter = root.glob("*.json")

    grouped: Dict[Path, List[Tuple[Path, dict]]] = defaultdict(list)
    for meta_path in metadata_iter:
        if not meta_path.is_file():
            continue
        data = _load_json(meta_path)
        if data is None or data.get("category") != "twitter":
            continue
        grouped[meta_path.parent].append((meta_path, data))

    created: List[Path] = []
    for directory, entries in grouped.items():
        index_path = build_index(directory, entries=entries, overwrite=overwrite)
        if index_path:
            created.append(index_path)
    return created


def build_index_from_jsonl(
    json_file: Path,
    *,
    output: Optional[Path] = None,
) -> Optional[Path]:
    """Build a single HTML timeline from a JSONL file produced by --dump-json."""
    json_file = json_file.expanduser().resolve()
    if not json_file.is_file():
        raise FileNotFoundError(json_file)

    entries: List[Tuple[Path, dict]] = []
    log_messages: List[str] = []
    with json_file.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith(")") or line.startswith("[") and "]" in line:
                log_messages.append(line)
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                log_messages.append(line)
                continue
            if isinstance(data, str):
                log_messages.append(data)
                continue
            if data.get("category") != "twitter":
                continue
            entries.append((json_file, data))

    if not entries:
        for message in log_messages:
            print(message)
        return None

    output_path = (output or json_file.with_suffix(".html")).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tweets = _tweets_from_metadata(output_path.parent, entries)
    if not tweets:
        return None

    all_tags = sorted({tag.lower() for tweet in tweets for tag in tweet.hashtags})
    label = (
        entries[0][1].get("search")
        or entries[0][1].get("user", {}).get("name")
        or output_path.stem
    )
    html_content = _render_timeline(label, tweets, all_tags)
    output_path.write_text(html_content, encoding="utf-8")
    for message in log_messages:
        print(message)
    return output_path


def build_index(
    directory: Path,
    *,
    entries: Optional[Sequence[Tuple[Path, dict]]] = None,
    overwrite: bool = True,
) -> Optional[Path]:
    """Build a single index.html in *directory* from the provided metadata."""
    directory = directory.expanduser().resolve()
    if entries is None:
        collected: List[Tuple[Path, dict]] = []
        for path in directory.glob("*.json"):
            data = _load_json(path)
            if data and data.get("category") == "twitter":
                collected.append((path, data))
        entries = collected

    tweets = _tweets_from_metadata(directory, entries)
    if not tweets:
        return None

    index_path = directory / "index.html"
    if index_path.exists() and not overwrite:
        return index_path

    all_tags = sorted({tag.lower() for tweet in tweets for tag in tweet.hashtags})
    html_content = _render_timeline(directory.name, tweets, all_tags)
    index_path.write_text(html_content, encoding="utf-8")
    return index_path


def _load_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _tweets_from_metadata(
    directory: Path,
    entries: Sequence[Tuple[Path, dict]],
) -> List[Tweet]:
    tweets: Dict[str, Tweet] = {}
    attachments: Dict[str, Dict[int, Attachment]] = defaultdict(dict)

    for meta_path, data in entries:
        tweet_id = _coerce_id(data)
        if not tweet_id:
            continue

        author = data.get("author") or {}
        screen_name = author.get("name") or data.get("user", {}).get("name") or "unknown"
        display_name = author.get("nick") or author.get("name") or screen_name
        permalink = data.get("tweet_url") or f"https://x.com/{screen_name}/status/{tweet_id}"
        hashtags = data.get("hashtags") or []
        if isinstance(hashtags, str):
            hashtags = [hashtags]
        mentions = data.get("mentions") or []
        if isinstance(mentions, list):
            mentions = [mention.get("name") or "" for mention in mentions if isinstance(mention, dict)]

        tweet = tweets.get(tweet_id)
        if not tweet:
            tweet = Tweet(
                tweet_id=tweet_id,
                author_screen_name=screen_name,
                author_display_name=display_name,
                content=data.get("content") or data.get("text") or "",
                date_raw=_stringify(data.get("date")),
                lang=data.get("lang"),
                permalink=permalink,
                hashtags=[tag for tag in hashtags if tag],
                mentions=[mention for mention in mentions if mention],
                stats={
                    "replies": _safe_int(data.get("reply_count")),
                    "retweets": _safe_int(data.get("retweet_count")),
                    "quotes": _safe_int(data.get("quote_count")),
                    "likes": _safe_int(data.get("favorite_count")),
                    "bookmarks": _safe_int(data.get("bookmark_count")),
                    "views": _safe_int(data.get("view_count")),
                },
            )
            tweets[tweet_id] = tweet
        else:
            if not tweet.content:
                tweet.content = data.get("content") or data.get("text") or tweet.content
            if not tweet.date_raw:
                tweet.date_raw = _stringify(data.get("date"))

        attachment = _attachment_from_metadata(directory, meta_path, data)
        if attachment:
            num = _safe_int(data.get("num")) or len(attachments[tweet_id]) + 1
            attachments[tweet_id][num] = attachment

    for tweet_id, media in attachments.items():
        tweet = tweets.get(tweet_id)
        if not tweet:
            continue
        tweet.attachments = [media[idx] for idx in sorted(media)]

    ordered = sorted(
        tweets.values(),
        key=lambda tw: (tw.date or datetime.min, tw.tweet_id),
        reverse=True,
    )
    return ordered


def _attachment_from_metadata(directory: Path, meta_path: Path, data: dict) -> Optional[Attachment]:
    candidates: List[Path] = []
    rel_hint = data.get("_path") or data.get("filepath") or ""
    if rel_hint:
        rel_candidate = Path(rel_hint)
        if rel_candidate.is_absolute():
            candidates.append(rel_candidate)
        else:
            candidates.append((directory / rel_candidate).resolve())

    filename = data.get("filename") or data.get("_filename")
    extension = data.get("extension")
    if filename:
        base = directory / filename
        candidates.append(base if base.suffix else base.with_suffix(f".{extension or ''}"))
        candidates.append(directory / f"{filename}.part")
    if meta_path.stem:
        candidates.append(directory / meta_path.stem)

    existing = next((cand for cand in candidates if cand.exists()), None)
    if not existing:
        return None

    try:
        rel_path = existing.relative_to(directory)
    except ValueError:
        rel_path = existing

    ext = existing.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        media_type = "image"
    elif ext in VIDEO_EXTENSIONS:
        media_type = "video"
    else:
        media_type = "file"

    return Attachment(rel_path=rel_path.as_posix(), media_type=media_type, alt_text=data.get("description"))


def _render_timeline(user_label: str, tweets: Sequence[Tweet], hashtags: Sequence[str]) -> str:
    stats_labels = {
        "replies": "Réponses",
        "retweets": "Retweets",
        "quotes": "Citations",
        "likes": "J'aime",
        "bookmarks": "Favoris",
        "views": "Vues",
    }

    buttons = '\n'.join(
        f'<button data-tag="{html.escape(tag)}">#{html.escape(tag)}</button>'
        for tag in hashtags
    )
    timeline = []
    for tweet in tweets:
        text_html = _format_text(tweet.content)
        date_str = tweet.date.strftime("%d %b %Y %H:%M") if tweet.date else (tweet.date_raw or "")
        tag_attr = ",".join(tweet.hashtags)
        media_html = "".join(_render_attachment(att) for att in tweet.attachments)
        stats_chunks: List[str] = []
        for name, label in stats_labels.items():
            value = tweet.stats.get(name)
            if value:
                stats_chunks.append(f'<span class="stat stat-{name}">{label} : {value:,}</span>')
        stats_html = " ".join(stats_chunks)
        hashtag_html = " ".join(
            f'<a href="https://x.com/hashtag/{html.escape(tag)}" target="_blank">#{html.escape(tag)}</a>'
            for tag in tweet.hashtags
        )
        mentions_html = " ".join(
            f'<a href="https://x.com/{html.escape(mention)}" target="_blank">@{html.escape(mention)}</a>'
            for mention in tweet.mentions
        )
        search_terms = " ".join(
            [
                tweet.content,
                " ".join(f"#{tag}" for tag in tweet.hashtags),
                " ".join(f"@{mention}" for mention in tweet.mentions),
            ]
        ).lower()
        card = f"""
        <article class="tweet" data-hashtags="{html.escape(tag_attr)}" data-search="{html.escape(search_terms)}">
          <header class="tweet-header">
            <div class="avatar-circle">{html.escape(tweet.author_display_name[:1].upper())}</div>
            <div>
              <div class="author">
                <span class="display-name">{html.escape(tweet.author_display_name)}</span>
                <span class="handle">@{html.escape(tweet.author_screen_name)}</span>
              </div>
              <a class="timestamp" href="{html.escape(tweet.permalink)}" target="_blank">{html.escape(date_str)}</a>
            </div>
          </header>
          <div class="tweet-body" lang="{html.escape(tweet.lang or '')}">{text_html}</div>
          {"<div class='tweet-mentions'>" + mentions_html + "</div>" if mentions_html else ""}
          {"<div class='tweet-hashtags'>" + hashtag_html + "</div>" if hashtag_html else ""}
          {"<div class='attachments'>" + media_html + "</div>" if media_html else ""}
          {"<footer class='tweet-stats'>" + stats_html + "</footer>" if stats_html else ""}
        </article>
        """
        timeline.append(card)

    return f"""<!DOCTYPE html>
<html lang="fr">
  <head>
    <meta charset="utf-8">
    <title>Exports X.com – {html.escape(user_label)}</title>
    <style>
      :root {{
        color-scheme: dark;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background-color: #0f1419;
        color: #e7e9ea;
      }}
      body {{
        margin: 0;
        padding: 0 0 4rem;
      }}
      header.page-header {{
        position: sticky;
        top: 0;
        backdrop-filter: blur(12px);
        background: rgba(15, 20, 25, 0.85);
        padding: 1.2rem 1.6rem;
        border-bottom: 1px solid rgba(231, 233, 234, 0.2);
      }}
      header.page-header h1 {{
        margin: 0 0 0.4rem;
        font-size: 1.5rem;
        font-weight: 700;
      }}
      header.page-header p {{
        margin: 0;
        color: #8b98a5;
        font-size: 0.95rem;
      }}
      .tools {{
        display: flex;
        flex-wrap: wrap;
        gap: 0.8rem;
        align-items: center;
        margin-top: 0.9rem;
      }}
      #search-box {{
        flex: 1 1 220px;
        padding: 0.45rem 0.9rem;
        border-radius: 999px;
        border: 1px solid rgba(113, 118, 123, 0.4);
        background: rgba(15, 20, 25, 0.6);
        color: #e7e9ea;
        outline: none;
        font-size: 0.95rem;
        transition: border-color 0.2s;
      }}
      #search-box:focus {{
        border-color: rgba(29, 155, 240, 0.7);
      }}
      .filters {{
        display: flex;
        gap: 0.6rem;
        flex-wrap: wrap;
      }}
      .filters button {{
        border: 1px solid rgba(113, 118, 123, 0.4);
        border-radius: 999px;
        padding: 0.2rem 0.9rem;
        background: transparent;
        color: #e7e9ea;
        cursor: pointer;
        transition: background 0.2s;
      }}
      .filters button.active,
      .filters button:hover {{
        background: rgba(29, 155, 240, 0.2);
        border-color: rgba(29, 155, 240, 0.7);
      }}
      main.timeline {{
        max-width: 720px;
        margin: 0 auto;
        padding: 1rem 1.2rem;
        display: flex;
        flex-direction: column;
        gap: 1rem;
      }}
      article.tweet {{
        border: 1px solid rgba(113, 118, 123, 0.4);
        border-radius: 16px;
        padding: 1rem;
        background: rgba(15, 20, 25, 0.7);
        display: flex;
        flex-direction: column;
        gap: 0.75rem;
      }}
      .tweet-header {{
        display: flex;
        align-items: center;
        gap: 0.8rem;
      }}
      .avatar-circle {{
        width: 48px;
        height: 48px;
        border-radius: 50%;
        background: #1d9bf0;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 700;
        font-size: 1.2rem;
        color: #0f1419;
      }}
      .author {{
        display: flex;
        gap: 0.4rem;
        align-items: baseline;
      }}
      .display-name {{
        font-weight: 700;
      }}
      .handle {{
        color: #8b98a5;
      }}
      .timestamp {{
        color: #8b98a5;
        font-size: 0.9rem;
        text-decoration: none;
      }}
      .tweet-body {{
        white-space: pre-wrap;
        font-size: 1.05rem;
        line-height: 1.45;
      }}
      .tweet-body a {{
        color: #1d9bf0;
        text-decoration: none;
      }}
      .tweet-body a:hover {{
        text-decoration: underline;
      }}
      .tweet-hashtags, .tweet-mentions {{
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
        color: #1d9bf0;
      }}
      .attachments {{
        display: grid;
        gap: 0.6rem;
      }}
      .attachments img {{
        max-width: 100%;
        border-radius: 12px;
      }}
      .attachments video {{
        max-width: 100%;
        border-radius: 12px;
      }}
      .attachment-file {{
        color: #1d9bf0;
      }}
      footer.tweet-stats {{
        display: flex;
        flex-wrap: wrap;
        gap: 0.8rem;
        color: #8b98a5;
        font-size: 0.9rem;
      }}
      .hidden {{
        display: none !important;
      }}
    </style>
  </head>
  <body>
    <header class="page-header">
      <h1>Exports X.com – {html.escape(user_label)}</h1>
      <p>{len(tweets)} publication(s) disponibles hors ligne.</p>
      <div class="tools">
        <input type="search" id="search-box" placeholder="Rechercher dans le fil…" autocomplete="off">
        {"<div class='filters'><button data-tag='__all__' class='active'>Toutes</button>" + buttons + "</div>" if hashtags else ""}
      </div>
    </header>
    <main class="timeline">
      {''.join(timeline)}
    </main>
    <script>
      const filterButtons = document.querySelectorAll('.filters button');
      const tweets = document.querySelectorAll('article.tweet');
      const searchInput = document.querySelector('#search-box');
      let activeTag = '__all__';

      function applyFilters() {{
        const query = (searchInput?.value || '').trim().toLowerCase();
        tweets.forEach(tweet => {{
          const matchesTag = activeTag === '__all__' || (tweet.dataset.hashtags || '').split(',').filter(Boolean).includes(activeTag);
          const matchesText = !query || (tweet.dataset.search || '').includes(query);
          if (matchesTag && matchesText) {{
            tweet.classList.remove('hidden');
          }} else {{
            tweet.classList.add('hidden');
          }}
        }});
      }}

      filterButtons.forEach(btn => {{
        btn.addEventListener('click', () => {{
          filterButtons.forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          activeTag = btn.dataset.tag || '__all__';
          applyFilters();
        }});
      }});

      if (searchInput) {{
        searchInput.addEventListener('input', applyFilters);
      }}
    </script>
  </body>
</html>"""


def _render_attachment(att: Attachment) -> str:
    if att.media_type == "image":
        alt = f' alt="{html.escape(att.alt_text)}"' if att.alt_text else ""
        return f'<figure class="attachment-image"><img src="{html.escape(att.rel_path)}"{alt}></figure>'
    if att.media_type == "video":
        return (
            f'<figure class="attachment-video"><video controls preload="metadata">'
            f'<source src="{html.escape(att.rel_path)}" type="video/mp4">'
            "Votre navigateur ne peut pas lire cette vidéo hors ligne."
            "</video></figure>"
        )
    return (
        f'<div class="attachment-file"><a href="{html.escape(att.rel_path)}" download>'
        f"Télécharger {html.escape(att.rel_path)}</a></div>"
    )


def _format_text(text: str) -> str:
    if not text:
        return ""
    escaped = html.escape(text)
    linked = URL_PATTERN.sub(lambda m: f'<a href="{html.escape(m.group(1))}" target="_blank">{html.escape(m.group(1))}</a>', escaped)
    return linked.replace("\n", "<br>")


def _coerce_id(data: dict) -> Optional[str]:
    for key in ("tweet_id", "tweetid", "id", "id_str"):
        value = data.get(key)
        if value:
            return str(value)
    return None


def _stringify(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _safe_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Construire des index HTML hors-ligne pour les exports X.com",
    )
    parser.add_argument(
        "--json",
        dest="json",
        help="Fichier JSONL produit par --dump-json (optionnel)",
    )
    parser.add_argument(
        "--output",
        dest="output",
        help="Chemin du fichier HTML à générer (si --json est utilisé)",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Dossier racine contenant les téléchargements (défaut: répertoire courant)",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Ne pas traverser récursivement les sous-dossiers",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Ne pas écraser un index.html déjà présent",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    if args.json:
        result = build_index_from_jsonl(
            Path(args.json),
            output=Path(args.output) if args.output else None,
        )
        if result:
            print(f"Index généré : {result}")
            return 0
        print("Aucun tweet trouvé dans le fichier JSON.")
        return 1
    else:
        created = build_indexes(
            Path(args.directory),
            recursive=not args.no_recursive,
            overwrite=not args.no_overwrite,
        )
        if not created:
            print("Aucun index généré (vérifiez que --write-metadata est activé).")
            return 1

        print("Index générés :")
        for path in created:
            print(f"  {path}")
        return 0


if __name__ == "__main__":
    sys.exit(main())

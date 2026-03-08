"""
Post to X (Twitter) via Tweepy v2 API.

Credentials in .env:
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
"""
from __future__ import annotations

import os

from loguru import logger


def post_tweet(text: str) -> bool:
    """Post a tweet. Returns True on success."""
    api_key       = os.getenv("X_API_KEY", "").strip()
    api_secret    = os.getenv("X_API_SECRET", "").strip()
    access_token  = os.getenv("X_ACCESS_TOKEN", "").strip()
    access_secret = os.getenv("X_ACCESS_TOKEN_SECRET", "").strip()

    if not all([api_key, api_secret, access_token, access_secret]):
        logger.warning("[Reporter] X credentials not set — skipping tweet")
        return False

    try:
        import tweepy  # type: ignore

        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
        )
        resp = client.create_tweet(text=text)
        tweet_id = resp.data.get("id") if resp.data else "?"
        logger.info(f"[Reporter] Tweet posted: id={tweet_id}")
        return True
    except Exception as exc:
        logger.error(f"[Reporter] Tweet failed: {exc}")
        return False

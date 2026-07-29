"""Brief archive relay: ntfy topic -> gist -> Pages.

The analyst sandbox cannot write to the gist, so briefs reach the desk by a
different door: the scheduled task POSTs its finished brief to an unguessable
ntfy topic (E1), and every poll run reads that topic's JSON feed and
republishes anything new to the gist (E2), which the Pages mirror then serves.
That restores the brief archive and the two-most-recent-briefs continuity step
without giving the analyst any credential at all.

ntfy retains messages for ~12 hours on the free tier, and the poll's effective
cadence is ~1-3.5h (GitHub skips most 30-minute slots), so a brief posted
between polls is safely inside the retention window. Bodies over ~4KB arrive as
attachments; both forms are handled.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

INDEX_FILE = "briefs-index.json"
MAX_BRIEF_BYTES = 400_000


def relay(gist, state: dict, topic: str, http: httpx.Client) -> list[str]:
    """Pull new messages from the briefs topic and republish them to the gist.

    Returns the filenames written. Never raises: a relay failure must not kill
    a healthy poll -- it logs, and tries again next run (since= is only
    advanced past messages that were successfully stored).
    """
    if not topic:
        return []

    since = state.get("briefs_relay_since") or "all"
    try:
        r = http.get(f"https://ntfy.sh/{topic}/json",
                     params={"poll": "1", "since": since}, timeout=30)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("briefs relay poll failed: %s", e)
        return []

    written: list[str] = []
    files: dict[str, str] = {}
    try:
        index = json.loads(gist.read(INDEX_FILE) or "{}")
    except json.JSONDecodeError:
        index = {}
    briefs = index.setdefault("briefs", [])
    last_id = None

    for line in r.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("event") != "message":
            continue
        last_id = m.get("id") or last_id

        content = m.get("message") or ""
        att = m.get("attachment") or {}
        if att.get("url"):
            try:
                ar = http.get(att["url"], timeout=60)
                ar.raise_for_status()
                if len(ar.content) <= MAX_BRIEF_BYTES:
                    content = ar.content.decode("utf-8", errors="replace")
                else:
                    log.warning("brief attachment %s too large (%d bytes); keeping the stub",
                                att.get("name"), len(ar.content))
            except httpx.HTTPError as e:
                log.warning("attachment fetch failed (%s); keeping message body", e)

        if len(content.strip()) < 200:
            # Not a brief -- a test ping or a fragment. Skip, but advance past it.
            continue

        ts = datetime.fromtimestamp(int(m.get("time") or 0), tz=timezone.utc)
        # Gist filenames cannot contain '/'; keep them sortable.
        name = f"brief-{ts:%Y%m%d-%H%M%S}.md"
        title = (m.get("title") or "").strip()
        if not title:
            first = next((l for l in content.splitlines() if l.strip()), "")
            title = re.sub(r"^#+\s*", "", first)[:80]

        files[name] = content
        briefs.insert(0, {"file": name, "ts": ts.isoformat(),
                          "title": title[:120], "bytes": len(content)})
        written.append(name)

    if files:
        briefs[:] = briefs[:60]
        files[INDEX_FILE] = json.dumps(
            {"note": ("Briefs relayed from the analyst's ntfy postings. Fetch a brief at "
                      "the mirror as d/<prefix>/<file>. Newest first."),
             "briefs": briefs}, indent=1)
        try:
            gist.write(files)
            log.info("relayed %d brief(s): %s", len(written), ", ".join(written))
        except Exception as e:
            log.error("brief relay gist write failed: %s", e)
            return []

    if last_id:
        state["briefs_relay_since"] = last_id
    return written

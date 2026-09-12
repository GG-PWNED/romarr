"""TitleDB integration for Nintendo Switch catalog validation.

TitleDB is the de facto database for Switch game titles, providing title IDs,
versions, and metadata that can be used to validate and catalog Switch games
in the absence of traditional DAT files.

TitleDB API endpoints:
- https://titledb.com/api/v1/titles - Search/query titles
- Each title has: id (title_id), name, region, version, size, etc.

This module provides:
- Lookup by title ID or name
- Validation of Switch ROMs against known titles
- Collection building based on TitleDB data
"""

from __future__ import annotations

import logging
import urllib.request
import json
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

TITLED_BASE_URL = "https://titledb.com"


@dataclass(frozen=True)
class SwitchTitle:
    """One entry from TitleDB."""
    title_id: str  # e.g., "01007EF00011E00"
    name: str
    region: str = ""
    version: int = 0
    size: int = 0  # in bytes
    release_date: str = ""
    publisher: str = ""
    description: str = ""
    
    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.title_id})"


@dataclass
class TitleDBIndex:
    """Cache of TitleDB data for offline queries."""
    titles: dict[str, SwitchTitle] = field(default_factory=dict)
    _by_name: dict[str, list[str]] = field(default_factory=dict)  # lowercase name -> title_ids
    
    def lookup_by_id(self, title_id: str) -> Optional[SwitchTitle]:
        """Look up a title by its Title ID."""
        normalized = title_id.upper().replace("0X", "")
        return self.titles.get(normalized)
    
    def search_by_name(self, query: str) -> list[SwitchTitle]:
        """Search titles by name (case-insensitive partial match)."""
        query_lower = query.lower()
        matching_ids = []
        for name_lower, ids in self._by_name.items():
            if query_lower in name_lower:
                matching_ids.extend(ids)
        
        # Deduplicate and return
        seen = set()
        results = []
        for title_id in matching_ids:
            if title_id not in seen:
                seen.add(title_id)
                if title_id in self.titles:
                    results.append(self.titles[title_id])
        
        return results
    
    def add_title(self, title: SwitchTitle) -> None:
        """Add a title to the index."""
        self.titles[title.title_id] = title
        name_lower = title.name.lower()
        if name_lower not in self._by_name:
            self._by_name[name_lower] = []
        self._by_name[name_lower].append(title.title_id)


def fetch_from_titled(endpoint: str) -> Optional[dict]:
    """Fetch JSON data from TitleDB API."""
    url = f"{TITLED_BASE_URL}{endpoint}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as exc:
        log.warning("TitleDB fetch failed for %s: %s", endpoint, exc)
        return None


def search_titles(query: str = "", limit: int = 50) -> list[SwitchTitle]:
    """Search TitleDB for Switch titles."""
    if query:
        endpoint = f"/api/v1/titles?search={urllib.parse.quote(query)}&limit={limit}"
    else:
        endpoint = f"/api/v1/titles?limit={limit}"
    
    data = fetch_from_titled(endpoint)
    if not data or "titles" not in data:
        return []
    
    results = []
    for item in data["titles"]:
        title = SwitchTitle(
            title_id=item.get("id", ""),
            name=item.get("name", ""),
            region=item.get("region", ""),
            version=item.get("version", 0),
            size=item.get("size", 0),
            release_date=item.get("releaseDate", ""),
            publisher=item.get("publisher", ""),
            description=item.get("description", ""),
        )
        if title.title_id:
            results.append(title)
    
    return results


def build_index(titles: Optional[list[SwitchTitle]] = None) -> TitleDBIndex:
    """Build a TitleDB index from a list of titles or fetch from API."""
    index = TitleDBIndex()
    
    if titles:
        for title in titles:
            index.add_title(title)
    else:
        # Fetch popular/recent titles as a starting point
        log.info("Fetching initial TitleDB data...")
        fetched = search_titles(limit=1000)
        for title in fetched:
            index.add_title(title)
        log.info("Indexed %d Switch titles from TitleDB", len(index.titles))
    
    return index


def validate_switch_rom(filename: str, title_id: Optional[str] = None, 
                       index: Optional[TitleDBIndex] = None) -> dict:
    """Validate a Switch ROM file against TitleDB.
    
    Returns a dict with validation status and details.
    """
    result = {
        "valid": False,
        "status": "unknown",
        "title": None,
        "details": "",
    }
    
    # Try to extract title ID from filename
    # Common patterns: [TitleID], TID_TitleID, or just the ID in the name
    import re
    if title_id:
        tid = title_id
    else:
        # Look for 16-char hex string in filename
        matches = re.findall(r'[0-9A-Fa-f]{16}', filename)
        tid = matches[0] if matches else None
    
    if not tid and index:
        # Try searching by filename
        basename = filename.rsplit('.', 1)[0] if '.' in filename else filename
        matches = index.search_by_name(basename)
        if matches:
            result["status"] = "matched_by_name"
            result["title"] = matches[0]
            result["valid"] = True
            result["details"] = f"Found {len(matches)} matching titles"
            return result
    
    if tid and index:
        title = index.lookup_by_id(tid)
        if title:
            result["status"] = "verified"
            result["title"] = title
            result["valid"] = True
            result["details"] = f"Verified: {title.display_name}"
            return result
        else:
            result["status"] = "not_in_database"
            result["details"] = f"Title ID {tid} not found in TitleDB"
            return result
    
    result["status"] = "no_identifier"
    result["details"] = "Could not extract title ID from filename"
    return result

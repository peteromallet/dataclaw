"""Local search functionality for DataClaw using scout.search.

This module provides offline search capabilities for Claude Code sessions
using BM25 ranking with confidence scores.

Key design:
- Raw (un-anonymized) session content is indexed for searchability
- Search results are anonymized at display time to protect privacy
- Users can search for original terms (file paths, usernames)
"""

import json
import logging
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, load_config
from .parser import get_claude_dir, discover_projects, parse_project_sessions
from .parser import AnonymizerWrapper, PassthroughAnonymizer

logger = logging.getLogger(__name__)

# Search index storage location
SEARCH_DB_PATH = CONFIG_DIR / "search.db"

# Default max content length - can be overridden in config
DEFAULT_MAX_CONTENT_LENGTH = 20000


def _get_max_content_length() -> int:
    """Get MAX_CONTENT_LENGTH from config or use default."""
    try:
        config = load_config()
        search_config = config.get("search") or {}
        return search_config.get("max_content_length", DEFAULT_MAX_CONTENT_LENGTH)
    except Exception:
        return DEFAULT_MAX_CONTENT_LENGTH


def _ensure_search_available() -> Any:
    """Ensure scout.search is available, import it or raise helpful error."""
    try:
        from scout.search import SearchIndex
        return SearchIndex
    except ImportError:
        try:
            from scout import search as scout_search_module
            from scout.search import SearchIndex
            return SearchIndex
        except ImportError:
            raise ImportError(
                "scout-core is required for search. Install with:\n"
                "  pip install scout-core\n"
                "Or for local development:\n"
                "  pip install -e ../scout"
            )


def _get_index() -> Any:
    """Create and return a SearchIndex instance."""
    SearchIndex = _ensure_search_available()
    return SearchIndex(str(SEARCH_DB_PATH))


def _session_to_document(session: dict[str, Any]) -> dict[str, Any]:
    """Convert a parsed session to a search document.
    
    The session should contain raw (un-anonymized) content for indexing.
    This allows users to search for original file paths and usernames.
    
    Args:
        session: A parsed session dict from parser.parse_project_sessions()
        
    Returns:
        A document dict suitable for SearchIndex
    """
    # Combine all message content
    content_parts = []
    for msg in session.get("messages", []):
        if msg.get("content"):
            content_parts.append(msg["content"])
        # Optionally include thinking (can be large, so maybe skip for now)
        # if msg.get("thinking"):
        #     content_parts.append(msg["thinking"])
    
    content = " ".join(content_parts)
    
    # Get configurable max content length
    max_content_length = _get_max_content_length()
    
    # Truncate content to keep index manageable
    if len(content) > max_content_length * 4:  # rough token estimate
        content = content[:max_content_length * 4]
    
    # Extract project name from session
    project = session.get("project", "unknown")
    
    # Format title with project and date
    start_time = session.get("start_time", "")
    date_str = start_time[:10] if start_time else "unknown"
    
    return {
        "id": session.get("session_id", ""),
        "title": f"{project} - {date_str}",
        "content": content,
        "project": project,
        "start_time": start_time,
        "session_id": session.get("session_id", ""),
    }


def build_index(projects: list[str] | None = None, force: bool = False) -> dict[str, Any]:
    """Build or update the search index from Claude Code sessions.
    
    Indexes RAW (un-anonymized) content so users can search for original terms.
    Results will be anonymized at display time.
    
    Args:
        projects: Optional list of project names to index. If None, all projects.
        force: If True, rebuild from scratch. Otherwise, adds to existing index.
        
    Returns:
        Dict with indexing results (document_count, projects_indexed, errors)
    """
    SearchIndex = _ensure_search_available()
    
    claude_dir = get_claude_dir()
    if not claude_dir.exists():
        return {
            "error": f"Claude Code directory not found: {claude_dir}. Use --claude-dir to specify the path.",
            "document_count": 0,
        }
    
    # Discover projects
    all_projects = discover_projects(claude_dir=claude_dir)
    if not all_projects:
        return {
            "error": "No Claude Code sessions found",
            "document_count": 0,
        }
    
    # Filter to requested projects
    if projects:
        project_names = set(p["display_name"] for p in all_projects)
        invalid = set(projects) - project_names
        if invalid:
            return {
                "error": f"Unknown projects: {', '.join(invalid)}",
                "available_projects": sorted(project_names),
                "document_count": 0,
            }
        all_projects = [p for p in all_projects if p["display_name"] in projects]
    
    # Create a passthrough anonymizer - we want raw data for indexing
    # Anonymization happens at display time in search()
    passthrough_anonymizer = PassthroughAnonymizer()
    
    # Parse sessions and convert to documents
    documents: list[dict[str, Any]] = []
    errors: list[str] = []
    projects_indexed: list[str] = []
    
    for project in all_projects:
        project_name = project["display_name"]
        print(f"  Indexing {project_name}...", end="", flush=True)
        
        try:
            # Pass anonymize=False to get RAW content for indexing
            sessions = parse_project_sessions(
                project["dir_name"],
                anonymizer=passthrough_anonymizer,
                include_thinking=True,
                claude_dir=claude_dir,
                anonymize=False,  # Index raw data for searchability
            )
            
            for session in sessions:
                doc = _session_to_document(session)
                if doc["id"] and doc["content"]:
                    documents.append(doc)
            
            projects_indexed.append(project_name)
            print(f" {len(sessions)} sessions")
            
        except Exception as e:
            error_msg = f"Error indexing {project_name}: {e}"
            errors.append(error_msg)
            print(f" error: {e}")
    
    if not documents:
        return {
            "document_count": 0,
            "projects_indexed": projects_indexed,
            "errors": errors,
        }
    
    # Build the index
    index = SearchIndex(str(SEARCH_DB_PATH))
    
    if force:
        index.build(documents)
    else:
        index.add_documents(documents)
    
    return {
        "document_count": len(documents),
        "projects_indexed": projects_indexed,
        "errors": errors,
        "index_path": str(SEARCH_DB_PATH),
    }


def search(
    query: str,
    limit: int = 20,
    min_confidence: int = 0,
    anonymize: bool = True,
) -> list[dict[str, Any]]:
    """Search the indexed sessions.
    
    Raw content is indexed, but results are anonymized by default to protect privacy.
    Set anonymize=False to get raw snippets (useful for debugging).
    
    Args:
        query: Search query string
        limit: Maximum number of results to return
        min_confidence: Minimum confidence score (0-100) to include
        anonymize: Whether to anonymize snippets (default: True)
        
    Returns:
        List of result dicts with keys:
        - id: Session ID
        - title: Session title (project - date)
        - project: Project name
        - confidence: Confidence score (0-100)
        - snippet: Text snippet with search terms highlighted
        - start_time: Session start time
    """
    if not query or not query.strip():
        return []
    
    index = _get_index()
    results = index.search(query, limit=limit, min_confidence=min_confidence)
    
    # Create anonymizer for display (if needed)
    display_anonymizer = None
    if anonymize:
        try:
            config = load_config()
            extra_usernames = config.get("redact_usernames", [])
            display_anonymizer = AnonymizerWrapper(extra_usernames=extra_usernames)
        except Exception as e:
            logger.warning(f"Could not load config for anonymizer: {e}")
            display_anonymizer = AnonymizerWrapper()
    
    # Format results for DataClaw users
    formatted = []
    for r in results:
        snippet = r.get("snippet", "")[:200]
        
        # Anonymize snippet at display time
        if anonymize and display_anonymizer:
            try:
                snippet = display_anonymizer.text(snippet)
            except Exception as e:
                logger.warning(f"Failed to anonymize snippet: {e}")
        
        formatted.append({
            "id": r.get("id", ""),
            "title": r.get("title", ""),
            "project": r.get("project", ""),
            "confidence": r.get("confidence", 0),
            "snippet": snippet,
            "start_time": r.get("start_time", ""),
        })
    
    return formatted


def get_index_stats() -> dict[str, Any]:
    """Get statistics about the search index.
    
    Returns:
        Dict with index stats (document_count, index_path, index_exists)
    """
    index_path = Path(SEARCH_DB_PATH)
    index_exists = index_path.exists()
    
    if not index_exists:
        return {
            "document_count": 0,
            "index_path": str(SEARCH_DB_PATH),
            "index_exists": False,
        }
    
    try:
        index = _get_index()
        count = index.count()
        return {
            "document_count": count,
            "index_path": str(SEARCH_DB_PATH),
            "index_exists": True,
        }
    except Exception:
        return {
            "document_count": 0,
            "index_path": str(SEARCH_DB_PATH),
            "index_exists": True,
            "error": "Could not read index",
        }

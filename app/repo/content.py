import base64
import json
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from ..config import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, VALID_MATCH_MODES
from .db import Jsonb, clean_tag_list, db_connect, ensure_db_ready, normalize_tag, parse_csv_tags

try:
    from psycopg.rows import dict_row
except ImportError:
    dict_row = None


def normalize_color(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    color = str(value).strip()
    if not color:
        return None
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise HTTPException(400, "Color must be a hex value like #E5E5E5")
    return color.upper()


def ensure_tag(cur, name: str) -> Optional[int]:
    name = " ".join(name.strip().split())
    norm = normalize_tag(name)
    if not name or not norm:
        return None
    cur.execute(
        """
        INSERT INTO tags (name, normalized)
        VALUES (%s, %s)
        ON CONFLICT (normalized) DO UPDATE SET name = tags.name
        RETURNING id
        """,
        (name, norm),
    )
    row = cur.fetchone()
    return row[0] if row else None


def tag_summary_rows() -> list[dict[str, Any]]:
    ensure_db_ready()
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.id,
                    t.name,
                    t.normalized,
                    t.color,
                    COUNT(DISTINCT it.image_id) AS image_count,
                    COUNT(DISTINCT CASE WHEN it.kind = 'auto' THEN it.image_id END) AS auto_count,
                    COUNT(DISTINCT CASE WHEN it.kind = 'user' THEN it.image_id END) AS user_count
                FROM tags t
                LEFT JOIN image_tags it ON it.tag_id = t.id
                GROUP BY t.id
                ORDER BY lower(t.name), t.name
                """
            )
            rows = cur.fetchall()
    return [
        {
            "name": row["name"],
            "normalized": row["normalized"],
            "color": row["color"],
            "image_count": int(row["image_count"] or 0),
            "auto_count": int(row["auto_count"] or 0),
            "user_count": int(row["user_count"] or 0),
            "is_auto": int(row["auto_count"] or 0) > 0,
        }
        for row in rows
    ]


def tag_summary_by_norm(norm: str) -> Optional[dict[str, Any]]:
    for row in tag_summary_rows():
        if row["normalized"] == norm:
            return row
    return None


def replace_image_tags(cur, image_id: str, tags: list[str], kind: str) -> None:
    cleaned = clean_tag_list(tags)
    cur.execute("DELETE FROM image_tags WHERE image_id = %s AND kind = %s", (image_id, kind))
    for tag in cleaned:
        tag_id = ensure_tag(cur, tag)
        if tag_id is None:
            continue
        cur.execute(
            """
            INSERT INTO image_tags (image_id, tag_id, kind)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (image_id, tag_id, kind),
        )


def load_session() -> dict[str, Any]:
    ensure_db_ready()
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT root_path, search_tags, search_mode, last_image_id, tabs, active_tab_id
                FROM app_session
                WHERE id = 1
                """
            )
            row = cur.fetchone()
    return {
        "root_path": row["root_path"] if row else None,
        "search_tags": list(row["search_tags"] or []) if row else [],
        "search_mode": row["search_mode"] if row else "any",
        "last_image_id": row["last_image_id"] if row else None,
        "tabs": row["tabs"] or [] if row else [],
        "active_tab_id": row["active_tab_id"] if row else None,
    }


def save_session_fields(**fields: Any) -> dict[str, Any]:
    ensure_db_ready()
    if "search_mode" in fields and fields["search_mode"] not in ("any", "all"):
        fields["search_mode"] = "any"
    if "search_tags" in fields and fields["search_tags"] is not None:
        fields["search_tags"] = [normalize_tag(t) for t in clean_tag_list(fields["search_tags"])]

    allowed = {"root_path", "search_tags", "search_mode", "last_image_id", "tabs", "active_tab_id"}
    updates = [(key, value) for key, value in fields.items() if key in allowed]
    if updates:
        assignments = ", ".join(f"{key} = %s" for key, _ in updates)
        values = [Jsonb(value) if key == "tabs" and Jsonb is not None else value for key, value in updates]
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_session (id)
                    VALUES (1)
                    ON CONFLICT (id) DO NOTHING
                    """
                )
                cur.execute(
                    f"""
                    UPDATE app_session
                    SET {assignments}, updated_at = now()
                    WHERE id = 1
                    """,
                    values,
                )
    return load_session()


def get_image_record(img_id: str) -> Optional[dict[str, Any]]:
    ensure_db_ready()
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, root_path, path, thumb, size, mtime, width, height, hidden
                FROM images
                WHERE id = %s AND hidden = false
                """,
                (img_id,),
            )
            return cur.fetchone()


def _decode_cursor(raw: Optional[str]) -> Optional[tuple[str, str, str]]:
    if not raw:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("utf-8")).decode("utf-8"))
        lower_path = str(payload.get("lower_path") or "")
        path = str(payload.get("path") or "")
        image_id = str(payload.get("id") or "")
        if not image_id:
            return None
        return (lower_path, path, image_id)
    except Exception:
        return None


def _encode_cursor(lower_path: str, path: str, image_id: str) -> str:
    raw = json.dumps({"lower_path": lower_path, "path": path, "id": image_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def _normalize_limit(limit: Optional[int]) -> int:
    if limit is None:
        return DEFAULT_PAGE_LIMIT
    return max(1, min(int(limit), MAX_PAGE_LIMIT))


def query_images_page(
    *,
    root_path: str,
    tags: Optional[str] = None,
    include_tags: Optional[str] = None,
    exclude_tags: Optional[str] = None,
    match_mode: Optional[str] = None,
    mode: Optional[str] = None,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    sort: Optional[str] = None,
    include_total: bool = False,
) -> dict[str, Any]:
    ensure_db_ready()
    limit_value = _normalize_limit(limit)
    sort_mode = sort or "path_asc"
    if sort_mode != "path_asc":
        raise HTTPException(400, "Unsupported sort. Allowed: path_asc")

    include_source = include_tags if include_tags is not None else tags
    include_list = parse_csv_tags(include_source)
    exclude_list = parse_csv_tags(exclude_tags)
    include_list = list(dict.fromkeys(include_list))
    exclude_list = list(dict.fromkeys(exclude_list))

    resolved_mode = (match_mode if include_tags is not None else mode) or "any"
    if resolved_mode not in VALID_MATCH_MODES:
        resolved_mode = "any"

    cursor_parts = _decode_cursor(cursor)

    filters: list[str] = ["i.root_path = %s", "i.hidden = false"]
    params: list[Any] = [root_path]

    if cursor_parts is not None:
        filters.append("(lower(i.path), i.path, i.id) > (%s, %s, %s)")
        params.extend(cursor_parts)

    join_sql = ""
    having_clauses: list[str] = []

    if include_list or exclude_list:
        join_sql = "LEFT JOIN image_tags it ON it.image_id = i.id LEFT JOIN tags t ON t.id = it.tag_id"

    if include_list:
        if resolved_mode == "all":
            having_clauses.append(
                "COUNT(DISTINCT CASE WHEN t.normalized = ANY(%s) THEN t.normalized END) = %s"
            )
            params.append(include_list)
            params.append(len(include_list))
        else:
            having_clauses.append(
                "COUNT(DISTINCT CASE WHEN t.normalized = ANY(%s) THEN t.normalized END) > 0"
            )
            params.append(include_list)

    if exclude_list:
        having_clauses.append(
            "COUNT(DISTINCT CASE WHEN t.normalized = ANY(%s) THEN t.normalized END) = 0"
        )
        params.append(exclude_list)

    where_sql = " AND ".join(filters)
    group_by_sql = "GROUP BY i.id"
    having_sql = f"HAVING {' AND '.join(having_clauses)}" if having_clauses else ""

    base_from = f"FROM images i {join_sql} WHERE {where_sql}"

    select_filtered = f"""
        SELECT
            i.id,
            i.path,
            i.thumb,
            i.size,
            i.mtime,
            i.width,
            i.height,
            lower(i.path) AS lower_path
        {base_from}
        {group_by_sql}
        {having_sql}
    """
    if include_total:
        query = f"""
            WITH filtered AS (
                {select_filtered}
            ),
            counted AS (
                SELECT COUNT(*)::bigint AS total FROM filtered
            )
            SELECT f.id, f.path, f.thumb, f.size, f.mtime, f.width, f.height, f.lower_path, c.total
            FROM filtered f
            CROSS JOIN counted c
            ORDER BY f.lower_path, f.path, f.id
            LIMIT %s
        """
    else:
        query = f"""
            {select_filtered}
            ORDER BY lower_path, path, id
            LIMIT %s
        """
    query_params = [*params, limit_value + 1]

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query, query_params)
            raw_rows = cur.fetchall()

    has_more = len(raw_rows) > limit_value
    page_rows = raw_rows[:limit_value]
    total = int(page_rows[0]["total"]) if include_total and page_rows else None
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_cursor(last["lower_path"], last["path"], last["id"])

    rows = [
        {
            "id": row["id"],
            "path": row["path"],
            "thumb": row["thumb"],
            "size": row["size"],
            "mtime": row["mtime"],
            "width": row["width"],
            "height": row["height"],
        }
        for row in page_rows
    ]

    return {
        "rows": rows,
        "page": {
            "next_cursor": next_cursor,
            "has_more": has_more,
            "limit": limit_value,
            "returned": len(rows),
            "total": total,
            "include_total": include_total,
            "sort": sort_mode,
        },
    }


def fetch_tags_for_image_ids(ids: list[str]) -> dict[str, dict[str, list[str]]]:
    tags_by_image: dict[str, dict[str, list[str]]] = {img_id: {"auto": [], "user": []} for img_id in ids}
    if not ids:
        return tags_by_image

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT it.image_id, it.kind, t.name
                FROM image_tags it
                JOIN tags t ON t.id = it.tag_id
                WHERE it.image_id = ANY(%s)
                ORDER BY lower(t.name), t.name
                """,
                (ids,),
            )
            for row in cur.fetchall():
                tags_by_image[row["image_id"]][row["kind"]].append(row["name"])
    return tags_by_image


def rows_to_images(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [row["id"] for row in rows]
    tags_by_image = fetch_tags_for_image_ids(ids)
    images: list[dict[str, Any]] = []
    for row in rows:
        auto_tags = tags_by_image[row["id"]]["auto"]
        user_tags = tags_by_image[row["id"]]["user"]
        all_tags = clean_tag_list(auto_tags + user_tags)
        height = row["height"] or 0
        width = row["width"] or 0
        aspect_ratio = (width / height) if height > 0 else 1
        images.append(
            {
                "id": row["id"],
                "path": row["path"],
                "thumb": row["thumb"],
                "thumb_url": f"/thumb-file/{row['id']}.jpg",
                "size": row["size"],
                "mtime": row["mtime"],
                "width": width,
                "height": height,
                "aspect_ratio": aspect_ratio,
                "tags": all_tags,
                "auto_tags": auto_tags,
                "folder_tags": auto_tags,
                "user_tags": user_tags,
            }
        )
    return images


def upsert_image_row(
    cur,
    *,
    root_str: str,
    rel: str,
    thumb_rel: str,
    size: int,
    mtime: int,
    width: int,
    height: int,
    existing_id: Optional[str] = None,
) -> str:
    img_id = existing_id or uuid.uuid4().hex[:12]
    cur.execute(
        """
        INSERT INTO images (
            id, root_path, path, thumb, size, mtime, width, height, hidden
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false)
        ON CONFLICT (root_path, path) DO UPDATE SET
            thumb = EXCLUDED.thumb,
            size = EXCLUDED.size,
            mtime = EXCLUDED.mtime,
            width = EXCLUDED.width,
            height = EXCLUDED.height,
            hidden = false,
            updated_at = now()
        RETURNING id
        """,
        (
            img_id,
            root_str,
            rel,
            thumb_rel,
            size,
            mtime,
            width,
            height,
        ),
    )
    row = cur.fetchone()
    return row[0] if row else img_id


def mark_images_hidden_for_root(cur, root_path: str) -> None:
    cur.execute("UPDATE images SET hidden = true, updated_at = now() WHERE root_path = %s", (root_path,))


def fetch_existing_images_map(cur, root_path: str) -> dict[str, str]:
    cur.execute("SELECT path, id FROM images WHERE root_path = %s", (root_path,))
    return {path: image_id for path, image_id in cur.fetchall()}
